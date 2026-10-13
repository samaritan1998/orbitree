# PPT 轨迹宽表 Schema（原始轨迹 + Thinking Signature 解码）

状态：公开版 Schema 设计示例，不包含生产验收结果。
建议物理表名：`ppt_trajectory`  
建议展示名：`PPT轨迹宽表`

## 1. 目标

PPT 轨迹宽表用于保存刷 PPT 数据时产生的原始 Agent 轨迹，而不是训练格式转换后的 `messages`、XML tools 或 qianfan-coder 数据。

一行对应一次完整 rollout attempt，成功和失败 attempt 都保留。对于带 thinking signature 的轨迹，必须同时保存原 signature、签名出现位置、解码状态和解码明文。

核心原则：

1. `trajectory` 保存原生 Agent event stream，消息、工具调用和事件顺序不转换；只允许确定性的凭据脱敏、图片二进制外置和 thinking 明文回填。
2. `llm_trace` 保存原生 LLM request/response 序列，用于 signature、usage 和模型响应追溯。
3. thinking 轨迹必须在产生该 signature 的 `llm_trace` response block 中同时保留原 signature 和解码明文；若该 block 被 Agent stream 暴露，`trajectory` 对应 block 也必须回填。`thinking_decode` 另外保存 occurrence 级审计信息。
4. 训练数据转换是下游行为，转换产物只通过 `source_trajectory_id` 回链本表。
5. 不保存 Authorization、API key、cookie、代理账号或 API key hash。这里的“原始轨迹”指原生协议结构，不表示允许保存未脱敏字节。

## 2. 与现有秒哒宽表的关系

现有秒哒 `apps` 表：

- 展示名：`秒哒宽表`
- 去重键：`(app_id, version_id)`
- 已有可复用字段：`batch_id`、`created_at`、`status`、`query`、`trajectory`、`trajectory_path`、`model`、`rollout_model`、`reward_scores` 等。
- `trajectory` 当前作为详情页轨迹查看来源。

PPT 表不应伪造 `app_id/version_id`。建议使用 PPT 自身稳定键：

```text
unique_key = (run_id, task_id, attempt_index)
```

同时生成全局稳定的 `trajectory_id`，供 Reward、SFT 转换和数据集清单引用。

### 2.1 为什么 PPT 也使用宽表

参考秒哒 Warehouse 的现行实现：

- 每个 batch 的 JSONL/Parquet 可以拥有不同列集。
- `*_all` 对所有 confirmed batch 做按列名并集，缺失字段补 `NULL`，并附加 `batch_id`、`batch_ingested_at`。
- 查询视图把 batch 中实际出现的同名字段统一转成 `VARCHAR`，由查询或 typed view 显式转回数值、布尔和时间。
- 当前视图按稳定业务键去重，保留 `batch_ingested_at` 最新的记录；历史版本仍在 `*_all` 中。

上游数据的列集会持续演进；轨迹表应按列名兼容合并，并明确保留字段的语义类型。

但“宽表”不等于预先堆叠所有可能字段。首版一次性固定稳定核心；只有确实会独立筛选、统计或发生版本变化的属性才新增显式列，单次实验细节保留在结构化 JSON 列中。

## 3. 行粒度与生命周期

一行 = 一个 task 的一次 rollout attempt。

例如同一任务首轮失败、重试成功，应有两行：

```text
(run_20260803, task_x, 1) -> failed
(run_20260803, task_x, 2) -> success, retry_of_trajectory_id=<attempt 1>
```

禁止把多个 attempt 合并后只保留“最佳结果”，否则无法分析失败原因、成本和重试收益。

同一 attempt 后续补齐 signature 解码或 Reward 时，沿用原 `(run_id, task_id, attempt_index)`，以新的 `record_revision` 重新入库。单个 batch 内业务键唯一；跨 batch 允许同一业务键存在多个 revision。`ppt_trajectory` 优先选择最大 revision，`ppt_trajectory_all` 保留每次入库历史。

## 4. 字段定义

