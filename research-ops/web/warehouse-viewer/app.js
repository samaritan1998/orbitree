const state = {
  manifest: null,
  sessionId: null,
  selectedBatch: null,
  selectedSummary: null,
  detail: null,
  view: "trajectory",
  sourceType: "dataset",
  loading: false,
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const jsonText = (value) => JSON.stringify(value, null, 2);
const formatNumber = (value) => Number(value || 0).toLocaleString("zh-CN");
const formatBytes = (value) => {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
};
const valueType = (value) => Array.isArray(value) ? `array[${value.length}]` : value === null ? "null" : typeof value === "object" ? `object{${Object.keys(value).length}}` : typeof value;
const contentText = (content) => {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((item) => typeof item === "string" ? item : item?.text ?? jsonText(item)).join("\n");
  return content == null ? "" : jsonText(content);
};
const getMessages = (record) => {
  let messages = record?.messages;
  if (typeof messages === "string") {
    try { messages = JSON.parse(messages); } catch (_) { messages = []; }
  }
  if (!Array.isArray(messages) && Array.isArray(record?.original_sample?.messages)) messages = record.original_sample.messages;
  return Array.isArray(messages) ? messages : [];
};

function toast(message, type = "") {
  const element = $("toast");
  element.textContent = message;
  element.className = `toast show ${type}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 3200);
}

async function api(path, init) {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

function setLoading(loading) {
  state.loading = loading;
  $("loadButton").disabled = loading;
  $("resampleButton").disabled = loading || !state.manifest;
  $("loadButton").textContent = loading ? "正在采样…" : state.sourceType === "batch" ? "加载 Batch" : "加载数据集";
  if (loading) {
    $("batchList").innerHTML = '<div class="empty compact-empty loading">正在流式扫描各 batch</div>';
    $("detailContent").innerHTML = '<div class="empty loading">采样完成后展示轨迹</div>';
  }
}

function updateSummary() {
  const payload = state.manifest;
  if (!payload) return;
  const source = payload.source || payload.dataset || payload.batch || {};
  const fallbackDescription = payload.source_kind === "batch"
    ? [source.status, ...(source.tags || [])].filter(Boolean).join(" · ")
    : payload.source_id;
  $("datasetName").textContent = source.name || payload.source_id;
  $("datasetDescription").textContent = source.description || fallbackDescription;
  $("batchCount").textContent = formatNumber(payload.batch_count);
  $("sampleCount").textContent = formatNumber(payload.sample_count);
  $("recordCount").textContent = formatNumber(payload.batches.reduce((sum, batch) => sum + batch.record_count, 0));
  $("seedValue").textContent = payload.seed;
  $("browserMeta").textContent = `${payload.batch_count} batches / ${payload.sample_count} samples`;
  $("warehouseLink").href = payload.warehouse_url;
  $("warehouseLink").classList.remove("disabled");
}

function sampleLabel(summary) {
  const identity = summary.identity || {};
  return identity.app_id || identity.id || identity.sample_index || `line ${summary.source_line}`;
}

function renderBrowser() {
  const batches = state.manifest?.batches || [];
  $("batchList").innerHTML = batches.map((batch, batchIndex) => `
    <section class="batch-group ${batchIndex === 0 ? "open" : ""}" data-batch="${escapeHtml(batch.batch_id)}">
      <button class="batch-summary" type="button" data-toggle-batch="${escapeHtml(batch.batch_id)}">
        <span><strong title="${escapeHtml(batch.batch_id)}">${escapeHtml(batch.batch_id)}</strong><span>${formatNumber(batch.record_count)} records · ${formatBytes(batch.file_size)}${batch.invalid_record_count ? ` · ${batch.invalid_record_count} invalid` : ""}</span></span>
        <em>${batch.samples.length}</em>
      </button>
      <div class="batch-samples">
        ${batch.samples.map((sample, index) => `
          <button class="sample-row" type="button" data-batch-id="${escapeHtml(batch.batch_id)}" data-sample-id="${sample.sample_id}">
            <span class="sample-number">${String(index + 1).padStart(2, "0")}</span>
            <span><strong title="${escapeHtml(sample.preview)}">${escapeHtml(sampleLabel(sample))} · ${escapeHtml(sample.preview)}</strong><span>L${sample.source_line} · ${sample.message_count} turns · R ${formatNumber(sample.reasoning_chars)} · M ${sample.explicit_mask_count}</span></span>
          </button>`).join("")}
      </div>
    </section>`).join("");

  document.querySelectorAll("[data-toggle-batch]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".batch-group").classList.toggle("open"));
  });
  document.querySelectorAll("[data-sample-id]").forEach((button) => {
    button.addEventListener("click", () => selectSample(button.dataset.batchId, button.dataset.sampleId));
  });
}

function setSourceType(sourceType) {
  state.sourceType = sourceType === "batch" ? "batch" : "dataset";
  document.querySelectorAll("[data-source-type]").forEach((button) => {
    const active = button.dataset.sourceType === state.sourceType;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const isBatch = state.sourceType === "batch";
  $("sourceInputLabel").textContent = isBatch ? "Warehouse SFT batch" : "Warehouse dataset";
  $("datasetInput").name = state.sourceType;
  $("datasetInput").placeholder = isBatch
    ? "https://warehouse.example.com/batch/2026..."
    : "https://warehouse.example.com/datasets/ds_...";
  if (!state.loading) $("loadButton").textContent = isBatch ? "加载 Batch" : "加载数据集";
}

async function loadSource() {
  const sourceValue = $("datasetInput").value.trim();
  const perBatch = Number($("perBatchInput").value);
  const seed = Number($("seedInput").value);
  setLoading(true);
  try {
    const payload = await api("/api/samples", {
      method: "POST",
      body: JSON.stringify({ [state.sourceType]: sourceValue, per_batch: perBatch, seed }),
    });
    setSourceType(payload.source_kind);
    state.manifest = payload;
    state.sessionId = payload.session_id;
    state.detail = null;
    state.selectedSummary = null;
    updateSummary();
    renderBrowser();
    $("resampleButton").disabled = false;
    const firstBatch = payload.batches.find((batch) => batch.samples.length);
    if (firstBatch) await selectSample(firstBatch.batch_id, firstBatch.samples[0].sample_id);
    const url = new URL(window.location.href);
    url.searchParams.delete("dataset");
    url.searchParams.delete("batch");
    url.searchParams.set(payload.source_kind, payload.source_id);
    url.searchParams.set("n", payload.per_batch);
    url.searchParams.set("seed", payload.seed);
    history.replaceState({}, "", url);
    toast(payload.source_kind === "batch"
      ? `已从该 batch 采样 ${payload.sample_count} 条`
      : `已从 ${payload.batch_count} 个 batch 采样 ${payload.sample_count} 条`);
  } catch (error) {
    $("batchList").innerHTML = `<div class="empty compact-empty">${escapeHtml(error.message)}</div>`;
    $("detailContent").innerHTML = '<div class="empty">加载失败，请检查 Warehouse URL、ID 和来源状态。</div>';
    toast(error.message, "error");
  } finally {
    setLoading(false);
  }
}

async function selectSample(batchId, sampleId) {
  if (!state.sessionId) return;
  state.selectedBatch = state.manifest.batches.find((batch) => batch.batch_id === batchId);
  state.selectedSummary = state.selectedBatch?.samples.find((sample) => sample.sample_id === sampleId);
  document.querySelectorAll(".sample-row").forEach((row) => row.classList.toggle("active", row.dataset.sampleId === sampleId));
  const group = document.querySelector(`[data-batch="${CSS.escape(batchId)}"]`);
  group?.classList.add("open");
  $("detailContent").innerHTML = '<div class="empty loading">正在读取完整记录</div>';
  try {
    state.detail = await api(`/api/sessions/${state.sessionId}/samples/${sampleId}`);
    renderSelected();
  } catch (error) {
    $("detailContent").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
    toast(error.message, "error");
  }
}

function renderSelected() {
  const record = state.detail?.record;
  const summary = state.selectedSummary;
  if (!record || !summary) return;
  $("sampleLocation").textContent = `${state.selectedBatch.batch_id} / LINE ${summary.source_line}`;
  $("sampleTitle").textContent = `${summary.message_count} turns · ${formatBytes(summary.byte_size)}`;
  const identity = Object.entries(summary.identity || {}).map(([key, value]) => `${key}=${value}`).join(" · ");
  $("sampleIdentity").textContent = identity || summary.top_level_keys.join(" · ");
  $("expandButton").disabled = false;
  $("collapseButton").disabled = false;
  $("copyButton").disabled = false;
  renderDetail();
  renderInspector();
}

function badge(text, kind = "") { return `<span class="badge ${kind}">${escapeHtml(text)}</span>`; }

function renderToolCalls(calls) {
  if (!Array.isArray(calls) || !calls.length) return "";
  return `<div class="data-block"><div class="block-heading"><span>tool_calls</span><span>${calls.length}</span></div>${calls.map((call) => {
    const fn = call?.function || call || {};
    return `<div class="tool-call"><div class="tool-name">${escapeHtml(fn.name || call?.name || "tool")}</div><pre>${escapeHtml(jsonText(fn.arguments ?? call))}</pre></div>`;
  }).join("")}</div>`;
}

function renderMessage(message, index, total) {
  const role = String(message?.role || "unknown");
  const calls = Array.isArray(message?.tool_calls) ? message.tool_calls : [];
  const reasoning = contentText(message?.reasoning_content);
  const badges = [];
  if (Object.prototype.hasOwnProperty.call(message, "step_loss_mask")) badges.push(badge(`mask=${message.step_loss_mask}`, Number(message.step_loss_mask) === 1 ? "train" : "skip"));
  if (reasoning) badges.push(badge(`reason ${formatNumber(reasoning.length)}`, "reason"));
  if (calls.length) badges.push(badge(`${calls.length} tool`, "tool"));
  const blocks = [];
  if (Object.prototype.hasOwnProperty.call(message, "content")) blocks.push(`<div class="data-block"><div class="block-heading"><span>content</span><span>${valueType(message.content)}</span></div><pre>${escapeHtml(contentText(message.content) || "(empty)")}</pre></div>`);
  if (reasoning) blocks.push(`<div class="data-block reasoning-block"><div class="block-heading"><span>reasoning_content</span><span>${formatNumber(reasoning.length)} chars</span></div><div class="reasoning-content"><pre>${escapeHtml(reasoning)}</pre></div></div>`);
  if (Object.prototype.hasOwnProperty.call(message, "thinking_blocks") && message.thinking_blocks != null) blocks.push(`<div class="data-block reasoning-block"><div class="block-heading"><span>thinking_blocks</span><span>${valueType(message.thinking_blocks)}</span></div><div class="reasoning-content"><pre>${escapeHtml(jsonText(message.thinking_blocks))}</pre></div></div>`);
  blocks.push(renderToolCalls(calls));
  const handled = new Set(["role", "content", "reasoning_content", "thinking_blocks", "tool_calls", "step_loss_mask"]);
  const other = Object.fromEntries(Object.entries(message || {}).filter(([key]) => !handled.has(key)));
  if (Object.keys(other).length) blocks.push(`<div class="data-block"><div class="block-heading"><span>other message fields</span><span>${Object.keys(other).length}</span></div><pre>${escapeHtml(jsonText(other))}</pre></div>`);
  return `<details class="message role-${escapeHtml(role)}" ${index === 0 || index === total - 1 ? "open" : ""}><summary><span class="role-label">${escapeHtml(role)}</span><span class="message-badges">${badges.join("")}</span><span class="message-turn">TURN ${String(index + 1).padStart(3, "0")}</span></summary><div class="message-body">${blocks.filter(Boolean).join("") || '<pre>(empty message)</pre>'}</div></details>`;
}

function renderMask(record) {
  const messages = getMessages(record);
  const rows = messages.map((message, index) => {
    const explicit = Object.prototype.hasOwnProperty.call(message, "step_loss_mask");
    const value = explicit ? Number(message.step_loss_mask) : null;
    const kind = !explicit ? "unset" : value === 1 ? "train" : "skip";
    const label = !explicit ? "未设置" : value === 1 ? "1 · train" : `${message.step_loss_mask} · skip`;
    return `<tr><td>${index + 1}</td><td>${escapeHtml(message.role || "unknown")}</td><td class="mask-cell ${kind}">${escapeHtml(label)}</td><td>${formatNumber(contentText(message.reasoning_content).length)}</td><td>${escapeHtml(contentText(message.content).replace(/\s+/g, " ").slice(0, 120) || "(empty)")}</td></tr>`;
  }).join("");
  const topLevel = Object.prototype.hasOwnProperty.call(record, "loss_mask") ? `<div class="data-block"><div class="block-heading"><span>top-level loss_mask</span><span>${valueType(record.loss_mask)}</span></div><pre>${escapeHtml(jsonText(record.loss_mask))}</pre></div>` : "";
  return `<p class="mask-note">这里展示数据源中的 message-level <code>step_loss_mask</code>。训练框架按 chat template 生成的 token-level mask 不属于原始 Warehouse 记录；若源数据包含顶层 <code>loss_mask</code>，会在表格下方原样展示。</p><table class="mask-table"><thead><tr><th>Turn</th><th>Role</th><th>step_loss_mask</th><th>Reason chars</th><th>Content preview</th></tr></thead><tbody>${rows}</tbody></table>${topLevel}`;
}

function renderDetail() {
  const record = state.detail?.record;
  if (!record) return;
  if (state.view === "raw") {
    $("detailContent").innerHTML = `<div class="raw-view"><pre>${escapeHtml(jsonText(record))}</pre></div>`;
    return;
  }
  if (state.view === "mask") {
    $("detailContent").innerHTML = renderMask(record);
    return;
  }
  const messages = getMessages(record);
  $("detailContent").innerHTML = messages.length
    ? `<div class="message-list">${messages.map((message, index) => renderMessage(message, index, messages.length)).join("")}</div>`
    : '<div class="empty">这条记录没有可识别的 messages；请在“原始 JSON”查看全部字段。</div>';
}

function renderInspector() {
  const record = state.detail?.record;
  const summary = state.selectedSummary;
  if (!record || !summary) return;
  const messages = getMessages(record);
  const metadata = record.metadata ?? record.source_meta ?? null;
  const fields = Object.entries(record).map(([key, value]) => `<div class="field-row"><span class="field-key" title="${escapeHtml(key)}">${escapeHtml(key)}</span><span class="field-type">${escapeHtml(valueType(value))}</span></div>`).join("");
  const redactions = state.detail.redacted_paths || [];
  $("inspectorContent").innerHTML = `
    <section class="inspector-section"><h2>样本概览</h2><div class="inspector-metrics">
      <div><span>Messages</span><strong>${summary.message_count}</strong></div>
      <div><span>Tools</span><strong>${summary.tool_call_count}</strong></div>
      <div><span>Reason chars</span><strong>${formatNumber(summary.reasoning_chars)}</strong></div>
      <div><span>Explicit masks</span><strong>${summary.explicit_mask_count}</strong></div>
    </div></section>
    <section class="inspector-section"><h2>训练信号</h2><div class="inspector-metrics">
      <div><span>Train turns</span><strong>${summary.trained_steps}</strong></div>
      <div><span>Skip turns</span><strong>${summary.skipped_steps}</strong></div>
      <div><span>Assistant</span><strong>${summary.role_counts.assistant || 0}</strong></div>
      <div><span>Tool result</span><strong>${summary.role_counts.tool || 0}</strong></div>
    </div></section>
    <section class="inspector-section"><h2>顶层字段 · ${Object.keys(record).length}</h2><div class="field-list">${fields}</div></section>
    ${metadata ? `<section class="inspector-section"><h2>${record.metadata ? "metadata" : "source_meta"}</h2><div class="mini-json"><pre>${escapeHtml(jsonText(metadata))}</pre></div></section>` : ""}
    ${Array.isArray(record.tools) ? `<section class="inspector-section"><h2>Tools · ${record.tools.length}</h2><div class="mini-json"><pre>${escapeHtml(jsonText(record.tools))}</pre></div></section>` : ""}
    ${redactions.length ? `<section class="inspector-section"><h2>安全脱敏 · ${redactions.length}</h2><p class="redaction-note">以下字段的值未返回：${escapeHtml(redactions.join("、"))}</p></section>` : ""}
    <section class="inspector-section"><h2>Role 分布</h2><div class="mini-json"><pre>${escapeHtml(jsonText(summary.role_counts))}</pre></div></section>`;
}

$("datasetForm").addEventListener("submit", (event) => { event.preventDefault(); loadSource(); });
document.querySelectorAll("[data-source-type]").forEach((button) => {
  button.addEventListener("click", () => setSourceType(button.dataset.sourceType));
});
$("resampleButton").addEventListener("click", () => {
  $("seedInput").value = Math.floor(Math.random() * 2_147_483_647);
  loadSource();
});
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  state.view = tab.dataset.view;
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
  renderDetail();
}));
$("expandButton").addEventListener("click", () => document.querySelectorAll("details.message").forEach((item) => { item.open = true; }));
$("collapseButton").addEventListener("click", () => document.querySelectorAll("details.message").forEach((item) => { item.open = false; }));
$("copyButton").addEventListener("click", async () => {
  if (!state.detail?.record) return;
  try { await navigator.clipboard.writeText(jsonText(state.detail.record)); toast("完整 JSON 已复制"); }
  catch (_) { toast("浏览器拒绝访问剪贴板", "error"); }
});

const params = new URLSearchParams(window.location.search);
const initialSourceType = params.get("batch") ? "batch" : "dataset";
const initialSource = params.get(initialSourceType);
setSourceType(initialSourceType);
if (initialSource) {
  $("datasetInput").value = initialSource;
  if (params.get("n")) $("perBatchInput").value = params.get("n");
  if (params.get("seed")) $("seedInput").value = params.get("seed");
  loadSource();
}
