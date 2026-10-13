"""Opaque per-episode correlation carried through managed Miaoda rollouts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

_CORRELATION_TOKEN_RE = re.compile(r"^ep-[0-9a-f]{32}$")
_CORRELATION_MARKER_RE = re.compile(
    r"\[\[ORS_MOPD_EPISODE:(?P<token>ep-[0-9a-f]{32})\]\][ \t]*(?:\r?\n)?"
)


class EpisodeCorrelationError(ValueError):
    """Raised when a managed request contains ambiguous correlation data."""


def episode_correlation_token(
    *, batch_id: str, episode_id: str, member_index: int
) -> str:
    """Return a stable opaque token without exposing the source episode ID."""

    material = f"{batch_id}\0{episode_id}\0{int(member_index)}".encode()
    return f"ep-{hashlib.sha256(material).hexdigest()[:32]}"


def tag_miaoda_query(query: str, correlation_token: str) -> str:
    """Prefix a query with a marker that the trusted policy proxy removes."""

    normalized_query = str(query)
    token = str(correlation_token).strip().casefold()
    if not _CORRELATION_TOKEN_RE.fullmatch(token):
        raise EpisodeCorrelationError("invalid managed episode correlation token")
    if _CORRELATION_MARKER_RE.search(normalized_query):
        raise EpisodeCorrelationError("Miaoda query already contains a correlation marker")
    return f"[[ORS_MOPD_EPISODE:{token}]]\n{normalized_query}"


def strip_episode_correlation(value: Any) -> tuple[Any, str | None]:
    """Remove markers recursively and return their unique opaque token."""

    found: set[str] = set()

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            found.update(
                match.group("token")
                for match in _CORRELATION_MARKER_RE.finditer(item)
            )
            return _CORRELATION_MARKER_RE.sub("", item)
        if isinstance(item, Mapping):
            return {str(key): visit(child) for key, child in item.items()}
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [visit(child) for child in item]
        return item

    cleaned = visit(value)
    if len(found) > 1:
        raise EpisodeCorrelationError(
            "managed policy request contains multiple episode correlation tokens"
        )
    return cleaned, next(iter(found), None)
