"""Credential-safe client and result parser for the Goblin Reward API.

Goblin accepts a rollout result file and creates an asynchronous job.  This
module intentionally does not submit anything at import time: callers choose
when to create a job, and the API token is resolved from an environment
variable only for that request.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx

DEFAULT_GOBLIN_BASE_URL = "http://127.0.0.1:8118"
DEFAULT_GOBLIN_IMAGE = "registry.example.com/acg-agi/miaoda-all:0526-codex"
DEFAULT_PROMPT_VERSION = "v1_6"
DEFAULT_REWARD_MODEL = "gpt-5.5-agent"
DEFAULT_QUERY_ROUND = "first_round"


class GoblinRewardError(RuntimeError):
    """Base class for configuration, transport, and result errors."""


class GoblinConfigurationError(GoblinRewardError):
    """Raised when the API token or job configuration is unavailable."""


class GoblinHTTPError(GoblinRewardError):
    """Raised for a non-successful Reward API response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class GoblinJobFailed(GoblinRewardError):
    """Raised when a waited-on Goblin job reaches failed/cancelled state."""

    def __init__(self, snapshot: GoblinJobSnapshot) -> None:
        super().__init__(
            f"Goblin job {snapshot.job_id} ended with status {snapshot.status}"
        )
        self.snapshot = snapshot


@dataclass(frozen=True)
class GoblinJobSnapshot:
    """Safe subset of a Goblin job response."""

    job_id: str
    status: str
    workdir_prefix: str | None = None
    log_dir: str | None = None
    result_file: str | None = None
    total: int | None = None
    completed: int | None = None
    succeeded: int | None = None
    failed: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}

    @property
    def successful(self) -> bool:
        return self.status == "completed"

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> GoblinJobSnapshot:
        job_id = str(payload.get("job_id") or payload.get("id") or "").strip()
        if not job_id:
            raise GoblinRewardError("Goblin response omitted job_id")
        def integer(name: str) -> int | None:
            value = payload.get(name)
            return int(value) if isinstance(value, (int, float)) else None

        return cls(
            job_id=job_id,
            status=str(payload.get("status") or "unknown").strip().lower(),
            workdir_prefix=_optional_text(payload.get("workdir_prefix")),
            log_dir=_optional_text(payload.get("log_dir")),
            result_file=_optional_text(payload.get("result_file")),
            total=integer("total"),
            completed=integer("completed"),
            succeeded=integer("succeeded"),
            failed=integer("failed"),
            raw=dict(payload),
        )


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_response_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = response.text
    text = str(body)
    for header in ("X-API-Token", "Authorization"):
        value = response.request.headers.get(header)
        if value:
            text = text.replace(value, "<redacted>")
    return text[:1_000]