下表中的类型是字段的**语义类型**。Warehouse 查询视图为了跨 batch 兼容统一暴露为 `VARCHAR`，查询时再显式转换；生产者和入库校验仍应按语义类型验证。复杂字段应保留结构化语义；缺失字段需显式补 NULL。

类型约定：

- `VARCHAR`：普通字符串。
- `TIMESTAMP`：ISO 8601 UTC 时间。
- `JSON(VARCHAR)`：JSON 序列化字符串；复杂对象统一这样存，避免跨 batch 的 DuckDB nested schema 漂移。
- `P0`：入库必填或条件必填。
- `P1`：强烈建议。
- `P2`：评测/分析扩展。

### 4.1 主键与来源

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `schema_version` | VARCHAR | P0 | 固定版本，如 `ppt_trajectory.v1` |
| `record_revision` | BIGINT | P0 | 同一 attempt 的富化版本，从 1 递增；不代表新的 rollout |
| `trajectory_id` | VARCHAR | P0 | 全局稳定 ID；建议由 `run_id/task_id/attempt_index` 确定性生成 |
| `domain` | VARCHAR | P0 | 固定为 `ppt` |
| `run_id` | VARCHAR | P0 | 一次批量 rollout 的运行 ID |
| `task_id` | VARCHAR | P0 | PPT 原始任务 ID |
| `attempt_index` | BIGINT | P0 | 从 1 开始；重试递增 |
| `attempt_id` | VARCHAR | P1 | 执行系统生成的 attempt/pod/session ID |
| `retry_of_trajectory_id` | VARCHAR | P1 | 非重试为空；重试时指向上一 attempt |
| `source_dataset_name` | VARCHAR | P0 | 原始任务池/评测集名称 |
| `source_batch_id` | VARCHAR | P1 | 来源 batch；未知时明确为空，不从路径猜测 |
| `source_record_id` | VARCHAR | P1 | 来源记录 ID |
| `source_line_no` | BIGINT | P1 | 来源 JSONL 行号 |
| `source_manifest_path` | VARCHAR | P1 | 来源 manifest 路径 |
| `source_manifest_sha256` | VARCHAR | P1 | 来源 manifest 哈希 |
| `created_by` | VARCHAR | P1 | 运行/入库负责人 |
| `batch_id` | VARCHAR | P0 | Warehouse 入库批次 ID，由 Warehouse 自动附加 |
| `batch_ingested_at` | TIMESTAMP | P0 | Warehouse 入库时间，由 Warehouse 自动附加 |

### 4.2 任务输入

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `query` | VARCHAR | P0 | 用户原始 PPT 请求，不做改写 |
| `query_sha256` | VARCHAR | P0 | 原始 query 哈希 |
| `task_type` | VARCHAR | P0 | 如 `open_create`、`document_grounded_create`、`style_reference_create`、`open_edit` |
| `operation_type` | VARCHAR | P0 | `create` / `edit` / `continue` |
| `task_axes` | JSON(VARCHAR) | P1 | 内容模式、交互模式、难度、编辑轮次等结构化轴 |
| `reference_files` | JSON(VARCHAR) | P0 条件 | 参考文档、参考 PPT、图片等；无参考时为 `[]` |
| `input_assets` | JSON(VARCHAR) | P1 | 每个输入资产的路径、类型、大小、sha256 |
| `is_multimodal` | BOOLEAN | P0 | Agent 是否收到图片/文档视觉输入 |
| `input_image_count` | BIGINT | P1 | 输入图片数量 |
| `task_metadata` | JSON(VARCHAR) | P1 | 其余任务元数据，不能放凭据 |

