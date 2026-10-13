# Repository Guidance

## Architecture

- `backend/research_ops/api`: HTTP and WebSocket boundaries only.
- `backend/research_ops/services`: deterministic task, branch, memory, and workspace state transitions.
- `backend/research_ops/codex`: official CLI execution adapter plus version-isolated App Server lineage adapter.
- `backend/research_ops/models.py`: operational state. Raw chat is not canonical memory.
- `web`: operational React workspace for trees, conversations, merges, and memory.
- `config`: versioned Program definitions and defaults.

## Commands

- Install: `uv sync --extra dev` and `cd web && npm install`
- Test backend: `uv run pytest`
- Lint backend: `uv run ruff check backend tests`
- Run API: `uv run rops serve --reload`
- Run web: `cd web && npm run dev`

## Invariants

- A Codex thread is an execution surface, never the source of truth for task state.
- WorkItem state transitions, leases, acceptance, and external job identities are deterministic.
- Forks freeze a context snapshot; parent and child do not silently share mutable context.
- Writable sibling branches never share a Git worktree.
- Secrets remain environment references and never enter briefs, messages, memory, or traces.
- Memory promotion requires provenance and explicit review.
- Codex CLI and App Server protocol changes are isolated behind `CodexAdapter`.