class GoblinRewardClient:
    """Synchronous HTTP client for create/get/poll Goblin jobs."""

    provider = "goblin-reward"

    def __init__(
        self,
        base_url: str = DEFAULT_GOBLIN_BASE_URL,
        *,
        token_env: str = "GOBLIN_REWARD_API_TOKEN",
        timeout_seconds: float = 120.0,
        verify: bool = True,
        trust_env: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = str(base_url).strip().rstrip("/")
        if not normalized:
            raise GoblinConfigurationError("Goblin Reward base URL is empty")
        self.base_url = normalized
        self.token_env = str(token_env).strip() or "GOBLIN_REWARD_API_TOKEN"
        self.timeout_seconds = timeout_seconds
        self.verify = verify
        self.trust_env = trust_env
        self.transport = transport

    def _token(self) -> str:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise GoblinConfigurationError(
                "Goblin Reward token is unavailable; configure environment variable "
                f"{self.token_env}"
            )
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-API-Token": self._token(),
        }

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
    ) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                verify=self.verify,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client:
                response = client.request(
                    method,
                    path,
                    headers=self._headers(),
                    json=json_body,
                    data=data,
                    files=files,
                )
        except httpx.HTTPError as exc:
            raise GoblinRewardError(
                f"Goblin Reward request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise GoblinHTTPError(response.status_code, _safe_response_text(response))
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise GoblinRewardError("Goblin Reward returned non-JSON") from exc
        if not isinstance(payload, Mapping):
            raise GoblinRewardError("Goblin Reward returned a non-object response")
        return dict(payload)

    @staticmethod
    def build_job_payload(
        *,
        rollout_model: str,
        benchmark_dataset_name: str,
        image: str = DEFAULT_GOBLIN_IMAGE,
        concurrency: int = 50,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        reward_model: str = DEFAULT_REWARD_MODEL,
        query_round: str = DEFAULT_QUERY_ROUND,
        eval_mm: bool = True,
        eval_miniprog: bool = True,
        timeout: int = 1_200,
        max_retries: int = 1,
        pod_retry: int = 1,
        resume: bool = True,
        template_mode: bool = True,
        api_env: str = "evaluation",
        description: str | None = None,
        result_file: str | None = None,
        workdir_prefix: str | None = None,
        log_dir: str | None = None,
    ) -> dict[str, Any]:
        model = str(rollout_model).strip()
        benchmark = str(benchmark_dataset_name).strip()
        if not model:
            raise GoblinConfigurationError("rollout_model must not be empty")
        if not benchmark:
            raise GoblinConfigurationError("benchmark_dataset_name must not be empty")
        if not str(image).strip():
            raise GoblinConfigurationError("Goblin image must not be empty")
        if concurrency < 1:
            raise GoblinConfigurationError("Goblin concurrency must be positive")
        payload: dict[str, Any] = {
            "image": str(image).strip(),
            "concurrency": int(concurrency),
            "prompt_version": str(prompt_version).strip(),
            "model": str(reward_model).strip(),
            "rollout_model": model,
            "benchmark_dataset_name": benchmark,
            "query_round": str(query_round).strip(),
            "eval_mm": bool(eval_mm),
            "eval_miniprog": bool(eval_miniprog),
            "template_mode": bool(template_mode),
            "resume": bool(resume),
            "timeout": int(timeout),
            "max_retries": int(max_retries),
            "pod_retry": int(pod_retry),
            "api_env": str(api_env).strip(),
            "base64_encode": True,
            "description": description
            or f"{model}; {benchmark}; reward {prompt_version}; c{concurrency}",
        }
        for key, value in (
            ("result_file", result_file),
            ("workdir_prefix", workdir_prefix),
            ("log_dir", log_dir),
        ):
            if value:
                payload[key] = str(value)
        return payload

    def submit_file(
        self,
        file_path: str | Path,
        *,
        rollout_model: str,
        benchmark_dataset_name: str,
        upload: bool = True,
        **job_options: Any,
    ) -> GoblinJobSnapshot:
        """Submit an xlsx/jsonl file using upload or shared-path mode."""

        path = Path(file_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise GoblinConfigurationError(f"reward input file is missing or empty: {path}")
        payload = self.build_job_payload(
            rollout_model=rollout_model,
            benchmark_dataset_name=benchmark_dataset_name,
            **job_options,
        )
        if not upload:
            payload["file"] = str(path)
            response = self._request_json("POST", "/api/v1/jobs/", json_body=payload)
        else:
            # The upload endpoint expects JobCreate (without ``file``) as a
            # JSON string in multipart field ``params``.
            with path.open("rb") as stream:
                response = self._request_json(
                    "POST",
                    "/api/v1/jobs/upload",
                    data={"params": json.dumps(payload, ensure_ascii=False)},
                    files={"file": (path.name, stream, _mime_type(path))},
                )
        return GoblinJobSnapshot.from_payload(response)

    def get_job(self, job_id: str) -> GoblinJobSnapshot:
        identifier = str(job_id).strip()
        if not identifier:
            raise ValueError("job_id must not be empty")
        response = self._request_json("GET", f"/api/v1/jobs/{identifier}")
        return GoblinJobSnapshot.from_payload(response)

    def wait(
        self,
        job_id: str,
        *,
        poll_interval_seconds: float = 10.0,
        timeout_seconds: float = 3_600.0,
    ) -> GoblinJobSnapshot:
        """Poll until terminal; infrastructure failures are not reward zero."""

        deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
        interval = max(float(poll_interval_seconds), 0.05)
        while True:
            snapshot = self.get_job(job_id)
            if snapshot.status in {"failed", "cancelled"}:
                raise GoblinJobFailed(snapshot)
            if snapshot.status == "completed":
                return snapshot
            if time.monotonic() >= deadline:
                raise GoblinRewardError(f"Goblin job {snapshot.job_id} polling timed out")
            time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))


