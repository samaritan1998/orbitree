import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Button,
  FluentProvider,
  Input,
  Select,
  Spinner,
  Tab,
  TabList,
  Tooltip,
  webLightTheme,
} from "@fluentui/react-components";
import {
  ArrowCollapseAll20Regular,
  ArrowDownload20Regular,
  ArrowExpandAll20Regular,
  ArrowReset20Regular,
  ChevronLeft20Regular,
  ChevronRight20Regular,
  Copy20Regular,
  Dismiss20Regular,
  Eye20Regular,
  Search20Regular,
} from "@fluentui/react-icons";
import "./styles.css";

type PreviewRow = {
  trajectory_id: string;
  run_id: string;
  task_id: string;
  attempt_index: number;
  source_batch_id: string | null;
  task_type: string;
  query: string;
  status: string;
  artifact_status: string;
  slide_count: number | null;
  rollout_model: string | null;
  is_multimodal: boolean;
  trajectory_event_count: number;
  llm_request_count: number;
  tool_call_count: number;
  image_count: number;
  signature_count: number;
  decoded_thinking_count: number;
  thinking_decode_status: string;
  reward_status: string;
  reward_total: number | null;
  cost_usd: number | null;
  duration_ms: number;
  integrity_status: string;
  secret_scan_status: string;
  secret_scan_hit_count: number;
  trajectory_size_bytes: number;
  llm_trace_size_bytes: number;
  decode_failed_count: number;
  decode_not_attempted_count: number;
  validation_error_count: number;
};

type FieldDefinition = {
  name: string;
  semantic_type: string;
  warehouse_view_type: string;
};

type MetaPayload = {
  batch: Record<string, unknown>;
  validation: {
    actual: Record<string, any>;
    checks: Record<string, boolean>;
  };
  schema: {
    schema_version: string;
    producer_version: string;
    table_key: string;
    display_name: string;
    unique_key: string[];
    field_count: number;
    fields: FieldDefinition[];
  };
};

type FullRow = Record<string, any>;
type DetailTab = "summary" | "trajectory" | "llm" | "thinking" | "reward" | "fields";

const STATUS_LABELS: Record<string, string> = {
  success: "成功",
  failed: "失败",
  complete: "完整",
  partial: "部分",
  unresolved: "未解码",
  not_applicable: "不适用",
  not_selected_for_evaluation: "未选评测",
  preflight_failed: "预检失败",
  not_evaluated: "未评测",
  pass: "通过",
  missing: "缺失",
  score_mismatch: "分数不一致",
  pptx_only: "仅 PPTX",
};

const JSON_FIELD_NAMES = new Set([
  "task_axes",
  "reference_files",
  "input_assets",
  "task_metadata",
  "skill_refs",
  "runtime_config",
  "trajectory",
  "trajectory_transformations",
  "llm_trace",
  "trajectory_images",
  "raw_manifest",
  "thinking_decode",
  "error_detail",
  "risk_flags",
  "rendered_slides",
  "output_files",
  "usage_detail",
  "reward_judge_models",
  "reward_detail",
  "validation_errors",
]);

