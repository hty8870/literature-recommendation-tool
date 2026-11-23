## 数据契约、状态机与接口

本章是跨模块数据契约的唯一权威定义。检索、评分、证据核验和前端章节 MUST 引用本章类型，不得另建同义字段或改变状态语义。

### 1. 通用契约约束

#### CON-001：契约单一事实来源

- 实现 MUST 将本章实体转换为 JSON Schema 2020-12，存放于 `packages/contracts/schemas/`。
- 后端 Pydantic/数据类与前端 TypeScript 类型 SHOULD 从 JSON Schema 生成；生成文件 MUST NOT 手工修改。
- 对外请求对象 MUST 使用 `additionalProperties: false` 拒绝未知字段。响应消费者 MUST 忽略不影响当前语义的未知可选字段，以便向后兼容扩展。
- 可验证条件：修改任一 Schema 后，后端和前端生成类型同步变化；契约测试能够发现字段漂移。

#### CON-002：序列化与数值约定

- JSON MUST 使用 UTF-8；字段名 MUST 使用 `snake_case`。
- 时间 MUST 使用 RFC 3339 UTC 字符串，例如 `2026-11-14T08:00:00Z`。数据库 SHOULD 使用带时区时间类型。
- ID MUST 为字符串。`event_id` SHOULD 使用 UUID/ULID；`sequence` MUST 为从 1 开始的正整数。
- 所有偏移量 MUST 为 UTF-8 字节偏移，采用左闭右开区间 `[start_offset, end_offset)`；禁止直接混用 JavaScript UTF-16 下标或 Python 字符下标。
- 分数 MUST 为有限数值，禁止 `NaN`、正负无穷。`display_score` 范围为 `[0, 100]`。
- 数组顺序具有语义时 MUST 明确定义；`source_ids` 等集合型数组 MUST 去重。
- 必填字段允许为 `null` 时，键仍 MUST 出现；未标为可空或可选的字段 MUST NOT 为 `null`。

#### CON-003：版本与审计字段

- 所有公开事件 envelope MUST 携带 `schema_version`，初始值为 `1.0.0`；REST 主版本由 `/api/v1` 表达，嵌套实体无需重复该字段。
- 可变业务实体 MUST 包含 `created_at`、`updated_at`；不可变快照 MUST 只包含 `created_at`。
- `score_version`、`parser_version`、`index_version` 和 `schema_version` MUST 独立管理，禁止用一个版本号代替全部版本。

### 2. 基础值对象

#### CON-010：Author

| 字段 | 类型 | 必填/可空 | 约束 |
|---|---|---|---|
| `full_name` | string | 必填 | 来源中可核查的完整显示名，禁止模型补写。 |
| `given_name` | string/null | 必填空可 | 名；未知时为 `null`。 |
| `family_name` | string/null | 必填空可 | 姓；未知时为 `null`。 |
| `orcid` | string/null | 必填空可 | 规范化 ORCID，未知时为 `null`。 |

#### CON-011：BoundingBox

- 必填字段：`page`、`x0`、`y0`、`x1`、`y1`、`origin`。
- `page` 为从 1 开始的 PDF 页序号；坐标单位为 PDF point。
- `origin` 枚举为 `top_left`、`bottom_left`。入库时 SHOULD 统一转换为 `top_left`。
- MUST 满足 `x0 <= x1`、`y0 <= y1`，且坐标位于对应页面范围内。

#### CON-012：SourceLocator

| 字段 | 类型 | 约束 |
|---|---|---|
| `pdf_page` | integer/null | PDF 证据必填；外部 API 证据可为 `null`。 |
| `printed_page` | string/null | 印刷页码无法可靠识别时为 `null`，不得用 PDF 页序号冒充。 |
| `section_path` | string[] | 从一级章节到当前章节的标题路径；未知时使用空数组。 |
| `paragraph_index` | integer/null | 当前章节内从 0 开始的段落序号。 |
| `bounding_boxes` | BoundingBox[] | 无坐标的外部来源可为空数组。 |
| `source_uri` | string/null | 外部来源必须提供可核查 URI；自控 PDF 可使用内部受控 URI。 |