### 4.3 Rollout 配置

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `rollout_model_requested` | VARCHAR | P0 | 请求模型名，如 `claude-opus-4-7` |
| `rollout_model_observed` | VARCHAR | P0 | 响应实际模型名；必须从响应取值 |
| `provider` | VARCHAR | P1 | 模型提供方/路由类型，不写带凭据 URL |
| `agent_name` | VARCHAR | P0 | Agent/CLI 名称 |
| `agent_version` | VARCHAR | P1 | Agent 或 Claude Code 版本 |
| `runtime_image` | VARCHAR | P1 | 执行镜像或环境版本 |
| `skill_refs` | JSON(VARCHAR) | P0 | `pptx` 等 skill 的名称、版本/commit、sha256 |
| `tool_schema_sha256` | VARCHAR | P1 | 工具声明集合的稳定哈希 |
| `system_prompt_sha256` | VARCHAR | P1 | system prompt 哈希；正文已在原始 request 中时不重复平铺 |
| `thinking_enabled` | BOOLEAN | P0 | 是否请求 thinking |
| `max_output_tokens` | BIGINT | P1 | 单次模型请求上限 |
| `runtime_config` | JSON(VARCHAR) | P1 | 脱敏后的运行参数；不得出现 token/header |
| `pod_name` | VARCHAR | P1 | 执行 Pod 名 |
| `session_id` | VARCHAR | P1 | Agent session ID |

### 4.4 原始轨迹（本表核心）

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `trajectory_format` | VARCHAR | P0 | 如 `claude_stream_json.v1`、`agentos_coding_messages.v1` |
| `trajectory` | JSON(VARCHAR) | P0 | 完整、按顺序保存的原生 Agent event stream；不转 OpenAI/XML/SFT。Agent stream 中实际存在的 thinking block 保留 signature，并将成功解码的明文写回其原生文本字段 |
| `trajectory_path` | VARCHAR | P0 | 原始/脱敏后轨迹文件的可访问路径 |
| `trajectory_sha256` | VARCHAR | P0 | `trajectory` 规范序列化后的 sha256 |
| `source_trajectory_sha256` | VARCHAR | P1 | 凭据脱敏和 thinking 回填前的源文件整体哈希；不得保存单个凭据的 hash |
| `trajectory_transformations` | JSON(VARCHAR) | P0 | 按顺序记录 `credential_redaction`、`image_externalization`、`thinking_decode_backfill` 及其版本；不得包含消息/工具协议转换 |
| `trajectory_size_bytes` | BIGINT | P0 | 完整轨迹字节数；禁止静默截断 |
| `trajectory_event_count` | BIGINT | P0 | Agent stream event 数 |
| `trajectory_parse_status` | VARCHAR | P0 | `ok` / `partial` / `invalid` |
| `llm_trace_format` | VARCHAR | P0 | 如 `anthropic_ccrelay_request_response.v1` |
| `llm_trace` | JSON(VARCHAR) | P0 条件 | 原生 request/response 序列；thinking 或需要成本审计时必填 |
| `llm_trace_path` | VARCHAR | P0 条件 | ccrelay/模型 trace 的脱敏后路径 |
| `llm_trace_sha256` | VARCHAR | P0 条件 | `llm_trace` 哈希 |
| `llm_trace_size_bytes` | BIGINT | P1 | trace 字节数 |
| `llm_request_count` | BIGINT | P0 | LLM 请求数 |
| `llm_response_count` | BIGINT | P0 | LLM 响应数 |
| `tool_call_count` | BIGINT | P0 | 原始 tool call 数 |
| `tool_result_count` | BIGINT | P0 | 原始 tool result 数 |
| `trajectory_image_count` | BIGINT | P0 | 原始轨迹内关联图像总数 |
| `trajectory_image_placeholder_count` | BIGINT | P0 | 原生消息中的图像占位符数量 |
| `trajectory_images` | JSON(VARCHAR) | P0 条件 | 多模态轨迹必填；按占位顺序保存 message index、tool call ID、路径、类型、尺寸和 sha256 |
| `image_binding_status` | VARCHAR | P0 | `not_applicable` / `complete` / `mismatch` / `missing_file` |
| `raw_manifest` | JSON(VARCHAR) | P1 | 原生 rollout manifest 的脱敏副本 |

说明：`trajectory` 和 `llm_trace` 都必须永久内联保存，路径与哈希用于审计和恢复；不能只存一个容易失效的路径。`trajectory_images` 的顺序必须与遍历消息所得图像占位符顺序完全一致，图像二进制按 SHA-256 去重外置，原位置写稳定引用，不在 JSON 中保留 base64。