function parseJSON<T>(value: unknown, fallback: T): T {
  if (typeof value !== "string") return (value as T) ?? fallback;
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

function formatNumber(value: unknown): string {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? new Intl.NumberFormat("zh-CN").format(numeric) : "-";
}

function formatBytes(value: unknown): string {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function formatDuration(value: unknown): string {
  const ms = Number(value);
  if (!Number.isFinite(ms)) return "-";
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.round((ms % 60000) / 1000);
  return minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function formatReward(value: unknown): string {
  const score = Number(value);
  return Number.isFinite(score) ? score.toFixed(3) : "-";
}

function formatCost(value: unknown): string {
  const cost = Number(value);
  return Number.isFinite(cost) ? `$${cost.toFixed(2)}` : "-";
}

function label(value: unknown): string {
  const text = String(value ?? "");
  return STATUS_LABELS[text] || text || "-";
}

function statusTone(value: unknown): string {
  const text = String(value ?? "");
  if (["success", "complete", "pass"].includes(text)) return "ok";
  if (["failed", "unresolved", "preflight_failed", "fail"].includes(text)) return "bad";
  if (["partial", "pptx_only", "not_evaluated"].includes(text)) return "warn";
  return "neutral";
}

function StatusText({ value }: { value: unknown }) {
  return <span className={`status-text status-${statusTone(value)}`}>{label(value)}</span>;
}

function mediaURL(path: unknown): string | undefined {
  if (typeof path !== "string" || !path) return undefined;
  if (path.startsWith("assets/images/")) return `/${path}`;
  return `/media?path=${encodeURIComponent(path)}`;
}

function copyText(value: string) {
  void navigator.clipboard.writeText(value);
}

function IconButton({
  label: tooltip,
  icon,
  onClick,
  disabled,
}: {
  label: string;
  icon: React.ReactElement;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Tooltip content={tooltip} relationship="label">
      <Button
        appearance="subtle"
        size="small"
        icon={icon}
        aria-label={tooltip}
        onClick={onClick}
        disabled={disabled}
      />
    </Tooltip>
  );
}

function CodeBlock({ value, className = "" }: { value: unknown; className?: string }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return <pre className={`code-block ${className}`}>{text}</pre>;
}

function ImageBlock({ source, alt }: { source: Record<string, any>; alt: string }) {
  const src = mediaURL(source.path || source.url);
  if (!src) return <CodeBlock value={source} />;
  return (
    <figure className="trace-image">
      <img
        src={src}
        alt={alt}
        loading="lazy"
        width={source.width || undefined}
        height={source.height || undefined}
      />
      <figcaption>
        <span>{source.media_type || "image"}</span>
        <span>{source.width && source.height ? `${source.width} x ${source.height}` : ""}</span>
        <span className="path-text">{source.path}</span>
      </figcaption>
    </figure>
  );
}

function ContentBlock({ block, path }: { block: any; path: string }) {
  if (block === null || block === undefined) return <span className="empty-value">null</span>;
  if (typeof block === "string") return <CodeBlock value={block} className="natural-text" />;
  if (Array.isArray(block)) {
    return (
      <div className="block-stack">
        {block.map((item, index) => (
          <ContentBlock key={`${path}-${index}`} block={item} path={`${path}/${index}`} />
        ))}
      </div>
    );
  }
  if (typeof block !== "object") return <span>{String(block)}</span>;
  if (["image", "input_image"].includes(block.type) && block.source) {
    return <ImageBlock source={block.source} alt={`轨迹图片 ${path}`} />;
  }
  if (block.type === "thinking") {
    return (
      <section className="thinking-block">
        <div className="block-title-row">
          <strong>Thinking</strong>
          <span>{block.thinking ? `${block.thinking.length} chars` : "无明文"}</span>
        </div>
        {block.thinking ? <CodeBlock value={block.thinking} className="natural-text" /> : null}
        {block.signature ? (
          <details className="signature-disclosure">
            <summary>Signature ({block.signature.length} chars)</summary>
            <div className="copy-row">
              <CodeBlock value={block.signature} />
              <IconButton
                label="复制 signature"
                icon={<Copy20Regular />}
                onClick={() => copyText(block.signature)}
              />
            </div>
          </details>
        ) : null}
      </section>
    );
  }
  if (block.type === "text" || block.type === "input_text") {
    return <CodeBlock value={block.text || ""} className="natural-text" />;
  }
  if (block.type === "tool_use") {
    return (
      <section className="tool-block">
        <div className="block-title-row">
          <strong>{block.name || "tool_use"}</strong>
          <span className="mono">{block.id}</span>
        </div>
        <CodeBlock value={block.input ?? {}} />
      </section>
    );
  }
  if (block.type === "tool_result") {
    return (
      <section className="tool-result-block">
        <div className="block-title-row">
          <strong>Tool result</strong>
          <span className="mono">{block.tool_use_id}</span>
        </div>
        <ContentBlock block={block.content ?? ""} path={`${path}/content`} />
      </section>
    );
  }
  return <CodeBlock value={block} />;
}

function eventSummary(event: any, index: number): string {
  const role = event?.message?.role || event?.type || "event";
  const subtype = event?.subtype ? ` / ${event.subtype}` : "";
  const blocks = Array.isArray(event?.message?.content) ? event.message.content : [];
  const tools = blocks.filter((item: any) => item?.type === "tool_use").map((item: any) => item.name);
  return `${String(index + 1).padStart(3, "0")}  ${role}${subtype}${tools.length ? ` / ${tools.join(", ")}` : ""}`;
}

function EventView({ event, index, expandAll }: { event: any; index: number; expandAll: boolean }) {
  const message = event?.message;
  const content = message?.content;
  const important = event?.type === "result" || event?.type === "assistant";
  return (
    <details className={`event-row event-${event?.type || "unknown"}`} open={expandAll || important}>
      <summary>
        <span>{eventSummary(event, index)}</span>
        <span>{event?.timestamp || ""}</span>
      </summary>
      <div className="event-body">
        {content !== undefined ? (
          <ContentBlock block={content} path={`/trajectory/${index}/message/content`} />
        ) : event?.result ? (
          <CodeBlock value={event.result} className="natural-text" />
        ) : (
          <CodeBlock value={event} />
        )}
        {message?.usage ? (
          <details className="minor-disclosure">
            <summary>Usage</summary>
            <CodeBlock value={message.usage} />
          </details>
        ) : null}
      </div>
    </details>
  );
}

function TrajectoryTab({ row }: { row: FullRow }) {
  const [expandAll, setExpandAll] = useState(false);
  const events = parseJSON<any[]>(row.trajectory, []);
  return (
    <div className="trace-tab">
      <div className="tab-toolbar">
        <span>{formatNumber(events.length)} 个 Agent events</span>
        <IconButton
          label={expandAll ? "全部收起" : "全部展开"}
          icon={expandAll ? <ArrowCollapseAll20Regular /> : <ArrowExpandAll20Regular />}
          onClick={() => setExpandAll((value) => !value)}
        />
      </div>
      <div className="event-list">
        {events.map((event, index) => (
          <EventView key={event.uuid || `${event.type}-${index}`} event={event} index={index} expandAll={expandAll} />
        ))}
      </div>
    </div>
  );
}

function LLMMessage({ message, path }: { message: any; path: string }) {
  return (
    <section className={`llm-message role-${message?.role || "unknown"}`}>
      <div className="message-role">{message?.role || "message"}</div>
      <ContentBlock block={message?.content ?? message} path={path} />
    </section>
  );
}

function LLMTraceTab({ row }: { row: FullRow }) {
  const trace = parseJSON<any[]>(row.llm_trace, []);
  const [selectedIndex, setSelectedIndex] = useState(0);
  useEffect(() => setSelectedIndex(0), [row.trajectory_id]);
  const selected = trace[selectedIndex] || {};
  const request = selected.request || {};
  const response = selected.response || {};
  return (
    <div className="llm-layout">
      <aside className="request-index" aria-label="LLM 请求列表">
        {trace.map((item, index) => {
          const signed = (item.response?.content || []).some((block: any) => block?.signature);
          return (
            <button
              type="button"
              key={item.request_id || index}
              className={selectedIndex === index ? "request-button is-active" : "request-button"}
              onClick={() => setSelectedIndex(index)}
            >
              <span>#{index + 1}</span>
              <span>{item.response?.model || item.request?.model || "unknown"}</span>
              <span className={signed ? "signed-marker" : ""}>{item.raw_response_status_code || "-"}</span>
            </button>
          );
        })}
      </aside>
      <div className="request-detail">
        <header className="request-header">
          <div>
            <h3>Request #{selectedIndex + 1}</h3>
            <p className="mono">{selected.request_id}</p>
          </div>
          <div className="request-meta">
            <span>{request.model || "-"}</span>
            <span>HTTP {selected.raw_response_status_code || "-"}</span>
            <span>{formatDuration(selected.raw_response_duration_ms)}</span>
          </div>
        </header>
        {request.system ? (
          <details className="request-section">
            <summary>System prompt</summary>
            <ContentBlock block={request.system} path={`/llm/${selectedIndex}/request/system`} />
          </details>
        ) : null}
        <section className="request-section is-open">
          <h4>Messages ({formatNumber(request.messages?.length || 0)})</h4>
          <div className="message-list">
            {(request.messages || []).map((message: any, index: number) => (
              <LLMMessage
                key={`${message.role}-${index}`}
                message={message}
                path={`/llm/${selectedIndex}/request/messages/${index}`}
              />
            ))}
          </div>
        </section>
        {request.tools ? (
          <details className="request-section">
            <summary>Tools ({request.tools.length})</summary>
            <CodeBlock value={request.tools} />
          </details>
        ) : null}
        <section className="response-section">
          <div className="response-heading">
            <h4>Response</h4>
            <span>{response.stop_reason || "-"}</span>
          </div>
          <ContentBlock block={response.content || response} path={`/llm/${selectedIndex}/response`} />
          {response.usage ? (
            <details className="minor-disclosure">
              <summary>Response usage</summary>
              <CodeBlock value={response.usage} />
            </details>
          ) : null}
        </section>
      </div>
    </div>
  );
}

function ThinkingTab({ row }: { row: FullRow }) {
  const decode = parseJSON<any>(row.thinking_decode, { occurrences: [] });
  const [filter, setFilter] = useState("all");
  const occurrences = (decode.occurrences || []).filter(
    (item: any) => filter === "all" || item.decode_status === filter,
  );
  return (
    <div className="thinking-tab">
      <div className="tab-toolbar thinking-toolbar">
        <div className="segmented-control" aria-label="Thinking 状态筛选">
          {[
            ["all", "全部"],
            ["success", "成功"],
            ["failed", "失败"],
            ["not_attempted", "未请求"],
          ].map(([value, text]) => (
            <button
              type="button"
              key={value}
              className={filter === value ? "is-active" : ""}
              onClick={() => setFilter(value)}
            >
              {text}
            </button>
          ))}
        </div>
        <span>{formatNumber(occurrences.length)} 条</span>
      </div>
      <div className="thinking-list">
        {occurrences.map((item: any) => (
          <article className="thinking-occurrence" key={item.occurrence_index}>
            <header>
              <div>
                <strong>#{item.occurrence_index}</strong>
                <StatusText value={item.decode_status} />
              </div>
              <div className="occurrence-location">
                <span>{item.source_stream}</span>
                <span>request {item.request_index ?? "-"}</span>
                <span>event {item.message_index ?? "未进入 Agent stream"}</span>
              </div>
            </header>
            {item.decoded_text ? (
              <CodeBlock value={item.decoded_text} className="natural-text decoded-text" />
            ) : (
              <div className="decode-empty">
                <strong>{item.error_code || "没有解码明文"}</strong>
                <span>HTTP {item.http_status || "-"}</span>
              </div>
            )}
            <details className="signature-disclosure">
              <summary>
                Signature {item.signature_length} chars / {item.signature_sha256}
              </summary>
              <div className="copy-row">
                <CodeBlock value={item.signature || ""} />
                <IconButton
                  label="复制 signature"
                  icon={<Copy20Regular />}
                  onClick={() => copyText(item.signature || "")}
                />
              </div>
            </details>
            <dl className="inline-facts">
              <div><dt>模型</dt><dd>{item.response_model || "-"}</dd></div>
              <div><dt>解码器</dt><dd>{item.decoded_by || item.decoder_mode || "-"}</dd></div>
              <div><dt>尝试</dt><dd>{item.decode_attempt_count ?? "-"}</dd></div>
              <div><dt>Cache</dt><dd>{item.cache_hit === true ? "命中" : item.cache_hit === false ? "未命中" : "-"}</dd></div>
            </dl>
          </article>
        ))}
      </div>
    </div>
  );
}

function RewardEvidence({ evidence, index }: { evidence: any; index: number }) {
  const screenshot = evidence?.screenshot_path;
  if (screenshot) {
    return (
      <figure className="reward-evidence-image">
        <img
          src={mediaURL(screenshot)}
          alt={`Reward 视觉证据 ${index + 1}`}
          loading="lazy"
        />
        <figcaption>
          <span>Slide {evidence.slide_number ?? "-"}</span>
          <span>{evidence.observation || evidence.quote || "视觉证据"}</span>
        </figcaption>
      </figure>
    );
  }
  const primary = evidence?.quote ?? evidence?.observation ?? evidence?.value;
  return (
    <div className="reward-evidence-text">
      <div>
        <strong>{evidence?.type || "evidence"}</strong>
        <span>
          {evidence?.name ||
            (evidence?.slide_number ? `Slide ${evidence.slide_number}` : `#${index + 1}`)}
        </span>
      </div>
      {primary !== undefined ? (
        typeof primary === "string" ? (
          <p>{primary}</p>
        ) : (
          <CodeBlock value={primary} />
        )
      ) : (
        <CodeBlock value={evidence} />
      )}
    </div>
  );
}

function RewardTab({ row }: { row: FullRow }) {
  const detail = parseJSON<any>(row.reward_detail, null);
  const judgeModels = parseJSON<Record<string, unknown>>(row.reward_judge_models, {});
  const [method, setMethod] = useState("all");
  const [itemStatus, setItemStatus] = useState("all");
  useEffect(() => {
    setMethod("all");
    setItemStatus("all");
  }, [row.trajectory_id]);
  if (!detail) {
    return (
      <div className="reward-tab">
        <section className="detail-section">
          <h3>Reward</h3>
          <FactGrid
            items={[
              ["状态", <StatusText value={row.reward_status} />],
              ["明细状态", <StatusText value={row.reward_detail_status} />],
              ["Reward 关联", <span className="mono wrap-anywhere">{row.reward_record_id || "-"}</span>],
              ["评测批次", row.reward_wide_batch_id || "-"],
            ]}
          />
        </section>
      </div>
    );
  }
  const allItems = Array.isArray(detail.item_results) ? detail.item_results : [];
  const statuses: string[] = [
    ...new Set<string>(allItems.map((item: any) => String(item.status || "unknown"))),
  ];
  const items = allItems.filter(
    (item: any) =>
      (method === "all" || item.evaluation_method === method) &&
      (itemStatus === "all" || item.status === itemStatus),
  );
  const confidence = detail.evaluation_confidence || {};
  const ordinal = detail.ordinal_quality || {};
  const domains = detail.domain_scores || {};
  const summary = detail.summary || {};
  return (
    <div className="reward-tab">
      <section className="detail-section reward-overview">
        <h3>Reward 总览</h3>
        <FactGrid
          items={[
            ["Reward", formatReward(detail.reward_score)],
            ["质量层级", detail.quality_band],
            ["Ordinal", `${ordinal.score_1_to_5 ?? "-"} / ${ordinal.label || "-"}`],
            ["评测有效", confidence.evaluation_valid === true ? "是" : "否"],
            ["训练可用", confidence.training_usable === true ? "是" : "否"],
            ["评测置信度", formatReward(confidence.score)],
            ["覆盖率", formatReward(confidence.coverage)],
            ["Rubric 来源", row.reward_rubric_source],
            ["逐项结果", formatNumber(allItems.length)],
            ["通过 / 失败", `${summary.pass ?? 0} / ${summary.fail ?? 0}`],
            ["LLM judge", String(judgeModels.llm_text || "-")],
            ["VLM judge", String(judgeModels.vlm_visual || "-")],
          ]}
        />
        <div className="reward-domain-grid">
          {Object.entries(domains).map(([name, value]) => (
            <div key={name}>
              <span>{name}</span>
              <strong>{formatReward(value)}</strong>
            </div>
          ))}
        </div>
        <dl className="reward-lineage">
          <div><dt>Reward record</dt><dd>{row.reward_record_id || "-"}</dd></div>
          <div><dt>Source SHA256</dt><dd>{row.reward_detail_source_sha256 || "-"}</dd></div>
          <div><dt>Inline SHA256</dt><dd>{row.reward_detail_sha256 || "-"}</dd></div>
          <div><dt>一致性</dt><dd><StatusText value={row.reward_detail_integrity_status} /></dd></div>
        </dl>
      </section>
      <div className="tab-toolbar reward-toolbar">
        <div className="segmented-control" aria-label="Reward 评分方式筛选">
          {[
            ["all", "全部"],
            ["script", "Script"],
            ["llm_text", "LLM"],
            ["vlm_visual", "VLM"],
          ].map(([value, text]) => (
            <button
              type="button"
              key={value}
              className={method === value ? "is-active" : ""}
              onClick={() => setMethod(value)}
            >
              {text}
            </button>
          ))}
        </div>
        <Select
          aria-label="Reward 结果状态筛选"
          value={itemStatus}
          onChange={(_, data) => setItemStatus(data.value)}
        >
          <option value="all">全部结果</option>
          {statuses.map((value) => <option key={value} value={value}>{label(value)}</option>)}
        </Select>
        <span>{formatNumber(items.length)} / {formatNumber(allItems.length)} 项</span>
      </div>
      <div className="reward-item-list">
        {items.map((item: any, index: number) => {
          const evidence = Array.isArray(item.evidence) ? item.evidence : [];
          return (
            <details
              className={`reward-item reward-item-${statusTone(item.status)}`}
              key={`${item.item_id || "item"}-${index}`}
              open={item.status === "fail" || item.status === "cannot_determine"}
            >
              <summary>
                <span className="reward-item-id">{item.item_id || `#${index + 1}`}</span>
                <span>{item.domain || "-"}</span>
                <span>{item.evaluation_method || "-"}</span>
                <StatusText value={item.status} />
                <strong>{formatReward(item.score)}</strong>
              </summary>
              <div className="reward-item-body">
                <p>{item.reason || "-"}</p>
                <FactGrid
                  items={[
                    ["Item ID", <span className="mono">{item.item_id || "-"}</span>],
                    ["Domain", item.domain || "-"],
                    ["Method", item.evaluation_method || "-"],
                    ["Score", formatReward(item.score)],
                    ["Script check", item.script_check || "-"],
                    ["Cap if fail", item.cap_if_fail ?? "-"],
                    ["排除质量计分", item.quality_scoring_excluded === true ? "是" : "否"],
                    ["证据数", formatNumber(evidence.length)],
                  ]}
                />
                {evidence.length ? (
                  <div className="reward-evidence-list">
                    {evidence.map((entry: any, evidenceIndex: number) => (
                      <RewardEvidence
                        key={`${item.item_id || index}-evidence-${evidenceIndex}`}
                        evidence={entry}
                        index={evidenceIndex}
                      />
                    ))}
                  </div>
                ) : null}
                {item.cap_reason || item.quality_scoring_exclusion_reason ? (
                  <div className="reward-item-notes">
                    {item.cap_reason ? <p><strong>Cap</strong>{item.cap_reason}</p> : null}
                    {item.quality_scoring_exclusion_reason ? (
                      <p><strong>Exclusion</strong>{item.quality_scoring_exclusion_reason}</p>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </details>
          );
        })}
      </div>
      <details className="detail-section reward-raw">
        <summary>完整 Reward JSON</summary>
        <CodeBlock value={detail} />
      </details>
    </div>
  );
}

function FactGrid({ items }: { items: Array<[string, React.ReactNode]> }) {
  return (
    <dl className="fact-grid">
      {items.map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{value ?? "-"}</dd>
        </div>
      ))}
    </dl>
  );
}

function SummaryTab({ row }: { row: FullRow }) {
  const slides = parseJSON<any[]>(row.rendered_slides, []);
  const transformations = parseJSON<any[]>(row.trajectory_transformations, []);
  return (
    <div className="summary-tab">
      <section className="detail-section">
        <h3>任务</h3>
        <p className="query-full">{row.query}</p>
        <FactGrid
          items={[
            ["Run", <span className="mono">{row.run_id}</span>],
            ["Task", <span className="mono">{row.task_id}</span>],
            ["Attempt", row.attempt_index],
            ["状态", <StatusText value={row.status} />],
            ["任务类型", row.task_type],
            ["来源 batch", row.source_batch_id],
            ["模型", row.rollout_model_observed],
            ["Agent", `${row.agent_name || "-"} ${row.agent_version || ""}`],
          ]}
        />
      </section>
      <section className="detail-section">
        <h3>轨迹完整性</h3>
        <FactGrid
          items={[
            ["Agent events", formatNumber(row.trajectory_event_count)],
            ["LLM requests", formatNumber(row.llm_request_count)],
            ["Tool calls", formatNumber(row.tool_call_count)],
            ["Tool results", formatNumber(row.tool_result_count)],
            ["图片", formatNumber(row.trajectory_image_count)],
            ["Signature", `${formatNumber(row.decoded_thinking_count)} / ${formatNumber(row.signature_occurrence_count)}`],
            ["解码状态", <StatusText value={row.thinking_decode_status} />],
            ["安全扫描", <StatusText value={row.secret_scan_status} />],
            ["Agent 轨迹大小", formatBytes(row.trajectory_size_bytes)],
            ["LLM trace 大小", formatBytes(row.llm_trace_size_bytes)],
            ["行校验", <StatusText value={row.integrity_status} />],
            ["Record SHA", <span className="mono wrap-anywhere">{row.record_sha256}</span>],
          ]}
        />
        <div className="transformation-list">
          {transformations.map((item) => (
            <span key={item.type}>{item.type} / {item.version}</span>
          ))}
        </div>
      </section>
      <section className="detail-section">
        <h3>Reward 与成本</h3>
        <FactGrid
          items={[
            ["Reward 状态", <StatusText value={row.reward_status} />],
            ["Reward", formatReward(row.reward_total)],
            ["质量层级", row.quality_tier],
            ["Reward 关联", <span className="mono wrap-anywhere">{row.reward_record_id || "-"}</span>],
            ["Reward batch", row.reward_wide_batch_id],
            ["成本", formatCost(row.cost_usd)],
            ["输入 tokens", formatNumber(row.input_tokens)],
            ["输出 tokens", formatNumber(row.output_tokens)],
            ["Cache read", formatNumber(row.cache_read_input_tokens)],
            ["Cache create", formatNumber(row.cache_creation_input_tokens)],
            ["耗时", formatDuration(row.duration_ms)],
            ["成本审计", row.cost_audit_status],
          ]}
        />
      </section>
      <section className="detail-section">
        <h3>PPT 产物 ({slides.length} 页)</h3>
        {slides.length ? (
          <div className="slide-grid">
            {slides.map((slide) => (
              <figure key={slide.index}>
                <img src={mediaURL(slide.path)} alt={`输出 PPT 第 ${slide.index} 页`} loading="lazy" />
                <figcaption>Slide {slide.index}</figcaption>
              </figure>
            ))}
          </div>
        ) : (
          <div className="empty-panel">该 attempt 没有可展示的渲染页。</div>
        )}
      </section>
    </div>
  );
}

function FieldsTab({ row, fields, onTab }: { row: FullRow; fields: FieldDefinition[]; onTab: (tab: DetailTab) => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const names = [...fields.map((field) => field.name), ...Object.keys(row).filter((key) => !fields.some((field) => field.name === key))];
  const definitions = new Map(fields.map((field) => [field.name, field]));
  const toggle = (name: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };
  return (
    <div className="field-table-wrap">
      <table className="field-table">
        <thead><tr><th>字段</th><th>语义类型</th><th>值</th></tr></thead>
        <tbody>
          {names.map((name) => {
            const value = row[name];
            const definition = definitions.get(name);
            const linkedTab =
              name === "trajectory" ? "trajectory" :
              name === "llm_trace" ? "llm" :
              name === "reward_detail" ? "reward" : null;
            const text = value === null || value === undefined ? "NULL" : String(value);
            const canExpand = JSON_FIELD_NAMES.has(name) || text.length > 480;
            let parsed: unknown = value;
            if (JSON_FIELD_NAMES.has(name)) parsed = parseJSON(value, value);
            return (
              <tr key={name}>
                <td className="mono field-name">{name}</td>
                <td>{definition?.semantic_type || typeof value}</td>
                <td>
                  {linkedTab ? (
                    <button type="button" className="inline-command" onClick={() => onTab(linkedTab)}>
                      在对应页查看完整内容 ({formatBytes(
                        name === "trajectory" ? row.trajectory_size_bytes :
                        name === "llm_trace" ? row.llm_trace_size_bytes :
                        row.reward_detail_size_bytes
                      )})
                    </button>
                  ) : canExpand ? (
                    <div>
                      <button type="button" className="inline-command" onClick={() => toggle(name)}>
                        {expanded.has(name) ? "收起完整值" : `展开完整值 (${formatNumber(text.length)} chars)`}
                      </button>
                      {expanded.has(name) ? <CodeBlock value={parsed} /> : <div className="value-preview">{text.slice(0, 480)}</div>}
                    </div>
                  ) : (
                    <span className={text === "NULL" ? "empty-value" : "wrap-anywhere"}>{text}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DetailDrawer({
  preview,
  row,
  loading,
  error,
  fields,
  onClose,
  onMove,
}: {
  preview: PreviewRow;
  row: FullRow | null;
  loading: boolean;
  error: string | null;
  fields: FieldDefinition[];
  onClose: () => void;
  onMove: (delta: number) => void;
}) {
  const [tab, setTab] = useState<DetailTab>("summary");
  useEffect(() => setTab("summary"), [preview.trajectory_id]);
  const download = () => {
    if (!row) return;
    const blob = new Blob([JSON.stringify(row, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${row.trajectory_id}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="drawer-shell" role="dialog" aria-modal="true" aria-label="轨迹详情">
      <button className="drawer-scrim" type="button" aria-label="关闭详情" onClick={onClose} />
      <aside className="drawer">
        <header className="drawer-header">
          <div className="drawer-title">
            <div className="drawer-title-row">
              <StatusText value={preview.status} />
              <span>Attempt {preview.attempt_index}</span>
              <span>{preview.task_type}</span>
            </div>
            <h2>{preview.task_id}</h2>
            <p>{preview.query}</p>
          </div>
          <div className="drawer-actions">
            <IconButton label="上一条" icon={<ChevronLeft20Regular />} onClick={() => onMove(-1)} />
            <IconButton label="下一条" icon={<ChevronRight20Regular />} onClick={() => onMove(1)} />
            <IconButton label="下载原始行" icon={<ArrowDownload20Regular />} onClick={download} disabled={!row} />
            <IconButton label="关闭" icon={<Dismiss20Regular />} onClick={onClose} />
          </div>
        </header>
        <TabList
          className="detail-tabs"
          selectedValue={tab}
          onTabSelect={(_, data) => setTab(data.value as DetailTab)}
        >
          <Tab value="summary">概览</Tab>
          <Tab value="trajectory">Agent 轨迹</Tab>
          <Tab value="llm">LLM trace</Tab>
          <Tab value="thinking">Thinking</Tab>
          <Tab value="reward">Reward 明细</Tab>
          <Tab value="fields">{fields.length} 字段</Tab>
        </TabList>
        <div className="drawer-content">
          {loading ? (
            <div className="detail-loading"><Spinner size="small" label="正在读取完整行" /></div>
          ) : error ? (
            <div className="error-panel"><strong>读取失败</strong><span>{error}</span></div>
          ) : row ? (
            tab === "summary" ? <SummaryTab row={row} /> :
            tab === "trajectory" ? <TrajectoryTab row={row} /> :
            tab === "llm" ? <LLMTraceTab row={row} /> :
            tab === "thinking" ? <ThinkingTab row={row} /> :
            tab === "reward" ? <RewardTab row={row} /> :
            <FieldsTab row={row} fields={fields} onTab={setTab} />
          ) : null}
        </div>
      </aside>
    </div>
  );
}

type SortKey = keyof PreviewRow | "none";

const COLUMNS: Array<{
  key: SortKey;
  label: string;
  className?: string;
  render: (row: PreviewRow) => React.ReactNode;
}> = [
  { key: "task_id", label: "Task / query", className: "col-task", render: (row) => <><strong>{row.task_id}</strong><span>{row.query}</span></> },
  { key: "attempt_index", label: "Attempt", render: (row) => row.attempt_index },
  { key: "status", label: "状态", render: (row) => <StatusText value={row.status} /> },
  { key: "task_type", label: "任务类型", render: (row) => row.task_type },
  { key: "rollout_model", label: "模型", render: (row) => row.rollout_model || "-" },
  { key: "trajectory_event_count", label: "Events", render: (row) => formatNumber(row.trajectory_event_count) },
  { key: "llm_request_count", label: "LLM", render: (row) => formatNumber(row.llm_request_count) },
  { key: "tool_call_count", label: "Tools", render: (row) => formatNumber(row.tool_call_count) },
  { key: "image_count", label: "Images", render: (row) => formatNumber(row.image_count) },
  { key: "signature_count", label: "Thinking", render: (row) => <span>{row.decoded_thinking_count}/{row.signature_count}</span> },
  { key: "reward_total", label: "Reward", render: (row) => formatReward(row.reward_total) },
  { key: "cost_usd", label: "成本", render: (row) => formatCost(row.cost_usd) },
  { key: "duration_ms", label: "耗时", render: (row) => formatDuration(row.duration_ms) },
  { key: "integrity_status", label: "校验", render: (row) => <StatusText value={row.integrity_status} /> },
];

function LoadingTable() {
  return (
    <div className="loading-table">
      {Array.from({ length: 12 }, (_, index) => <div key={index} className="skeleton-row" />)}
    </div>
  );
}

function App() {
  const [meta, setMeta] = useState<MetaPayload | null>(null);
  const [rows, setRows] = useState<PreviewRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [taskType, setTaskType] = useState("all");
  const [thinking, setThinking] = useState("all");
  const [multimodal, setMultimodal] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("none");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selected, setSelected] = useState<PreviewRow | null>(null);
  const [detail, setDetail] = useState<FullRow | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const detailCache = useRef(new Map<string, FullRow>());

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch("/api/meta", { signal: controller.signal }).then((response) => {
        if (!response.ok) throw new Error(`meta HTTP ${response.status}`);
        return response.json();
      }),
      fetch("/api/rows", { signal: controller.signal }).then((response) => {
        if (!response.ok) throw new Error(`rows HTTP ${response.status}`);
        return response.json();
      }),
    ])
      .then(([metaPayload, rowPayload]) => {
        setMeta(metaPayload);
        setRows(rowPayload);
        const requested = new URLSearchParams(window.location.search).get("row");
        if (requested) {
          const initial = (rowPayload as PreviewRow[]).find((row) => row.trajectory_id === requested);
          if (initial) setSelected(initial);
        }
      })
      .catch((reason) => {
        if (reason.name !== "AbortError") setError(String(reason));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selected) {
      if (loading) return;
      setDetail(null);
      setDetailError(null);
      const url = new URL(window.location.href);
      url.searchParams.delete("row");
      window.history.replaceState({}, "", url);
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set("row", selected.trajectory_id);
    window.history.replaceState({}, "", url);
    const cached = detailCache.current.get(selected.trajectory_id);
    if (cached) {
      setDetail(cached);
      setDetailLoading(false);
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    fetch(`/api/row/${encodeURIComponent(selected.trajectory_id)}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`row HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        detailCache.current.set(selected.trajectory_id, payload);
        if (detailCache.current.size > 6) {
          const oldest = detailCache.current.keys().next().value;
          if (oldest) detailCache.current.delete(oldest);
        }
        setDetail(payload);
      })
      .catch((reason) => {
        if (reason.name !== "AbortError") setDetailError(String(reason));
      })
      .finally(() => setDetailLoading(false));
    return () => controller.abort();
  }, [selected, loading]);

  const taskTypes = useMemo(() => [...new Set(rows.map((row) => row.task_type))].sort(), [rows]);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    const result = rows.filter((row) => {
      if (status !== "all" && row.status !== status) return false;
      if (taskType !== "all" && row.task_type !== taskType) return false;
      if (thinking !== "all" && row.thinking_decode_status !== thinking) return false;
      if (multimodal === "yes" && !row.is_multimodal) return false;
      if (multimodal === "no" && row.is_multimodal) return false;
      if (!normalized) return true;
      return `${row.task_id}\n${row.query}\n${row.task_type}\n${row.rollout_model || ""}`.toLowerCase().includes(normalized);
    });
    if (sortKey === "none") return result;
    return [...result].sort((a, b) => {
      const left = a[sortKey as keyof PreviewRow];
      const right = b[sortKey as keyof PreviewRow];
      const comparison = typeof left === "number" && typeof right === "number"
        ? left - right
        : String(left ?? "").localeCompare(String(right ?? ""), "zh-CN");
      return sortDirection === "asc" ? comparison : -comparison;
    });
  }, [rows, query, status, taskType, thinking, multimodal, sortKey, sortDirection]);

  useEffect(() => setPage(1), [query, status, taskType, thinking, multimodal, pageSize]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const visibleRows = filtered.slice((safePage - 1) * pageSize, safePage * pageSize);

  const resetFilters = () => {
    setQuery("");
    setStatus("all");
    setTaskType("all");
    setThinking("all");
    setMultimodal("all");
    setSortKey("none");
  };

  const openRelative = (delta: number) => {
    if (!selected || !filtered.length) return;
    const index = filtered.findIndex((row) => row.trajectory_id === selected.trajectory_id);
    const next = (index + delta + filtered.length) % filtered.length;
    setSelected(filtered[next]);
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
      if (event.key === "Escape" && selected) setSelected(null);
      if (typing) return;
      if (event.key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
      } else if (event.key.toLowerCase() === "j" && selected) {
        openRelative(1);
      } else if (event.key.toLowerCase() === "k" && selected) {
        openRelative(-1);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  const sort = (key: SortKey) => {
    if (sortKey === key) setSortDirection((value) => (value === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDirection("asc");
    }
  };

  const actual = meta?.validation.actual || {};
  const checks = meta?.validation.checks || {};
  return (
    <FluentProvider theme={webLightTheme} className="app-provider">
      <main className="app-shell">
        <header className="topbar">
          <div>
            <h1>PPT 轨迹宽表 Canary</h1>
            <p>
              <span className="mono">{meta?.batch.batch_id as string || "loading"}</span>
              <span>{meta?.schema.field_count || "-"} fields</span>
              <span>未注册正式 Warehouse</span>
            </p>
          </div>
          <div className="topbar-validation">
            <StatusText value={Object.values(checks).every(Boolean) ? "pass" : "failed"} />
            <span>{Object.values(checks).filter(Boolean).length}/{Object.keys(checks).length} checks</span>
          </div>
        </header>

        <section className="metrics-band" aria-label="Canary 摘要">
          {[
            ["Attempts", actual.row_count],
            ["Tasks", actual.task_count],
            ["LLM responses", actual.llm_request_count],
            ["Signatures", actual.signature_occurrence_count],
            ["Decoded", actual.decoded_signature_count],
            ["Multimodal", actual.multimodal_row_count],
            ["Image refs", actual.trajectory_image_occurrence_count],
            ["Reward items", actual.reward_detail_item_count],
          ].map(([name, value]) => (
            <div key={name as string}>
              <span>{name}</span>
              <strong>{formatNumber(value)}</strong>
            </div>
          ))}
        </section>

        <section className="controls-band" aria-label="宽表筛选">
          <Input
            ref={searchRef}
            className="search-input"
            size="medium"
            contentBefore={<Search20Regular />}
            placeholder="搜索 task、query、模型"
            aria-label="搜索轨迹"
            value={query}
            onChange={(_, data) => setQuery(data.value)}
          />
          <Select aria-label="状态筛选" value={status} onChange={(_, data) => setStatus(data.value)}>
            <option value="all">全部状态</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
          </Select>
          <Select aria-label="任务类型筛选" value={taskType} onChange={(_, data) => setTaskType(data.value)}>
            <option value="all">全部任务类型</option>
            {taskTypes.map((value) => <option key={value} value={value}>{value}</option>)}
          </Select>
          <Select aria-label="Thinking 筛选" value={thinking} onChange={(_, data) => setThinking(data.value)}>
            <option value="all">全部 Thinking</option>
            <option value="complete">完整</option>
            <option value="partial">部分</option>
            <option value="unresolved">未解码</option>
          </Select>
          <Select aria-label="多模态筛选" value={multimodal} onChange={(_, data) => setMultimodal(data.value)}>
            <option value="all">全部模态</option>
            <option value="yes">多模态</option>
            <option value="no">非多模态</option>
          </Select>
          <IconButton label="重置筛选" icon={<ArrowReset20Regular />} onClick={resetFilters} />
          <span className="filter-count">{formatNumber(filtered.length)} rows</span>
        </section>

        <section className="table-section">
          {loading ? <LoadingTable /> : error ? (
            <div className="error-panel"><strong>数据加载失败</strong><span>{error}</span></div>
          ) : filtered.length === 0 ? (
            <div className="empty-panel">没有符合当前筛选条件的轨迹。</div>
          ) : (
            <div className="table-scroll">
              <table className="wide-table">
                <thead>
                  <tr>
                    <th className="inspect-column"><Eye20Regular aria-hidden /></th>
                    {COLUMNS.map((column) => (
                      <th key={column.key} className={column.className || ""}>
                        <button type="button" onClick={() => sort(column.key)}>
                          {column.label}
                          {sortKey === column.key ? <span>{sortDirection === "asc" ? "↑" : "↓"}</span> : null}
                        </button>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((row) => (
                    <tr
                      key={row.trajectory_id}
                      className={selected?.trajectory_id === row.trajectory_id ? "is-selected" : ""}
                      onClick={() => setSelected(row)}
                    >
                      <td className="inspect-column"><Eye20Regular aria-hidden /></td>
                      {COLUMNS.map((column) => (
                        <td key={column.key} className={column.className || ""}>{column.render(row)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <footer className="pagination-bar">
            <span>
              {filtered.length ? `${(safePage - 1) * pageSize + 1}-${Math.min(safePage * pageSize, filtered.length)}` : "0"} / {formatNumber(filtered.length)}
            </span>
            <Select aria-label="每页行数" value={String(pageSize)} onChange={(_, data) => setPageSize(Number(data.value))}>
              <option value="50">50 / 页</option>
              <option value="100">100 / 页</option>
              <option value="508">全部</option>
            </Select>
            <IconButton label="上一页" icon={<ChevronLeft20Regular />} onClick={() => setPage(Math.max(1, safePage - 1))} disabled={safePage <= 1} />
            <span>{safePage} / {pageCount}</span>
            <IconButton label="下一页" icon={<ChevronRight20Regular />} onClick={() => setPage(Math.min(pageCount, safePage + 1))} disabled={safePage >= pageCount} />
          </footer>
        </section>
      </main>
      {selected ? (
        <DetailDrawer
          preview={selected}
          row={detail}
          loading={detailLoading}
          error={detailError}
          fields={meta?.schema.fields || []}
          onClose={() => setSelected(null)}
          onMove={openRelative}
        />
      ) : null}
    </FluentProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