def _mime_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }.get(suffix, "application/octet-stream")


_ID_FIELDS = (
    "episode_id",
    "case_id",
    "app_id",
    "record_id",
    "id",
    "跑批单条记录id",
)
_REWARD_FIELDS = (
    "reward",
    "overall_reward",
    "final_reward",
    "reward_score",
    "goblin_reward",
    "score",
    "codex_reward",
    "codex_reward_score",
)


def load_reward_records(path: str | Path) -> list[dict[str, Any]]:
    """Load JSON/JSONL/CSV, with optional pandas support for Excel."""

    source = Path(path)
    if not source.is_file():
        raise GoblinRewardError(f"reward result file does not exist: {source}")
    suffix = source.suffix.casefold()
    if suffix in {".jsonl", ".ndjson"}:
        records: list[dict[str, Any]] = []
        with source.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise GoblinRewardError(
                        f"invalid JSONL at line {line_number}: {source}"
                    ) from exc
                if not isinstance(item, Mapping):
                    raise GoblinRewardError(f"JSONL line {line_number} is not an object")
                records.append(dict(item))
        return records
    if suffix == ".json":
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GoblinRewardError(f"invalid JSON result file: {source}") from exc
        if isinstance(value, Mapping):
            # Accept either one row or a wrapper such as {"results": [...]}
            for key in ("results", "records", "data", "items"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return [dict(item) for item in nested if isinstance(item, Mapping)]
            return [dict(value)]
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
        raise GoblinRewardError(f"JSON result is neither object nor list: {source}")
    if suffix == ".csv":
        with source.open(newline="", encoding="utf-8", errors="replace") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    if suffix in {".xlsx", ".xls"}:
        try:
            return _load_xlsx_records(source)
        except (OSError, KeyError, ValueError, ElementTree.ParseError):
            # Keep pandas as a fallback for unusual Excel files (multiple
            # sheets, rich strings, or legacy conversion artifacts).
            try:
                import pandas as pd  # type: ignore[import-not-found]
            except ImportError as pandas_exc:
                raise GoblinRewardError(
                    "could not parse Excel result with the stdlib parser; use JSONL "
                    "or install the optional pandas parser"
                ) from pandas_exc
            try:
                frame = pd.read_excel(source)
            except Exception as pandas_error:  # pragma: no cover - provider-specific
                raise GoblinRewardError(f"invalid Excel result file: {source}") from pandas_error
            return [
                {str(key): _json_scalar(value) for key, value in row.items()}
                for row in frame.to_dict(orient="records")
            ]
    raise GoblinRewardError(f"unsupported reward result suffix: {source.suffix}")


def _json_scalar(value: Any) -> Any:
    # Avoid pandas NaN leaking into JSON/joins.
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        with_context = value.item()
        return _json_scalar(with_context)
    return value


_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _column_number(reference: str) -> int:
    letters = "".join(char for char in reference if char.isalpha())
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def _load_xlsx_records(source: Path) -> list[dict[str, Any]]:
    """Read the first worksheet of a normal XLSX without a heavyweight dep."""

    with ZipFile(source) as archive:
        names = set(archive.namelist())
        sheet_name = next(
            (name for name in sorted(names) if name.startswith("xl/worksheets/sheet")),
            None,
        )
        if sheet_name is None:
            raise ValueError("xlsx has no worksheet")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{{{_XLSX_NS}}}si"):
                shared.append(
                    "".join(
                        text.text or ""
                        for text in item.iter(f"{{{_XLSX_NS}}}t")
                    )
                )
        sheet = ElementTree.fromstring(archive.read(sheet_name))
        rows: dict[int, dict[int, Any]] = defaultdict(dict)
        for row in sheet.findall(f".//{{{_XLSX_NS}}}row"):
            row_number = int(row.attrib.get("r", "0"))
            for cell in row.findall(f"{{{_XLSX_NS}}}c"):
                reference = cell.attrib.get("r", "")
                if not reference:
                    continue
                column = _column_number(reference)
                value_node = cell.find(f"{{{_XLSX_NS}}}v")
                inline_node = cell.find(f"{{{_XLSX_NS}}}is")
                value: Any = None
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr" and inline_node is not None:
                    value = "".join(
                        text.text or ""
                        for text in inline_node.iter(f"{{{_XLSX_NS}}}t")
                    )
                elif value_node is not None:
                    raw = value_node.text or ""
                    if cell_type == "s" and raw.isdigit():
                        index = int(raw)
                        value = shared[index] if index < len(shared) else raw
                    elif cell_type == "b":
                        value = raw == "1"
                    elif cell_type in {"str", "e"}:
                        value = raw
                    else:
                        try:
                            number = float(raw)
                            value = int(number) if number.is_integer() else number
                        except ValueError:
                            value = raw
                rows[row_number][column] = value
        if not rows:
            return []
        header_row_number = min(rows)
        header_row = rows[header_row_number]
        max_column = max(max(row) for row in rows.values())
        headers = [str(header_row.get(column) or "").strip() for column in range(1, max_column + 1)]
        if not any(headers):
            raise ValueError("xlsx has no header row")
        records: list[dict[str, Any]] = []
        for row_number in sorted(rows):
            if row_number == header_row_number:
                continue
            record = {
                headers[column - 1]: rows[row_number].get(column)
                for column in range(1, max_column + 1)
                if headers[column - 1]
            }
            if any(value not in (None, "") for value in record.values()):
                records.append(record)
        return records


def _coerce_reward(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    match = re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", text)
    if not match:
        return None
    try:
        number = float(text.rstrip("%"))
    except ValueError:
        return None
    if text.endswith("%"):
        number /= 100.0
    return number if math.isfinite(number) else None


def _first_value(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[str, Any] | None:
    for field_name in fields:
        if field_name in row:
            value = row[field_name]
            if isinstance(value, Mapping):
                nested = _first_value(value, ("reward", "score", "value", "overall"))
                if nested is not None:
                    return f"{field_name}.{nested[0]}", nested[1]
            if _coerce_reward(value) is not None:
                return field_name, value
    return None


def _first_identifier(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[str, Any] | None:
    for field_name in fields:
        if field_name not in row:
            continue
        value = row[field_name]
        if value is None or isinstance(value, (Mapping, list, tuple, set)):
            continue
        if str(value).strip():
            return field_name, value
    return None


def reward_map_from_file(
    path: str | Path,
    *,
    id_fields: Sequence[str] = _ID_FIELDS,
    reward_fields: Sequence[str] = _REWARD_FIELDS,
    normalize_scale: float = 1.0,
) -> dict[str, float]:
    """Return ``episode/case/app id -> scalar reward`` with strict joins.

    No textual rating is silently converted.  If Goblin's result schema uses a
    new reward column, the caller must name it explicitly in ``reward_fields``
    rather than accidentally training on the wrong metric.
    """

    if not math.isfinite(float(normalize_scale)) or normalize_scale == 0:
        raise ValueError("normalize_scale must be finite and non-zero")
    result: dict[str, float] = {}
    for index, row in enumerate(load_reward_records(path), start=1):
        identifier = _first_identifier(row, id_fields)
        if identifier is None:
            raise GoblinRewardError(f"reward row {index} has no join id")
        key = str(identifier[1]).strip()
        if not key:
            raise GoblinRewardError(f"reward row {index} has an empty join id")
        reward_value = _first_value(row, reward_fields)
        if reward_value is None:
            raise GoblinRewardError(
                f"reward row {index} has no numeric reward field; tried {list(reward_fields)}"
            )
        reward = _coerce_reward(reward_value[1])
        if reward is None:
            raise GoblinRewardError(f"reward row {index} reward is not numeric")
        if key in result:
            raise GoblinRewardError(f"duplicate reward join id: {key}")
        result[key] = reward / float(normalize_scale)
    return result
