import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { workflowLinksFixture } from "../web/src/test/workflowFixture.ts";

// Read-only UI preview with fixtures. It never contacts the production API.
const root = resolve(process.argv[2] || "web/dist-workflows");
const port = Number(process.argv[3] || 5175);
const now = "2026-09-15T08:00:00Z";
const program = { id: "p", code: "ADAPT", slug: "adapt", name: "Workflow Preview", objective: "", color: "#28704f", sort_order: 1, active: true };
const work = { id: "w", human_id: "ADAPT-PREVIEW", program_id: "p", title: "Preview: long-context paired evaluation", objective: "", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "workflow-test", work_item_id: "w", name: "Read-only preview", slug: "workflow-test", status: "active", mode: "thread", cwd: "/tmp/workflow-preview", codex_thread_id: "preview-not-a-real-session", runtime: "codex", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: false, can_interrupt: false, web_turn_status: "idle", local_turn_active: false, blocked_reason: "Read-only preview" };
const turn = { id: "preview-turn", branch_id: branch.id, thread_id: branch.codex_thread_id, external_turn_id: "preview", status: "completed", prompt: "Check the latest experiment", final_answer: "Training is complete. The paired evaluation is running.", completed_at: now, started_at: now, metadata_json: {} };
const detail = { branch, work_item: work, runtime: "codex", session: null, turns: [turn], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
const fixture = structuredClone(workflowLinksFixture);
const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".png": "image/png" };
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  const pathname = url.pathname;
  res.setHeader("Cache-Control", "no-store");
  if (pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (req.method !== "GET") { res.writeHead(405).end(JSON.stringify({ detail: "Read-only preview; no task was changed" })); return; }
    let body = [];
    if (pathname === "/api/programs") body = [program];
    else if (pathname.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
    else if (pathname.endsWith("/desk")) body = { work_item: work, branch };
    else if (pathname.endsWith("/conversation-state")) body = state;
    else if (pathname.endsWith("/native-goal")) body = { supported: false, goal: null, runtime: {} };
    else if (pathname.endsWith("/workflow-links")) body = fixture;
    else if (pathname.endsWith("/workflow-test")) body = detail;
    res.end(JSON.stringify(body));
    return;
  }
  const path = resolve(root, "." + pathname);
  if (!path.startsWith(root + "/")) { res.writeHead(403).end(); return; }
  const file = extname(path) ? path : resolve(root, "index.html");
  try { res.setHeader("Content-Type", mime[extname(file)] || "application/octet-stream"); res.end(await readFile(file)); }
  catch { res.writeHead(404).end(); }
});
server.on("error", (error) => { console.error(error.message); process.exitCode = 1; });
server.listen(port, "0.0.0.0", () => console.log(`Read-only workflow preview: http://127.0.0.1:${port}/programs/ADAPT/branches/workflow-test/results`));
