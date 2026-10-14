from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from .runtime import provision, start_worker, validate_release
from .store import TeamStore, valid_username

logger = logging.getLogger(__name__)


class SetupBusy(ValueError):
    pass


class SetupFailed(RuntimeError):
    pass


def worker_running(root: Path, username: str) -> bool:
    pid_file = root / "control" / f"{username}.pid"
    if not pid_file.exists():
        return False
    policy = json.loads((root / "control" / f"{username}.json").read_text())
    process = Path(f"/proc/{int(pid_file.read_text())}")
    try:
        if process.stat().st_uid != policy["uid"]:
            raise RuntimeError("Worker PID no longer belongs to this account")
        if b"research_ops.team.worker" not in (process / "cmdline").read_bytes().split(b"\0"):
            raise RuntimeError("Worker PID is not a Orbitree worker")
    except FileNotFoundError:
        return False
    return True


def wait_for_worker(account: dict, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=account["worker_socket"]),
        base_url="http://worker",
        trust_env=False,
        timeout=1,
        headers={"authorization": f"Bearer {account['worker_token']}"},
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("/api/health")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
    raise TimeoutError("The new worker did not become healthy")


class AccountProvisioner:
    """Only fixed, administrator-configured releases can create Web accounts."""

    def __init__(self, store: TeamStore, release: Path) -> None:
        self.store = store
        self.release = validate_release(release)

    @contextmanager
    def _lock(self, username: str):
        valid_username(username)
        path = self.store.root / "control" / f"{username}.setup.lock"
        with path.open("a") as handle:
            path.chmod(0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SetupBusy("账号正在初始化，请稍后刷新列表。") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def create(self, username: str, display_name: str, actor: str) -> dict:
        if os.geteuid() != 0:
            raise ValueError("账号网关没有创建独立用户的权限，请联系服务器管理员。")
        with self._lock(username):
            if self.store.account(username):
                raise SetupBusy("账号已存在，请刷新列表；初始化失败的账号可直接重试。")
            # Validate deployment configuration before persisting a new account.
            validate_release(self.release)
            self.store.add_account(username, display_name, actor=actor, setup=True)
            return self._prepare(username, actor)

    def retry(self, username: str, actor: str) -> dict:
        with self._lock(username):
            account = self.store.account(username)
            if not account or not self.store.setup(username):
                raise ValueError("未找到可重试的账号初始化记录。")
            if account["password_hash"] or not account["enabled"]:
                raise ValueError("只能初始化已启用且尚未激活的账号。")
            return self._prepare(username, actor)

    def _prepare(self, username: str, actor: str) -> dict:
        stage = "工作区初始化"
        self.store.set_setup(username, "provisioning", actor)
        try:
            provision(self.store.root, username, self.release, [])
            stage = "用户实例启动"
            self.store.set_setup(username, "starting", actor)
            account = self.store.account(username)
            if not worker_running(self.store.root, username):
                start_worker(self.store.root, username)
            wait_for_worker(account)
        except Exception as exc:
            # No directories or identities are removed on failure. The root-owned
            # provisioning receipt and worker PID allow the same account to retry.
            message = f"{stage}失败，账号和已有文件已保留。请重试初始化；重复失败请联系管理员。"
            self.store.set_setup(username, "error", actor, message)
            logger.error(
                "Account setup failed: user=%s stage=%s error=%s",
                username,
                stage,
                type(exc).__name__,
            )
            raise SetupFailed(message) from exc
        self.store.set_setup(username, "ready", actor)
        return self.store.public(account) | {"setup": self.store.setup(username)}
