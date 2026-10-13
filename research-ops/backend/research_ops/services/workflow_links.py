from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.services.core import NotFoundError
from research_ops.settings import Settings

_LABEL = re.compile(r"workflow|\bdag\b|工作流|流程图", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\[\]`]+")
_SECRET_PARAM = re.compile(r"token|secret|password|signature|authorization|api[_-]?key", re.I)


def safe_url(value: str) -> str | None:
    if len(value) > 2048 or any(ord(char) < 33 for char in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username or parts.password or not parts.port and parts.netloc.endswith(":"):
            return None
        query = [*parse_qsl(parts.query), *parse_qsl(parts.fragment)]
        if any(_SECRET_PARAM.search(key) or key.lower() == "key" for key, _ in query):
            return None
        return urlunsplit(parts)
    except ValueError:
        return None


def workflow_links(text: str, octo_base: str) -> list[dict[str, str]]:
    """Extract links, never instructions or code-block examples, without following URLs."""
    parser = MarkdownIt("commonmark")
    octo = urlsplit(octo_base)
    links: dict[str, dict[str, str]] = {}

    def add(href: str, label: str = "") -> None:
        url = safe_url(href)
        if not url:
            return
        parts = urlsplit(url)
        known = (
            parts.netloc.lower() == octo.netloc.lower()
            and parts.fragment.startswith("workflow/")
            and bool(parts.fragment.removeprefix("workflow/"))
        )
        labelled = bool(_LABEL.search(label)) and not label.startswith(("http://", "https://"))
        if not known and not labelled:
            return
        title = label.strip() if label and label != href else ""
        title = title or unquote(parts.fragment.removeprefix("workflow/"))
        links.setdefault(url, {"url": url, "title": title[:240] or "Workflow"})

    for block in parser.parse(text):
        if block.type != "inline":
            continue
        tokens = block.children or []
        in_link = False
        for index, token in enumerate(tokens):
            if token.type == "link_open":
                in_link = True
                label = []
                for child in tokens[index + 1 :]:
                    if child.type == "link_close":
                        break
                    if child.type in {"text", "code_inline"}:
                        label.append(child.content)
                add(token.attrGet("href") or "", "".join(label))
            elif token.type == "link_close":
                in_link = False
            elif token.type == "text" and not in_link:
                for match in _URL.finditer(token.content):
                    add(match.group().rstrip(".,;!。；，！"))
    return list(links.values())


def _bounded(column):
    # Keep the beginning and end in SQL; very large answers never cross the DB boundary.
    return case(
        (func.length(column) <= 32768, column),
        else_=func.substr(column, 1, 16384) + "\n\n" + func.substr(column, -16384),
    )


def _candidate(column):
    return and_(
        column.contains("http"),
        or_(
            column.ilike("%workflow%"),
            column.ilike("%dag%"),
            column.contains("工作流"),
            column.contains("流程图"),
        ),
    )


class WorkflowLinkService:
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    def view(self, branch_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            if session.get(models.ExecutionBranch, branch_id) is None:
                raise NotFoundError("Branch not found")
            turns = session.execute(
                select(
                    models.CodexTurn.id,
                    models.CodexTurn.completed_at,
                    _bounded(models.CodexTurn.prompt),
                    _bounded(models.CodexTurn.final_answer),
                )
                .where(
                    models.CodexTurn.branch_id == branch_id,
                    or_(
                        _candidate(models.CodexTurn.prompt),
                        _candidate(models.CodexTurn.final_answer),
                    ),
                )
                .order_by(models.CodexTurn.completed_at.desc(), models.CodexTurn.id.desc())
                .limit(200)
            ).all()
            messages = session.execute(
                select(
                    models.ConversationMessage.id,
                    models.ConversationMessage.created_at,
                    _bounded(models.ConversationMessage.content),
                )
                .where(
                    models.ConversationMessage.branch_id == branch_id,
                    models.ConversationMessage.role.in_(["user", "assistant"]),
                    _candidate(models.ConversationMessage.content),
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(200)
            ).all()
            legacy = session.get(models.SessionWorkflow, branch_id)
            graphs = list(legacy.graphs or []) if legacy else []

        records = [
            (at, f"turn:{id}", f"{prompt or ''}\n\n{answer or ''}")
            for id, at, prompt, answer in turns
        ]
        records.extend((at, f"message:{id}", content) for id, at, content in messages)
        result: dict[str, dict[str, Any]] = {}
        remaining = 512_000
        for at, record_id, text in sorted(records, key=lambda row: row[0], reverse=True):
            if remaining <= 0:
                break
            text = text[:remaining]
            remaining -= len(text)
            for link in workflow_links(text, self.settings.octo_base_url):
                result.setdefault(
                    link["url"],
                    {
                        **link,
                        "source": "conversation",
                        "record_id": record_id,
                        "at": at.isoformat(),
                    },
                )
        # Preserve previously associated Octo links, but never start the former observer.
        for graph in graphs:
            if graph.get("source") != "octo":
                continue
            job_id = str(graph.get("id", "")).removeprefix("octo:")
            if not job_id:
                continue
            url = safe_url(
                f"{self.settings.octo_base_url.rstrip('/')}/#workflow/{quote(job_id, safe='')}"
            )
            if url:
                result.setdefault(
                    url,
                    {
                        "url": url,
                        "title": str(graph.get("title") or job_id)[:240],
                        "source": "octo",
                        "record_id": graph.get("id"),
                        "at": graph.get("observed_at"),
                    },
                )
        return {"branch_id": branch_id, "links": list(result.values())[:64]}
