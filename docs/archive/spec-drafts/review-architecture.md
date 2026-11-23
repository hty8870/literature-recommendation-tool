# 架构与技术选型对抗性评审

> 评审对象：`文献推荐工具_AGENT_SPEC.md` v1.1.0（1704 行）与 `文献推荐工具_设计文档.docx` V1.0。  
> 评审范围：总体架构、检索/联邦检索、流式增量结果、共享资源调度、单 Agent 必要性、统一评分与校准、技术选型与可实施性。  
> 评审方式：逐条对照规格中的 ADR、需求 ID 与验收场景；检索并阅读官方文档、论文和真实系统实践；提出可证伪的替代方案与实验。  
> 结论边界：**这是评审与提案，不是规格变更**。本报告不声称证明任何架构全局最优；所有“推荐”都附有采用条件、收益、代价和待测假设。未修改任何现有规格文件。

---

## 0. 结论摘要

规格在“正确性不变量”上是强的：证据逐字定位、`insufficient` 拒答、快照不可变、事件幂等、展示排名与数字编号分离，这些都不应为了性能或便利而削弱。真正的问题不在这些不变量本身，而在**它们被实现成什么执行结构**。

最重要的五个判断：

1. **规格防住了最外层的“等待全部 Worker”屏障，但没有防住共享资源层的队头阻塞。** 每库全流程 Worker 在逻辑上互相独立，物理上仍共享重排器、LLM、数据库、对象存储和网络。共享队列若是 FIFO，一个重库的 120 条候选可以让轻库的首条结果排在后面。`AT-STR-001` 只阻塞 B 的 Worker，不阻塞共享重排器，因此可能“测试通过、用户体验失败”。

2. **协调器“唯一权威”被写成了可能“唯一计算点”。** `SCR-004` 允许在协调器内联调用 `CommonReranker`，而 `RANK-002` 要求先有分数再插入。若协调器在单写事件循环或数据库事务内调用模型，500ms 的重排会阻塞该查询的所有事件，45s 的模型超时会阻塞 45s。这不是全局屏障，却是更隐蔽的全局串行化。

3. **容量模型缺失。** `budget.global_max=120` 个候选进入“重排和证据核验”，而单次模型超时 45s、硬截止 90s、并发 8。按 `ceil(120/8) × 单次核验延迟` 计算，单次 5s 就是 75s，单次 8s 就是 120s，已经超过硬截止；若首个候选是慢调用或重试，Worker 可能在 90s 时零结果超时。首结果延迟也没有数值 SLO，无法作为门禁。

4. **单 Agent 不是产品必需，且不应在首结果关键路径上。** 规格把“单 Agent + LangChain”冻结为 ADR，但查询规划、预算、检索、评分、去重、编号大多是确定性的；Agent 的主要增量价值是查询扩展和停止判断。若 Agent 在派发前运行 3–10s，首结果就被推迟 3–10s。建议“默认立即派发 + 有界规划器并行 + 可选 Agent 扩展”，保留 Skill 作为策略文档；这是提案，不是对用户需求的擅自变更。

5. **M0–M1 存在明显过度设计。** 50 篇 PDF 的首个可运行版本不需要 4 个服务、两级索引、全量校准、LangChain Agent 和 20 条引用句预生成。建议模块化单体起步（FastAPI + Postgres + pgvector + FTS + 进程内 lane），在 M2 前用基准决定是否引入 OpenSearch/Vespa 和共享池。设计文档 §7.4 已经写了“小规模 Postgres/pgvector，规模增长后 OpenSearch/专用向量库”的演进路径，规格没有把它变成决策门禁。

严重度统计：**P0 2 项、P1 10 项、P2 8 项**。P0 不是“代码一定有 bug”，而是“按规格字面实现会破坏核心承诺，必须在 M2 前钉死”的架构歧义或容量缺口。

---

## 1. 证据分级：已确认事实、推断、待基准假设

本报告刻意区分三类陈述，避免把“官方文档有这个名字”误当成“在我们的负载上一定更好”。

### 1.1 已确认事实