### 4.5 Thinking Signature 与解码

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `has_thinking` | BOOLEAN | P0 | 是否观测到 thinking block |
| `thinking_block_count` | BIGINT | P0 | thinking block 总数 |
| `signature_occurrence_count` | BIGINT | P0 | signature 出现次数 |
| `unique_signature_count` | BIGINT | P0 | 按原始 signature 精确去重后的数量 |
| `thinking_decode_status` | VARCHAR | P0 | `not_applicable` / `complete` / `partial` / `unresolved` |
| `decoded_thinking_count` | BIGINT | P0 | 成功得到非空明文的 occurrence 数 |
| `unresolved_signature_count` | BIGINT | P0 | 未解码 occurrence 数 |
| `thinking_decode` | JSON(VARCHAR) | P0 条件 | occurrence 对齐的 signature + 解码明文数组，结构见下文；用于审计 `trajectory` 中的明文回填 |

`thinking_decode` 每个 occurrence 至少保存：

| 子字段 | 说明 |
|---|---|
| `occurrence_index` | 在本 attempt 内从 1 开始的稳定顺序 |
| `source_stream` | `trajectory` 或 `llm_trace` |
| `request_index` / `request_id` / `request_ts` | 对应模型请求定位信息 |
| `message_index` / `content_index` | 对应消息和 content block 位置 |
| `response_model` | 产生 signature 的实际模型 |
| `signature` | 原始完整 signature，必须保留 |
| `signature_sha256` / `signature_length` | 完整性校验 |
| `base64_valid` | signature 基础结构检查 |
| `decode_status` | `success` / `failed` / `not_attempted` |
| `decoded_text` | 成功时的非空思考明文；失败时为空 |
| `decoder_protocol_version` | 解码协议/脚本版本 |
| `decoder_mode` | 调用参数，如 `sonnet5`；不假定等于 rollout 模型 |
| `decoded_by` | 服务实际返回的解码器标识 |
| `cache_hit` | 是否命中 decoder cache |
| `decode_attempt_count` | 解码尝试次数 |
| `decoded_at` | 最终解码时间 |
| `http_status` / `error_code` | 诊断字段；不保存响应中的敏感信息 |
| `decoder_usage` | 解码 token usage，可选 |
| `decoder_error_sidecar_path` | 失败项原始 decoder 响应的受控 sidecar 路径；PPT 表本身只保留结构化错误码 |

完整性要求：

```text
decoded_thinking_count + unresolved_signature_count
    == signature_occurrence_count
```

同一 signature 多次出现时，每个 occurrence 都要保留；可以共享同一个解码结果，但不能只留 unique 级记录而丢失位置。

对每个 `decode_status=success` 的 occurrence，`llm_trace` 中产生 signature 的 response thinking block 必须满足：原 signature 未删除、解码文本非空、位置与 `thinking_decode` 一致。若同一 block 被 Agent stream 暴露，`trajectory` 中也必须满足相同要求。部分非流式或内部响应可能只存在于 `llm_trace`；此时 `message_index=null`，不得为了补齐 Agent 轨迹而伪造 event。解码失败的 block 仍保留 signature，不伪造明文。

### 4.6 执行状态与错误

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `status` | VARCHAR | P0 | `success` / `failed` / `timeout` / `partial` / `cancelled` |
| `started_at` | TIMESTAMP | P0 | attempt 开始时间 |
| `ended_at` | TIMESTAMP | P0 | attempt 结束时间 |
| `duration_ms` | BIGINT | P0 | 端到端耗时 |
| `terminal_reason` | VARCHAR | P1 | 正常完成或终止原因 |
| `stop_reason` | VARCHAR | P1 | 最终模型 stop reason |
| `error_type` | VARCHAR | P0 条件 | 失败时必填 |
| `error_message` | VARCHAR | P0 条件 | 脱敏后的短错误信息 |
| `error_detail` | JSON(VARCHAR) | P1 | 脱敏后的结构化错误；禁止整段请求头 |
| `risk_flags` | JSON(VARCHAR) | P1 | 运行期风险标记 |