### 3. 核心实体

#### CON-100：Paper

| 字段 | 类型 | 必填性与约束 |
|---|---|---|
| `paper_id` | string | 必填，库内稳定且全局唯一；SHOULD 包含库命名空间。 |
| `library_id` | string | 必填，租户和权限过滤边界。 |
| `doi` | string/null | 必填空可；保存来源 DOI，不得虚构。 |
| `doi_normalized` | string/null | 必填空可；去除 `https://doi.org/`、`doi:` 等前缀及首尾空白并转为小写后的 DOI；有 DOI 时必填。 |
| `title` | string | 必填，非空。 |
| `authors` | Author[] | 必填；来源未知时可为空数组，但 `metadata_verified` 不得据此声称完整。 |
| `year` | integer/null | 必填空可，合理出版年份。 |
| `journal` | string/null | 必填空可。 |
| `csl_json` | object/null | 必填空可；引文格式化所需 CSL 数据，未核验时为 `null`。 |
| `document_type` | string enum | `research_article`、`review`、`correction`、`retraction`、`editorial`、`other`。 |
| `document_type_eligible` | boolean | 必填，由文献类型策略计算，客户端不得自行指定。 |
| `publication_status` | string enum | `published`、`corrected`、`retracted`、`updated`。 |
| `related_dois` | object | 必填；含 `corrects`、`corrected_by`、`retracted_by` 三个字符串数组，默认均为空。 |
| `source_format` | string enum | `pdf`、`publisher_api`、`metadata_api`、`other`。 |
| `source_uri` | string | 必填，受控原文或元数据来源。 |
| `metadata_source` | string | 必填，记录权威元数据来源标识。 |
| `metadata_verified` | boolean | 必填；只有完成规定元数据核验才能为 `true`。 |
| `created_at`、`updated_at` | datetime | 必填。 |

可验证条件：无 DOI 文献不会生成虚构的 `doi_normalized`；更正声明能通过 `related_dois.corrects` 关联原论文。

#### CON-110：Passage

| 字段 | 类型 | 必填性与约束 |
|---|---|---|
| `passage_id` | string | 必填，稳定标识一个语义块。 |
| `paper_id`、`library_id` | string | 必填，必须与被引用 Paper 一致。 |
| `raw_text` | string | 必填，保留用于证据字节偏移核验的原始提取文本。 |
| `normalized_text` | string | 必填，用于检索的清洗文本；可以与 `raw_text` 相同。 |
| `section_path` | string[] | 必填，未知时使用空数组。 |
| `paragraph_index` | integer | 必填，当前章节内从 0 开始。 |
| `page_start`、`page_end` | integer/null | PDF 必填且 `1 <= page_start <= page_end`；外部来源可空。 |
| `printed_page_start`、`printed_page_end` | string/null | 必填空可。 |
| `bounding_boxes` | BoundingBox[] | 必填，无坐标时为空数组。 |
| `parser_version` | string | 必填。 |
| `embedding_version` | string/null | 必填空可，尚未向量化时为 `null`。 |
| `index_version` | string | 必填，标明当前可检索索引版本。 |
| `created_at`、`updated_at` | datetime | 必填。 |

`raw_text` 与 `normalized_text` 的清洗映射 SHOULD 持久化；无法提供映射时，证据 MUST 使用 `raw_text` 做逐字核验，不能用清洗文本反推原文。

#### CON-120：Evidence