| 事实 | 来源 | 对本评审的意义 |
|---|---|---|
| Elasticsearch 异步搜索可返回 `is_partial=true` 的中间结果，`batched_reduce_size` 控制部分归并频率，默认每收到 5 个分片响应做一次部分归并。 | [Elasticsearch async search API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-async-search-submit) | “部分结果”不是只能自研；原生平台已有可借鉴的执行模型。但它是“协调节点归并后可见”，不是逐文档事件流。 |
| OpenSearch `_msearch` 并行执行多个搜索，但响应数组包含**全部子搜索的结果**；它不是逐子搜索的流式 API。 | [OpenSearch multi-search](https://www.opensearch.org/docs/latest/api-reference/multi-search/) | 若用 `msearch` 实现多库检索，会引入响应级屏障；不能用它做“快库先出”。 |
| OpenSearch 2.10 引入 normalization processor，2.19 引入 score ranker processor（RRF）；hybrid query 可组合关键词和神经查询。 | [OpenSearch hybrid search](https://docs.opensearch.org/latest/search-plugins/hybrid-search/) | 混合召回 + RRF 是平台原生能力，不必自研融合层。 |
| Elasticsearch 提供 RRF retriever，可组合多个子 retriever，`rank_constant` 默认 60。 | [Elasticsearch RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion) | 库内/跨路 RRF 有成熟实现；自研只需做业务语义，不必重写算法。 |
| Elasticsearch `query_then_fetch` 使用分片本地 term/document frequencies，`dfs_query_then_fetch` 使用全局统计、更慢但更准。 | [Elasticsearch async search API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-async-search-submit) | 即使都叫 BM25，分片/库间的原始分也不能直接比较；这支持 `SCR-003` 的“同口径”要求，也说明跨库统一分不能靠原始分。 |
| Vespa 支持 first-phase、second-phase、global-phase 三阶段排序；global-phase 可在 stateless container 做跨 hit 归一化和 RRF，也可跑 ONNX cross-encoder；WAND/weakAnd 提供 top-k 子线性检索。 | [Vespa phased ranking](https://docs.vespa.ai/en/phased-ranking.html) | “先便宜召回、再少量重排”是成熟搜索引擎的默认结构，不是本项目的特殊发明。 |
| Qdrant 支持 prefetch + RRF/DBSF 的多阶段混合查询。 | [Qdrant hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/) | dense+sparse 的融合可下沉到平台；但 Qdrant 的全文索引主要用于过滤，BM25 排序通常要借助 sparse vector，需按版本验证。 |
| pgvector 支持 HNSW/IVFFlat、稀疏向量、半精度/二值量化，并可与 Postgres 全文检索组合做 hybrid；多租户建议分区或独立表。 | [pgvector README](https://github.com/pgvector/pgvector) | M0–M1 的 50 篇 PDF 完全可以用一个 Postgres 解决，避免过早引入搜索集群。 |
| 浏览器 `EventSource` 在 HTTP/1.1 下每浏览器/域最多 6 个连接；HTTP/2 用多路复用缓解（默认约 100 个流）。标准 `EventSource` 构造参数只有 `withCredentials`，没有自定义 `Authorization` 头。 | [MDN: Using server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)、[MDN: HTTP messages](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Messages) | 规格的 SSE 鉴权与连接管理不能只写“复用对象级授权”，必须明确 Cookie/流票据/HTTP2。 |
| 大规模 fan-out 会放大尾延迟；可用 hedged requests、tied requests、micro-partitions、“good enough”结果、把长请求切小以避免队头阻塞。 | [The Tail at Scale](https://cacm.acm.org/research/the-tail-at-scale/) | 共享池、并行核验、提前返回部分结果都有成熟依据；但“good enough”必须与覆盖诚实不变量兼容。 |
| RRF 是 rank-based 融合，`k=60` 在 TREC 实验中表现稳健，优于 Condorcet 和多数单系统。 | [Cormack et al., SIGIR 2009](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) | 跨库/跨路融合可以用秩，不必先做分数校准；这为“冷启动 RRF 回退”提供了依据。 |
| 联邦检索的三大问题是 collection representation、collection selection、result merging；不同集合的分数不可直接比较，result merging 是核心难点。 | [Shokouhi & Si, Federated Search, 2011](https://www.khoury.northeastern.edu/home/vip/teach/IRcourse/IR_surveys/FnTIR_2011.pdf) | 规格的“统一展示分/校准”方向正确，但冷启动成本被低估；秩融合是现实回退。 |
| BEIR 显示 BM25 是稳健基线；重排和 late-interaction 平均最好但计算成本高；dense/sparse 更高效但常在外分布上落后。 | [BEIR, NeurIPS 2021](https://arxiv.org/abs/2104.08663) | 不能假设“向量召回一定优于关键词”；固定 `top_k=50` 需要按召回目标校准。 |
| SciFact 把“科学主张验证”定义为独立任务，需要领域适配；它与“检索相关”不是同一件事。 | [SciFact, EMNLP 2020](https://arxiv.org/abs/2004.14974) | 规格把检索与关系核验分开是正确的；但“每个候选都上 LLM 核验”不是唯一实现，可用廉价模型做前置门控。 |
| LangChain 把 Agent 描述为 “Model + Harness”，核心是模型循环、工具和中间件；ReAct 是 reasoning/acting 交错循环。 | [LangChain agents](https://python.langchain.com/docs/concepts/agents/)、[ReAct](https://arxiv.org/abs/2210.03629) | Agent 的价值在“多步决策”；如果决策可以一次结构化调用完成，就不需要 Agent 循环。 |
| 事务性 outbox 把业务写入和事件写入放在同一数据库事务，再由独立 relay 至少一次投递，消费者必须幂等。 | [Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html) | 规格 `EVT-003` 的方向正确；但 relay 的公平调度和背压没有展开。 |

### 1.2 推断（有依据，但未在本项目测量）

- 每库全流程 Worker 的共享重排器/LLM 队列会成为新的队头阻塞点。依据：Tail at Scale 的 shared-resource/queueing 分析；规格没有定义跨库公平队列。
- 协调器内联模型调用会串行化事件。依据：规格把 Coordinator 定义为唯一权威（`FND-042`，`文献推荐工具_AGENT_SPEC.md:168`），`RANK-002` 要求先评分再插入（`:511-529`），但没有把“计算”与“提交”分开。
- 120 个候选全部核验在 90s 硬截止下容量不足。依据：`budget.global_max=120`（`:1432`）、`model.call_timeout_ms=45000`（`:1423`）、`worker.hard_deadline_ms=90000`（`:1439`）、`worker.max_concurrency=8`（`:1436`）的算术。
- Agent 若在派发前运行，会线性推迟首结果。依据：`ADR-002`（`:89`）、`FND-021`（`:67`）没有规定 Agent 的运行时机；LangChain/ReAct 的循环本质是多次模型调用。
- 固定 `keyword_top_k=50`、`vector_top_k=50` 在大型库上可能召回不足。依据：BEIR 的外分布结果与 HNSW 近似召回特性；规格只要求“记录 Recall@K”（`RET-010`，`:260`），没有给目标。
- 两级索引（论文级 + 段落级）会带来双写、双版本和双倍向量成本。依据：`ADR-005`（`:92`）、`IDX-001/002`（`:222-223`）；规格没有给出“论文级向量带来多少召回增益”的验收门槛。
- 冷启动时大量外部源会落到 `uncalibrated`，单一跨库排序可能不可用。依据：`SCR-005`（`:393-409`）要求按 source/query_type/topic 的标注校准器；没有标注就没有可比性。
- SSE 鉴权若按 Bearer header 设计，浏览器 `EventSource` 无法直接发送。依据：MDN 的 EventSource API 只有 `withCredentials`。

### 1.3 待基准测试假设（可证伪）

| 假设 | 内容 | 若成立 | 若不成立 |
|---|---|---|---|
| H1 | 共享重排池使用 per-library WFQ/DRR，可把重库负载下的轻库首结果 p95 降低 ≥5×（相对 FIFO） | 采用共享池 + 公平调度 | 保留每库独立池或提高隔离 |
| H2 | 对 top-k 候选并行/投机核验，可把“首个候选为假阳性”场景的首结果降低 ≥50% | 首结果调度器采用并行 top-k | 顺序处理足够，省略复杂度 |
| H3 | 廉价 NLI/交叉编码器前置门控 + LLM 仅处理边界样本，可降成本 ≥40% 且证据支持率下降 <2 个百分点 | 采用两级核验 | 保留 LLM 全量核验或换模型 |
| H4 | 论文级向量相对纯段落级召回提升 <2 个百分点 Recall@100 | 删除论文级向量，单索引 + 元数据 | 保留两级索引 |
| H5 | 有界规划器（1 次结构化调用，无 Agent 循环）在 50 篇语料上召回不劣于 Agent，首结果 p95 降低 ≥30% | 默认用有界规划器，Agent 仅作可选 | 保留 Agent，但移出关键路径 |
| H6 | 冷启动时跨库 RRF 回退与有标注共同重排的 nDCG@10 差距 ≤2 个百分点 | 允许 `rrf_fusion` 作为过渡 | 必须优先补标注/校准 |
| H7 | 在 >100 万段落或 >5 库时，OpenSearch/Vespa 的 p95 延迟 ≤ pgvector 的 0.7×，Recall@50 差距 ≤2 个百分点 | 采用原生平台 | 继续 Postgres 或自研轻量层 |
| H8 | 120 候选、并发 8、核验 p95 ≤3s 时，最终快照能在 90s 内完成 | 保持 120 预算 | 降低核验预算或提高并发/换模型 |

---

## 2. 基线架构：规格实际描述了什么

把规格的 ADR、§6–§9、§10.5–§10.6 串起来，当前架构是：

```text
Browser
  │  POST /searches, GET /events (SSE)
  ▼
API (FastAPI) ──► Coordinator (per query: budget, dedup, score, rank, snapshot)
                     │
                     ├─ Worker(library A): recall → RRF → rerank → evidence → verify ─┐
                     ├─ Worker(library B): recall → RRF → rerank → evidence → verify ─┤
                     └─ Worker(library C): ...                                         │
                                                                                       ▼
                                            Coordinator: idempotent ingest → dedup → score → ordered insert
                                                                                       │
                                                                                       ├─ outbox → SSE
                                                                                       └─ Snapshot → Citation/Export
```

规格明确禁止的东西（这些是优点，不应改）：

- 首条结果前等待全部 Worker 的 `gather`/`Promise.all`/join（`STR-001`，`:290`；`AGENTS.md` INV-01）。
- 固定比例 TopN（`BUD-001`，`:270`）。
- 在线批次 min-max 归一化（`SCR-007`，`:422-431`）。
- 无证据生成可粘贴引用句（`RANK-001`，`:497-509`；`AGENTS.md` INV-03）。
- 快照原地修改（`SNAP-002`，`:562`；`AGENTS.md` INV-04）。
- 实时排名直接生成数字编号（`RANK-005`，`:542`；`AGENTS.md` INV-05）。

规格没有明确规定、但决定性能与可实施性的东西：

- 共享重排器/LLM 的**跨库公平调度**；
- **核验预算**与重排预算的分离；
- **协调器计算**与**协调器提交**的边界；
- 查询编码/扩展的**跨库复用**；
- **截止时间感知**的模型调用准入；
- SSE 的**浏览器鉴权方式**；
- 外部库文本送第三方模型的**许可标志**；
- 首结果/最终结果的**数值 SLO**；
- `worker.max_concurrency=8` 与“并行启动所有符合条件的 Worker”的**准入语义**。

这些缺口正是下面 P0/P1 的来源。

---

## 3. P0 发现：按字面实现会破坏核心承诺

### P0-1 协调器内联重排/核验：把“唯一权威”误解为“唯一计算点”

**严重度**：P0（架构歧义，必须在 T07 前钉死）  
**证据**：
- `FND-042` 规定 Coordinator 是 `display_score`、`result_id`、去重、排序、覆盖和快照的唯一权威（`文献推荐工具_AGENT_SPEC.md:168`）。
- `SCR-004` 规定异构库“MUST 优先在 Worker 末端**或协调器**对每条新结果增量调用 `CommonReranker`”（`:389`）。
- `RANK-002` 要求新结果先“计算或验证统一展示分”，再有序插入，再提交内部状态（`:511-529`）。
- `API-003` 要求当前状态接口反映已提交状态，不得为了等待其他 Worker 延迟可见性（`:1013-1025`）。
- `EVT-003` 要求结果变更和 outbox 事件在同一数据库事务中写入（`:961-968`）。

**问题**：`FND-042` 的“权威”是**提交权**，不是**计算位置**。如果实现者把 `CommonReranker` 放在协调器里，并且协调器是单写者/单事件循环，那么：

- 一次 500ms 的重排调用会阻塞该查询的所有后续事件；
- 一次 45s 的模型超时会阻塞该查询 45s；
- 如果协调器进程服务多个查询，还会跨查询队头阻塞；
- 如果在数据库事务内调用模型，事务持有时间从毫秒级变成秒级，连接池和锁竞争放大。

**失败反例**：查询 Q1 的一条已核验结果到达协调器，协调器同步调用重排器 500ms；此时 Q2 的一条已核验结果也到达同一协调器。Q2 的 SSE 事件被推迟 500ms；在 10 QPS 下，事件队列开始增长，`citation_event_lag_ms` 超过阈值。规格的 `AT-STR-001` 不会发现这个问题，因为它只阻塞 Worker B，不阻塞协调器或共享重排器。

**推荐修复**（提案，P-01）：
1. 把“评分/核验计算”下沉到无状态 `ScoringPool` / `VerificationPool`，Worker 或独立 scoring service 产出带 `score_version` 的 `ScoringResult`。
2. Coordinator 只做：契约校验 → 幂等 → 去重 → 验证分数版本/可比性 → 有序插入 → outbox → SSE。**Coordinator 代码路径中禁止同步模型调用**。
3. 如果必须在 Coordinator 侧补算，先在事务外完成，使用 `pending_scoring` 状态；写事务只提交结果，不持有模型调用。
4. 用 trace 断言：`coordinator` span 中不存在 `model_call` 子 span；`citation_event_lag_ms` p95 < 50ms。

**验证**：`T-ARCH-02`（见 §8）。  
**置信度**：高（架构推断；单写者 + 同步外部调用的组合在工程上确定会串行化）。

---

### P0-2 核验预算与 90s 硬截止不匹配

**严重度**：P0（容量模型缺口，M2 前必须基准）  
**证据**：
- `BUD-001` 把 `N_i` 定义为“最多可进入统一重排和证据核验的候选数”（`:270`）。
- `BUD-004` 的全局硬上限 `budget.global_max=120`（`:1432`）。
- `model.call_timeout_ms=45000`（`:1423`）、`verification.transient_retry_limit=1`（`:1424`）。
- `worker.hard_deadline_ms=90000`（`:1439`）、`worker.max_concurrency=8`（`:1436`）。
- `RET-008/009` 要求每个拟推荐候选读取原文并完成关系核验（`:258-259`）。

**问题**：规格没有区分“重排预算”和“核验预算”。按字面，120 个候选都可能进入 LLM 核验。粗算：

```text
T_final ≈ ceil(N_verify / C) × L_verify + T_recall + T_rerank
N_verify = 120, C = 8
L_verify = 5s  → 75s
L_verify = 8s  → 120s  > 90s
L_verify = 10s → 150s  > 90s
```

若首个候选触发 45s 超时并重试一次，单个候选就消耗 90s，Worker 在硬截止时可能零结果；而检索本身可能已经成功。这不是“慢库不阻塞快库”的问题，而是**所有库都可能被核验容量拖垮**。

**失败反例**：3 个库，每库 40 个候选，共 120 个；核验 p95=8s，并发 8。理论完成时间 120s；90s 时 Worker 被硬取消，快照 `is_partial=true`，用户看到“部分覆盖”。规格的 `AT-STR-003` 只测一个 Worker 超时，不测“所有 Worker 都因核验容量不足而超时”。

**推荐修复**（提案，P-02）：
1. 引入独立配置：`verification.global_max`（建议 30–60）、`verification.per_library_max`、`verification.concurrency`、`verification.call_timeout_ms`（交互场景 5–10s）。
2. 漏斗改为：召回 `K=50+50` → RRF → 重排 `N_rerank=120` → **核验 `N_verify=30–60`**。重排分数只用于排序核验队列，不直接决定最终推荐。
3. 对 top-k 候选并行/投机核验（hedged/tied requests），首个通过即发布，取消其余；用 Tail at Scale 的 hedged request 思路控制额外负载。
4. 截止感知准入：若 `remaining_budget < p95_verify_latency + retry_budget`，不再启动新核验，标记 `skipped_deadline` 并保留诊断；不要用“启动后硬取消”浪费成本。
5. 记录 `verification_queue_depth`、`verification_wait_ms`、`verification_timeout_total`，按库和查询类型分桶。

**验证**：`T-ARCH-03`、`EXP-ARCH-03`。  
**置信度**：算术高；实际延迟分布中等（需用真实模型测 p50/p95）。

---

## 4. P1 发现：共享资源、首结果路径与可实施性

### P1-1 每库全流程 Worker 的共享资源队头阻塞

**严重度**：P1（核心首结果承诺在负载下可能失效）  
**证据**：
- `ADR-003` 要求每库独立 Worker、逐条/小批发送、任何全局步骤不得等待全部库（`:90`）。
- `STR-001` 要求并行启动独立 Worker、逐条接收、禁止 join/Promise.all 屏障（`:290`）。
- `STR-002` 允许 Worker 内部逐条或小批处理（`:291`）。
- §8 没有定义跨 Worker 的共享重排器/LLM 队列、公平调度或每库并发上限。

**问题**：ADR-003 的“独立”是**逻辑隔离**，不是**物理隔离**。所有 Worker 仍然共享：

- 同一个 `CommonReranker`（CPU/GPU）；
- 同一个 LLM provider（速率限制、连接池）；
- 同一个 Postgres（连接、锁、outbox）；
- 同一个对象存储（PDF/段落读取）；
- 同一个协调器事件循环。

如果共享重排器或 LLM 客户端是 FIFO，重库的一次大批量会排在轻库的单条前面。规格的 `AT-STR-001` 只阻塞 B 的 Worker，A 的结果仍然先到；但如果 A 的 120 条重排把共享池占满，B 的 1 条仍要排队。测试通过，用户体验失败。

**失败反例**：库 A 一次提交 120 个重排请求，库 B 提交 1 个；重排器每条约 50ms，FIFO。B 的首条结果等待 120×50ms=6s；若重排器每条约 200ms，等待 24s。换成 LLM 核验，120 条 × 5s / 8 并发 = 75s，B 的单条可能排到 70s 之后。

**推荐修复**（提案，P-04）：
1. 共享池使用 per-library **加权公平队列**（WFQ/DRR）或至少轮询准入；给“首结果候选”更高优先级（deadline class），给“补充候选”较低优先级。
2. 每库并发上限 + 全局并发上限；不要让一个库占满池。
3. 每库/每 provider 熔断：一个库的模型调用连续失败/超时，只降级该库，不拖垮其他库。
4. 若坚持每库独立进程，也必须在共享客户端前加一个轻量调度器；否则“独立 Worker”只是逻辑假象。

**验证**：`T-ARCH-01`、`EXP-ARCH-01`。  
**置信度**：机制高；幅度中（取决于池实现和负载）。

---

### P1-2 首条结果的串行核验路径

**严重度**：P1（首结果延迟可能随假阳性数量线性增长）  
**证据**：
- `STR-002` 要求每个候选完成核验后立即发布（`:291`）。
- `RET-009` 要求 `verify_relationship` 通过后才可发出合格推荐（`:259`）。
- §11 EVD 没有要求对 top-k 候选并行核验；`worker.max_concurrency` 是库级并发，不是候选级。

**问题**：Worker 顺序处理候选时，首条合格结果的时间 = 前 k 个候选的核验时间之和。如果第 1、2 个候选是 `insufficient`，每个 8s，第 3 个才 verified，首结果 24s。规格的“逐条发布”只保证“完成一个发一个”，不保证“尽快完成第一个”。

**失败反例**：top-5 中第 1、2 个是主题相似但关系不足，第 3 个是真正支持。顺序核验：8s + 8s + 8s = 24s；并行 top-3：max(8s) = 8s。用户感知差距 3×。

**推荐修复**（提案，P-05）：
1. Worker 内部对 top-k 候选做有界并行核验（例如 k=3–5），首个 `verified` 立即发布，取消其余；保留 `candidate_parallelism` 配置。
2. 先做廉价前置门控（交叉编码器/NLI/规则），只把高分候选送 LLM；降低单次核验成本和假阳性等待。
3. 对“首结果候选”使用 hedged request：主请求 + 95 分位后发副本，取先返回者，取消另一个；控制额外负载 ≤5%。
4. 事件中保留 `candidate_attempts` 诊断，避免并行导致重复发布。

**验证**：`T-ARCH-04`、`EXP-ARCH-04`。  
**置信度**：机制高；质量/成本权衡需测。

---

### P1-3 每库重复查询编码/扩展

**严重度**：P1（成本与首结果延迟的线性放大）  
**证据**：
- `ADR-003` 每库独立 Worker（`:90`）。
- `RET-002` 每库独立执行关键词/向量召回（`:239`）。
- `FND-023` 禁止 LLM 全库扫描，但允许查询规划（`:71`）。
- 规格没有定义共享的 `QueryBundle` 或查询编码缓存。

**问题**：同一个查询在 N 个库上被 N 次嵌入、N 次改写、N 次构造过滤器。10 个库就是 10× 嵌入成本和 10× 查询扩展 LLM 调用；更糟的是，各 Worker 可能得到细微不同的查询表示，破坏“同口径”评分的前提（`SCR-003`，`:374-385`）。

**失败反例**：10 个库，查询嵌入 200ms，LLM 查询扩展 2s。若每个 Worker 各做一次，首结果前多出 2s+ 的重复工作；若扩展结果不一致，跨库分数不可比。

**推荐修复**（提案，P-03）：
1. 增加 `QueryBundle`：规范化查询、原始精确查询、扩展查询、查询向量（可多组）、硬过滤、模型版本、`query_bundle_id`。
2. 在 Coordinator/Query Service 计算一次，Worker 只消费不可变 bundle；缓存键 = `(query_text, input_mode, model_version, expansion_policy_version)`。
3. 查询扩展失败时回退到原始查询（`RET-001` 已要求），不阻塞派发。
4. 在事件/快照中记录 `query_bundle_id`，保证可复现。

**验证**：`T-ARCH-05`。  
**置信度**：高。

---

### P1-4 单 Agent 在首结果关键路径

**严重度**：P1（首结果延迟与可测试性）  
**证据**：
- `ADR-002` 冻结 LangChain 单 Agent + Citation Skill + 确定性工具（`:89`）。
- `FND-021` 说 Skill 引导 Agent 的查询规划、补充检索和停止判断（`:67`）。
- `FND-022` 说运行时首期使用单 Agent 加并行 Worker（`:69`）。
- 规格没有规定 Agent 必须在派发前完成，也没有规定最大工具调用次数/超时。

**问题**：如果 Agent 在派发前运行（选库、生成查询、分配预算），首结果要等 Agent 的模型循环。LangChain Agent 的本质是 “Model + Harness”，ReAct 是 reasoning/acting 交错循环；每次工具调用都可能增加 1–5s。若 Agent 运行 8s，首结果 8s + 3s = 11s；默认派发只要 3s。

此外，Agent 的随机性会让 `AT-STR-001` 等流式测试难以稳定复现；规格要求确定性测试替身（`QAL-001`，`:1506`），但 Agent 的自由文本决策天然不稳定。

**推荐修复**（提案，P-06）：
1. **默认路径不依赖 Agent**：收到查询后立即按默认库/默认预算派发；查询扩展和预算调整异步进行，不阻塞首结果。
2. 把 Agent 降级为**有界规划器**：最多 1 次结构化 LLM 调用（JSON Schema），超时 2s，失败回退原始查询；不允许无限工具循环。
3. 若保留 Agent，限制 `max_tool_calls`、`max_planning_ms`，并只允许它在首结果之后追加查询/库/停止；不得撤销已发布结果。
4. Skill 仍然是策略文档和行为约束，不必绑定 LangChain 运行时；可以用普通 prompt + JSON Schema 实现。

**验证**：`T-ARCH-06`、`EXP-ARCH-05`。  
**置信度**：延迟高；召回收益中（需 H5）。

**注意**：这触及 `ADR-002` 与 `FND-021/022` 的“单 Agent + LangChain”表述。本报告只提案，不修改用户需求；若用户坚持 Agent，至少把它移出关键路径。

---

### P1-5 Worker 并发/准入语义矛盾

**严重度**：P1（覆盖公平性与规格自洽）  
**证据**：
- `STR-001` 要求“并行启动符合条件的独立库 Worker”（`:290`）。
- `worker.max_concurrency=8` 是“单协调器 Worker 并发上限”（`:1436`）。
- `BUD-004` 有 per-library min/max 和 global max（`:273`）。
- `Coverage` 有 `pending_library_ids`（`CON-150`，`:840-880`）。

**问题**：如果有 10 个库，`max_concurrency=8`，只有 8 个能启动；哪 2 个等待？规格没有定义准入策略。FIFO 会让后两个库在慢库释放槽位前一直 pending；它们的预算被分配却未使用。`STR-001` 的“并行启动所有符合条件的 Worker”与 `max_concurrency=8` 在字面上冲突，除非把“启动”解释为“准入”。

**失败反例**：10 个库，前 8 个都是慢库，软截止 60s；库 9/10 一直 pending，直到 60s 才有槽位，但此时已经接近截止；它们的探索配额从未被使用，覆盖显示 pending，用户可能误以为“没有相关文献”。

**推荐修复**（提案，P-07）：
1. 明确 `worker.max_active_libraries` 与 `worker.max_concurrency` 的区别：前者是同时运行的库数，后者是候选级并发。
2. 准入使用轮询/DRR，保证每个库在一个调度周期内至少获得一次机会；不要 FIFO。
3. `pending` 库保留预算，但不占用全局候选预算；在快照中记录 `pending_reason=admission_queue`。
4. 若库数经常大于并发上限，考虑共享池 + lane，而不是增加进程数。

**验证**：`T-ARCH-07`。  
**置信度**：高（规格歧义）。

---

### P1-6 截止时间不感知的模型调用

**严重度**：P1（浪费成本、零结果超时）  
**证据**：
- `STR-007` 软截止后停止启动新高成本候选，硬截止协作取消（`:303`）。
- `model.call_timeout_ms=45000`（`:1423`）、`worker.hard_deadline_ms=90000`（`:1439`）、`verification.transient_retry_limit=1`（`:1424`）。

**问题**：Worker 可能在剩余 5s 时启动一个 45s 的模型调用；硬截止一到，调用被取消，成本已发生，结果为零。重试一次更糟：90s 预算被一个候选吃掉。

**失败反例**：软截止 60s 时队列里还有 10 个候选；Worker 逐个启动 45s 调用；硬截止 90s 时只完成了 1–2 个，其余全部 `timed_out`；快照部分覆盖，用户看到“检索失败”而非“预算不足”。

**推荐修复**（提案，P-08）：
1. 准入控制：`remaining_ms > p95_call_latency + retry_budget` 才启动；否则标记 `skipped_deadline`。
2. 每个候选有 `candidate_deadline`，到点取消；不要让单个候选吃掉库预算。
3. 交互式核验使用 5–10s 超时；45s 只用于后台/全文深度分析。
4. 重试只在“剩余预算足够 + 错误可重试”时进行；`transient_retry_limit=1` 不应无条件执行。

**验证**：`T-ARCH-08`。  
**置信度**：高。

---

### P1-7 SSE 鉴权实现缺口

**严重度**：P1（安全 + 可实施性）  
**证据**：
- `API-004` 要求 `text/event-stream`、禁用代理缓冲、支持 `Last-Event-ID`/`after_sequence`，建立 SSE 前完成权限和 replay 窗口校验（`:1027-1034`）。
- `SEC-006` 要求 SSE/WebSocket 复用对象级授权和租户隔离，禁止仅持有 `query_id` 就无限期读取（`:1391`）。
- 规格没有说明浏览器如何把凭据放进 SSE 请求。

**问题**：标准 `EventSource` 不能设置 `Authorization: Bearer` 头；它只支持 `withCredentials`（Cookie）。如果 API 统一要求 Bearer header，浏览器 SSE 会 401 并自动重连。把 token 放 URL query 又会进入访问日志、Referer、浏览器历史，违背 `SEC-004` 的“不记录令牌”。

**失败反例**：前端 `new EventSource('/api/v1/searches/q/events')`，后端中间件要求 `Authorization`；浏览器无法发送，连接 401；`EventSource` 自动重试，形成 401 风暴；日志里出现 `?token=...`。

**推荐修复**（提案，P-09）：
1. 使用 HttpOnly、Secure、SameSite 的会话 Cookie 做 SSE 鉴权；配合 CSRF 防护（SameSite + 自定义头用于非 SSE 请求）。
2. 或签发一次性、短 TTL 的 `stream_ticket`，放在 URL；服务端一次性校验并在日志中脱敏；ticket 绑定 `query_id` + 用户 + 租户。
3. 或前端使用 `fetch` + `ReadableStream` 解析 SSE，这样可以带 Authorization 头；代价是不能用原生 `EventSource` 的自动重连，需要自己实现 `Last-Event-ID`。
4. 明确 HTTP/2 部署；HTTP/1.1 下每域 6 个连接，限制多标签页/多查询并发。

**验证**：`T-ARCH-09`。  
**置信度**：高（API 事实）。

---

### P1-8 外部库文本送统一重排的授权/合规缺口

**严重度**：P1（合规风险）  
**证据**：
- `SCR-004` 说“只要外部库能够提供标题、摘要和证据文本，就 MUST 优先在 Worker 末端或协调器调用 `CommonReranker`”（`:389`）。
- `SEC-003` 要求外部模型数据最小化，部署方必须使用符合数据处理约定的模型服务（`:1375-1380`）。
- 规格没有定义每个来源的 `model_use_allowed` / `redistribution_allowed` / `retention_allowed` 标志。

**问题**：外部库的 API 条款可能允许检索，但不允许把摘要/证据发送给第三方模型，或不允许长期保留。`SCR-004` 的 MUST 会让实现者无条件送文本，造成许可违约或数据跨境问题。

**失败反例**：某出版社 API 返回摘要，条款禁止将内容发送给第三方 LLM；系统为了“统一重排”把摘要送给外部 reranker；合规审计发现违约。

**推荐修复**（提案，P-10）：
1. 在 `library_adapters` 契约中增加来源策略：`model_use_allowed`、`redistribution_allowed`、`retention_allowed`、`allowed_model_regions`。
2. 策略为 false 时，改用本地模型/来源校准/仅用书目字段排序；不得静默送外部模型。
3. 在结果/快照中记录 `score_kind` 和 `policy_decision`，便于审计。
4. 法务确认前，外部源默认 `model_use_allowed=false`，而不是默认允许。

**验证**：`T-ARCH-10`。  
**置信度**：合规风险高；具体条款需法务确认。

---

### P1-9 固定 top_k 缺乏召回目标

**严重度**：P1（质量风险，需基准）  
**证据**：
- `RET-003` 要求关键词/向量 TopK 分别由 `keyword_top_k`、`vector_top_k` 控制（`:240`）。
- 默认值各 50（`:1430-1431`；CFG-001 明确这是开发起始值，不是生产质量承诺，但规格没有给生产目标）。
- `RET-010` 要求分阶段评估 Recall@K，但没有目标值（`:260`）。
- BEIR 显示 BM25 稳健、dense 在外分布上可能落后；混合召回有帮助，但 K 太小仍会漏。

**问题**：50+50 的候选集在大型库上可能召回不足；正确论文若不在候选集，后续重排/核验无法补救。规格的 `AT-EVAL-001` 只要求“记录 Recall@K”，没有“达到多少才算通过”。

**失败反例**：100 万段落库，100 条标注查询；关键词 Recall@50=0.62，向量 Recall@50=0.55，并集 Recall@50=0.71；正确论文 29% 在候选集外。最终推荐质量被候选召回上限锁死。

**推荐修复**（提案，P-11）：
1. 建立 100–200 条标注查询（可用 50 篇 PDF + 合成/公开集扩充），测每条路由与并集的 Recall@50/100/200。
2. 设定目标：候选并集 Recall@100 ≥0.90（示例，不是用户需求）；按库规模/查询类型自适应 K。
3. 记录每路召回深度、RRF 窗口、HNSW 参数；不要只报告最终 LLM 质量。
4. 若 K 增大导致成本，使用“召回层大 K + 重排层小 N”的漏斗，而不是把大 K 直接送 LLM。

**验证**：`T-ARCH-11`、`EXP-ARCH-07`。  
**置信度**：中（需实测）。

---

### P1-10 缺少数值化 SLO

**严重度**：P1（无法作为交付门禁）  
**证据**：
- `OBS-002` 定义了 `citation_first_verified_latency_ms`、`citation_first_citable_latency_ms`、`citation_search_finalize_latency_ms` 等指标（`:1457-1470`）。
- `AT-EVAL-005` 要求分别报告首条推荐、首个可导出快照、最终快照的时间（`:1599`）。
- `DOD-002` 要求流式成立（`:1675`），但没有数值目标。
- `CFG-001` 的 60s 软截止/90s 硬截止是超时上限，不是首结果 SLO。

**问题**：没有 p50/p95 目标，性能回归无法阻断交付；团队可能用“平均 2s”掩盖 p95 20s。`AT-EVAL-005` 只要求“报告”，不要求“通过”。

**推荐修复**（提案，P-12）：
1. 定义分层 SLO（示例，需用户确认）：50 篇 PDF/3 库，首条 verified p50 ≤3s、p95 ≤8s；首条 citable p95 ≤15s；最终快照 p95 ≤30s。
2. 按库数、候选预算、输入类型分桶；不把不同规模混在一起平均。
3. 在 CI 中用可控 Worker/假模型跑基线，超过预算即失败；真实模型基准单独跑，标记环境。
4. 指标必须用单调时钟；首条指标不能被长尾平均。

**验证**：`T-ARCH-12`、`EXP-ARCH-11`。  
**置信度**：高（规格缺口）。

---

## 5. P2 发现：过度设计、可维护性与规模化

### P2-1 两级索引可能重复

**证据**：`ADR-005`（`:92`）、`IDX-001/002`（`:222-223`）。  
**问题**：论文级索引 + 段落级索引意味着双写、双向量、双版本、双重建。论文级向量的收益是“标题摘要相关但段落不相关”的宏观召回；但一个段落级索引 + 论文元数据 + 聚合（best passage、命中段落数）通常也能覆盖。  
**失败反例**：重索引时论文级向量与段落级向量版本不一致；查询绑定 `index_snapshot` 后，两路结果来自不同版本，`score_version` 不可复现。  
**推荐修复**：M1 先用单一段落索引 + 论文元数据；用 `EXP-ARCH-14` 测“论文级向量是否带来 ≥2 个百分点 Recall@100”；若不显著，删除论文级向量，保留论文级摘要字段用于展示。  
**代价**：若论文级向量确实提升宏观召回，单索引会损失少量召回；用实验决定。  
**置信度**：中。

### P2-2 统一分数/校准冷启动过重

**证据**：`ADR-007`（`:94`）、`SCR-004/005/006`（`:387-420`）、`SCR-007`（`:422-431`）。  
**问题**：跨源校准需要 `source × query_type × topic` 的人工标注；冷启动时大量来源 `uncalibrated`，单一跨库排序不可用。规格禁止在线 min-max，这是对的；但也禁止 RRF 作为跨库展示分（`RET-006` 说 RRF 只用于库内，`:243`）。结果是：没有标注就没有统一分，产品承诺的“一个列表”在冷启动时退化为分组。  
**推荐修复**（提案，P-13，涉及 ADR-007/INV-02）：增加 `score_kind=rrf_fusion` 的过渡模式：用一个**离线固定的单调映射**把 RRF 分数映射到 `0..100`（不得使用当前批次 min-max），`calibration_status=bootstrap`，`comparability=ordinal`；只用于排序，不宣称基数可比。或采用“证据层优先 + 层内 RRF”的分层列表。  
**代价**：RRF 丢失分数间距；可能与“统一展示分”的字面要求冲突。需要用户批准契约变更。  
**置信度**：中；需 H6。

### P2-3 引用句预生成

**证据**：`citation.generation_top_m=20`（`:1413`）；`CIT-004` 要求按需生成（`:1254-1258`）。  
**问题**：top 20 会随迟到高分/合并而变；预生成的引用句可能立即失效，浪费 token；若用户未展开，纯浪费。  
**推荐修复**：默认按需生成；缓存键 `(result_id, result_version, claim_id, style, generator_version)`；只有在 top-M 稳定 X 秒或用户展开时才预取。  
**验证**：`EXP-ARCH-13`。  
**置信度**：高。

### P2-4 协调器单写吞吐与事件体积

**证据**：`RANK-002`（`:511-529`）、`EVT-003`（`:961-968`）。  
**问题**：每个事件一个事务 + outbox；`recommendation_reranked.changes` 在一次插入靠近顶部时可能包含 O(N) 个排名变化；120 条结果、120 次插入，最坏约 1.4 万条变更项。SSE 和 DB 都可能被放大。  
**推荐修复**：按 `query_id` 分区/单写 actor；group commit；`changes` 只发窗口或让客户端按 `sequence` 重排；对 1000 条结果做压力测试。  
**置信度**：中。

### P2-5 快照哈希规范化

**证据**：`CON-160` 的 `immutable_hash` 要求“键名递归排序、无多余空白”（`:855-880`）；`SNAP-002`（`:562`）。  
**问题**：键排序 + 无空白不是完整的规范 JSON；数字（`1` vs `1.0`）、Unicode 归一化、转义、负数零在不同语言可能不同，导致跨语言哈希不一致。  
**推荐修复**：采用 RFC 8785 JCS 或 canonical CBOR；在 Python/Node 两端做同一快照哈希测试。  
**置信度**：高。

### P2-6 微服务骨架过早

**证据**：§4.1 推荐 `apps/web`、`apps/api`、`services/coordinator`、`services/worker`、`services/indexer`、`services/citation`（`:131-160`）；`ADR-001`（`:88`）。  
**问题**：50 篇 PDF 的 M0–M1 不需要 4 个服务；进程边界会增加部署、日志、契约、联调、故障注入成本。规格的 M0 只要求“API/Worker/Web 可启动骨架”，没有要求物理拆分。  
**推荐修复**（提案，P-14）：M0–M1 用模块化单体：一个 FastAPI 进程 + Postgres/pgvector + 进程内 lane + outbox；M2 再把重排/核验池拆出；M4 再拆索引/引文服务。§4.1 本身允许“实现 MAY 调整物理目录”，只要保持依赖与权属；因此这不违反现有条款。保持 `contracts` 包和依赖方向不变。  
**边界说明**：如果规格维护者把 `ADR-001` 的“独立 Worker”解释为**必须独立进程**，则本提案需要批准；若“独立”指逻辑任务隔离，则模块化单体不改变 ADR 语义。  
**代价**：后期拆分有成本；但比一开始维护 6 个进程低。  
**置信度**：高（工程判断）。

### P2-7 SSE HTTP/1.1 连接上限

**证据**：`API-004`（`:1027`）；MDN。  
**问题**：HTTP/1.1 下每浏览器/域 6 个连接；多标签页/多查询会耗尽；SSE 长连接会占用连接。  
**推荐修复**：HTTP/2；一个页面最多一个活动查询流；查询结束后关闭；断线重连用 `Last-Event-ID`；不要用多个 EventSource 订阅同一查询。  
**置信度**：高。

### P2-8 队列/背压/取消通道

**证据**：`STR-006` 有界队列与背压（`:295`）；`worker.queue_capacity=500`（`:1440`）。  
**问题**：如果 Worker 在“队列满”时阻塞式入队，它可能无法及时处理取消/截止；控制面（cancel）和数据面（events）共用队列会互相阻塞。  
**推荐修复**：数据面非阻塞入队 + deadline；控制面独立通道；每查询 outbox 分区；取消优先于新事件。  
**置信度**：中。

---

## 6. 方案比较：每库全流程 Worker、共享分阶段池、原生平台、单 Agent

### 6.1 四种形态的直接比较

| 维度 | 每库全流程 Worker（规格现状） | 共享分阶段处理池 | 原生搜索平台 + 薄协调器 | 单 Agent / LangChain |
|---|---|---|---|---|
| 隔离性 | 好：单库失败不影响其他库 | 中：逻辑 lane + 公平队列；需要熔断/配额 | 取决于平台：index/tenant 隔离 | 差：Agent 是单点，工具循环可能拖垮全查询 |
| 首结果延迟 | 取决于最快 Worker，但受共享 FIFO 影响 | 可做到最好：首结果候选优先调度 | 好：平台内并行/部分结果；但 `msearch` 是屏障 | 最差：规划在派发前会线性推迟 |
| 吞吐/GPU 利用 | 差：每库重复查询编码，难以批处理 | 好：共享重排/核验池可批处理 | 好：平台优化 | 差：LLM 循环串行 |
| 成本 | 高：N× 查询编码 + 120 次核验 | 中：共享缓存/批处理，可去重前核验 | 中：平台许可/运维成本 | 高：多轮工具调用 |
| 复杂度 | 低–中：结构直观 | 高：调度、公平、取消、租约 | 中–高：平台运维/迁移/许可 | 高：Agent 可观测/测试/提示注入 |
| 失败模式 | 共享池队头阻塞；重复计算 | 跨库耦合；调度 bug；公平性 | 平台故障面；锁死；许可 | 不确定性；工具循环；延迟尖刺 |
| 适合阶段 | ≤2 库、低 QPS、M1 | ≥3 库、共享 GPU/LLM、M2–M4 | >100 万段落、>5 库、需要原生部分结果 | 复杂查询的补充层，不是默认路径 |

### 6.2 替代架构 A：库级召回 lane + 全局共享池（推荐，条件采用）

**核心思想**：把“每库全流程 Worker”拆成“每库召回 lane + 全局共享重排/核验池 + 协调器只提交”。它保留 ADR-003 的逻辑隔离和覆盖语义，但把昂贵的共享阶段集中调度。

```text
Query Service (once)
  └─ QueryBundle: normalized query + expansions + embeddings + filters + versions
       │
       ├─ Lane A: recall → RRF → candidate descriptors ─┐
       ├─ Lane B: recall → RRF → candidate descriptors ─┤
       └─ Lane C: ...                                    │
                                                          ▼
                         Candidate Registry (DOI dedup + single-flight verification lease)
                                                          │
                         ┌────────────────────────────────┼──────────────────────────────┐
                         ▼                                ▼                              ▼
                 Rerank Pool (shared, batched,      Verification Pool (shared,     Coordinator
                 per-library WFQ/DRR, deadline      per-library caps, EDF,         (validate score,
                 classes)                           cheap gate → LLM)              dedup, insert, outbox, SSE)
```

**关键机制**：
- Lane 只做“便宜召回 + 候选描述”，不直接调用 LLM；首结果候选可以打 `priority=first_result` 标签。
- Candidate Registry 用 DOI 做单飞核验租约：同一 DOI 只让一个 lane 做昂贵核验，其他 lane 附加来源；租约失败则接管，避免重复成本。
- Rerank Pool 批处理 query-doc 对，按库 WFQ/DRR 调度；首结果候选优先。
- Verification Pool 使用 EDF（earliest deadline first）+ 每库并发上限 + 熔断；廉价 NLI/交叉编码器前置，LLM 只处理边界。
- Coordinator 不调用模型；只做确定性校验/去重/插入/outbox。

**与当前方案相比的收益**：
- 首结果不再被重库的批处理排在后面；共享池可设优先级。
- 查询编码只做一次；重排/核验可批处理，GPU 利用率更高。
- 同 DOI 只核验一次；跨库证据合并更早。
- 全局容量可控：池的并发、队列深度、每库配额是显式的。

**代价**：
- 调度器复杂度上升；需要公平性、饥饿、取消、租约、熔断的测试。
- 库级隔离从“进程隔离”变成“逻辑 lane + 配额隔离”；需要防止一个库的异常拖垮池。
- 与 ADR-003 的“每库独立 Worker”字面有张力；需要把 Worker 重新定义为“逻辑执行 lane”，或保留进程但共享池客户端加调度器。

**采用条件**：
- 库数 ≥3，或观察到共享重排/LLM 队列导致的首结果 p95 超过基线 2×；
- 有共享 GPU/模型配额，批处理收益明显；
- 团队能维护调度器/租约/公平性测试。
- 若只有 1–2 个库、50 篇 PDF、低 QPS，保持现状更简单。

**不改变的不变量**：事件契约、快照、证据门槛、编号分离都不变；只改执行结构。

### 6.3 替代架构 B：原生搜索平台 + 薄协调器（规模化时采用）

**核心思想**：把检索、混合召回、RRF、分阶段重排下沉到 OpenSearch/Vespa/Elasticsearch，Coordinator 只做证据核验、去重、快照、引用和事件。

**形态**：
- 每个库一个 index（或 `library_id` 字段 + routing）；`indexer` 写入平台；`library_adapters` 统一查询接口。
- 混合召回用平台原生 hybrid/RRF；重排用平台 rescore/global-phase，或外部 reranker。
- 部分结果用平台 async search（Elasticsearch 已确认；OpenSearch 需按版本验证）或每库独立查询 + 应用层合并；**不要用 `msearch` 做流式**，因为它是响应级屏障。
- 证据锚点、`source_trace_valid`、快照、引用仍由自有服务处理；平台只负责召回/排序。

**收益**：
- 不重复造 shard、倒排、HNSW、RRF、分阶段排序、部分结果。
- 平台提供运维工具、副本、快照、监控；Vespa 的 global-phase 可直接跑 ONNX cross-encoder。
- 对 >100 万段落/多库场景，通常比自研更稳。

**代价**：
- 平台运维/许可/锁定；Elasticsearch 的许可需法务确认；OpenSearch Apache-2.0 更宽松。
- 每库预算/公平调度仍要在应用层做；平台不解决跨库公平。
- 证据锚点/逐字核验仍需自有存储；平台文档存储不是证据真源。
- 迁移成本和双写期。

**采用条件**：
- `EXP-ARCH-10` 显示在目标规模下 Recall@50 差距 ≤2 个百分点，p95 延迟 ≤ pgvector 的 0.7×；
- 或团队没有搜索基础设施经验，宁可买平台；
- 或需要原生部分结果/分阶段排序/GPU 重排。

### 6.4 替代架构 C：模块化单体起步（M0–M1 推荐）

**核心思想**：一个 FastAPI 进程 + Postgres + pgvector + FTS + 进程内 lane + outbox；不引入 Redis/队列/独立 Worker 进程。

**收益**：
- 50 篇 PDF 的 M1 可以在数周内跑通；调试、事务、契约一致更简单。
- Postgres 的 ACID/事务/outbox 天然适合快照和幂等；pgvector 支持 HNSW/IVFFlat 与 FTS hybrid。
- 保留 `contracts` 包和依赖方向，后续拆分不改契约。

**代价**：
- 单进程 CPU/内存上限；长模型调用会占用事件循环（用线程/进程池隔离）。
- 后续拆分有迁移成本；需要在 M2 前把 lane/pool 接口留好。

**采用条件**：M0–M1；库数 ≤2；语料 ≤10 万段落；QPS 低。到 M2 再用 `EXP-ARCH-10` 决定是否拆共享池/平台。

### 6.5 替代架构 D：有界规划器替代 Agent 循环（提案，需批准）

**核心思想**：把“查询规划”做成一次结构化 LLM 调用（或规则 + 小模型），输出 `QueryPlan`：原始查询、扩展、库选择、预算、停止策略；失败回退原始查询。Agent 只在首结果之后作为“补充检索顾问”，可选。

**收益**：
- 首结果不被多轮规划阻塞；测试可复现；token 成本可控。
- Skill 仍是策略文档，不依赖 LangChain 运行时。
- 对 50 篇 PDF 的简单查询，规则/一次调用足够。

**代价**：
- 复杂全文查询的适应性可能下降；需要 H5 验证。
- 与 `ADR-002`/`FND-021/022` 的“单 Agent + LangChain”字面冲突；必须用户批准。

**采用条件**：
- H5 成立（召回不劣、首结果 p95 降低 ≥30%）；
- 或团队无法稳定测试 Agent 行为；
- 若 Agent 在复杂查询上召回提升 ≥5 个百分点，保留 Agent，但移出关键路径。

---

## 7. 技术选型矩阵

| 方案 | 混合召回 | 部分/流式结果 | 分阶段重排 | 多租户隔离 | 运维/许可 | 适用阶段 | 结论 |
|---|---|---|---|---|---|---|---|
| **Postgres + pgvector + FTS** | BM25（`ts_rank`）+ 向量，SQL 层 RRF | 无原生；应用层逐条发 | 外部 reranker | schema/RLS/分区 | 低；PostgreSQL 许可 | M0–M1 | **首选起步**；50 篇 PDF 足够 |
| **OpenSearch** | hybrid query + normalization/RRF（2.10/2.19） | `msearch` 是屏障；async 需版本验证 | 搜索管道/外部 | index/DLS | 中；Apache-2.0 | M2–M3 | **优先候选**；许可宽松 |
| **Elasticsearch** | RRF retriever | async search 部分结果（已确认） | rescore/外部 | index/DLS | 中；Elastic License 2.0/SSPL | 需要 async search 且许可可接受 | 功能强，许可需法务 |
| **Vespa** | BM25 + 向量 + WAND | 分阶段/global-phase；流式需验证 | 原生 first/second/global，ONNX/GPU | 需验证 | 高；Apache-2.0 | 大规模/GPU 重排 | **重排密集型首选**；学习曲线陡 |
| **Qdrant** | dense+sparse+RRF/DBSF | 无原生 BM25 排序；用 sparse | 外部 | collection/payload | 中；Apache-2.0 | 向量优先 | 若 BM25 非硬需求可考虑 |
| **自研 Lucene/Tantivy** | 完全可控 | 自研 | 自研 | 自研 | 高 | 极端定制 | 不建议 v1 |

**选型结论**：
1. M0–M1 不要自研搜索引擎；Postgres + pgvector + FTS 足够，且事务/快照/outbox 简单。
2. M2 前跑 `EXP-ARCH-10`：同一 50 篇语料 + 100 条查询，比较 Postgres 与 OpenSearch；用 Recall@10/50 和 p95 延迟决定。不要在没数据前选“看起来更强大”的平台。
3. 如果核心差异是 cross-encoder 重排/GPU，Vespa 的 global-phase 比“自研 + 外部 reranker”更省集成；但运维成本高。
4. 如果许可敏感，优先 OpenSearch；Elasticsearch 的 async search 很好，但许可需要法务确认。
5. Qdrant 适合向量优先；规格要求 BM25 关键词召回，Qdrant 需要 sparse vector 模拟，需额外验证。

---

## 8. 失败反例与测试清单

以下测试是**提案**，用于覆盖规格现有 `AT-STR-001` 看不到的共享资源/容量问题。测试 ID 不与现有 `AT-*` 冲突；命名可按 `T-ARCH-*` 登记到追踪矩阵（需规格维护者批准）。

| 测试 ID | 场景 | 断言 | 对应规格 | 严重度 |
|---|---|---|---|---|
| `T-ARCH-01.shared_pool_fairness` | 库 A 提交 120 个重排，库 B 提交 1 个；共享池 FIFO vs WFQ | WFQ 下 B 首结果 p95 ≤1s；无库饥饿超过 1 个软截止 | `ADR-003`、`STR-001`、`STR-002` | P1 |
| `T-ARCH-02.coordinator_no_model` | 注入 500ms 重排服务；20 个并发查询 | Coordinator trace 无模型调用；事件循环 lag p95 <50ms；Q2 事件不被 Q1 重排阻塞 | `FND-042`、`RANK-002`、`SCR-004` | P0 |
| `T-ARCH-03.verification_capacity` | 120 候选、并发 8、核验 p95 5s/8s | 5s 时最终 ≤90s；8s 时显式降级/部分覆盖，不静默零结果 | `BUD-001`、`CFG-001` | P0 |
| `T-ARCH-04.parallel_first_k` | top-5 中前 2 个 8s 返回 `insufficient`，第 3 个 verified | 并行 top-3 首结果 ≤10s；顺序 ≥24s | `STR-002`、`RET-009` | P1 |
| `T-ARCH-05.query_bundle_reuse` | 10 个库、1 个查询 | 查询嵌入/扩展只调用 1 次；所有 Worker 使用同一 `query_bundle_id` | `ADR-003`、`RET-002` | P1 |
| `T-ARCH-06.agent_fast_path` | 规划器延迟 0/3/10s | 默认派发立即发生；首结果延迟差 ≤0.5s；Agent 只追加不阻塞 | `ADR-002`、`FND-021` | P1 |
| `T-ARCH-07.admission_fairness` | 10 库、`max_concurrency=8`、前 8 慢 | 库 9/10 在 1 个软截止内获得槽位；覆盖不长期 pending | `STR-001`、`CFG-001` | P1 |
| `T-ARCH-08.deadline_admission` | 剩余 5s，p95 调用 10s | 不启动新调用，标记 `skipped_deadline`；不产生 45s 挂起 | `STR-007`、`CFG-001` | P1 |
| `T-ARCH-09.sse_auth` | 浏览器 EventSource + Cookie；无 Cookie；Bearer header | Cookie 可连接；无凭据 401；Bearer-only 不导致 401 风暴；token 不入日志 | `API-004`、`SEC-006` | P1 |
| `T-ARCH-10.source_model_policy` | 外部源 `model_use_allowed=false` | 不调用外部 reranker/LLM；走本地/校准；记录 policy | `SCR-004`、`SEC-003` | P1 |
| `T-ARCH-11.recall_budget` | 100 条标注查询、1M 段落 | 并集 Recall@100 ≥0.90；K=100 比 50 提升 ≥5 个百分点 | `RET-003`、`RET-010` | P1 |
| `T-ARCH-12.slo` | 50 PDF/3 库/20 查询 | 首 verified p50 ≤3s、p95 ≤8s；final p95 ≤30s（示例目标） | `OBS-002`、`AT-EVAL-005` | P1 |
| `T-ARCH-13.dedup_singleflight` | 3 库同 DOI | 只核验 1 次；3 个来源合并；1 张卡片 | `DED-001`、`DED-004` | P2 |
| `T-ARCH-14.snapshot_hash` | Python/Node 同一快照 | `immutable_hash` 一致 | `CON-160`、`SNAP-002` | P2 |
| `T-ARCH-15.citation_cache` | top 20 预生成 vs 按需 | 按需成本 ≤50%；失效版本不展示 | `CIT-004`、`CFG-001` | P2 |
| `T-ARCH-16.event_backpressure` | 队列满 + 取消 | 取消仍被处理；无全局死锁；事件不丢 | `STR-006`、`STR-008` | P2 |

**说明**：上表是测试设计，不是已执行结果。当前仓库没有源码/测试入口；`AGENTS.md` §11.3 命令表为空。M0 建立后，应把实际命令写入命令表，再运行并记录通过/失败/跳过数。

---

## 9. 未来实验与通过标准

以下实验是“可运行”的设计：给定 M0 骨架和可控 Worker/假模型，即可实现。命令是**未来入口**，当前未建立、未执行。

### EXP-ARCH-01 共享池公平性

- **问题**：FIFO vs WFQ/DRR 对轻库首结果的影响。
- **设置**：3 个库；A 每轮 120 个候选，B 每轮 1 个，C 每轮 10 个；共享重排器每项 50ms；运行 10 分钟；记录每个库首结果延迟。
- **指标**：B 的首结果 p50/p95；A 的吞吐；池利用率；饥饿次数。
- **通过标准**：WFQ 下 B p95 ≤1s，FIFO 下 ≥5s；WFQ 吞吐不低于 FIFO 的 80%；无库饥饿超过 1 个软截止。
- **决策**：通过则采用共享池 + WFQ；否则保留每库独立池或增加隔离。
- **命令（未来）**：`pytest tests/architecture/test_shared_pool_fairness.py -q`

### EXP-ARCH-02 协调器临界区

- **问题**：Coordinator 内联 500ms 重排是否阻塞其他查询。
- **设置**：20 个并发查询；每个查询注入 1 个已核验事件；重排服务固定 500ms；对比“Coordinator 内联” vs “独立 ScoringPool”。
- **指标**：事件循环 lag p95；Q2 事件延迟；`citation_event_lag_ms`。
- **通过标准**：独立池 lag p95 <50ms；内联 lag p95 >500ms（证明问题存在）。
- **决策**：Coordinator 禁止同步模型调用。
- **命令（未来）**：`pytest tests/architecture/test_coordinator_no_model_calls.py -q`

### EXP-ARCH-03 核验容量

- **问题**：120 候选在 90s 硬截止内是否可完成。
- **设置**：假核验服务，延迟分布 p50=2s、p95=5s/8s/10s；并发 8/16；候选 60/120。
- **指标**：最终快照时间；完成率；超时率；首结果时间。
- **通过标准**：p95=5s、并发 8、候选 120 时 final ≤90s；p95=8s 时显式 `is_partial` 且不静默零结果；首结果 ≤5s。
- **决策**：若不通过，降低 `verification.global_max` 到 60 或提高并发到 16；仍不通过则改模型/级联。
- **命令（未来）**：`pytest tests/architecture/test_verification_capacity.py -q`

### EXP-ARCH-04 首结果并行核验

- **问题**：顺序 vs 并行 top-k 的首结果延迟。
- **设置**：top-5；第 1/2 个 8s 返回 `insufficient`，第 3 个 8s 返回 verified；分别跑顺序、并行 top-3、hedged。
- **指标**：首 verified 时间；总调用数；成本。
- **通过标准**：并行 top-3 首结果 ≤10s；额外调用 ≤2×；证据质量不下降。
- **决策**：采用并行 top-k；若成本过高，改用廉价前置门控。
- **命令（未来）**：`pytest tests/architecture/test_parallel_first_k.py -q`

### EXP-ARCH-05 Agent 快速路径

- **问题**：Agent 在派发前运行对首结果的影响。
- **设置**：规划器延迟 0/3/10s；对比“默认立即派发 + 异步规划” vs “规划后派发”。
- **指标**：首结果 p50/p95；召回@K；规划成本。
- **通过标准**：快速路径首结果延迟差 ≤0.5s；召回不劣于 Agent 路径 2 个百分点。
- **决策**：默认快速路径；Agent 仅追加查询/停止。
- **命令（未来）**：`pytest tests/architecture/test_agent_fast_path.py -q`

### EXP-ARCH-06 SSE 鉴权

- **问题**：浏览器 EventSource 在 Cookie/Bearer/URL ticket 下的行为。
- **设置**：Playwright 三个场景；检查 401、重连、日志脱敏。
- **指标**：连接成功率；401 次数；token 是否出现在日志/URL。
- **通过标准**：Cookie 或一次性 ticket 成功；Bearer-only 不造成 401 风暴；日志无 token。
- **决策**：采用 Cookie + CSRF 或 fetch-based SSE。
- **命令（未来）**：`npx playwright test tests/e2e/sse_auth.spec.ts`

### EXP-ARCH-07 召回预算

- **问题**：`top_k=50` 是否足够。
- **设置**：50 篇 PDF + 100 条标注查询；分别跑 BM25、dense、union；K=50/100/200；记录每阶段召回。
- **指标**：Recall@50/100/200；MRR/nDCG；延迟。
- **通过标准**：union Recall@100 ≥0.90；K=100 比 50 提升 ≥5 个百分点；延迟增加 ≤20%。
- **决策**：按目标设 K；不要固定 50。
- **命令（未来）**：`python tests/eval/recall_budget.py --corpus data/abiotech --queries tests/eval/queries.jsonl`

### EXP-ARCH-08 去重单飞

- **问题**：跨库同 DOI 是否重复核验。
- **设置**：3 个库返回同一 DOI 的不同段落；统计核验调用数。
- **指标**：核验调用数；来源合并数；卡片数。
- **通过标准**：1 次核验、3 个来源、1 张卡片；无重复 `result_id`。
- **决策**：采用 DOI 租约；否则保留重复核验但合并结果。
- **命令（未来）**：`pytest tests/architecture/test_dedup_singleflight.py -q`

### EXP-ARCH-09 校准冷启动

- **问题**：没有标注时，RRF 回退是否可接受。
- **设置**：2 个异构源；0/50/500 条标注；对比 RRF、共同重排、来源校准。
- **指标**：nDCG@10；Brier；`uncalibrated` 比例；用户可解释性。
- **通过标准**：0 标注时 RRF 与 oracle 共同重排差距 ≤2 个百分点；有标注时校准优于 RRF。
- **决策**：允许 `rrf_fusion` 过渡；有标注后切校准。
- **命令（未来）**：`python tests/eval/calibration_cold_start.py --labels 0,50,500`

### EXP-ARCH-10 原生平台基准

- **问题**：Postgres+pgvector vs OpenSearch/Vespa 在目标规模下的召回/延迟。
- **设置**：50 篇 PDF（M1）；合成 100 万段落（M2 预估）；100 条查询；同 embedding/reranker。
- **指标**：Recall@10/50；p50/p95 查询延迟；索引构建时间；运维成本。
- **通过标准**：OpenSearch/Vespa Recall@50 差距 ≤2 个百分点；p95 ≤ pgvector 的 0.7×（大规模时）；小规模不强制。
- **决策**：小规模 Postgres；大规模按结果选平台。
- **命令（未来）**：`python tests/eval/platform_bench.py --backend pgvector --backend opensearch`

### EXP-ARCH-11 端到端 SLO

- **问题**：当前架构在 50 PDF/3 库下能否达到首结果/最终延迟目标。
- **设置**：20 条真实查询；真实模型；记录首 verified、首 citable、final；按库数/查询类型分桶。
- **指标**：p50/p95；超时率；事件 lag；成本/查询。
- **通过标准**：首 verified p50 ≤3s、p95 ≤8s；final p95 ≤30s；成本 ≤预算（示例）。
- **决策**：不达标则先修 P0/P1，再谈 UI。
- **命令（未来）**：`k6 run tests/load/sse_first_result.js`

### EXP-ARCH-12 成本

- **问题**：每查询 token/调用成本。
- **设置**：记录 `citation_model_tokens_total`、调用数、缓存命中；对比 120 全量核验 vs 60 漏斗 vs 级联。
- **指标**：美元/查询（按价格参数）；证据支持率；首结果。
- **通过标准**：成本降低 ≥40% 且证据支持率下降 <2 个百分点。
- **决策**：采用级联/漏斗；否则保留全量。
- **命令（未来）**：`python tests/eval/cost_model.py --price-input ... --price-output ...`

**实验边界**：以上数字是**建议通过标准**，不是用户已批准的需求。真实阈值应来自 M1 基线、模型价格和目标硬件；报告中必须区分“实测”与“假设”。

---

## 10. 综合评估：质量、召回、首结果、最终延迟、吞吐、成本、复杂度、安全、可实施性

| 维度 | 当前规格 | 主要风险 | 建议 | 证据强度 |
|---|---|---|---|---|
| 质量 | 证据门槛强；关系分类/引用句有校验 | LLM 核验假阳性/假阴性；证据严格匹配可能误拒 | 两级核验 + 标注集；保留人工复核 | 强（规格） |
| 召回 | 关键词+向量+RRF；论文/段落两级 | 固定 top_k=50；HNSW 近似损失；论文级索引收益未证 | 测 Recall@K；按目标设 K；评估单索引 | 中 |
| 首结果延迟 | 逐条发布；无全局屏障 | Agent 前置；串行核验；共享池 FIFO | 快速路径 + 并行 top-k + 公平池 | 中–强 |
| 最终延迟 | 软 60s/硬 90s | 120 候选 × 核验；45s 超时 | 核验预算 + 截止准入 | 强（算术） |
| 吞吐 | 每库 Worker + 协调器单写 | 重复查询编码；共享池无批处理；单写事务 | 共享池 + 批处理 + group commit | 中 |
| 成本 | 120 核验 + 20 引用句 | 重复嵌入/扩展；预生成浪费 | QueryBundle 复用；按需引用；级联 | 中 |
| 复杂度 | 4 服务 + 2 应用 + 全量校准 | M0–M1 过重；调度器缺失 | 模块化单体起步；按基准拆分 | 强（工程判断） |
| 安全 | 证据/租户/日志/上传约束强 | SSE 鉴权；外部模型许可；提示注入 | Cookie/ticket；来源策略；不可信数据隔离 | 强（规格）+ 中（实现） |
| 可实施性 | 规格完整、测试夹具详细 | 平台选型未定；无源码/命令表；Agent 不确定 | 先 M0 骨架 + 平台基准；有界规划器 | 强（规格）+ 弱（当前仓库） |

---

## 11. 提案清单与批准需求

| 提案 | 内容 | 影响 | 是否需要用户批准 | 若不做的风险 |
|---|---|---|---|---|
| P-01 | Coordinator 只提交，不内联模型调用 | 实现澄清；不改事件契约 | 否（实现约束） | P0 串行化 |
| P-02 | 独立核验预算 + 截止感知准入 | 改 §7/§8/§15 配置与需求 | 是 | P0 容量不足 |
| P-03 | `QueryBundle` 跨库复用 | 新增内部接口 | 否（内部） | P1 成本/延迟 |
| P-04 | 共享重排/核验池 + WFQ/DRR | 改执行结构；不改事件契约 | 是（架构） | P1 队头阻塞 |
| P-05 | 首结果 top-k 并行核验 | 改 Worker 调度 | 是（质量/成本） | P1 首结果慢 |
| P-06 | 有界规划器/Agent 移出关键路径 | 触及 ADR-002/FND-021/022 | 是 | P1 首结果/可测试性 |
| P-07 | 明确 Worker 准入与 `max_concurrency` 语义 | 改 §8/§15 | 是 | P1 覆盖不公平 |
| P-08 | 截止感知模型调用 | 改 §8/§15 | 是 | P1 浪费/零结果 |
| P-09 | SSE Cookie/一次性 ticket 鉴权 | 改 API-004/SEC-006 实现说明 | 是（安全） | P1 401/泄露 |
| P-10 | 来源 `model_use_allowed` 策略 | 改 adapter 契约 | 是（合规） | P1 许可违约 |
| P-11 | Recall@K 目标与自适应 K | 改 §6/§17 评估 | 是 | P1 召回上限 |
| P-12 | 数值化 SLO | 改 §16/§17 | 是 | P1 无法门禁 |
| P-13 | `rrf_fusion` 冷启动回退 / `comparability` 枚举 | 触及 ADR-007/INV-02/契约 | 是 | P2 冷启动不可用 |
| P-14 | M0–M1 模块化单体 | 实现策略；不改 ADR | 否（实现） | P2 过度设计 |
| P-15 | 单索引 + 论文元数据（替代两级索引） | 触及 ADR-005 | 是 | P2 双写成本 |
| P-16 | 快照 JCS 规范化 | 改 CON-160 细节 | 是（契约） | P2 哈希不一致 |

**明确不提案的变更**：
- 不削弱证据门槛、不删除 `insufficient` 拒答、不允许无证据引用句。
- 不取消快照不可变、不把数字编号移入实时列表、不允许在线 min-max。
- 不把“部分覆盖”伪装成完整结果；不为了首结果牺牲证据真实性。
- 不把 `msearch` 当流式 API；不把 Agent 生成的分数当作权威分。

---

## 12. 结论与建议顺序

**先做 P0（M2 前）**：
1. 把 Coordinator 改成“只提交”，模型调用下沉到 Scoring/Verification Pool；加 trace 断言。
2. 引入独立核验预算与截止感知准入；用 `EXP-ARCH-03` 得到真实容量曲线，再定 120/60/30 的漏斗。

**再做 P1（M2–M3）**：
3. QueryBundle 复用；共享池公平调度；首结果 top-k 并行核验。
4. Agent 移出关键路径（或改有界规划器）；明确 Worker 准入语义。
5. SSE 鉴权方案；来源模型使用策略；Recall@K 目标；数值 SLO。

**最后做 P2（M3–M4）**：
6. 单索引 vs 两级索引实验；RRF 冷启动回退；引用句按需/缓存；快照 JCS；模块化单体；原生平台基准。

**一句话总结**：规格的“正确性骨架”值得保留；需要重构的是**执行骨架**——把“每库全流程 Worker + 协调器内联计算”改成“库级 lane + 共享公平池 + 协调器只提交”，把 Agent 从默认关键路径降为可选顾问，把统一分数从“全量校准”扩展为“证据层 + 序数融合 + 可选基数校准”。这些是提案，不是对用户需求的擅自变更。

---

## 13. 来源

以下来源用于支撑“已确认事实”；未标注来源的内容均为本报告的推断或待测假设。

1. [Elasticsearch async search API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-async-search-submit) — 部分结果、`is_partial`、`batched_reduce_size`。
2. [Elasticsearch RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion) — RRF retriever、`rank_constant`。
3. [OpenSearch hybrid search](https://docs.opensearch.org/latest/search-plugins/hybrid-search/) — normalization processor 2.10、score ranker processor 2.19、hybrid query。
4. [OpenSearch multi-search](https://www.opensearch.org/docs/latest/api-reference/multi-search/) — 并行执行、响应数组、响应级屏障。
5. [Vespa phased ranking](https://docs.vespa.ai/en/phased-ranking.html) — first/second/global phase、RRF、ONNX、WAND。
6. [Qdrant hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/) — prefetch、RRF/DBSF。
7. [pgvector](https://github.com/pgvector/pgvector) — HNSW/IVFFlat、hybrid with Postgres FTS、多租户分区。
8. [MDN: Using server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events) — EventSource、6 连接限制、`id`/`retry`/注释心跳。
9. [MDN: HTTP messages](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Messages) — HTTP/1.1 HOL、HTTP/2 多路复用。
10. [The Tail at Scale](https://cacm.acm.org/research/the-tail-at-scale/) — fan-out 尾延迟、hedged/tied requests、micro-partitions、good-enough、HOL blocking。
11. [Cormack et al., RRF, SIGIR 2009](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) — RRF 公式、k=60、TREC 结果。
12. [Shokouhi & Si, Federated Search, 2011](https://www.khoury.northeastern.edu/home/vip/teach/IRcourse/IR_surveys/FnTIR_2011.pdf) — 联邦检索三问题、result merging、分数不可比。
13. [BEIR, NeurIPS 2021](https://arxiv.org/abs/2104.08663) — BM25 稳健、重排成本、dense/sparse 外分布。
14. [SciFact, EMNLP 2020](https://arxiv.org/abs/2004.14974) — 科学主张验证任务。
15. [LangChain agents](https://python.langchain.com/docs/concepts/agents/) — Agent = Model + Harness、工具循环。
16. [ReAct](https://arxiv.org/abs/2210.03629) — reasoning/acting 交错。
17. [Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html) — 原子写、至少一次、消费者幂等。

---

## 附录 A：规格行号索引（本报告引用）

| 需求/位置 | 行号 | 本报告用途 |
|---|---:|---|
| ADR-001 | 88 | 云端/API/异步/独立 Worker |
| ADR-002 | 89 | LangChain 单 Agent（被质疑） |
| ADR-003 | 90 | 每库独立 Worker、无全局屏障 |
| ADR-005 | 92 | 两级索引（被质疑） |
| ADR-007 | 94 | 统一评分/校准（被质疑冷启动） |
| FND-021 | 67 | Skill 引导 Agent |
| FND-022 | 69 | 单 Agent + 并行 Worker |
| FND-023 | 71 | 禁止全库 LLM 扫描 |
| FND-042 | 168 | Coordinator 唯一权威 |
| IDX-001/002 | 222-223 | 论文级/段落级索引 |
| RET-002 | 239 | 关键词+向量并集 |
| RET-003 | 240 | 两路 TopK |
| RET-006 | 243 | RRF 只用于库内 |
| RET-007/008/009 | 257-259 | 重排/证据/关系核验 |
| RET-010 | 260 | Recall@K 评估 |
| BUD-001 | 270 | 动态 TopN 定义 |
| BUD-003/004 | 272-273 | 探索配额/预算上限 |
| STR-001 | 290 | 并行 Worker、禁止屏障 |
| STR-002 | 291 | 逐条/小批发布 |
| STR-006 | 295 | 有界队列/背压 |
| STR-007 | 303 | 软/硬截止 |
| SCR-003 | 374-385 | 同口径评分 |
| SCR-004 | 387-390 | 异构统一重排（协调器可调用） |
| SCR-005 | 393-409 | 来源校准/不可比 |
| SCR-006 | 411-420 | bootstrap 映射 |
| SCR-007 | 422-431 | 禁止在线 min-max |
| RANK-001 | 497-509 | 资格门槛 |
| RANK-002 | 511-529 | 有序插入/先评分 |
| RANK-005 | 542 | 展示排名 vs 数字编号 |
| SNAP-002 | 562 | 快照不可变 |
| SNAP-004 | 580 | 迟到结果 |
| EVT-003 | 961-968 | 原子写/outbox/至少一次 |
| API-003 | 1013-1025 | 当前状态接口 |
| API-004 | 1027-1034 | SSE |
| SEC-003 | 1375-1380 | 外部模型数据最小化 |
| SEC-006 | 1391 | SSE 授权 |
| CFG-001 | 1405-1443 | 配置默认值 |
| `budget.global_max` | 1432 | 120 候选 |
| `worker.max_concurrency` | 1436 | 8 并发 |
| `worker.micro_batch_*` | 1437-1438 | 微批 |
| `worker.hard_deadline_ms` | 1439 | 90s |
| `citation.generation_top_m` | 1413 | 预生成 20 |
| `model.call_timeout_ms` | 1423 | 45s |
| `worker.soft_deadline_ms` | 1421 | 60s |
| OBS-002 | 1457-1470 | 指标 |
| QAL-001 | 1506 | 确定性测试 |
| AT-STR-001 | 1537 | 慢库不阻塞快库 |
| AT-EVAL-005 | 1599 | 流式性能报告 |
| DOD-002 | 1675 | 流式成立 |

**行号说明**：以上行号基于 2026-11-14 当前工作区的 `文献推荐工具_AGENT_SPEC.md`；规格后续编辑可能导致行号漂移，需求 ID 是稳定引用。

---

## 附录 B：一句话回答“每个技术决定是否必要”

| 决定 | 是否必要 | 结论 |
|---|---|---|
| 单 Agent + LangChain | 产品不必要；技术可选 | 保留 Skill，默认用有界规划器；Agent 仅作补充检索/停止，移出首结果路径。 |
| 每库全流程 Worker | 逻辑隔离必要；物理形态不必要 | 保留库级 lane/覆盖语义；昂贵阶段共享池 + 公平调度。 |
| 统一展示分 | 排序需要；基数校准不必要于 v1 | 保留 `display_score` 契约；冷启动允许 RRF/序数回退，不宣称概率。 |
| 两级索引 | 非必要 | 先单索引 + 论文元数据；用 Recall@100 实验决定是否加论文级向量。 |
| 120 候选全量核验 | 非必要 | 重排 120，核验 30–60；并行 top-k；截止准入。 |
| 4 服务 + 2 应用 | M0–M1 非必要 | 模块化单体起步；M2 后按基准拆分。 |
| SSE | 必要 | 但需明确 Cookie/ticket 鉴权与 HTTP/2；不能用 Bearer-only EventSource。 |
| 不可变快照/事件幂等/证据门槛 | 必要 | 不建议为性能或简化而削弱；这些是产品可信度的基础。 |

---

*报告完。本文所有“推荐/建议/提案”均需用户或规格维护者批准后才能进入实现；未修改任何现有规格或代码。*
