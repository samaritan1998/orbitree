from pathlib import Path
from types import SimpleNamespace
from urllib.request import ProxyHandler

from research_ops import cli
from research_ops.services.restart_guard import RestartBlocker
from typer.testing import CliRunner

runner = CliRunner()


class FakeWindowRegistry:
    def scan(self) -> None:
        pass

    def list_windows(self, *, include_ignored: bool) -> list[dict[str, object]]:
        assert include_ignored is False
        return [
            {
                "state": "managed",
                "live": False,
                "work_id": "COMMON-0001",
                "work_title": "Recovery test",
                "display_name": "Recovery test",
                "thread_id": "019f59c9-a3fb-7512-a6c6-661ff6d8c42a",
                "branch_id": "2bd3ea75-4a2f-4b98-806b-b9ed77d73e1c",
                "cwd": "/workspace/recovery",
            }
        ]


def test_sessions_lists_complete_offline_resume_identifiers(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "runtime",
        lambda: SimpleNamespace(codex_windows=FakeWindowRegistry()),
    )

    result = runner.invoke(cli.app, ["sessions"])

    assert result.exit_code == 0, result.output
    assert "019f59c9-a3fb-7512-a6c6-661ff6d8c42a" in result.output
    assert "2bd3ea75-4a2f-4b98-806b-b9ed77d73e1c" in result.output
    assert "rops open 2bd3ea75-4a2f-4b98-806b-b9ed77d73e1c" in result.output


def test_server_health_bypasses_environment_proxies(monkeypatch) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    class FakeOpener:
        def open(self, url: str, *, timeout: float):
            assert url == "http://127.0.0.1:8020/api/health"
            assert timeout == 2.0
            return FakeResponse()

    def fake_build_opener(handler: ProxyHandler) -> FakeOpener:
        assert isinstance(handler, ProxyHandler)
        assert handler.proxies == {}
        return FakeOpener()

    monkeypatch.setattr(cli, "build_opener", fake_build_opener)

    assert cli._server_healthy(8020) is True


def test_server_status_does_not_require_the_web_ui(settings, monkeypatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_server_healthy", lambda _port: True)
    monkeypatch.setattr(cli, "_read_server_pid", lambda _settings: 4321)
    monkeypatch.setattr(cli, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(cli, "_owned_supervisor", lambda _pid: True)

    result = runner.invoke(cli.app, ["server", "status"])

    assert result.exit_code == 0, result.output
    assert "Web: healthy" in result.output
    assert "Supervisor: 4321" in result.output
    assert f"Log: {Path(settings.runtime_data_dir) / 'run/server.log'}" in result.output


def run_idle_restart(settings, monkeypatch, probes, *, health=None):
    clock = [0.0]
    actions = []

    def blockers():
        value = next(probes)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "RestartGuard", lambda _: SimpleNamespace(blockers=blockers))
    monkeypatch.setattr(cli, "_server_healthy", health or (lambda _: True))
    monkeypatch.setattr(cli, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda interval: clock.__setitem__(0, clock[0] + interval),
    ))
    monkeypatch.setattr(cli, "_stop_detached_server", lambda *_: actions.append(("stop", clock[0])))
    monkeypatch.setattr(
        cli, "_start_detached_server", lambda *_: actions.append(("start", clock[0]))
    )
    result = runner.invoke(cli.app, [
        "server", "restart-when-idle", "--poll-seconds", "15", "--stable-checks", "2",
        "--quiet-seconds", "60",
    ])
    return result, actions


def test_idle_restart_requires_continuously_quiet_time_and_final_check(settings, monkeypatch):
    busy = (RestartBlocker("PresentBench", "native_goal_active_or_retrying"),)
    # Quiet streak interrupted at 30s, then the final recheck at 105s is busy.
    probes = iter([(), (), busy, (), (), (), (), (), busy, (), (), (), (), (), ()])
    result, actions = run_idle_restart(settings, monkeypatch, probes)
    assert result.exit_code == 0, result.output
    assert actions == [("stop", 180.0), ("start", 180.0)]
    assert '"phase": "final_check_busy"' in result.output
    assert '"phase": "completed"' in result.output


def test_idle_restart_probe_error_resets_quiet_streak(settings, monkeypatch):
    probes = iter([(), (), OSError("unavailable"), (), (), (), (), (), ()])
    result, actions = run_idle_restart(settings, monkeypatch, probes)
    assert result.exit_code == 0, result.output
    assert actions == [("stop", 105.0), ("start", 105.0)]
    assert '"reason": "OSError"' in result.output


def test_idle_restart_does_not_treat_failed_health_check_as_idle(settings, monkeypatch):
    checks = iter([True, False, True, True, True, True, True, True])
    result, actions = run_idle_restart(
        settings, monkeypatch, iter([()] * 8), health=lambda _: next(checks),
    )
    assert result.exit_code == 0, result.output
    assert actions == [("stop", 90.0), ("start", 90.0)]
    assert '"reason": "health_check_failed"' in result.output