| 字段 | 类型 | 必填性与约束 |
|---|---|---|
| `evidence_id` | string | 必填，稳定且全局唯一。 |
| `paper_id`、`passage_id` | string | 必填，必须指向同一篇论文及其段落。 |
| `quote` | string | 必填，必须等于 `Passage.raw_text` 指定字节区间的内容。 |
| `display_quote` | string/null | 必填空可，用于展示清洗后的文本，不得替代 `quote` 做核验。 |
| `context` | string | 必填，必要上下文；不得通过拼接改变原意。 |
| `start_offset`、`end_offset` | integer | 必填，UTF-8 字节偏移，满足 `0 <= start_offset < end_offset <= raw_text_bytes.length`。 |
| `locator` | SourceLocator | 必填，定位到该证据而非仅定位整篇论文。 |
| `extractor_version` | string | 必填。 |
| `source_trace_valid` | boolean | 必填，由服务端核验计算，客户端输入 MUST 被忽略。 |
| `created_at` | datetime | 必填。 |

可验证条件：对 `raw_text` 的 UTF-8 字节切片严格等于 `quote`；PDF 证据具有有效页码；定位无效时 `source_trace_valid=false`，对应推荐不得进入 `verified`。

#### CON-130：Recommendation

| 字段 | 类型 | 必填性与约束 |
|---|---|---|
| `result_id` | string | 必填，推荐稳定身份；有 DOI 时为 `doi:<doi_normalized>`，否则初始为 `paper:<paper_id>`；跨库合并时保留协调器选定的主 `result_id`。 |
| `query_id` | string | 必填，所属搜索会话。 |
| `paper` | Paper | 必填，推荐产生时的书目快照。 |
| `evidence` | Evidence[] | 必填；`verified` 时至少一项且全部定位有效。 |
| `relationship` | string/null | `support`、`complement`、`contrast`、`insufficient` 或 `null`。 |
| `verification_status` | string enum | `pending`、`verified`、`insufficient`、`error`。 |
| `retrieval_score` | object/null | 必填空可；存在时包含 `source`、`value`、`metric`、`rank`，只用于库内诊断。 |
| `display_score` | number/null | 必填空可，范围 `[0,100]`；公开可见推荐 MUST 有值。 |
| `score_version` | string/null | 必填空可；有 `display_score` 时必填。 |
| `calibration_status` | string enum | `bootstrap`、`global_calibrated`、`source_calibrated`、`uncalibrated`。 |
| `provisional` | boolean | 必填；实时结果通常为 `true`，最终快照内复制项 MUST 为 `false`。 |
| `source_ids` | string[] | 必填，至少一个来源库，合并后不得重复。 |
| `result_version` | integer | 必填，从 1 开始；实时结果每次实质变化递增。 |
| `insertion_anchor` | object/null | 必填空可；全文模式下见下述约束。 |
| `citation_text` | string/null | 必填空可；只有 `verified` 且关系为支持/补充/对比时才可非空。 |
| `created_at`、`updated_at` | datetime | 必填。 |

`insertion_anchor` 存在时 MUST 包含 `manuscript_id`、`section_path`、`paragraph_index`、`sentence_id`、`quote_anchor`。`paragraph_index` 从 0 开始；`quote_anchor` 是用于重新定位的稿件原句，不得为空。

约束：

- `verification_status=verified` 时，`relationship` MUST 为 `support`、`complement` 或 `contrast`。
- `verification_status=insufficient` 时，`relationship=insufficient` 且 `citation_text=null`。
- `pending` 或 `error` 时 `relationship=null`、`citation_text=null`。
- 结果排名是协调器派生状态，不作为 Recommendation 的持久字段；排名变化通过事件表达。

#### CON-140：WorkerStatus

| 字段 | 类型 | 必填性与约束 |
|---|---|---|
| `query_id`、`library_id` | string | 必填，组成当前 attempt 的关联键。 |
| `worker_state` | string enum | `pending`、`running`、`completed`、`failed`、`timed_out`、`cancelled`。 |
| `attempt` | integer | 必填，从 1 开始；重试时递增。 |
| `candidates_seen` | integer | 必填，大于等于 0。 |
| `candidates_verified` | integer | 必填，大于等于 0，且不得超过 `candidates_seen`。 |
| `recommendations_emitted` | integer | 必填，大于等于 0。 |
| `started_at`、`ended_at`、`heartbeat_at` | datetime/null | 必填空可；`pending` 时 `started_at`、`ended_at` 为 `null`。 |
| `last_error` | ErrorObject/null | 必填空可；成功完成时 MUST 为 `null`。 |
| `updated_at` | datetime | 必填。 |