### 4.7 PPT 产物

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `artifact_status` | VARCHAR | P0 | `complete` / `pptx_only` / `missing` / `invalid` |
| `pptx_path` | VARCHAR | P0 条件 | 成功 attempt 必填 |
| `pptx_sha256` | VARCHAR | P0 条件 | PPTX 内容哈希 |
| `pptx_size_bytes` | BIGINT | P1 | PPTX 大小 |
| `slide_count` | BIGINT | P0 条件 | 成功产物页数 |
| `pdf_path` | VARCHAR | P1 | 渲染 PDF 路径 |
| `pdf_sha256` | VARCHAR | P1 | PDF 哈希 |
| `rendered_dir` | VARCHAR | P1 | 分页渲染目录 |
| `rendered_slides` | JSON(VARCHAR) | P1 | 每页图片路径、sha256、宽高 |
| `output_files` | JSON(VARCHAR) | P1 | 其它输出文件清单 |
| `source_pptx_path` | VARCHAR | P0 条件 | edit 任务的原 PPTX |
| `source_pptx_sha256` | VARCHAR | P0 条件 | edit 来源文件哈希 |

不在 JSONL 中内嵌 PPTX/PDF/base64；只保存共享存储路径、哈希和大小。

### 4.8 Token、Cache 与成本

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `input_tokens` | BIGINT | P1 | 所有请求聚合 input tokens |
| `cache_creation_input_tokens` | BIGINT | P1 | cache 创建 tokens |
| `cache_read_input_tokens` | BIGINT | P1 | cache read tokens |
| `output_tokens` | BIGINT | P1 | output tokens |
| `thinking_tokens` | BIGINT | P1 | thinking tokens |
| `usage_detail` | JSON(VARCHAR) | P1 | 每请求 usage，保留 iterations/cache 分项 |
| `cost_usd` | DOUBLE | P1 | attempt 总成本 |
| `cost_cny` | DOUBLE | P1 | 可选折算成本 |
| `usd_cny_rate` | DOUBLE | P1 | 折算汇率 |
| `pricing_version` | VARCHAR | P1 | 计价规则版本/日期 |
| `cache_cost_included` | BOOLEAN | P1 | 成本是否已包含 cache create/read |
| `cost_audit_status` | VARCHAR | P1 | `complete` / `estimated` / `missing` |

### 4.9 质量与 Reward（摘要与完整逐项结果均内联）

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `reward_status` | VARCHAR | P2 | 是否完成评测 |
| `reward_total` | DOUBLE | P2 | 统一总分，需配合版本 |
| `reward_version` | VARCHAR | P2 | Reward 流程版本 |
| `quality_tier` | VARCHAR | P2 | 如 `excellent` / `usable` / `reject` |
| `hard_filter_pass` | BOOLEAN | P2 | 脚本硬门禁 |
| `reward_record_id` | VARCHAR | P2 | 关联 Reward 宽表的 ID |
| `reward_wide_batch_id` | VARCHAR | P2 | 关联 `reward_wide` 的评测批次 ID |
| `reward_rubric_source` | VARCHAR | P1 | `generated_this_run` / `reused_historical` |
| `reward_evaluation_valid` | BOOLEAN | P1 | 本次评测是否有效 |
| `reward_training_usable` | BOOLEAN | P1 | Reward 是否可作训练标签 |
| `reward_system_error_items` | BIGINT | P1 | 系统错误项数 |
| `reward_cannot_determine_items` | BIGINT | P1 | 无法判定项数 |
| `reward_not_evaluated_items` | BIGINT | P1 | 未评测项数 |
| `reward_total_items` | BIGINT | P1 | rubric 总项数 |
| `reward_requirement_compliance` | DOUBLE | P1 | Requirement Compliance 域分数 |
| `reward_substance_quality` | DOUBLE | P1 | Substance Quality 域分数 |
| `reward_presentation_design_quality` | DOUBLE | P1 | Presentation Design Quality 域分数 |
| `reward_judge_models` | JSON(VARCHAR) | P1 | 本轮 LLM/VLM judge 模型映射 |
| `reward_detail_status` | VARCHAR | P0 | `complete` / `missing` / `not_applicable` |
| `reward_detail_schema` | VARCHAR | P1 | 内联 Reward 对象 schema |
| `reward_detail_item_count` | BIGINT | P0 | `reward_detail.item_results` 项数 |
| `reward_detail` | JSON(VARCHAR) | P0 | 最终修复后的完整 `evaluation.json`，含逐项 script/LLM/VLM 结果、证据、理由与评分 |
| `reward_detail_source_path` | VARCHAR | P1 | 最终评测源文件路径 |
| `reward_detail_source_sha256` | VARCHAR | P1 | 最终评测源文件原始 SHA256 |
| `reward_detail_sha256` | VARCHAR | P0 | 脱敏后内联对象规范 SHA256 |
| `reward_detail_size_bytes` | BIGINT | P1 | 脱敏后内联对象规范字节数 |
| `reward_detail_integrity_status` | VARCHAR | P0 | 汇总总分与明细总分一致性 |

