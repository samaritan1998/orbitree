from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import hashlib
import os
import pty
import pwd
import secrets
import shlex
import shutil
import socket
import stat
import struct
import sys
import termios
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from research_ops.settings import Settings


class TerminalError(RuntimeError):
    pass


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise TerminalError("Terminal state directory must be private and owned by this user")


def terminal_key(settings: Settings, *, create: bool = False) -> str | None:
    directory = settings.runtime_data_dir / "web-terminal"
    path = directory / "unlock.key"
    if create:
        private_directory(directory)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_urlsafe(32))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise TerminalError("Terminal unlock key must be private and owned by this user")
        return handle.read(128).strip()


class WebTerminal:
    """One user's tmux session, with a bounded PTY stream and one controlling browser."""

    def __init__(self, settings: Settings):
        self.settings = settings
        digest = hashlib.sha256(str(settings.runtime_data_dir.resolve()).encode()).hexdigest()[:16]
        self.directory = (
            Path(os.environ.get("TMPDIR") or "/tmp") / f"rops-term-{os.geteuid()}-{digest}"
        )
        self.socket = self.directory / "tmux.sock"
        self.cwd = (settings.web_terminal_cwd or settings.workspace_root).resolve()
        self.process: asyncio.subprocess.Process | None = None
        self.fd: int | None = None
        self.tty: str | None = None
        self.wait_task: asyncio.Task | None = None
        self.client: str | None = None
        self.queue: asyncio.Queue | None = None
        self.history: deque[str] = deque()
        self.history_size = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending_output: list[str] = []
        self.pending_size = 0
        self.flush_handle: asyncio.TimerHandle | None = None
        self.tickets: dict[str, tuple[float, str]] = {}
        self.lock = asyncio.Lock()

    def authenticated(self, headers) -> bool:
        if self.settings.access_token:
            return secrets.compare_digest(
                headers.get("authorization", ""), f"Bearer {self.settings.access_token}"
            )
        supplied = headers.get("x-rops-terminal-key", "")
        expected = terminal_key(self.settings) if supplied else None
        return bool(expected and secrets.compare_digest(supplied, expected))

    def origin_allowed(self, origin: str | None, headers) -> bool:
        if self.settings.require_access_token and self.authenticated(headers):
            # The account gateway validates the browser Origin before adding
            # the worker-only bearer token. Workers have no public listener.
            return True
        public = urlsplit(self.settings.infoflow_public_base_url)
        allowed = {*self.settings.allowed_origins, f"{public.scheme}://{public.netloc}"}
        return bool(origin and origin in allowed)

    def ticket(self, origin: str) -> str:
        now = time.monotonic()
        self.tickets = {key: value for key, value in self.tickets.items() if value[0] > now}
        if len(self.tickets) >= 16:
            raise TerminalError("Too many pending terminal connections")
        token = secrets.token_urlsafe(32)
        self.tickets[token] = (now + 30, origin)
        return token

    def consume_ticket(self, token: str, origin: str) -> bool:
        entry = self.tickets.pop(token, None)
        return bool(entry and entry[0] > time.monotonic() and entry[1] == origin)

    def environment(self) -> dict[str, str]:
        account = pwd.getpwuid(os.geteuid())
        home = os.environ.get("HOME") or account.pw_dir
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "LANG",
                "LC_ALL",
                "TZ",
                "TMPDIR",
                "http_proxy",
                "https_proxy",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "no_proxy",
                "NO_PROXY",
                "all_proxy",
                "ALL_PROXY",
            }
        }
        env.update(
            HOME=home,
            USER=account.pw_name,
            LOGNAME=account.pw_name,
            SHELL=self.settings.web_terminal_shell,
            TERM="xterm-256color",
            COLORTERM="truecolor",
            LANG=env.get("LANG") or "C.UTF-8",
        )
        return env

    async def command(self, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            self.settings.web_terminal_tmux,
            "-S",
            str(self.socket),
            "-f",
            "/dev/null",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment(),
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
            return process.returncode or 0, stdout.decode(errors="replace").strip()
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise

    async def status(self, authenticated: bool) -> dict:
        available = bool(shutil.which(self.settings.web_terminal_tmux))
        result = {
            "enabled": self.settings.web_terminal_enabled,
            "requires_unlock": not authenticated,
            "available": available,
            "unlock_command": shlex.join(
                [str(Path(sys.executable).with_name("rops")), "terminal", "key"]
            ),
        }
        if not authenticated:
            return result
        active = False
        if available and self.socket.exists():
            active = (await self.command("has-session", "-t", "terminal"))[0] == 0
        return {
            **result,
            "active": active,
            "user": pwd.getpwuid(os.geteuid()).pw_name,
            "hostname": socket.gethostname(),
            "cwd": str(self.cwd),
            "shell": self.settings.web_terminal_shell,
            "persistent": True,
        }

    async def start(self, cols: int, rows: int) -> None:
        if self.process and self.process.returncode is None:
            self.resize(cols, rows)
            return
        if not self.settings.web_terminal_enabled:
            raise TerminalError("Web terminal is disabled")
        if not shutil.which(self.settings.web_terminal_tmux):
            raise TerminalError("tmux is not installed")
        if not self.cwd.is_dir() or not os.access(self.cwd, os.X_OK):
            raise TerminalError("Terminal working directory is not accessible")
        private_directory(self.directory)
        exists = (await self.command("has-session", "-t", "terminal"))[0] == 0
        if not exists:
            code, _ = await self.command(
                "new-session",
                "-d",
                "-s",
                "terminal",
                "-c",
                str(self.cwd),
                "-x",
                str(cols),
                "-y",
                str(rows),
                self.settings.web_terminal_shell,
            )
            if code:
                raise TerminalError("Unable to start the terminal shell")
            await self.command("set-option", "-t", "terminal", "status", "off")
            await self.command("set-option", "-t", "terminal", "history-limit", "5000")
        master, slave = pty.openpty()
        self.fd, self.tty = master, os.ttyname(slave)
        self.resize(cols, rows)
        os.set_blocking(master, False)
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("terminal_exec.py")),
                shutil.which(self.settings.web_terminal_tmux),
                "-u",
                "-S",
                str(self.socket),
                "attach-session",
                "-t",
                "terminal",
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=self.cwd,
                env=self.environment(),
                start_new_session=True,
            )
        except BaseException:
            os.close(master)
            self.fd = None
            raise
        finally:
            os.close(slave)
        self.history.clear()
        self.history_size = 0
        self.decoder.reset()
        asyncio.get_running_loop().add_reader(master, self._read)
        self.wait_task = asyncio.create_task(self._wait(self.process, master))

    def _read(self):
        if self.fd is None:
            return
        try:
            raw = os.read(self.fd, 16384)
        except BlockingIOError:
            return
        except OSError:
            raw = b""
        if not raw:
            asyncio.get_running_loop().remove_reader(self.fd)
            return
        text = self.decoder.decode(raw)
        if not text:
            return
        self.history.append(text)
        self.history_size += len(text)
        while self.history_size > 256_000:
            self.history_size -= len(self.history.popleft())
        self._buffer_output(text)

    def _buffer_output(self, text: str):
        if self.queue is None:
            return
        # Keystroke echoes can arrive as individual bytes. Batch one frame's
        # worth so normal typing cannot exhaust the bounded output queue.
        self.pending_output.append(text)
        self.pending_size += len(text)
        if self.pending_size >= 65_536:
            self._flush_output()
        elif self.flush_handle is None:
            self.flush_handle = asyncio.get_running_loop().call_later(0.016, self._flush_output)

    def _flush_output(self):
        if self.flush_handle is not None:
            self.flush_handle.cancel()
            self.flush_handle = None
        if self.pending_output:
            text = "".join(self.pending_output)
            self.pending_output.clear()
            self.pending_size = 0
            self._emit({"type": "output", "data": text})

    def _emit(self, event):
        if event["type"] != "output":
            self._flush_output()
        if self.queue is not None:
            if self.queue.full():
                while not self.queue.empty():
                    self.queue.get_nowait()
                self.queue.put_nowait({"type": "overflow"})
                self.client, self.queue = None, None
            else:
                self.queue.put_nowait(event)

    async def _wait(self, process, descriptor):
        code = await process.wait()
        if self.fd == descriptor:
            asyncio.get_running_loop().remove_reader(descriptor)
            os.close(descriptor)
            self.fd = None
            self._emit({"type": "exit", "code": code})

    async def attach(self, cols: int, rows: int):
        async with self.lock:
            await self.start(cols, rows)
            self._emit({"type": "replaced"})
            client = secrets.token_hex(16)
            queue = asyncio.Queue(maxsize=64)
            self.client, self.queue = client, queue
            return client, queue, "".join(self.history)

    def detach(self, client: str):
        if self.client == client:
            self.client, self.queue = None, None

    async def write(self, client: str, data: str):
        if client != self.client or self.fd is None:
            raise TerminalError("This browser no longer controls the terminal")
        payload = data.encode()
        if len(payload) > 16_384:
            raise TerminalError("Terminal input is too large")
        descriptor = self.fd
        while payload:
            if client != self.client or descriptor != self.fd:
                raise TerminalError("Terminal connection changed")
            try:
                written = os.write(descriptor, payload)
                payload = payload[written:]
            except BlockingIOError:
                await asyncio.sleep(0.01)

    def resize(self, cols: int, rows: int):
        if not (20 <= cols <= 500 and 5 <= rows <= 200):
            raise TerminalError("Invalid terminal dimensions")
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def terminate(self):
        async with self.lock:
            if self.socket.exists():
                await self.command("kill-session", "-t", "terminal")
            await self.close()
            self.history.clear()
            self.history_size = 0

    async def interrupt(self):
        if self.socket.exists():
            await self.command("send-keys", "-t", "terminal", "C-c")

    async def close(self):
        # Detaching the PTY client does not terminate the tmux server or its shell.
        self._emit({"type": "disconnected"})
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.wait_task:
            await self.wait_task
        self.client, self.queue = None, None
        self.process = None