#### CON-150：SearchSession 与 Coverage

`SearchSession` 必填字段：

- `query_id`、`input_type`、`search_state`、`requested_library_ids`、`created_at`、`updated_at`。
- `input_type` 枚举：`keywords`、`abstract`、`full_text`。
- `search_state` 枚举：`created`、`running`、`finalizing`、`completed`、`partial`、`failed`、`cancelled`。
- `latest_checkpoint_id`、`final_snapshot_id` 均为必填空可字段。

`Coverage` 必填字段：

- `total_library_count`：非负整数。
- `completed_library_ids`、`failed_library_ids`、`timed_out_library_ids`、`cancelled_library_ids`、`pending_library_ids`：互不重叠的字符串数组。
- 所有状态数组的并集 MUST 与 `requested_library_ids` 一致，数组数量之和 MUST 等于 `total_library_count`。

#### CON-160：Snapshot

| 字段 | 类型 | 必填性与约束 |
|---|---|---|
| `snapshot_id` | string | 必填，全局唯一。 |
| `query_id` | string | 必填。 |
| `snapshot_kind` | string enum | `checkpoint`、`final`、`supplemental`。 |
| `results` | Recommendation[] | 必填，为创建时的有序复制，而非实时对象引用。 |
| `coverage` | Coverage | 必填，明确完成、失败、超时、取消和待处理库。 |
| `termination_reason` | string enum | `all_completed`、`deadline_reached`、`sufficient_results`、`user_requested`、`checkpoint_requested`、`cancelled`、`coordinator_failed`。 |
| `model_versions` | object | 必填，至少记录实际使用的重排、核验和生成模型版本；未使用生成模型时可省略该键。 |
| `calibration_versions` | object | 必填，来源标识到校准版本的映射。 |
| `supersedes_snapshot_id` | string/null | 必填空可；补充版本指向前一快照。 |
| `immutable_hash` | string | 必填，64 位小写 SHA-256 十六进制。 |
| `created_at` | datetime | 必填；无 `updated_at`。 |

- `immutable_hash` MUST 对“除 `immutable_hash` 自身外的 Snapshot 规范 JSON”计算：UTF-8、键名递归排序、无多余空白。
- Snapshot MUST 采用追加写入；不得提供更新或删除其业务内容的接口。
- `checkpoint` 可以在搜索继续运行时创建，不改变 SearchSession 状态；其中结果保留 `provisional=true`。
- `final` 中结果 MUST 为 `provisional=false`，`pending_library_ids` MUST 为空；截止时未完成的库 MUST 已转为 `timed_out` 或 `cancelled`。
- `supplemental` 用于承载最终快照后的迟到结果，MUST 设置 `supersedes_snapshot_id`，不得修改前一快照。

### 4. 状态机

#### FSM-001：SearchSession 状态转换

| 当前状态 | 允许下一状态 | 触发条件 |
|---|---|---|
| `created` | `running` | 输入校验和持久化成功，Worker 调度开始。 |
| `created` | `cancelled` | 用户在调度前取消。 |
| `running` | `finalizing` | 全部 Worker 终态、达到截止条件、满足停止条件，或用户明确要求最终定稿。 |
| `running` | `cancelled` | 用户取消且不请求最终快照。 |
| `running` | `failed` | 协调器发生不可恢复错误且没有可用结果。 |
| `finalizing` | `completed` | 全部 Worker 成功完成且最终快照持久化成功；零推荐也可以是 `completed`。 |
| `finalizing` | `partial` | 存在失败、超时、取消或提前停止，但最终快照持久化成功。 |
| `finalizing` | `failed` | 最终快照持久化失败且无法形成有效结果版本。 |