PPT 表直接内联最终 Reward 明细，`reward_record_id` 仅作为稳定追踪键，不再是读取明细的前置依赖。`reward_detail` 保存最终修复版 evaluation 对象，覆盖逐项 rubric 结果、脚本评分、LLM/VLM judge 结论、证据和理由；常用聚合字段同步拆列以便筛选。judge 原始 API 日志与 prompt 不复制进宽表，继续留在受控 source sidecar。

### 4.10 安全与完整性

| 字段 | 类型 | 级别 | 说明 |
|---|---|---:|---|
| `sanitization_version` | VARCHAR | P0 | 凭据/请求头脱敏规则版本 |
| `sanitized_at` | TIMESTAMP | P0 | 脱敏时间 |
| `secret_scan_status` | VARCHAR | P0 | `pass` / `fail` |
| `secret_scan_hit_count` | BIGINT | P0 | 必须为 0 才能确认入库 |
| `integrity_status` | VARCHAR | P0 | `pass` / `fail` |
| `record_sha256` | VARCHAR | P0 | 排除 Warehouse 自动字段后的整行规范哈希 |
| `validation_errors` | JSON(VARCHAR) | P1 | 校验问题列表 |

禁止字段：

- `api_key`、`authorization`、`cookie`、真实 token。
- API key/token 的 sha256。低熵或可枚举凭据不应通过哈希“脱敏”。
- 未脱敏的 proxy URL 或账号密码。

## 5. `thinking_decode` 示例

示例只展示结构，不是真实 signature：

```json
{
  "version": "thinking_decode.v1",
  "status": "complete",
  "occurrences": [
    {
      "occurrence_index": 1,
      "source_stream": "llm_trace",
      "request_index": 1,
      "request_id": "request-001",
      "content_index": 0,
      "response_model": "claude-opus-4-7",
      "signature": "<raw-signature>",
      "signature_sha256": "<sha256>",
      "signature_length": 764,
      "base64_valid": true,
      "decode_status": "success",
      "decoded_text": "<decoded-thinking-text>",
      "decoder_protocol_version": "signature-decoder.v2",
      "decoder_mode": "sonnet5",
      "decoded_by": "sonnet5",
      "cache_hit": true,
      "decode_attempt_count": 1,
      "decoded_at": "2026-08-03T12:58:00Z",
      "http_status": 200,
      "error_code": null
    }
  ]
}
```

## 6. 最小记录骨架

