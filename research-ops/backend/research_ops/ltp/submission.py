from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

JOB_NAME_BASE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,42}$")
SUBMIT_SUCCESS_MARKER = "任务提交成功:"
REPORTED_JOB_NAME_PATTERN = re.compile(
    rf"{re.escape(SUBMIT_SUCCESS_MARKER)}\s*([a-z0-9][a-z0-9-]{{0,49}})"
)


class BundleValidationError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class LtpSubmissionError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class LtpSubmissionUncertain(LtpSubmissionError):
    """The command may have reached LTP and therefore must not be retried."""


@dataclass(frozen=True)
class BundleSpec:
    config_path: Path
    run_path: Path
    train_path: Path | None
    preflight_receipt_path: Path | None
    job_name: str
    config_sha256: str
    run_sha256: str
    train_sha256: str | None
    requested_nodes: int
    gpus_per_node: int
    requested_cards: int

    @property
    def fingerprint(self) -> str:
        material = ":".join(
            [
                self.config_sha256,
                self.run_sha256,
                self.train_sha256 or "",
                str(self.requested_nodes),
                str(self.gpus_per_node),
            ]
        )
        return hashlib.sha256(material.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class SubmitReceipt:
    reported_job_name: str
    submitted_job_base: str
    bundle_fingerprint: str


Runner = Callable[..., Any]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BundleInspector:
    """Validate an exact server-visible bundle without retaining its contents."""

    def __init__(
        self,
        *,
        allowed_roots: list[Path],
        check_script: Path,
        training_preflight_script: Path,
        runner: Runner | None = None,
    ) -> None:
        self.allowed_roots = [root.expanduser().resolve() for root in allowed_roots]
        self.check_script = check_script.expanduser().resolve()
        self.training_preflight_script = training_preflight_script.expanduser().resolve()
        self.runner = runner or subprocess.run

    def inspect(
        self,
        *,
        config_path: str,
        run_path: str,
        train_path: str | None,
        preflight_receipt_path: str | None,
        job_kind: str,
    ) -> BundleSpec:
        config = self._allowed_file(config_path, "config")
        run = self._allowed_file(run_path, "run script")
        train = self._allowed_file(train_path, "train script") if train_path else None
        receipt = (
            self._allowed_file(preflight_receipt_path, "training preflight receipt")
            if preflight_receipt_path
            else None
        )
        try:
            config_data = yaml.safe_load(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise BundleValidationError("无法解析 config.yaml。", "config_invalid") from exc
        if not isinstance(config_data, dict):
            raise BundleValidationError("config.yaml 顶层必须是映射。", "config_invalid")
        jobargs = config_data.get("jobargs")
        if not isinstance(jobargs, dict):
            raise BundleValidationError("config.yaml 缺少 jobargs。", "config_invalid")
        job_name = str(jobargs.get("job_name") or "").strip()
        if not JOB_NAME_BASE_PATTERN.fullmatch(job_name):
            raise BundleValidationError(
                "jobargs.job_name 必须为不超过 43 个字符的小写安全名称。",
                "job_name_invalid",
            )
        num_workers = jobargs.get("num_workers")
        gpus_per_node = jobargs.get("gpus_per_node")
        if isinstance(num_workers, bool) or not isinstance(num_workers, int) or num_workers < 0:
            raise BundleValidationError(
                "jobargs.num_workers 必须是非负整数。", "topology_invalid"
            )
        if (
            isinstance(gpus_per_node, bool)
            or not isinstance(gpus_per_node, int)
            or gpus_per_node <= 0
        ):
            raise BundleValidationError(
                "jobargs.gpus_per_node 必须是正整数。", "topology_invalid"
            )
        self._run_validator(
            [
                sys.executable,
                str(self.check_script),
                str(config),
                "--run",
                str(run),
                "--strict",
            ],
            code="bundle_preflight_failed",
            message="LTP bundle 严格检查未通过。",
        )
        if job_kind == "training":
            if receipt is None:
                raise BundleValidationError(
                    "正式训练必须提供已通知的 preflight receipt。",
                    "training_preflight_missing",
                )
            self._run_validator(
                [
                    sys.executable,
                    str(self.training_preflight_script),
                    "--receipt",
                    str(receipt),
                    "--config",
                    str(config),
                    "--run",
                    str(run),
                ],
                code="training_preflight_failed",
                message="正式训练 preflight receipt 与当前 bundle 不一致或已失效。",
            )
        requested_nodes = num_workers + 1
        return BundleSpec(
            config_path=config,
            run_path=run,
            train_path=train,
            preflight_receipt_path=receipt,
            job_name=job_name,
            config_sha256=sha256_file(config),
            run_sha256=sha256_file(run),
            train_sha256=sha256_file(train) if train else None,
            requested_nodes=requested_nodes,
            gpus_per_node=gpus_per_node,
            requested_cards=requested_nodes * gpus_per_node,
        )

    def _allowed_file(self, raw_path: str, label: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise BundleValidationError(f"{label} 路径必须是绝对路径。", "path_invalid")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise BundleValidationError(f"{label} 不存在或不可读取。", "path_unavailable") from exc
        if not resolved.is_file():
            raise BundleValidationError(f"{label} 不是普通文件。", "path_invalid")
        if not any(resolved.is_relative_to(root) for root in self.allowed_roots):
            raise BundleValidationError(f"{label} 不在允许的工作区内。", "path_not_allowed")
        return resolved

    def _run_validator(self, argv: list[str], *, code: str, message: str) -> None:
        if not Path(argv[1]).is_file():
            raise BundleValidationError("LTP bundle 校验器不可用。", "validator_unavailable")
        try:
            result = self.runner(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BundleValidationError(
                "LTP bundle 校验器不可用。", "validator_unavailable"
            ) from exc
        if int(getattr(result, "returncode", 1)) != 0:
            raise BundleValidationError(message, code)


class LtpSubmitAdapter:
    """The only boundary allowed to turn a managed request into an LTP job."""

    def __init__(
        self,
        *,
        command: str,
        timeout_seconds: float,
        runner: Runner | None = None,
    ) -> None:
        if not command or any(character.isspace() for character in command):
            raise ValueError("managed_job_ltp_cli must be one executable path")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.runner = runner or subprocess.run

    def submit(self, *, bundle: BundleSpec, submitter: str, cluster: str) -> SubmitReceipt:
        argv = [
            self.command,
            "submit",
            "--user",
            submitter,
            "--config",
            str(bundle.config_path),
            "--run",
            str(bundle.run_path),
            "--job-name",
            bundle.job_name,
            "--cluster",
            cluster,
        ]
        if bundle.train_path is not None:
            argv.extend(["--train", str(bundle.train_path)])
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.casefold() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        try:
            result = self.runner(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise LtpSubmissionUncertain(
                "LTP 提交超时，系统不会自动重试。", "submit_timeout"
            ) from exc
        except OSError as exc:
            raise LtpSubmissionError("LTP CLI 不可用。", "ltp_cli_unavailable") from exc
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        combined = f"{stdout}\n{stderr}"
        if len(combined.encode("utf-8", errors="replace")) > 1024 * 1024:
            raise LtpSubmissionUncertain(
                "LTP 提交输出超过安全上限，结果需要人工核验。",
                "submit_output_too_large",
            )
        if int(getattr(result, "returncode", 1)) != 0 or SUBMIT_SUCCESS_MARKER not in combined:
            raise LtpSubmissionUncertain(
                "LTP 未返回可核验的提交回执，系统不会自动重试。",
                "ltp_submit_unverified",
            )
        names = REPORTED_JOB_NAME_PATTERN.findall(combined)
        if not names:
            raise LtpSubmissionUncertain(
                "LTP 已接受提交，但回执中的任务名无法解析。",
                "reported_job_name_missing",
            )
        reported_name = names[-1]
        if reported_name != bundle.job_name and not reported_name.startswith(
            f"{bundle.job_name}-"
        ):
            raise LtpSubmissionUncertain(
                "LTP 回执中的任务名与提交基名不一致。",
                "reported_job_name_mismatch",
            )
        return SubmitReceipt(
            reported_job_name=reported_name,
            submitted_job_base=bundle.job_name,
            bundle_fingerprint=bundle.fingerprint,
        )