- `completed`、`partial`、`failed`、`cancelled` 为终态，不得回到 `running`。
- 单个库失败 MUST NOT 直接使整个 SearchSession 变为 `failed`。
- `finalizing` 仅表示终止条件已满足并持久化最终快照，MUST NOT 被实现为开始去重、评分、重排或证据核验的前置屏障。
- 创建 `checkpoint` 不改变 SearchSession 状态。

#### FSM-002：Worker 状态转换

- `pending → running → completed | failed | timed_out | cancelled`。
- `pending → cancelled` 合法。
- `failed | timed_out → pending` 仅在重试策略允许、搜索尚未最终定稿时合法，且 `attempt` MUST 递增。
- `completed` 不得重试同一 attempt；重试 MUST 使用新的 `producer_event_id`。
- 最终定稿后不得在原 query 内重启 Worker；迟到结果按补充版本协议处理。

#### FSM-003：快照一致性

- 创建最终快照 MUST 在同一事务中固定结果顺序、结果版本、覆盖范围和终止原因。
- 快照提交后 MUST 先持久化 `search_finalized` outbox 事件，再对外宣告快照可用；两者应使用同一事务或等价原子机制。
- 快照创建失败不得留下“前端可见但不可重放”的部分状态。

### 5. 流式事件契约

#### EVT-001：SearchEvent envelope

所有公开事件 MUST 使用以下 envelope：

```json
{
  "schema_version": "1.0.0",
  "event_id": "01JCDEF8Z4N7M4J4N0Y7E4A0K2",
  "event_type": "recommendation_created",
  "query_id": "q_01JCDEXAMPLE",
  "sequence": 18,
  "occurred_at": "2026-11-14T08:00:00Z",
  "producer": "coordinator",
  "payload": {}
}
```

- `event_type` MUST 为下表枚举之一。
- `sequence` 由协调器统一分配，在每个 query 内严格递增且步长为 1；Worker MUST NOT 自行分配全局 sequence。
- Worker 内部消息 MUST 另带 `producer_event_id`，协调器以 `(library_id, attempt, producer_event_id)` 去重后再生成公开事件。

#### EVT-002：事件类型与 payload

| 事件类型 | payload 必填字段与语义 |
|---|---|
| `recommendation_created` | `recommendation`、`current_rank`；新增一条可展示推荐。 |
| `recommendation_merged` | `recommendation`、`merged_result_ids`、`previous_result_version`、`current_rank`；保留主 `result_id` 并合并来源与证据。 |
| `recommendation_reranked` | `trigger_result_id`、`reason`、`changes`；`changes` 至少一项，每项含 `result_id`、`result_version`、`previous_rank`、`current_rank`。 |
| `recommendation_rejected` | `result_id`、`paper_id`、`verification_status`、`reason_code`；`reason_code` 使用稳定机器码。 |
| `worker_progress` | `worker`，即完整 WorkerStatus。 |
| `worker_completed` | `worker`，其状态必须为 `completed`。 |
| `worker_failed` | `worker`，其状态必须为 `failed` 且含 ErrorObject。 |
| `worker_timed_out` | `worker`，其状态必须为 `timed_out`。 |
| `search_state_changed` | `previous_state`、`current_state`，转换必须符合 FSM-001。 |
| `search_finalized` | `snapshot_id`、`snapshot_kind`、`coverage`、`termination_reason`、`immutable_hash`；完整快照通过 GET 获取。 |

`recommendation_reranked.reason` 枚举：`new_result`、`evidence_merged`、`score_updated`、`duplicate_merged`。

#### EVT-003：原子写入与至少一次投递

- 结果变更和对应 outbox 事件 MUST 在同一数据库事务中写入。
- 发布器 SHOULD 从 outbox 顺序派发；SSE 投递语义为至少一次，禁止宣称恰好一次。
- 消费者 MUST 以 `event_id` 幂等处理；重复事件不得再次合并证据、增加结果版本或触发导出。
- 相同 `event_id` 且载荷哈希相同：返回成功确认但不产生副作用。
- 相同 `event_id` 但载荷不同：MUST 拒绝并记录 `EVENT_ID_CONFLICT`，不得覆盖原事件。
- 持久化约束 MUST 包括 `(query_id, event_id)` 唯一和 `(query_id, sequence)` 唯一。