```json
{
  "schema_version": "ppt_trajectory.v1",
  "record_revision": 1,
  "trajectory_id": "ppttrj_<stable-id>",
  "domain": "ppt",
  "run_id": "<run-id>",
  "task_id": "<task-id>",
  "attempt_index": 1,
  "source_dataset_name": "<source-dataset>",
  "query": "<original-user-query>",
  "query_sha256": "<sha256>",
  "task_type": "document_grounded_create",
  "operation_type": "create",
  "reference_files": "[]",
  "is_multimodal": false,
  "rollout_model_requested": "claude-opus-4-7",
  "rollout_model_observed": "claude-opus-4-7",
  "agent_name": "claude-code",
  "skill_refs": "[{\"name\":\"pptx\",\"ref\":\"<commit>\"}]",
  "thinking_enabled": true,
  "trajectory_format": "claude_stream_json.v1",
  "trajectory": "[<native-agent-events>]",
  "trajectory_path": "<cfs-path>",
  "trajectory_sha256": "<sha256>",
  "source_trajectory_sha256": "<sha256-before-redaction-and-thinking-backfill>",
  "trajectory_transformations": "[{\"type\":\"credential_redaction\",\"version\":\"v1\"},{\"type\":\"image_externalization\",\"version\":\"v1\"},{\"type\":\"thinking_decode_backfill\",\"version\":\"v1\"}]",
  "trajectory_size_bytes": 7286724,
  "trajectory_event_count": 257,
  "trajectory_parse_status": "ok",
  "llm_trace_format": "anthropic_ccrelay_request_response.v1",
  "llm_trace": "[<native-request-response-events>]",
  "llm_trace_path": "<cfs-path>",
  "llm_trace_sha256": "<sha256>",
  "llm_request_count": 48,
  "llm_response_count": 48,
  "tool_call_count": 79,
  "tool_result_count": 79,
  "trajectory_image_count": 18,
  "trajectory_image_placeholder_count": 18,
  "trajectory_images": "[{\"message_index\":20,\"tool_call_id\":\"<tool-call-id>\",\"path\":\"<cfs-path>/slide-01.jpg\",\"sha256\":\"<sha256>\"}]",
  "image_binding_status": "complete",
  "has_thinking": true,
  "thinking_block_count": 6,
  "signature_occurrence_count": 6,
  "unique_signature_count": 6,
  "thinking_decode_status": "complete",
  "decoded_thinking_count": 6,
  "unresolved_signature_count": 0,
  "thinking_decode": "{<occurrence-aligned-decodes>}",
  "status": "success",
  "started_at": "<utc-time>",
  "ended_at": "<utc-time>",
  "duration_ms": 2755151,
  "artifact_status": "complete",
  "pptx_path": "<cfs-path>/slides.pptx",
  "pptx_sha256": "<sha256>",
  "slide_count": 10,
  "secret_scan_status": "pass",
  "secret_scan_hit_count": 0,
  "integrity_status": "pass",
  "record_sha256": "<sha256>"
}
```

## 7. 入库验收规则

P0 验收：

1. 每行 JSON 可解析，复杂列是合法 JSON 字符串。
2. `(run_id, task_id, attempt_index)` 在单个 batch 内唯一；`ppt_trajectory_all` 中 `(run_id, task_id, attempt_index, record_revision)` 唯一。同一任务真正重跑时增加 `attempt_index`，字段富化只增加 `record_revision`。
3. `trajectory` 非空、不截断，哈希、字节数、事件数一致；结构仍为原生 Agent 协议，不包含 OpenAI/XML/SFT 转换。图片只允许按已登记规则从 base64 确定性外置，原位置必须保留稳定引用。
4. thinking run 必须有 `llm_trace` 和 `thinking_decode`，成功解码的明文必须回填到 `llm_trace` response block 且保留 signature；Agent stream 中实际存在的对应 block 同步回填。非 thinking run 的状态为 `not_applicable`。
5. signature occurrence 数、顺序、sha256 与原始轨迹一致。
6. 解码成功项的 `decoded_text` 非空；失败项保留原 signature、定位信息和错误码。
7. 成功 attempt 的 PPTX 存在、sha256 可复算、页数大于 0。
8. 失败 attempt 必须有 `error_type`，不能因无产物被丢弃。
9. tool call/result ID 闭合；图像路径数、占位符数和 `trajectory_images` 数量一致，文件可访问；若原生格式不提供 ID，记录对应格式的完整性校验结果。
10. `secret_scan_hit_count == 0`；无 Authorization、API key、cookie、代理凭据或其 hash。
11. 所有模型字段取真实请求/响应值，不从目录名推断。
12. 任何 SFT/XML/OpenAI 转换产物必须记录 `source_trajectory_id` 和 `trajectory_sha256`，但不回写本表原始字段。

## 8. Warehouse 注册建议