#### EVT-004：断线重连与重放

- SSE 每条事件的 `id:` MUST 为十进制 `sequence`，`event:` 为 `event_type`，`data:` 为完整 envelope JSON。
- 服务 MUST 支持 `Last-Event-ID` 和 `after_sequence`；两者同时存在时 MUST 一致，否则返回 `REPLAY_REQUEST_CONFLICT`。
- 重放 MUST 先发送 `sequence > after_sequence` 的持久事件，再接入实时流，不得在交界处丢事件或重复分配序号。
- 请求的序号早于可用保留窗口时 MUST 返回 `REPLAY_GAP`；客户端随后 MUST 调用搜索当前状态接口进行全量重建，再监听新事件。
- 当前状态响应 MUST 包含 `latest_sequence`，作为全量重建后的续传起点。
- SSE 心跳 SHOULD 每 15 秒发送一次注释行 `: keep-alive`，心跳不得占用 sequence。

### 6. REST 与 SSE 接口

#### API-001：通用要求

- 公共 API 前缀 MUST 为 `/api/v1`；创建搜索、创建快照和导出 MUST 支持 `Idempotency-Key` 请求头。
- 服务 MUST 返回并记录 `X-Request-ID`。客户端 SHOULD 提供 UUID；缺省时由服务生成。
- 相同 Idempotency-Key 仅允许重放同一请求；键相同但请求哈希不同 MUST 返回 `IDEMPOTENCY_KEY_CONFLICT`。
- Worker 内部调用 MAY 使用消息队列或内部 HTTP，但 MUST 使用等价消息契约和服务间鉴权，不得暴露为匿名公共接口。

#### API-002：创建搜索

`POST /api/v1/searches`

请求：

```json
{
  "input_type": "keywords",
  "keywords": ["LFY2", "soybean floral development"],
  "abstract": null,
  "manuscript_id": null,
  "library_ids": ["abiotech-main"],
  "options": {
    "candidate_budget": 120,
    "soft_deadline_ms": 30000
  }
}
```

- `keywords`、`abstract`、`manuscript_id` MUST 根据 `input_type` 恰有一个有效输入，其余为 `null` 或省略。
- 未提供 `library_ids` 时由服务端按用户权限和默认路由决定；客户端不得借构造 ID 越权访问其他库。
- `candidate_budget`、`soft_deadline_ms` 为可选覆盖值，服务端 MUST 应用配置上限，不得信任客户端任意放大。
- 成功返回 `202`：`query_id`、`search_state`、`events_url`。输入验证失败返回 `400 VALIDATION_ERROR`，不得创建半成品会话。

#### API-003：读取当前状态

`GET /api/v1/searches/{query_id}`

返回 `200`：

- `session`：SearchSession。
- `results`：当前按协调器顺序排列的 Recommendation 数组。
- `workers`：WorkerStatus 数组。
- `latest_sequence`：可用于后续 SSE 续传。
- `is_live`：是否仍接受实时推荐事件。

该接口 MUST 反映已提交状态，不得为了等待其他 Worker 而延迟已提交结果的可见性。不存在的 query 返回 `404 QUERY_NOT_FOUND`，无权访问返回 `403 FORBIDDEN`。

#### API-004：监听事件

`GET /api/v1/searches/{query_id}/events`

- 成功响应为 `Content-Type: text/event-stream`，并禁用代理缓冲。
- 支持 `Last-Event-ID` 和查询参数 `after_sequence`。
- 在建立 SSE 前 MUST 完成权限和 replay 窗口校验，避免先返回 200 再才发现请求不可服务。
- 搜索已终结时 SHOULD 重放请求事件后正常关闭连接，不得伪造仍在运行。

#### API-005：取消搜索

`POST /api/v1/searches/{query_id}/cancel`

- 请求 Worker 取消并返回当前 SearchSession。
- 已取消或已终结的会话 MUST 返回当前状态，不产生额外副作用。
- 取消时已展示结果不自动删除；是否创建 checkpoint 由独立快照请求决定。

#### API-006：创建与读取快照

`POST /api/v1/searches/{query_id}/snapshots`

请求体：

```json
{
  "kind": "checkpoint"
}
```

- `kind=checkpoint`：原子复制当前结果，搜索可继续，返回 `201` 和 Snapshot。
- `kind=final`：仅在满足终止条件或用户明确要求提前定稿时创建；提前定稿 MUST 将未完成 Worker 转为取消或超时并如实记录覆盖范围。
- 最终快照已存在时，相同 final 请求 SHOULD 返回现有快照，不得另建语义重复的 final。
- 最终定稿后请求 checkpoint MUST 返回 `409 QUERY_STATE_CONFLICT`；需要迟到结果时创建 supplemental，而非 checkpoint。

`GET /api/v1/snapshots/{snapshot_id}` 返回完整不可变 Snapshot；不存在返回 `404 SNAPSHOT_NOT_FOUND`。API MUST NOT 提供修改快照内容的 PUT/PATCH 路径。

#### API-007：导出

`POST /api/v1/exports`

请求 MUST 包含 `snapshot_id`、`result_ids`、`format`、`citation_style`：

- `format` 枚举：`json`、`ris`、`bibtex`、`docx`。
- `result_ids` 至少一项，且全部必须存在于指定快照中，否则返回 `422 EXPORT_RESULT_MISMATCH`。
- 服务 MUST 基于快照重新校验选择和引文编号，不得接受客户端提供的最终数字编号作为权威值。
- 成功返回 `201`：`export_id`、`snapshot_id`、`immutable_hash`、`download_url`、`expires_at`。
- 下载地址 MUST 为短期签名 URL 或受控下载接口；导出任务失败不得返回看似可用的空文件。

#### API-008：私有稿件接口

- `POST /api/v1/manuscripts`：上传全文，返回 `manuscript_id`、`parse_status`、`delete_after`。
- `GET /api/v1/manuscripts/{manuscript_id}`：仅返回状态、元数据和授权用户可见的解析结果，不公开存储路径。
- `DELETE /api/v1/manuscripts/{manuscript_id}`：删除私有原文和派生缓存，成功返回 `204`；重复删除保持幂等。
- 上述接口 MUST 执行用户与租户级授权；不得通过 manuscript_id 推断或访问他人稿件。

### 7. 错误模型

#### ERR-001：ErrorObject

所有失败响应 MUST 使用 `application/problem+json`，并包含：

```json
{
  "type": "about:blank",
  "title": "Snapshot is immutable",
  "status": 409,
  "code": "SNAPSHOT_IMMUTABLE",
  "message": "该快照已固定，不能修改。",
  "retryable": false,
  "request_id": "req_01JCEXAMPLE",
  "details": {}
}
```

- `code` 为稳定机器标识，`message` 面向用户且不包含秘密、私有全文或堆栈。
- `details` 可包含字段路径、库 ID、当前状态等安全诊断信息；鉴权、数据库连接和模型密钥 MUST NOT 出现在其中。
- Worker 内部错误 SHOULD 使用相同结构；对外暴露前 MUST 清理内部敏感细节。

#### ERR-002：标准错误码