```text
table key: ppt_trajectory
display_name: PPT轨迹宽表
unique_key: [run_id, task_id, attempt_index]
dedup_order_by: TRY_CAST(record_revision AS BIGINT) DESC, batch_ingested_at DESC
view_name: ppt_trajectory
view_all_name: ppt_trajectory_all
viewer_conversation.field: trajectory
viewer_conversation.format: ppt_native_trajectory
```

首版首页建议展示：总记录数、唯一任务数、成功率、PPTX 产物率、thinking 覆盖率、signature 解码完整率、平均/分位成本、模型分布、任务类型分布。

## 9. 验证方法

公开版不分发原部署的统计结果。应使用自备数据验证业务键唯一性、轨迹完整性、图像顺序、评分一致性和凭据脱敏。验证报告应记录输入范围、检查项、失败位置与证据路径，不能把示例当成真实验收结果。

## 10. 字段演进与弃用规则

### 10.1 首版一次性固定的稳定核心

以下语义在 v1 后不得重命名或改变含义：

- 业务键与来源：`run_id`、`task_id`、`attempt_index`、`trajectory_id`、`source_*`、Warehouse batch 字段。
- 原始任务与执行事实：`query`、`task_type`、实际模型、开始/结束时间、状态和错误。
- 原生轨迹：`trajectory`、`llm_trace`、工具计数、图像顺序绑定、hash 和完整性状态。
- Thinking：signature occurrence 定位、解码状态、明文回填和 decoder 版本。
- PPT 产物：PPTX/PDF/渲染页路径、hash、页数和产物状态。
- 安全审计：脱敏版本、secret scan、record hash 和 validation errors。

这些简单且长期稳定的字段应一次设计好，不使用 `extra_v2`、`new_status` 之类临时替代列。

### 10.2 允许按周期增加的演进字段

以下模块随系统迭代允许新增 nullable 列：

- Agent、skill、runtime 和工具协议的新版本信息。
- thinking decoder 新协议、cache、usage 和错误诊断。
- 新 Reward 流程的关联键、稳定质量摘要与内联完整明细；流程新增的稳定筛选维度可增加 nullable 列。
- 新的多模态资产类型、成本口径和派生标签。

只有满足任一条件时才提升为显式宽表列：需要直接筛选/聚合；多个 batch 稳定产出；是跨系统 join key；或影响数据可用性。单次实验参数和稀疏诊断细节保留在已有 JSON 列中。

### 10.3 兼容性约束

1. 新字段只能以 nullable 方式增加；旧 batch 缺失时由 `ppt_trajectory_all` 补 `NULL`。
2. 已发布字段不删除、不复用、不静默改语义。停产字段在 registry 标为 deprecated，新 batch 写 `NULL`。
3. 字段替换时新增 replacement 列，并在至少一个完整数据周期内双写；registry 记录 `replaced_by`。
4. `schema_version` 记录生产者契约。仅增加兼容列升级 minor；改业务键、值域或语义才升级 major。
5. 每个字段在 registry 记录 `owner`、`semantic_type`、`introduced_in`、`deprecated_in`、`replaced_by`、值域和是否可用于筛选。
6. Warehouse 查询层统一 `VARCHAR`，包括全局缺失的标准占位列也显式使用 `CAST(NULL AS VARCHAR)`；数值、布尔和时间通过 typed view 或查询中的 `TRY_CAST` 暴露，避免不同 batch 推断出冲突类型。

建议同时提供两个视图：

```text
ppt_trajectory_all  = 所有 confirmed batch 的历史记录，按列名并集
ppt_trajectory      = 按 (run_id, task_id, attempt_index) 去重后的最新 revision
```

## 11. 已确认决策

以下约束用于示例生产者与入库校验：

1. `trajectory` 与 `llm_trace` 两者都永久在线内联，同时保留 path/hash 作为审计和恢复信息。
2. Reward 最终详细逐项结果直接内联到 PPT 表的 `reward_detail`；`reward_record_id` 只保留作追踪键。judge 原始 API 日志与 prompt 留受控 sidecar。
3. signature 解码失败时，PPT 表只保存结构化错误码；decoder 原始响应保存在受控 sidecar，并仅记录 sidecar 路径。
4. 首批回填先使用授权的小规模自备数据；人工确认校验结果后再扩展。