| HTTP | code | 使用条件与重试语义 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 字段、枚举或输入组合不合法；修改请求后重试。 |
| 400 | `SCHEMA_VERSION_UNSUPPORTED` | 不支持的 Schema 主版本；不得自动降级猜测。 |
| 401 | `AUTH_REQUIRED` | 缺少或失效认证。 |
| 403 | `FORBIDDEN` | 无库、稿件、搜索或快照权限。 |
| 404 | `QUERY_NOT_FOUND`、`SNAPSHOT_NOT_FOUND`、`MANUSCRIPT_NOT_FOUND` | 资源不存在；服务 MAY 为防枚举对越权资源统一返回 404。 |
| 409 | `QUERY_STATE_CONFLICT` | 当前状态不允许该操作。 |
| 409 | `SNAPSHOT_IMMUTABLE` | 尝试修改已固定快照。 |
| 409 | `IDEMPOTENCY_KEY_CONFLICT`、`EVENT_ID_CONFLICT` | 同一键对应不同载荷，必须人工排查而非覆盖。 |
| 409 | `REPLAY_REQUEST_CONFLICT` | 两个续传起点不一致。 |
| 410 | `REPLAY_GAP` | 事件已超出重放保留窗口，应全量重建状态。 |
| 422 | `SCORE_VERSION_INCOMPATIBLE` | 无法在当前请求中形成一致的评分口径。 |
| 422 | `EVIDENCE_TRACE_INVALID` | 原文、偏移或页码定位核验失败。 |
| 422 | `EXPORT_RESULT_MISMATCH` | 导出结果不属于指定快照。 |
| 429 | `RATE_LIMITED` | 触发限流；遵循 `Retry-After`。 |
| 500 | `INTERNAL_ERROR` | 未分类服务错误；是否重试由 `retryable` 决定。 |
| 503 | `SOURCE_UNAVAILABLE` | 数据源暂不可用；可在退避后重试。 |
| 504 | `WORKER_TIMEOUT` | Worker 超过截止时间；其他 Worker 结果不受影响。 |

#### ERR-003：失败可见性

- 部分库失败 MUST 体现在 WorkerStatus、Coverage 和最终快照中，不得把部分结果伪装成完整覆盖。
- 无推荐但全部检索成功属于正常 `completed`，不是 `INTERNAL_ERROR`。
- 可重试错误 MUST 采用有上限的指数退避和抖动；认证失败、契约冲突和证据定位失败 MUST NOT 盲目重试。

### 8. 版本兼容与演进

#### VER-001：Schema 版本规则

- URL 主版本与 Schema 主版本 MUST 同步演进。
- 新增可选字段、增加只读诊断信息属于向后兼容 minor 变更。
- 删除或重命名字段、改变类型/单位/偏移语义、收紧既有枚举含义、改变状态转换或事件幂等语义 MUST 升级主版本。
- 新增公开枚举值默认视为不兼容变更；若确需 minor 扩展，消费者 MUST 明确实现未知值降级路径，且不得把未知值解释为已核验或已完成。
- 新增事件类型时，旧消费者 MUST 能安全忽略并记录，不得因此破坏已有事件重放。

#### VER-002：独立版本和兼容失败

- `score_version` 只描述评分与校准口径，不随 API minor 变化而隐式变化。
- `parser_version` 或 `index_version` 改变不得自动改写既有 Snapshot。
- 无法兼容转换时 MUST 返回明确错误或隔离分组，不得静默混用不同契约的结果。
- 兼容适配器 MUST 有契约测试，覆盖旧事件重放、未知可选字段和未知事件类型。

### 9. 跨模块接口点

- 检索模块 MUST 产出符合 CON-100～CON-120 的 Paper、Passage 和 Evidence；无效定位不得被后续模块“修补”为有效。
- 评分与协调模块 MUST 填充 `display_score`、`score_version`、`result_version`、排名事件和全局 sequence；Worker 不得直接分配全局排名或序号。
- 引用生成模块只能消费满足 CON-130 核验约束的 Recommendation；格式化与导出只能以 Snapshot 为输入。
- 前端 MUST 依据 `result_id + result_version` 更新卡片，以 `event_id` 去重，并在 replay gap 后使用 `latest_sequence` 全量重建。
- 安全与配置模块可以调整预算、截止时间和保留窗口，但 MUST NOT 改变本章状态、快照不可变性和幂等语义。
