# 契约与分布式系统对抗性评审

> 评审对象：`文献推荐工具_AGENT_SPEC.md`（v1.1.0，1704 行）、`AGENTS.md`、`.agent_spec_parts/*.md` 与 `build_agent_spec.js` 的生成链。
> 评审范围：事务 outbox、SSE 恢复、增量物化视图与版本控制、Schema、状态机、重复/乱序/并发、`finalizing` 与迟到事件、异步导出在搜索终结后的可达性、快照引用与删除。
> 非目标：不修改任何规格或代码；不执行生产操作；不评价检索/重排模型本身的学术质量；不声称证明任何方案全局最优。
> 结论口径：本报告是**风险排序与可验证建议**，不是最优性证明。标注 `[事实]` 的结论有来源；`[推断]` 是从规格文本和常见实现推出的；`[假设]` 需要基准测试确认。所有性能与成本数字，除非注明来源，都是待测假设。

---

## 0. 一页结论

| 级别 | 数量 | 含义 |
|---|---:|---|
| **P0** | 4 | 会在真实并发/故障下**静默丢事件、假完整覆盖或使终态不可恢复**，直接破坏 INV-04 / INV-10 / FND-012，必须在对应里程碑前修 |
| **P1** | 9 | 会在并发、重启、删除、跨语言或真实模型环境下出错，或使验收测试不可实施；应在 M2/M3 前修 |
| **P2** | 8 | 可维护性、成本、边界语义与契约治理问题；可与功能迭代并行 |

**最重要的五个发现：**

1. **P0 / C-01：`sequence` 的“事务内分配”不等于“提交顺序”。** 规格要求每 query 严格递增、步长 1、唯一，并允许用数据库事务写 outbox；若实现用 `nextval` 或并发事务分配序号，PostgreSQL 序列是非事务性的，值立即对其它事务可见且回滚不回收；客户端可能先看到 `sequence=6`，重连后 `>6` 的重放永远收不到后提交的 `5`。这不是性能问题，是**永久丢事件**。
2. **P0 / C-02：`finalizing` 没有事件时间切点。** “全部 Worker 终态”是一个布尔判断，不是“截至某序号的事件都已应用”的水位。Worker 的 `completed` 与其最后一条推荐事件不在同一屏障内时，最终快照可以标称 `coverage=completed` 却漏掉一条已核验推荐，而 `SNAP-004` 又规定查询关闭后迟到结果只进诊断、不改快照——该推荐在该快照/导出中永久不可见。
3. **P0 / C-03：搜索终结后，异步导出事件没有规范定义的可达通道。** `export_ready` / `export_failed` 被定义在搜索事件枚举里，但 `API-004` 又要求搜索终结后关闭 SSE；导出发生在终结之后时，事件写进 outbox 却没有订阅者。`status_url` 存在，却没有被规定为权威完成机制。
4. **P0 / C-04：`GET /searches/{id}` 的 `results` 与 `latest_sequence` 没有要求同一一致性快照。** 在 PostgreSQL 默认 Read Committed 下，两条 SELECT 看到不同快照：状态读到 100，`latest_sequence` 读到 105，客户端从 105 续传，101–105 永久丢失。修法很小（一个 Repeatable Read 事务 + `as_of_sequence`），但不写进契约，实现很容易踩中。
5. **P1 / C-06：资格门槛把“可比性”与“可引用性”绑在一起。** 一条证据已核验、元数据完整的推荐，只因为外部库没有统一重排文本、`calibration_is_comparable=false`，就被 `RANK-001` 排除出可排序/可导出集合。这与 FND-010 把“相关性、证据、引文格式”分开的原则不一致（推断），也造成不必要的召回损失。这是**提案**，需要改 INV-07/ADR-007，须用户批准。

**方向性判断：** 当前架构的核心方向——无全局屏障、逐条发布、证据门槛、不可变快照、统一事件契约——是对的，不应为了修 P0 引入 `gather`/全局屏障。P0 的最小修复集中在四件事：**每 query 单写者、明确 finalize 切点、导出资源化、状态与续传点一致读**。替代架构（manifest + WORM、durable execution）适合 M3/M4 或可靠性数据证明需要时，不建议首期整体替换。

---

## 1. 评审基线：契约隐含的 7 个假设

这些假设没有被规格显式写出，但实现者会自然依赖它们；P0/P1 大多来自假设不成立。

| 编号 | 隐含假设 | 规格依据（`文献推荐工具_AGENT_SPEC.md`） | 状态 | 对应发现 |
|---|---|---|---|---|
| A1 | 同一 query 的事件按 `sequence` 提交并对读者可见 | EVT-001/EVT-003/EVT-004（`文献推荐工具_AGENT_SPEC.md:936-937, 963-968, 972-977`） | **不成立/未约束**：事务内分配 ≠ 提交顺序；`nextval` 非事务性 | C-01 |
| A2 | `worker_completed` 蕴含该 attempt 的所有推荐事件已持久化并被协调器应用 | FSM-002、STR-010/011（`:902-908, 306-307`） | **未约束**：没有“完成屏障”字段 | C-02 |
| A3 | 搜索终结后仍有通道送达 `export_ready` / `citation_text_ready` | EVT-002、API-004/007（`:952-957, 1034, 1072`） | **不成立**：搜索流已关闭；导出是独立生命周期 | C-03 |
| A4 | `GET state` 的 `results` 与 `latest_sequence` 来自同一一致性快照 | API-003、EVT-004（`:1019-1024, 976`） | **未约束**：默认隔离级别下每条 SELECT 各自取快照 | C-04 |
| A5 | 快照内的证据可脱离活库、解析器版本和签名 URL 独立复核 | CON-120/160、SEC-006（`:771-786, 855-880, 1391-1393`） | **未约束**：快照只复制 `quote`，没有证据字节包 | C-07 |
| A6 | `immutable_hash` 跨语言、跨序列化器一致 | CON-160（`:876-877`） | **未约束**：只写了“键名排序、无空白”，未定义数字/字符串/Unicode | C-09 |
| A7 | 同一 `score_version` 的分数可精确复现 | SCR-002、AT-SCR-001（`:359-370, 1547`） | **不成立（GPU）**：PyTorch 官方明确不保证跨版本/平台/CPU-GPU 复现 | C-10 |

---

## 2. P0 发现

### C-01｜`sequence` 的分配顺序与提交顺序脱钩，SSE 重放会永久跳过事件

**严重度：P0**
**需求 ID：** EVT-001、EVT-003、EVT-004、FSM-003；相关 STR-005/006、RANK-002。
**精确位置：**
- `文献推荐工具_AGENT_SPEC.md:936-937`：`sequence` 由协调器统一分配，每 query 严格递增、步长 1；Worker 不得分配全局 sequence。
- `文献推荐工具_AGENT_SPEC.md:963-968`：结果变更与 outbox 事件同事务；`(query_id, event_id)` 与 `(query_id, sequence)` 唯一。
- `文献推荐工具_AGENT_SPEC.md:972-977`：重放 `sequence > after_sequence`；`Last-Event-ID`/`after_sequence` 冲突返回 409；早于保留窗口返回 `REPLAY_GAP`。
- `文献推荐工具_AGENT_SPEC.md:910-914`：最终快照与 `search_finalized` outbox 同事务。
- `build_agent_spec.js:73-83`：契约生成时对 `04-contracts-api.md` 做字符串替换；注意 `04-contracts-api.md:289` 的 SSE 规则与生成后的规格一致，但 part 本身仍保留旧字段，属于 P2/C-15 的治理问题。

**已确认事实：**

1. **PostgreSQL 序列是非事务性的。** 官方文档在事务隔离章节明确写道：对 sequence 的修改“immediately visible to all other transactions and are not rolled back if the transaction that made the changes aborts”。`nextval` 本身是原子的、每个会话拿到不同值，但这只保证**唯一**，不保证**无间隙**，也不保证**提交顺序**。[PostgreSQL 18: Transaction Isolation](https://www.postgresql.org/docs/current/transaction-iso.html)；[Sequence Manipulation Functions](https://www.postgresql.org/docs/current/functions-sequence.html)
2. **Outbox 模式只保证“事务提交则消息最终会发”，不保证发布顺序跨并发事务。** microservices.io 的表述是：消息按应用发送顺序发布，且“这种顺序必须在多个更新同一聚合的服务实例之间保持”；relay 可能重复发布，消费者必须幂等。[Transactional Outbox](https://microservices.io/patterns/data/transactional-outbox.html)
3. **Debezium 的 outbox router 用 `aggregateid` 作为 Kafka message key，目的就是“maintaining correct order in Kafka partitions”。** 这说明成熟实现把“顺序”落在**分区键 + 单分区消费者**上，而不是“数据库序列”。[Debezium Outbox Event Router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html)

**失败反例（并发事务 + 重连）：**

```text
t0  T1: BEGIN; seq = nextval() -> 5; INSERT outbox(E5); 未提交
t1  T2: BEGIN; seq = nextval() -> 6; INSERT outbox(E6); COMMIT
t2  relay: SELECT ... ORDER BY seq 只看到 6（5 未提交）; 发布 E6; SSE id=6
t3  client: 收到 6 后网络断开; Last-Event-ID=6
t4  T1: COMMIT（E5 现在才可见）
t5  client: 重连; replay WHERE seq > 6 -> 永远收不到 E5
```

如果 relay 为了保证顺序而等待 5，则 E6 被 T1 的提交延迟阻塞；若 T1 在事务中跨模型调用（例如把 LLM 核验放在事务里），首结果延迟和吞吐都会受影响。两条路都不可接受。

**更隐蔽的版本：** 使用 `SERIAL`/`IDENTITY` 列作为 `sequence`，事务回滚留下空洞；规格要求“步长为 1”，空洞直接违反契约；如果客户端把空洞当成断线，会触发不必要的全量重建；如果客户端忽略空洞，则无法区分“真的丢了”与“回滚留下的洞”。

**根因：**
- 规格只规定“协调器统一分配”，没有规定**单写者/串行化**；
- `SHOULD 从 outbox 顺序派发`（`:964`）强度不足，允许多线程/多实例 relay；
- 没有规定“不得用数据库序列分配 `sequence`”；
- 没有把 `sequence` 与“已应用水位”绑定，导致快照/重放没有统一切点。

**推荐修复（最小改动，不引入全局屏障）：**

1. **每 query 单写者（sequencer）。** 同一 `query_id` 的公开事件由一个逻辑 actor/mailbox 串行处理：一次只允许一个在途事务；事务内写业务状态 + outbox（`sequence=N`），提交后再处理 `N+1`。这样 `sequence` 顺序 = 提交顺序 = relay 读取顺序。单 query 事件量通常 < 10³，串行化的吞吐代价可接受；不要把 LLM 调用放进这个事务。
2. **禁止用数据库序列分配 `sequence`。** 在契约中写 `MUST NOT use nextval()/IDENTITY for public sequence`；如需多副本，用 `pg_advisory_xact_lock(hash(query_id))` 或按 `query_id` 粘性路由，保证同一时刻只有一个 sequencer。
3. **Relay 按 query 分区。** `SELECT ... WHERE published_at IS NULL ORDER BY sequence LIMIT N FOR UPDATE SKIP LOCKED`；每个 query 只由一个 relay 消费；发布后标记 `published_at`，至少一次；relay 多实例时用 `query_id` 做一致性哈希/分区键（Debezium 的 aggregateid 做法）。
4. **加数据库兜底约束。** `UNIQUE(query_id, dedup_key)` 防止并发插入同一论文；合并时用 `UPDATE ... WHERE result_version = :expected` 做乐观锁，避免 lost update。
5. **快照记录切点。** `Snapshot.applied_through_sequence` = 快照包含的最后一条事件序号；`search_finalized` 事件的 `sequence = applied_through_sequence + 1`。客户端收到 `search_finalized` 即知道快照覆盖到哪。
6. **测试：** 注入 50 个 query × 10,000 条事件，随机延迟提交、随机 relay 重启、随机客户端断线；断言每个 query 的 SSE 流无空洞、无重复（按 `event_id` 去重后）、从任意 `after_sequence` 重放得到同一事件集合；首条结果延迟不因串行化显著上升（p95 增量 < 50ms 为初始目标，待基准校准）。

**残余风险：** 单 query 串行化在极端事件量下会成为热点；如果单 query 事件率超过 ~1,000/s，需要改为“按 query 分区的追加日志 + 单分区消费者”，但仍保持单写者。这个阈值是 `[假设]`，需 E1/E9 测。

---

### C-02｜`finalizing` 不是事件时间水位：快照可以标称完整却漏掉已核验推荐

**严重度：P0**
**需求 ID：** FSM-001/FSM-003、STR-010/STR-011、SNAP-001/002/004、EVAL-002。
**精确位置：**
- `文献推荐工具_AGENT_SPEC.md:884-900`：`running → finalizing` 的触发条件包括“全部 Worker 终态、达到截止条件、满足停止条件、用户明确要求”。
- `文献推荐工具_AGENT_SPEC.md:910-914`：最终快照在同一事务中固定结果顺序、版本、覆盖、终止原因；`search_finalized` outbox 同事务。
- `文献推荐工具_AGENT_SPEC.md:306`：最终化前到达的迟到合格事件进入去重/插入；最终快照后到达的事件不得修改原快照，满足策略时形成补充版本。
- `文献推荐工具_AGENT_SPEC.md:550-560`：快照记录各库 `pending/running/completed/failed/timed_out/cancelled`；存在未完成/失败库时 `is_partial=true`。
- `文献推荐工具_AGENT_SPEC.md:580-585`：查询仍活跃 → 实时状态/新快照；查询已关闭 → 只写 `late_result` 诊断，不改快照。
- `文献推荐工具_AGENT_SPEC.md:630`：EVAL-002 第 2 条要求“相同完整事件集合、所有到达顺序，最终去重数量、结果 ID、分数和排名完全一致”。

**已确认事实：** 流处理系统成熟做法是用 **watermark** 表达“截至某时间点（或某序号）大概率已完整”，并用 **allowed lateness** 与 **side output** 处理迟到数据；watermark 之后的迟到事件默认丢弃或进侧输出。[Flink: Event Time and Watermarks](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/streaming_analytics/)

**失败反例（worker completed 与其最后一条事件之间的竞态）：**

```text
t0  W1: 完成论文 P 的核验，准备写 recommendation_created(E_P)
t1  W1/协调器: 先写 worker_completed（或协调器的 all-terminal 判定先看到 completed）
t2  coordinator: 进入 finalizing，读当前结果（还没有 P），写快照 S
t3  W1: E_P 提交
t4  S: coverage[W1] = completed，is_partial=false；E_P 只能进 late_result
```

结果：`S` 声称 W1 完成、覆盖完整，但少了一条已核验推荐；用户导出 S，P 永久缺失。若改为“先写 E_P 再写 worker_completed”，仍有一个窗口：E_P 已提交但协调器尚未应用，finalize 读到的状态可能不含它（取决于事务可见性）。规格没有要求两者之间的屏障。

**另一个失败模式：** 用户在 `running` 时请求 `kind=final`；`API-006` 要求“提前定稿 MUST 将未完成 Worker 转为取消或超时”。如果某个 Worker 其实已完成、只是状态事件还在队列里，快照可能把它标成 `cancelled`，同时它的推荐事件在快照之后到达，被记为 `late_result`；覆盖状态与事实不符。

**根因：**
- `finalizing` 被当作状态机的布尔转移，而不是“截至序号的切点”；
- 没有 `applied_through_sequence` / `event_set_hash`；
- 没有规定 `worker_completed` 必须晚于该 attempt 的所有推荐事件，或携带“最后事件序号”；
- 没有规定 finalize 前要 drain 已接受的事件队列。

**推荐修复：**

1. **定义 finalize 水位：** `finalize_cut = (applied_through_sequence, worker_attempt_watermarks)`。协调器在进入 `finalizing` 前，先停止接受新事件，drain 所有 `sequence <= cut` 的事件；快照事务读取 `REPEATABLE READ` 状态，写入 `applied_through_sequence=cut`、`event_set_hash=hash(有序 event_id 集合)`。晚于 cut 的事件只进 `late_result` 或 `supplemental`。
2. **Worker 完成屏障：** `worker_completed` MUST 在该 attempt 的所有推荐事件持久化之后写入；`WorkerStatus` 增加 `last_emitted_sequence`、`emitted_event_count`；协调器在标记 Worker 终态前核对这些事件都已应用。若 Worker 崩溃无法保证，则状态为 `failed`/`timed_out`，覆盖为 partial，而不是 `completed`。
3. **用户提前定稿：** 切点 = 当前 `applied_through_sequence`；未完成 Worker 标 `cancelled`/`timed_out`，但 **已经 completed 且事件在切点内的 Worker 不得改标**；事件在切点外的 Worker 即使实际完成，也只能标 `cancelled` 并记录 `cancelled_at`，避免“完成但结果不在快照”的矛盾。
4. **补充快照：** `supplemental` 必须带 `supersedes_snapshot_id` 和 `applied_through_sequence`；`late_result` 表记录 `event_id`、`sequence`、`reason=after_finalize`，供审计和后续补充。
5. **契约增量（建议，需批准）：**

```text
Snapshot {
  applied_through_sequence: integer   // 快照包含的最后一条事件序号
  event_set_hash: string              // 有序 event_id 集合的 SHA-256
  late_result_count: integer          // 切点后收到、未进入本快照的合格事件数
  ...
}
WorkerStatus {
  last_emitted_sequence: integer
  emitted_event_count: integer
  ...
}
```

6. **测试：** 用可控闩锁让 Worker 在“最后一条事件提交”与“worker_completed 提交”之间暂停；协调器在任意时刻 finalize；重复 1,000 次随机延迟。断言：不存在“coverage=completed 但该 Worker 的合格事件不在快照且不在 late_result”的情况；`applied_through_sequence` 等于快照内最大事件序号；`is_partial=false` 时 `late_result_count=0`。

**残余风险：** finalize 需要 drain，最终延迟会增加一个“短事务提交”的时间；不要 drain 模型调用。若 drain 超过硬截止，应把未完成 Worker 标 `timed_out`，而不是无限等待。这个增量是 `[假设]`，需 E2 测量。

---

### C-03｜搜索终结后，异步导出/引用句事件不可达：`export_ready` 写进 outbox 却没人订阅

**严重度：P0**
**需求 ID：** API-004、API-007、EVT-002、EVT-003、UI-006、STR-010、SNAP-004。
**精确位置：**
- `文献推荐工具_AGENT_SPEC.md:1034`：搜索已终结时 SHOULD 重放请求事件后**正常关闭连接**。
- `文献推荐工具_AGENT_SPEC.md:1072`：导出成功受理返回 `202`：`export_id`、`snapshot_id`、`selection_hash`、`status_url`；**完成事件使用 `export_ready` 或 `export_failed`**。
- `文献推荐工具_AGENT_SPEC.md:952-957`：`citation_text_ready`、`citation_text_failed`、`export_ready`、`export_failed` 与搜索事件在同一个 `SearchEvent` 枚举。
- `文献推荐工具_AGENT_SPEC.md:580-585`：查询已关闭时，迟到结果只写 `late_result` 诊断。
- `文献推荐工具_AGENT_SPEC.md:1314-1318`：前端 MUST 通过 SSE 接收搜索事件；断线用 `Last-Event-ID` 恢复。

**已确认事实：**
- WHATWG SSE：**连接正常关闭时客户端会重连**；只有 HTTP 204 才能让客户端停止重连；非 200 或错误 `Content-Type` 会“fail the connection”，且**不再重连**。[HTML Standard: Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
- 原生 `EventSource` 只暴露 `onopen/onmessage/onerror`，错误事件不携带 HTTP 状态码；构造函数只接受 URL 和 `withCredentials`，不能设置 `Authorization` 头。[MDN: EventSource](https://developer.mozilla.org/en-US/docs/Web/API/EventSource)

**失败反例：**

```text
t0  user: 搜索完成；SSE 收到 search_finalized；客户端按 API-004 关闭连接
t1  user: POST /exports {snapshot_id, result_ids, format} -> 202 {export_id, status_url}
t2  export worker: 完成，写 export_ready 到 query outbox
t3  SSE: 连接已关闭；relay 没有订阅者；事件留在 outbox
t4  client: 等待 export_ready；永远不会到；status_url 没有被规范为权威完成机制
```

同样的问题适用于 `citation_text_ready`：`CIT-004` 允许按需生成；如果生成发生在 `search_finalized` 之后，事件没有活的搜索流可投递。规格没有定义“导出/引用句事件走哪条流、用什么序号、保留多久”。

**根因：**
- 搜索事件流被当成唯一事件通道，但导出和引用句是**独立生命周期**的资源；
- `search_finalized` 后关闭连接，但没有为后续资源定义新的流；
- `status_url` 只是“状态查询”，没有被规定为完成的权威来源；
- `export_ready` 与搜索事件共享 `sequence`，语义上会与“终态”冲突（见 C-01）。

**推荐修复：**

1. **导出资源状态机：** `export_state ∈ {queued, running, ready, failed, expired}`；`GET /api/v1/exports/{export_id}` 返回 `state`、`artifact_sha256`、`download_url`、`expires_at`、`error`、`snapshot_id`、`selection_hash`。**这是完成的权威来源**；SSE 事件只是加速器。
2. **独立事件命名空间：** 如果确实要推事件，给 `SearchEvent` 增加 `stream_id`：`search:<query_id>` 或 `export:<export_id>`；`sequence` 按流分别递增；导出事件不得写入搜索流。或者更简单：导出不推 SSE，客户端用 `GET` + `Retry-After`/长轮询。
3. **引用句改为读接口：** `GET /api/v1/snapshots/{snapshot_id}/results/{result_id}/citation-text?result_version=N`，确定性、可缓存、带 `generator_version`；不要在快照后发 `citation_text_ready`。如果保留事件，也走 `result:<result_id>` 流。
4. **客户端契约：** `UI-006` 明确“导出完成以 `GET /exports/{id}` 为准；搜索流关闭后不得依赖搜索流事件”；`status_url` 必须支持 `Retry-After` 或 `?wait=30s` 长轮询。
5. **幂等：** `export_ready` 重复投递按 `export_id` 幂等；`artifact_sha256` 相同即视为同一产物；`download_url` 过期时 `GET` 返回新的签名 URL。
6. **测试：** 终结搜索 → 关闭 SSE → 发起 100 次导出 → 只通过 `GET /exports/{id}` 等待；断言 100% 在 SLA 内到 `ready`，0 次依赖搜索流事件；重复 `export_ready` 不产生第二次副作用；`download_url` 过期后能刷新。

**残余风险：** 如果产品坚持“所有完成都走 SSE”，就需要一个用户级通知流（`GET /users/me/events`）并处理多标签页、多设备、重放窗口；这比 `GET` 轮询复杂，收益主要是即时性。对导出这种秒级任务，轮询足够。

---

### C-04｜`GET state` 与 `latest_sequence` 不是同一快照：续传点可能跳过已提交事件

**严重度：P0**
**需求 ID：** API-003、EVT-004、UI-001。
**精确位置：**
- `文献推荐工具_AGENT_SPEC.md:1019-1024`：返回 `session`、`results`、`workers`、`latest_sequence`、`is_live`。
- `文献推荐工具_AGENT_SPEC.md:976`：当前状态响应 MUST 包含 `latest_sequence`，作为全量重建后的续传起点。
- `文献推荐工具_AGENT_SPEC.md:1156`：前端在 replay gap 后用 `latest_sequence` 全量重建。

**已确认事实：** PostgreSQL 默认 Read Committed 下，“两条连续的 SELECT 可能看到不同的数据，即使它们在同一个事务里”；Repeatable Read 才保证“从事务第一条非事务控制语句开始看到稳定快照”。[PostgreSQL 18: Transaction Isolation](https://www.postgresql.org/docs/current/transaction-iso.html)

**失败反例：**

```text
t0  GET /searches/q: SELECT results ... -> 读到包含 seq 100 的状态
t1  T2: 提交 events 101..105
t2  GET /searches/q: SELECT max(sequence) -> 105
t3  client: 以 after_sequence=105 连接 SSE
t4  replay WHERE sequence > 105 -> 101..105 永久丢失
```

这不是理论问题：只要实现把 `results` 和 `latest_sequence` 分成两条查询（很自然），默认隔离级别就会产生这个窗口。反过来，如果 `latest_sequence` 取的是“事务开始时的 max”，而 `results` 在另一条语句读到更新的数据，也会出现“状态比续传点新”的不一致。

**根因：**
- 没有规定 `GET state` 必须在单一一致性快照中读取；
- `latest_sequence` 的语义没有定义成“返回的 `results`/`workers` 已包含的最大序号”，而是“当前最大序号”；
- `replay` 的 `>` 与状态包含关系没有显式对齐（`after_sequence=as_of_sequence` 是闭区间还是开区间）。

**推荐修复：**

1. **一个 `REPEATABLE READ` 事务读 `session/results/workers/latest_sequence`。** 在事务内计算 `as_of_sequence = max(sequence)`，并把它定义成“本响应中的 `results` 与 `workers` 已完整包含的最大事件序号”。
2. **响应字段改为 `as_of_sequence`（或保留 `latest_sequence` 但明确同义）。** 客户端全量重建后用 `after_sequence=as_of_sequence` 连接；服务端 replay `sequence > after_sequence`，语义清晰。
3. **终态搜索的状态来源：** 如果实时结果表在终结后被清理，`GET state` 必须从最终快照重建 `results`，并返回 `results_source="final_snapshot"`、`as_of_sequence=snapshot.applied_through_sequence`；不能返回空列表 + 一个很大的 `latest_sequence`。
4. **测试：** 在 `results` 与 `latest_sequence` 两条 SELECT 之间注入一个提交（用可控事务/闩锁）；重复 1,000 次；断言客户端从 `as_of_sequence` 续传后收到所有事件，0 丢失。另一个用例：终结后清理实时表，`GET state` 仍返回与快照一致的 `results`。

**残余风险：** Repeatable Read 下长事务会持有快照，增加 bloat；状态查询应短小、只读，避免在事务内做模型调用或大对象序列化。

---

## 3. P1 发现

### C-05｜`WorkerStatus` 的关联键缺少 `attempt`，重试会覆盖终态、丢失审计与覆盖真相

**严重度：P1**
**需求 ID：** CON-140、FSM-002、STR-005、STR-011。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:823-838`（`query_id`、`library_id`“组成当前 attempt 的关联键”）；`:902-908`（`failed|timed_out → pending`，`attempt` 递增）。

**问题：** 如果以 `(query_id, library_id)` 作为唯一键，attempt 1 的 `failed` 会被 attempt 2 的 `running/completed` 覆盖；如果以插入方式保留多行，又没有定义“当前 attempt”指针，`Coverage` 与 `candidates_seen` 的聚合语义不清。失败反例：attempt 1 失败（3 条推荐），attempt 2 成功（2 条推荐，其中 1 条重复）；快照只看到 attempt 2，审计丢失失败；`recommendations_emitted` 可能重复计数或漏计。

**推荐修复：**
- 主键 `(query_id, library_id, attempt)`；`SearchSession` 增加 `current_attempts: {library_id: attempt}`；`Coverage` 按当前 attempt 计算，快照记录每个库的 `attempt` 与终态原因。
- `candidates_seen`/`candidates_verified`/`recommendations_emitted` 明确是“每 attempt 计数”；另加 `unique_recommendations`（按 `result_id` 去重）用于质量指标。
- 测试：attempt 1 失败、attempt 2 成功；断言快照 coverage=completed、诊断含 attempt 1 失败、计数不重复；同一 `result_id` 跨 attempt 只保留一个身份。

### C-06｜资格门槛把“可比性”当成“可引用性”：已核验但不可校准的推荐被静默隐藏（提案，需批准）

**严重度：P1（产品/契约决策，不是实现 bug）**
**需求 ID：** RANK-001、SCR-005/009、INV-07、FND-010、ADR-007。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:497-509`（`eligible = ... AND score_status=="scored" AND calibration_is_comparable==true`）；`:393-409`（无法统一重排时用来源校准；无可靠校准器 → `uncalibrated`，进“不可比/待校准”分组）；`:55`（FND-010 要求相关性、证据、引文格式分开）。

**问题：** 一条推荐可以同时满足 `verification_status=verified`、`source_trace_valid=true`、`metadata_verified=true`、`document_type_eligible=true`，只因为外部库没有标题/摘要文本、无法统一重排，`display_score=null`、`calibration_is_comparable=false`，就被 `RANK-001` 排除出“可排序的建议引用集合”。如果快照只包含该集合，它也无法被导出（API-007 要求 `result_ids` 存在于快照）。这是把“能否跨库比较”与“能否引用”绑在一起，与 FND-010 的分层原则不一致（推断），也造成召回损失。

**失败反例：** 外部库 E 只提供元数据 + 原文证据（合法适配器），没有摘要文本；`CommonReranker` 无法运行；`Calibrate_i` 没有足够标注，`calibration_status=uncalibrated`；该论文证据完全支持稿件论述，但用户看不到、选不了、导不出。界面只显示“0 条推荐”，覆盖状态却是 completed——这不是“诚实覆盖”，是“把已核验结果藏起来”。

**推荐修复（提案）：**
- 拆分两个谓词：
  - `citable = verification_status=="verified" AND relationship IN {support,complement,contrast} AND source_trace_valid AND document_type_eligible AND paper.metadata_verified`
  - `rankable = score_status=="scored" AND calibration_is_comparable`
- 可引用但不可排序的结果进入独立分组：`comparability_group="uncalibrated"`，`display_score=null`，只显示来源内分数/无分数，明确标注“不可跨库比较”；仍允许导出，但导出/界面带警告。
- 如果产品坚持“不可比就不展示”，至少在 `Coverage`/指标中记录 `verified_but_unrankable_count`，让召回损失可见（FND-012）。
- **影响：** 需要修改 INV-07 的资格门槛、ADR-007 的评分策略、RANK-001 和快照结构；按 AGENTS.md §4.11 取得用户明确同意，不能由实现 Agent 自行放宽。
- **收益/代价：** 召回提升（尤其是外部异构库），证据门槛不变；代价是 UI 多一个分组、用户可能误比较来源内分数（用“无统一分”而非数字缓解）、导出语义需要警告。**这是提案，不是本报告擅自变更需求。**

### C-07｜快照只复制 `quote`，没有证据字节包：来源删除/重建索引/签名 URL 过期后“可核查”失效

**严重度：P1**
**需求 ID：** CON-120、CON-160、SNAP-002、SEC-006、SEC-007、ING-007、EVD-002。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:771-786`（Evidence 有 `quote`、偏移、`source_trace_valid`，但无 `raw_text_sha256`/`parser_version`）；`:855-880`（Snapshot 有 `index_versions`、`model_versions`、`calibration_versions`，但无 `parser_versions`/`evidence_bundle_hash`）；`:1391-1393`（原文链接 SHOULD 用短时签名 URL）；`:1419`（`storage.signed_url_ttl_seconds=900`）。

**问题：** 快照的 `Recommendation[]` 复制了 `Evidence.quote`，但 `passage_id`、`source_uri` 仍指向活库/对象存储。24 小时后（`manuscript.retention_hours=24`）或索引重建（`parser_version` 变化）后：快照仍能显示 quote，但“逐字核查”无法对 `Passage.raw_text` 复验；签名 URL 已过期，点击原文 404；如果来源被删除/撤稿，快照仍显示 `metadata_verified=true` 且没有撤回提示。`immutable_hash` 稳定，不等于“证据可核查”。

**失败反例：** 用户 30 天后打开快照 S1，点击证据链接 → 签名 URL 过期 → 404；系统尝试用 `passage_id` 回查 → 索引已重建，`raw_text` 变化/段落不存在 → `source_trace_valid` 无法复算；导出器只能信任旧的布尔值，或者失败。两种都不满足 INV-03 “可逐字核查”。

**推荐修复：**
- 在 Snapshot 中存**自包含证据包**（或内容寻址引用）：`{evidence_id, quote, quote_sha256, raw_text_sha256, start_offset, end_offset, normalization_version, extractor_version, parser_version, passage_snapshot_id, source_resolver_id}`；`Snapshot.evidence_bundle_hash` 覆盖整个包。
- `source_uri` 不要存签名 URL；存稳定的 `source_resolver_id`，读取时再换短时 URL。快照的“可核查”不依赖 URL 有效期。
- 撤回/更正用**追加式通知表**（按 DOI/`work_id`）：`notice_type`、`source`、`observed_at`、`effective_at`、`payload_hash`；读快照/导出时 join 最新通知，导出产物记录 `notice_version`。快照内容与 `immutable_hash` 不变，但用户能看到“该文已撤稿/更正”的风险提示。
- 测试：创建快照 → 删除源文件/重建索引/让签名 URL 过期 → `GET snapshot` 仍能校验 `quote` 与 `raw_text_sha256`；导出在来源不可用时 fail closed 或带明确警告；撤回后快照 hash 不变但导出含 `notice_version`。

### C-08｜原生 `EventSource` 无法实现规格要求的错误语义（410/401/204/终结关闭）

**严重度：P1**
**需求 ID：** UI-001、API-004、EVT-004、SEC-006。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:1314-1318`（前端 MUST 通过 SSE；断线用 `Last-Event-ID`）；`:1027-1034`（建立 SSE 前完成权限/replay 校验；终结后重放再关闭）；`:970-977`（`REPLAY_GAP` 410、`REPLAY_REQUEST_CONFLICT` 409）。

**已确认事实：**
- 原生 `EventSource` 的非 200/错误 `Content-Type` 会“fail the connection”且不再重连；204 会停止重连；正常关闭会重连。[WHATWG SSE](https://html.spec.whatwg.org/multipage/server-sent-events.html)
- 错误事件本身“little to no information can be made available”；浏览器不暴露 HTTP 状态码。[WHATWG SSE, Implementation advice](https://html.spec.whatwg.org/multipage/server-sent-events.html)
- `EventSource` 构造函数不能设置 `Authorization` 头；只能 `withCredentials`。[MDN EventSource](https://developer.mozilla.org/en-US/docs/Web/API/EventSource)
- HTTP/1.1 下每浏览器+域名最多约 6 个 SSE 连接；HTTP/2 多路复用可缓解。[MDN EventSource](https://developer.mozilla.org/en-US/docs/Web/API/EventSource)

**失败反例：**
- 服务端在建立流之前返回 `410 REPLAY_GAP`；原生 `EventSource` 只触发 `onerror`，前端无法区分“需要全量重建”与“401 需要重新登录”与“500 重试”。如果前端在每次 error 都 `GET state`，会把所有瞬时网络错误变成全量重建；如果只重试，gap 永远补不上。
- 终结搜索：服务端重放后正常关闭；原生 `EventSource` 按规范自动重连，再次重放、再次关闭，形成无限循环；如果前端在 `search_finalized` 时 `close()`，之后的新补充事件不会到达（对象已 CLOSED），必须新建 `EventSource`。
- Bearer Token 认证：原生 `EventSource` 不能带 `Authorization`，只能依赖 Cookie；与 `SEC-001`/`SEC-006` 的“重新授权”要求冲突，或迫使 API 同时支持 Cookie 认证（CSRF/SameSite 复杂化）。

**推荐修复：**
- `UI-001` 明确：**首选 fetch-based SSE 客户端**（`fetch` + `ReadableStream` + `TextDecoder` + `AbortController`），可读状态码、可带 `Authorization`、可精确处理 410/204；原生 `EventSource` 只作为 Cookie 认证的降级路径。
- `API-004` 明确状态语义：200 流；204 无更多事件（终结）；410 `REPLAY_GAP`；401/403；404；429 + `Retry-After`。终结搜索：若 `after_sequence < finalized_sequence`，重放后发送 `search_finalized` 并关闭；若 `after_sequence >= finalized_sequence`，直接 204（原生客户端不再重连）。
- 加 `retry: 3000` 注释/字段；心跳 15s 保留；慢消费者缓冲上限（见 C-12）。
- 测试矩阵：原生 vs fetch；410/401/204/200-close；断言 0 无限重连、0 无界内存、gap 后正确全量重建。

### C-09｜`immutable_hash` 的规范化不足：跨语言/跨序列化器会算出不同哈希

**严重度：P1**
**需求 ID：** CON-160、AT-EVAL-006、INV-04。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:876-877`（“UTF-8、键名递归排序、无多余空白”）；`:1600`（重复导出语义等价检查）。

**已确认事实：** RFC 8785（JCS）定义了 JSON 规范化的完整规则：属性名按 UTF-16 code unit 排序；数字按 ECMAScript `JSON.stringify` 规则（IEEE 754 double）；字符串转义规则；不做 Unicode 规范化；重复键/NaN/Infinity 必须报错。[RFC 8785: JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)

**问题：** 规格只写了“键名排序、无多余空白”，没有规定数字格式、字符串转义、Unicode 规范化、重复键、数组顺序、`-0.0`、大整数。Python 的 `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 通常不是 JCS：浮点 repr、转义、`-0.0`、代理对都可能与 JS 不同。`91.4` 与 `91.40` 是同一个数值但不同文本；`é` 的预组合/分解形式不同；这些都会让 `immutable_hash` 在 Python 服务端与 TS 客户端/审计器之间不一致。

**失败反例：** 服务端用 Python 计算 S1 的 hash，导出客户端用 JS 重算校验；标题含 `é`、分数 `91.40`、数组顺序相同；两边 canonical JSON 字节不同 → hash 不同 → 客户端判定“快照被篡改”，或审计报告无法复现。

**推荐修复：**
- 明确采用 **RFC 8785 JCS**；`immutable_hash = SHA-256(JCS(snapshot_without_hash))`；快照记录 `hash_alg="sha256"`、`canonicalization="RFC8785"`。
- 使用同一实现/库，或至少用同一组 golden vectors 做跨语言测试；不要用语言默认 JSON 序列化器“近似”。
- 测试：固定 fixtures（非 ASCII、`-0.0`、`1e+30`、`91.40`、嵌套对象、数组、空对象/空数组、代理对）；Python/TS 两边 hash 相等；1,000 个随机 JSON 值 0 mismatch。

### C-10｜`AT-SCR-001` 的“相同输入相同分数”在真实 GPU 上不可保证；`score_version` 缺少模型产物/运行时

**严重度：P1**
**需求 ID：** SCR-002/003、AT-SCR-001、EVAL-002、VER-002。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:359-370`（`score_version` 至少由重排器、校准器、特征 Schema 组成）；`:383`（`round1` 确定性舍入）；`:1547`（相同输入得到相同展示分）；`:629-630`（旧结果分数变化次数为 0）。

**已确认事实：** PyTorch 官方文档：“Completely reproducible results are not guaranteed across PyTorch releases, individual commits, or different platforms. Furthermore, results may not be reproducible between CPU and GPU executions, even when using identical seeds.” 即使开启 `torch.use_deterministic_algorithms(True)`，也只是“known deterministic implementations where available”，且通常更慢；不同 SDPA 后端的浮点累加顺序不同，bitwise 不保证。[PyTorch Reproducibility](https://docs.pytorch.org/docs/stable/notes/randomness.html)

**失败反例：** 同一 query、同一候选，两个 GPU worker 分别算出 91.43 和 91.47；`round1` 后 91.4 vs 91.5；排名翻转；`AT-SCR-001` 失败。或者模型升级后 `score_version` 仍是 `reranker-v1`（只写名字，不写权重哈希），两个不同分数被标成同一版本，违反 SCR-009/VER-002 的“不得无说明混排”。

**推荐修复：**
- `score_version` 增加 `reranker_model_sha256`、`tokenizer_sha256`、`runtime`（PyTorch/CUDA/device class）、`precision`、`batch_policy`；同一 `score_version` 才可比较。
- 对真实模型，**按 `(input_hash, score_version)` 缓存分数**；一条结果只算一次，重放读缓存，从根上保证同一结果版本分数不变。
- 契约测试用确定性 fake reranker（QAL-001 已允许）；真实模型回归用容差（如 `|Δ|≤0.05`）并固定设备/精度；如果容差会导致排名翻转，用稳定的 tie-breaker 或分数分档展示。
- 明确 `round1` 的舍入规则（建议 half-away-from-zero 或整数 basis points），不要依赖语言默认 `round`（Python 是 banker's rounding，JS 是 half-up）。
- 测试：同设备 100 次，最大 Δ 记录；跨设备 100 次，报告 Δ 分布与延迟代价；契约测试 0 失败；真实模型测试按容差判定。

### C-11｜协调器重启后的权威状态未定义：内存有序表 + outbox 可能无法重建

**严重度：P1**
**需求 ID：** RANK-002、EVT-003、AT-OPS-002、FSM-003。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:511-528`（哈希索引 + 有序列表/平衡树，`O(log N)` 插入）；`:961-968`（状态变更 + outbox 同事务）；`:1591`（重启后恢复进行中/已完成任务）。

**问题：** 规格没有定义“权威状态”在哪里：是内存有序表、DB 结果表，还是 outbox 事件日志？如果以内存为准，崩溃后丢失；如果以 outbox 重放为准，outbox 可能按保留窗口清理，长搜索无法完整重放；如果以 DB 结果表为准，又没有要求结果表与事件同事务更新。AT-OPS-002 因此不可验收。

**失败反例：** 搜索运行 2 小时；协调器崩溃；outbox 只保留 1 小时；重启后无法重放前 1 小时的事件；内存表为空；如果从 DB 结果表重建，结果表可能只保存了“已发布”状态，缺少未发布的合并/版本信息 → 列表与崩溃前不一致，甚至重复卡片。

**推荐修复：**
- 明确权威存储：`recommendations(query_id, result_id, result_version, display_score, rank_key, evidence_ids, updated_at)`，每个事件事务同时 upsert；内存堆只是缓存，启动时从表重建。
- Outbox 只负责投递，不作为状态真源；保留窗口 ≥ 最大搜索时长 + 补充窗口（建议 24h）。
- 增加 `coordinator_epoch`/fencing token：重启后旧实例的写入被拒绝，防止 split-brain。
- Finalize 幂等：`UNIQUE(query_id, kind='final')`；崩溃后重试同一 final 返回已有快照。
- 测试：搜索中途 kill -9；重启后列表/版本/排名与 kill 前一致；finalize 中途 kill；重启后只有一个 final 快照，无部分状态。

### C-12｜SSE 缺少保留窗口与慢消费者背压配置：`REPLAY_GAP` 无阈值，慢客户端可拖垮 relay

**严重度：P1**
**需求 ID：** EVT-004、CFG-001、STR-006、OBS-002。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:970-977`（`REPLAY_GAP`、心跳）；`:1405-1441`（配置表，无 `sse.replay_retention_seconds`、`event.retention_hours`、`sse.max_pending_events_per_connection`）；`:295`（Worker 队列有界/背压，但 SSE 侧没有）。

**问题：** `REPLAY_GAP` 需要一个“保留窗口”阈值，但 CFG 没有；实现只能猜。SSE 连接如果直接阻塞在 outbox relay 上，一个慢客户端会拖住整个 query 甚至整个 relay；如果无界缓冲，内存会涨。规格只规定了 Worker→Coordinator 的队列有界，没有规定 Coordinator→SSE 的每连接缓冲上限。

**失败反例：** 移动网络用户停留在一个旧标签页，SSE 读得很慢；relay 向该连接写入时阻塞；该 query 的其它事件延迟上升；如果 relay 是全局单线程，所有 query 受影响。或者实现选择无界队列，10k 事件后 OOM。

**推荐修复：**
- 增加配置：`sse.replay_retention_seconds`（建议 ≥ 24h 或 ≥ 最大搜索时长 + supplement window）、`event.retention_hours`、`sse.max_pending_events_per_connection`（如 1,000）、`sse.max_connections_per_user`。
- 慢消费者策略：每连接有界环形缓冲；溢出时发送一个 `resync_required` 控制事件（或直接关闭连接），客户端用 `Last-Event-ID` 重放；如果落后超过保留窗口，返回 `REPLAY_GAP` + 全量重建。不要阻塞 relay。
- 监控：`citation_sse_pending_events`、`citation_sse_dropped_total`、`citation_event_backlog` 已有部分指标，补 per-connection。
- 测试：注入 10k 事件 + 慢消费者；断言 relay 延迟不受影响、内存有界、客户端最终通过重放/全量重建收敛。

### C-13｜快照内联 `Recommendation[]` 的体积、签名 URL 与删除级联没有治理

**严重度：P1（规模/运维），MVP 可接受但需上限**
**需求 ID：** CON-160、SNAP-002、SEC-007、CFG-001。
**精确位置：** `文献推荐工具_AGENT_SPEC.md:862`（`results` 为 `Recommendation[]` 有序复制）；`:1418-1419`（原稿保留 24h、签名 URL 900s）；`:1395-1397`（删除范围包括未完成稿件快照及导出临时文件）。

**估算（`[假设]`，需实测）：** 20 条结果 × 4 条证据 × 1,200 字符展示 + 上下文/元数据 ≈ 100KB/快照；1,000 快照/天 ≈ 100MB/天 ≈ 36GB/年（未压缩、未计索引）。每条快照重复存 `Paper`/`Evidence`；如果同一论文出现在多个快照，重复放大。签名 URL 存进快照后 900s 过期；删除原稿后 `insertion_anchor.manuscript_id` 悬挂，导出可能 500 而不是 typed error。

**推荐修复（短期）：**
- `Snapshot` 增加 `retention_class`、`byte_size`、`compression`；快照大小上限（如 1MB），超过时存 manifest + 对象存储引用（见 Alt A）。
- 不存签名 URL；存 `source_resolver_id`；读取时换 URL。
- 删除协议：原稿删除后，快照保留但 `manuscript_state="deleted"`；导出 docx 返回 `MANUSCRIPT_DELETED`（409/422），不要 500；`insertion_anchor` 标记不可解析。
- 测试：删除原稿 → 快照可读、导出 typed error；签名 URL 过期 → 重新签发；1,000 快照的存储/创建延迟测量。

---

## 4. P2 发现

| ID | 问题 | 位置 / 需求 ID | 失败反例 | 推荐修复 |
|---|---|---|---|---|
| **C-14** | `Idempotency-Key` 语义不完整：作用域、TTL、并发重复请求未定义 | `:981-986`（API-001）、`:1117`（ERR-002） | 双击导出，同 key 两请求并发 → 两个导出或 500；key 跨端点/用户复用 → 串结果；无 TTL → 表无限增长 | 唯一键 `(tenant_id, principal_id, method, route, key)`；存 request fingerprint + response status/body hash；TTL ≥ 24h；in-flight → 409 `IDEMPOTENCY_IN_PROGRESS` + `Retry-After`；同 key 同 payload → 重放响应；不同 payload → 422（IETF 草案）或 409（当前规格），二选一并写死。[IETF draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) |
| **C-15** | 规格生成链是“parts + 字符串补丁”，契约有两个来源 | `build_agent_spec.js:73-83`；`04-contracts-api.md` vs 生成的 md | 修改 part 的某行导致 `replaceOnce` 抛错；或误改生成后的 md，重新生成被覆盖；part 与 md 字段不一致（如 `04:127` 无 `dedup_key`，md `:793` 有） | 把补丁合并回 parts，或改为“parts 是唯一源，脚本只做格式化”；CI 重新生成并 `git diff --exit-code`；契约字段用 JSON Schema 生成，不在脚本里做字符串手术 |
| **C-16** | 快照复制 `Recommendation` 并翻转 `provisional`，同一 `(result_id, result_version)` 在两处语义不同 | `:807`、`:862`、`:879` | 客户端按 `(result_id, result_version)` 缓存，live 是 `provisional=true`，快照是 `false`，误判冲突或覆盖 | 引入 `SnapshotItem`（`result_id, result_version, display_score, rank, evidence_ids`），不要复用 live `Recommendation`；快照项不携带 `provisional`，或单独 `snapshot_provisional` 字段 |
| **C-17** | 导出缺少产物哈希，`immutable_hash` 是快照哈希不是文件哈希 | `:954`（`export_ready`）、`:1067-1073`（API-007） | 用户重复导出，无法验证两次文件是否一致；下载被中间层篡改无法发现 | `export_ready`/`GET /exports/{id}` 增加 `artifact_sha256`、`export_spec_hash`（selection + style + manuscript_context_version + renderer_version + notice_version）、`byte_size`；导出文件头写入这些值 |
| **C-18** | `result_id`/`dedup_key` 是全局 DOI 语义，租户/库边界未进入缓存键 | `:455-466`（DED-001）、`:788-821`（CON-130） | 两个租户含同一 DOI，全局缓存/指标键冲突；跨租户缓存命中泄露元数据 | 所有缓存/事件存储键加 `tenant_id`；`result_id` 可保持全局，但访问层必须带租户；`library_id` 过滤在索引查询层（SEC-001） |
| **C-19** | 两个并发 `kind=final` 请求可能创建两个 final；`finalizing → failed` 没有重试状态 | `:884-900`（FSM-001）、`:1044-1061`（API-006） | 双请求竞态 → 两个 final 快照；DB 瞬时失败 → 搜索永久 failed，无法重试 finalize | `UNIQUE(query_id, kind='final')`；`SELECT ... FOR UPDATE` 会话行；`finalizing → finalizing`（retry with attempt）或 finalize 幂等操作；记录 `finalize_attempt` |
| **C-20** | `score_version` 缺少模型产物哈希/tokenizer/运行时；`round1` 舍入规则未指定 | `:359-370`、`:383`、`:876` | 模型升级但版本名不变，分数混排；Python/JS 舍入不同，91.45 → 91.4/91.5 | 见 C-10；`score_version` 加 artifact/runtime；舍入用共享库或整数 basis points |
| **C-21** | `Coverage` 数组是“集合”语义但 `requested_library_ids` 可重复；数量/并集约束可能被重复 ID 破坏 | `:849-853`（CON-150） | `requested_library_ids=["A","A"]`，`total_library_count=2`，并集只有 A，数量和不等于 2 | 对 `requested_library_ids` 做去重并拒绝重复；或定义 `total_library_count = count(distinct requested_library_ids)`；契约测试覆盖重复/空列表 |

---

## 5. 专题：事务 outbox 与 SSE 恢复的推荐时序

### 5.1 推荐的单 query 时序

```text
Worker ──producer_event──▶ Coordinator actor (per query)
                              │
                              │ 1. BEGIN
                              │ 2. upsert recommendation (result_version++, optimistic guard)
                              │ 3. INSERT outbox(query_id, seq=N, event_id, payload)
                              │ 4. COMMIT
                              ▼
                         Outbox table
                              │
                     Relay (partition by query_id)
                              │ SELECT ... ORDER BY seq FOR UPDATE SKIP LOCKED
                              │ publish → mark published_at
                              ▼
                         SSE writer
                              │ id: N
                              │ event: recommendation_created
                              │ data: {envelope}
                              ▼
                         Browser (fetch-based SSE)
                              │ Last-Event-ID: N / after_sequence=N
                              │ on error: 410 → GET state → resume at as_of_sequence
                              │ on 204 → stop
```

**规则清单：**

1. **单写者**：同一 query 同时只有一个在途事务；不要用 `nextval` 分配公开 `sequence`。
2. **短事务**：不要在事务里做检索/重排/LLM 调用；事件事务只写状态 + outbox。
3. **Relay 分区**：按 `query_id` 分区；每个 query 单消费者；发布后标记，至少一次；消费者按 `event_id` 幂等。
4. **状态读一致**：`GET state` 用 `REPEATABLE READ` 读 `results/workers/as_of_sequence`（C-04）。
5. **终结切点**：`finalize_cut = applied_through_sequence`；`search_finalized` 的 `sequence = cut+1`；快照记录 cut（C-02）。
6. **保留与 gap**：`sse.replay_retention_seconds` 与 `event.retention_hours` 明确；早于窗口返回 410，客户端全量重建（C-12）。
7. **终结关闭**：`after_sequence >= finalized_sequence` → 204；否则重放后关闭；客户端在 `search_finalized` 时 `close()`，不要依赖原生 EventSource 的自动重连（C-08）。
8. **导出独立**：`export_ready` 不写搜索流；`GET /exports/{id}` 为权威（C-03）。

### 5.2 为什么不是“全局 Kafka 就自动解决”

Kafka 只保证**分区内**顺序；如果 `query_id` 不作为 key，事件会落到不同分区，顺序丢失。Debezium outbox router 用 `aggregateid` 作为 message key 正是为此。引入 Kafka 可以提升吞吐/回放，但仍需：单分区消费者、幂等生产者/消费者、保留窗口、SSE 投影器。它不会自动解决 C-02/C-03/C-04。

---

## 6. 专题：`finalizing`、watermark 与迟到事件

### 6.1 用 watermark 替代表状态布尔

成熟流处理把“结束”表达为 **watermark**：`Watermark(t)` 断言“截至 t 大概率已完整”；`t` 之后的迟到事件默认丢弃或进 side output；`allowed lateness` 允许在窗口关闭后继续更新一段时间。[Flink: Event Time and Watermarks](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/streaming_analytics/)

映射到本系统：

| 流处理概念 | 本系统对应 | 建议字段/机制 |
|---|---|---|
| watermark | finalize 切点 | `Snapshot.applied_through_sequence`、`event_set_hash` |
| allowed lateness | 补充窗口 | `streaming.supplement_window_seconds`（已有，300s） |
| side output | 迟到诊断 | `late_result` 表 + `supplemental` 快照 |
| window state | 实时有序表 | `recommendations` 权威表 + 内存堆缓存 |
| late firing | 补充快照 | `supersedes_snapshot_id`、`applied_through_sequence` |

### 6.2 关键规则

- `finalizing` **不是**去重/评分/核验的前置屏障（FSM-001 已说），只是“停止接收新事件 + 确定切点 + 写快照”的短阶段。
- finalize 前必须 drain 已接受的事件；drain 的上界是一个短事务，不是模型调用。
- `worker_completed` 必须晚于该 attempt 的所有推荐事件；否则 coverage 不可信（C-02）。
- 用户提前 finalize 时，切点 = 当前 `applied_through_sequence`；切点外的 Worker 即使实际完成，也只能标 `cancelled/timed_out`，事件进 `late_result`，避免“completed 但不在快照”。
- 补充快照必须记录 `supersedes_snapshot_id` 和 `applied_through_sequence`；原快照不变。

### 6.3 与 INV-01 的关系

这些改动**不引入全局屏障**：finalize 的 drain 发生在快照路径，不在首条结果路径；worker 完成屏障只影响该 worker 的终态标记，不影响其它 worker 的逐条发布。首条合格结果仍然“谁先核验谁先出”。

---

## 7. 专题：增量物化视图与版本控制

### 7.1 PostgreSQL 物化视图不是“增量”的

**已确认事实：**
- `REFRESH MATERIALIZED VIEW` **完全替换**内容；`CONCURRENTLY` 只是“不锁住并发 SELECT”，仍然重新执行定义查询；它要求至少一个只含列名、包含所有行的 `UNIQUE` 索引；同一物化视图同时只能有一个 REFRESH。[PostgreSQL: REFRESH MATERIALIZED VIEW](https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html)
- `pg_ivm` 提供**立即增量维护**（AFTER 触发器，在基表修改的同一事务中更新），比全量刷新高效，但有明确限制：不支持 `ORDER BY`、`LIMIT/OFFSET`、窗口函数、`UNION/INTERSECT/EXCEPT`、部分子查询等；基表必须是简单表；并发事务下会持锁，`REPEATABLE READ/SERIALIZABLE` 下可能报错。[pg_ivm README](https://github.com/sraoss/pg_ivm)

**推断：** 本系统的“实时有序列表”是一个 **top-N + 自定义比较器 + 逐事件合并/重排** 的视图，不是简单的 SQL 聚合；`ORDER BY/LIMIT` 恰好是 `pg_ivm` 不支持的。因此：
- 不要试图用 DB 物化视图承载实时有序列表；
- 用应用层增量视图（`recommendations` 权威表 + 内存堆/跳表 + 事件日志），启动时从表重建；
- DB 物化视图只用于离线分析/报表，不用于在线排序。

### 7.2 版本控制的推荐组合

| 版本 | 作用 | 何时变 | 存在哪里 |
|---|---|---|---|
| `schema_version` | 事件/契约 | 不兼容变更 | envelope |
| `score_version` | 评分口径 | 重排器/校准器/特征/模型产物/运行时 | Recommendation、Snapshot |
| `result_version` | 单条结果实质变化 | 证据合并、证据失效、查询变化、评分版本变化 | Recommendation、SnapshotItem |
| `index_version` / `index_snapshot` | 索引快照 | 重建/增量切换 | WorkerStatus、Snapshot |
| `parser_version` / `extractor_version` | 解析/证据抽取 | 解析器升级 | Passage/Evidence、Snapshot 证据包 |
| `snapshot_id` | 不可变快照 | 每次创建 | Snapshot |
| `applied_through_sequence` | 快照事件切点 | 每次快照 | Snapshot（建议新增） |
| `event_set_hash` | 快照事件集合指纹 | 每次快照 | Snapshot（建议新增） |
| `notice_version` | 撤回/更正提示 | 新通知 | 导出产物（建议新增） |

**规则：** 任何影响结果的版本都必须进入快照；快照一经创建，上述字段不得回写；新版本只能形成新快照/补充快照。

### 7.3 如果未来真的需要 IVM 引擎

候选：Materialize / RisingWave / 差分数据流（differential dataflow）。**本次未核验这些产品的官方文档，标记为 `[假设]`。** 采用条件：单 query 事件率 > 10k/s、实时列表需要跨多节点共享、或 SQL 团队希望用声明式维护；代价是新基础设施、一致性模型、运维成本。首期不建议。

---

## 8. 专题：快照引用、删除与不可变性

### 8.1 当前模型

- `Snapshot.results` 是 `Recommendation[]` 的有序复制（`CON-160`，`:862`），每条含 `Paper`、`Evidence[]`、`display_score`、`result_version`。
- `immutable_hash` 对“除自身外的规范 JSON”计算（`:876`）。
- 快照追加写入，不提供更新/删除业务内容的接口（`:877`）。
- 删除例外：到期删除、隐私合规删除、经授权的运维清理（AGENTS.md INV-04；规格 `SEC-007`）。

### 8.2 三个未闭合的引用

1. **证据引用**：`Evidence.passage_id` 指向活库的 `Passage`；`raw_text` 可能因重新解析而变化。快照只复制 `quote`，没有 `raw_text_sha256`/`parser_version`（C-07）。
2. **来源引用**：`source_uri` 可能是 900s 签名 URL（`:1419`），快照存的是短时链接，不是稳定 resolver。
3. **稿件引用**：`insertion_anchor.manuscript_id` 指向私有稿件；稿件 24h 后删除（`:1418`），快照/导出仍引用它，没有 `manuscript_deleted` 语义。

### 8.3 推荐的数据模型（Alt A 的轻量版）

```text
Snapshot {
  snapshot_id
  query_id
  snapshot_kind: checkpoint | final | supplemental
  applied_through_sequence
  event_set_hash
  results: SnapshotItem[]          // 不再内联完整 Recommendation
  coverage
  score_version
  ranking_policy_version
  index_versions
  parser_versions                  // 新增
  model_versions
  calibration_versions
  evidence_bundle_hash             // 新增
  notice_version                   // 新增，可空
  immutable_hash
  created_at
}

SnapshotItem {
  result_id
  result_version
  display_score
  rank
  evidence_ids[]
  evidence_bundle_ref             // 内容寻址
  paper_snapshot_ref              // 内容寻址
}
```

**收益：** 快照行小、创建快（O(1) 写 manifest）、证据可复验、同一论文/证据跨快照去重、WORM/对象锁可用、删除可按引用计数/保留类执行。
**代价：** 两个存储（DB + 对象存储）、GC/引用计数、最终一致性、迁移成本、权限模型更复杂。
**采用条件：** 快照量 > 10k/天、平均大小 > 1MB、需要多年可复现/合规 WORM，或 M3 导出成为核心功能。M1/M2 可先保留内联，但必须加 `byte_size` 上限、稳定 resolver、`manuscript_deleted`。

### 8.4 删除协议（无论是否采用 Alt A）

1. **业务删除不 UPDATE 快照业务字段**；只允许独立的保留任务删除整个快照/对象，并写审计。
2. **稿件删除**：快照保留；`manuscript_state="deleted"`；`insertion_anchor` 标记不可解析；导出 docx 返回 `MANUSCRIPT_DELETED`（409/422），不返回 500，不生成猜测内容。
3. **来源删除/授权到期**：证据包仍可显示已保存的 `quote` + `raw_text_sha256`；原文链接不可用时明确提示“来源不可用”，不伪造跳转。
4. **撤回/更正**：追加 `bibliographic_notice`，读时 join；快照 hash 不变；导出记录 `notice_version`。

---

## 9. 替代架构

### 9.1 Alt A：Snapshot-as-manifest + 内容寻址证据账本 + WORM 对象存储

**核心：** 快照不再内联 `Recommendation[]`；只存 manifest（有序的 `SnapshotItem` 引用 + 覆盖/版本/哈希）。每条结果版本、每条证据包、每篇 Paper 快照作为内容寻址的不可变 blob 存入对象存储（支持 Object Lock/WORM）。DB 存 manifest + blob 指针 + hash。

**数据流：**

```text
ingest → evidence_bundle(hash) → object storage (immutable)
worker → result_version(hash) → object storage
snapshot → manifest(items[], coverage, versions, applied_through_sequence, hash)
export → read manifest + blobs → render → artifact_sha256
```

**收益：**
- 快照创建 O(1)，不再复制大 JSON；DB 行小，事务短，finalize 更快（对 C-02 有利）。
- 证据可脱离活库复验（对 C-07 有利）；同一证据跨快照去重。
- WORM/对象锁天然满足“不可变 + 可审计”；保留/删除按对象生命周期。
- 导出产物 hash 可直接对 blob 计算（对 C-17 有利）。

**代价：**
- 两套存储与权限；GC/引用计数；对象存储最终一致性；迁移/备份/恢复更复杂。
- 需要额外 metadata 表与访问层；小团队运维成本上升。
- 如果对象存储不可用，快照读取/导出受影响；需要本地缓存/降级。

**采用条件：** 快照量/大小超过阈值、需要多年可复现/合规 WORM、或 M3 导出成为核心功能。**不建议 M1/M2 就上。**

### 9.2 Alt B：Durable execution / log-first coordinator

**核心：** 用一个 durable execution 引擎（如 Temporal）承载每个 query 的搜索生命周期：workflow 负责状态机、软/硬截止定时器、重试、取消传播；activity 负责 Worker 调用、评分、快照、导出；workflow history 是权威事件日志；SSE 由 projector 从 history/outbox 投影。

或者：Kafka 按 `query_id` 分区 + 单分区消费者 + 状态存储；coordinator 是无状态消费者，状态从日志重建。

**收益：**
- 软/硬截止、取消、重试、finalize 重试是平台能力，不需要手写 FSM；C-02/C-11/C-19 的风险显著降低。
- 事件历史可重放、可审计；workflow_id = query_id 天然单执行。
- 多副本/故障恢复由平台处理；fencing/lease 内置。

**代价：**
- 新基础设施（Temporal cluster 或 Kafka + 状态存储），运维/学习成本高；首期团队可能没有余量。
- 延迟：workflow/activity 调度可能增加几十毫秒；对首结果延迟有影响（需测）。
- 契约映射：现有 `SearchEvent`/`Snapshot` 需要投影层；不是“换个库”就能解决。
- 供应商/平台锁定；本地开发复杂度上升。

**采用条件：** M2/M3 可靠性数据表明“卡在 finalizing / 重复事件 / 恢复不一致”超过阈值（例如 >1% 搜索、>0.1% 事件）；或需要多副本 coordinator；或团队已有 Temporal/Kafka 运维经验。**不建议首期整体替换。**

### 9.3 Alt C：Work-level 身份图（较小改动，可单独评估）

**核心：** 引入 `work_id`（来自 DOI/OpenAlex/Crossref/arXiv/PMID 的权威解析）与 `manifestation_id`（每个来源库的记录/版本）。`result_id = work_id`；证据按 manifestation 存；撤回/更正/预印本关系在 work 级维护。

**收益：** 跨库/无 DOI/预印本去重更好；引文身份稳定；撤回/更正传播更准确。
**代价：** 依赖外部权威 API（延迟/配额/隐私）；合并错误需要人工复核；`DED-001/002` 契约变更；离线解析成本。
**采用条件：** 候选里 >10% 无 DOI，或跨源重复率 >5%，或接入预印本库。**这是提案，需改 DED-001/002 并取得批准。**

### 9.4 对比表（定性，非全局最优）

| 维度 | 当前（内联快照 + 应用层 FSM + outbox） | Alt A（manifest + WORM） | Alt B（durable execution / log-first） |
|---|---|---|---|
| 质量/证据 | 强门槛；快照重验依赖活库（C-07） | **更强**：证据字节可复验 | 不变；平台不解决证据问题 |
| 召回 | 受 C-06 可比性门槛限制 | 不变 | 不变 |
| 首结果延迟 | 好（无全局屏障）；受 sequencer/SSE 实现影响 | 不变 | 可能 +10–50ms（调度），需测 |
| 最终延迟 | 依赖 finalize；可能卡住/漏事件（C-02） | **更快**：快照 O(1) | **更可控**：durable timer/retry |
| 吞吐 | 单 query 串行化；全局 8 Worker | 快照写放大降低 | 更高，但平台瓶颈/成本 |
| 成本 | 快照重复存储 + 模型调用 | 对象存储便宜；GC 成本 | 基础设施成本最高 |
| 复杂度 | 高（多契约、多状态机） | 中高（两存储、GC） | 最高（新平台） |
| 安全 | 租户过滤；签名 URL 过期 | **更强**：WORM/对象锁/审计 | 平台权限模型；需映射 |
| 可实施性 | **M1/M2 可行** | M3/M4 再做 | 首期不建议 |

**结论边界：** 上表是风险/成本排序，不是“Alt A/B 全面更优”的证明。Alt A 解决快照/证据，Alt B 解决生命周期/恢复；它们都不能替代 C-01–C-04 的契约修复。

---

## 10. 对既有技术决定的质疑（提案，不是变更）

> 以下均为**提案**。涉及 INV-06 / INV-07 / ADR-007 / DED-001 的，必须按 AGENTS.md §4.11 取得用户明确同意，先改规格再改代码；本报告不擅自变更需求。

### P-01｜单 Agent 保留，但把“决策产物”变成可审计的 `SearchPlan`

- **现状：** INV-06/ADR-002 要求单 Agent + Citation Skill；Agent 决定查询、选库、预算、补充检索、停止（`AGENTS.md:121-130`；`文献推荐工具_AGENT_SPEC.md:89`）。
- **问题：** 契约只约束“只有一个 Agent”，没有约束“Agent 的决策可重放/可审计”。同一 query 重跑时，Agent 可能生成不同查询/预算；`Snapshot` 没有记录这次决策，EVAL/回归无法解释差异。
- **提案：** Agent 输出一个版本化 `SearchPlan`：`{plan_id, plan_version, queries[], library_budget{}, stop_policy, max_rounds, created_at, agent_version, prompt_version}`；Coordinator 按 plan 确定性执行；`SearchSession`/`Snapshot` 记录 `plan_id`/`plan_version`。补充检索 = `plan_revision`（新 plan_id），不是隐式行为。
- **收益：** 决策可审计、预算可强制执行、回归可重放、成本可归因；仍然是**一个 Agent**，不违反 INV-06。
- **代价：** 自适应能力下降（需要显式 revision）；Skill 工具 Schema 变更（契约区）；prompt/plan 版本治理。
- **采用条件：** 如果 M2 的搜索成本/结果波动无法解释，或需要离线复现；不需要改多 Agent。

### P-02｜每库全流程 Worker 可以保留，但在高并发下改为“分阶段池 + 每 query 调度器”

- **现状：** 每个库一个 Worker，内部完成检索、重排、证据读取、关系核验（ADR-003，`文献推荐工具_AGENT_SPEC.md:90`；STR-001/002，`:290-291`）。
- **问题：** 每个 Worker 都直接调用 LLM/重排器；慢库的模型调用会与快库竞争配额；`worker.max_concurrency=8` 是 Worker 级，不是模型调用级；尾延迟可能由排队而非工作本身决定。
- **提案：** 把 Worker 拆成“库内检索/证据任务”和“共享的重排池/核验池”；每个 query 有一个 deadline-aware 调度器，按当前排名/覆盖/剩余预算分配核验配额；Worker 在“某条候选核验完成”时立即发事件（不引入全局屏障）。
- **收益：** 尾延迟/成本更可控；慢库不会独占模型配额；首结果仍可逐条出。
- **代价：** 失去“每库完全隔离”的简单性；背压/公平性更复杂；需要新的调度契约；实现难度上升。
- **采用条件：** 负载测试显示首结果 p95 主要由排队构成（而非检索/核验本身），或并发搜索 > N（如 20）。**不违反 INV-01**，因为核验完成即发，不等待其它库。

### P-03｜统一展示分保留，但把“可引用”与“可跨库排序”拆开

- **现状：** RANK-001 把 `calibration_is_comparable=true` 作为进入可排序/可引用集合的条件（`文献推荐工具_AGENT_SPEC.md:497-509`；C-06）。
- **提案：** `citable` 与 `rankable` 分开；不可校准但已核验的结果进入独立分组，无数值统一分，仍可导出（带警告）。需要修改 INV-07（`AGENTS.md:132-139`）、ADR-007（`文献推荐工具_AGENT_SPEC.md:94`）、RANK-001、Snapshot/导出契约。
- **收益：** 召回提升；更符合 FND-010 的分层；不伪造可比性。
- **代价：** UI 增加分组；用户可能误用来源内分数；导出语义需要警告；需要用户批准。
- **采用条件：** 外部异构库占比 >X%，或“已核验但不可排序”的隐藏数量 >Y%（用指标先观测）。

### P-04｜`result_id` 的 DOI 主键可以保留，但为无 DOI/预印本增加 work-level 身份

- 见 Alt C。需要改 DED-001/002；先离线评估重复率，再决定。

---

## 11. 综合评估（质量、召回、延迟、吞吐、成本、复杂度、安全、可实施性）

> 下表是**定性评估 + 待测假设**，不是基准结果，也不声称全局最优。

| 维度 | 当前规格 | 修完 P0 后（预期） | Alt A | Alt B | 备注 |
|---|---|---|---|---|---|
| 证据质量/可追溯 | 强：逐字偏移、页码、关系门槛 | 同等，快照可复验（C-07） | **更强**：证据字节包 + WORM | 同等 | 证据门槛不应因架构变化而放宽 |
| 召回 | 中高：多路召回 + 动态预算；受 C-06 限制 | 中高；若采纳 P-03 更高 | 同等 | 同等 | 无 DOI 去重是另一处损失（Alt C） |
| 首结果延迟 | 好：无全局屏障，逐条发布 | 同等；串行化增量小（待测） | 同等 | 可能 +10–50ms | 不要为修 P0 引入屏障 |
| 最终延迟 | 中：依赖 finalize；可能卡住/漏事件 | **更可预测**：切点 + drain | **更快**：快照 O(1) | **更可控**：durable timer | 目标 p95 需基线 |
| 吞吐 | 中：单 query 串行化；8 Worker；模型配额是瓶颈 | 中；relay 分区可扩 | 中高（快照写放大降低） | 高（平台） | 单 query 事件率是假设 |
| 成本 | 中：120 候选 × 重排/核验；快照重复存储 | 中；快照成本可降 | 存储成本降，GC 增 | 基础设施成本最高 | 需按查询类型分桶 |
| 复杂度 | 高：outbox + SSE + FSM + snapshot + export + citation | 高（契约更精确） | 中高（两存储） | 最高（新平台） | 首期团队带宽是关键 |
| 安全/隐私 | 中高：租户过滤、私有稿件隔离、签名 URL | 中高；补 idempotency/租户键/删除 | **更强**：WORM/审计 | 取决于平台 | 不把私有全文写入日志/事件 |
| 可实施性 | **M1/M2 可行** | 可行；P0 是小改动 + 测试 | M3/M4 | 首期不建议 | 先修契约，再谈替换 |

**判断：** 当前方案在“质量/证据/首结果延迟”上已经有正确骨架；主要债务在“事件日志的权威性/切点/可达性”。Alt A 是快照子系统的合理演进，Alt B 是可靠性工程的投资；两者都不应成为 M1 的前置条件。

---

## 12. 未来实验与通过标准

> 仓库当前没有源码、测试框架和 `AGENTS.md §11.3` 命令表；以下为**建议的测试文件与断言**，不是已执行证据。命令形态示例（待项目建立后替换为真实脚本）：
> `pytest -q tests/streaming/test_sequence_gap.py`
> 所有数值阈值都是 `[假设]`，需用基线校准；通过标准应写入 CI 门禁。

| 实验 | 目的 | 设置 | 指标 | 通过标准（初始） | 区分 |
|---|---|---|---|---|---|
| **E1 序列/重放** | 验证 C-01 | 50 query × 10k 事件；随机提交延迟、relay 重启、客户端断线；`tests/streaming/test_sequence_gap.py` | 丢失数、重复数、空洞数、首结果延迟 | 0 丢失 / 0 重复 / 0 空洞；任意 100 个 `after_sequence` 重放集合一致；p95 首结果增量 < 50ms | 单写者是否足够；串行化成本 |
| **E2 finalize 竞态** | 验证 C-02 | 可控闩锁在“最后事件提交”与 `worker_completed` 之间；1,000 次随机 finalize；`tests/streaming/test_finalize_race.py` | 静默丢失数、coverage 不一致数 | 0 个“completed 但事件不在快照/不在 late_result”；`applied_through_sequence` 匹配；`is_partial=false` 时 `late_result_count=0` | 完成屏障是否必要 |
| **E3 导出可达性** | 验证 C-03 | 终结搜索 → 关闭 SSE → 100 次导出；只通过 `GET /exports/{id}` 等待；`tests/contracts/test_export_after_finalize.py` | 到达 `ready` 的比例/时延、事件依赖数 | 100% 在 SLA（如 5s）内 ready；0 次依赖搜索流；重复 `export_ready` 0 副作用 | `status_url` 是否可作为权威 |
| **E4 状态/续传一致** | 验证 C-04 | 在两条 SELECT 之间注入提交；1,000 次；`tests/contracts/test_state_resume.py` | 丢失事件数、`as_of_sequence` 偏差 | 0 丢失；`as_of_sequence` = 响应中最大已包含序号；重放无 gap | Repeatable Read 是否足够 |
| **E5 SSE 客户端矩阵** | 验证 C-08 | 原生 vs fetch；410/401/204/200-close；慢消费者；`tests/streaming/test_sse_client_matrix.py` | 无限重连次数、内存峰值、gap 后收敛 | 0 无限重连；内存有界；410 后 100% 全量重建并收敛 | 原生 EventSource 是否可支持契约 |
| **E6 JCS 哈希** | 验证 C-09 | 固定 golden vectors + 1,000 随机 JSON；Python/TS 双实现；`tests/contracts/test_jcs_vectors.py` | hash mismatch 数 | 0 mismatch；`-0.0`/`1e+30`/`91.40`/非 ASCII/代理对全覆盖 | 规范化是否跨语言 |
| **E7 分数确定性** | 验证 C-10 | 同设备 100 次、跨设备 100 次；相同输入；`tests/eval/test_score_determinism.py` | max Δ、排名翻转数、延迟代价 | 同设备 Δ≤0.05（或 0 用 deterministic kernels）；跨设备报告分布；延迟代价 ≤20%；契约用 fake reranker 0 失败 | GPU 非确定性是否可接受 |
| **E8 快照/删除/撤回** | 验证 C-07/C-13 | 创建快照 → 删除源/原稿 → 重建索引 → 过期签名 URL → 撤回通知；`tests/integration/test_snapshot_deletion.py` | 快照可读率、证据校验率、typed error 率 | 快照 hash 不变；证据 `quote`/`raw_text_sha256` 可校验；导出 typed error；撤回提示出现 | 证据包是否必要 |
| **E9 负载/成本** | 综合评估 | 50 并发搜索 × 3 库；慢库/失败库；`tests/load/test_streaming_load.py` | 首结果 p50/p95、最终 p95、事件 lag、token/成本 | 首结果 p95 ≤ 15s；最终 p95 ≤ 120s；事件 lag p95 ≤ 500ms；成本 ≤ 基线 +10% | 架构是否满足目标 |
| **E10 快照存储** | Alt A 决策 | 10k 快照；不同大小；`tests/integration/test_snapshot_storage.py` | 创建 p95、平均大小、导出 p95、GC 时间 | 创建 p95 ≤ 50ms；存储 ≤ 预算；导出 p95 ≤ 10s；GC 可完成 | 内联 vs manifest |

**实验设计原则：**
- 时序控制用可控 Worker 替身/闩锁/注入时钟，不用 `sleep`（QAL-001）。
- 每个实验先记录**未修复**的失败，再记录修复后的通过；报告必须给出通过/失败/跳过数（AGENTS.md §11.5）。
- 性能数字按查询类型/库数/候选预算分桶；不要只报平均。
- 任何“通过”都必须有退出码与测试统计，不以“本地跑过”代替。

---

## 13. 建议落地顺序

**M2 前（流式/协调器契约）：**
1. C-01：单写者 sequencer + outbox 分区 + `UNIQUE(query_id, dedup_key)` + 乐观 `result_version`。
2. C-02：finalize 切点 + worker 完成屏障 + `applied_through_sequence`/`event_set_hash`。
3. C-04：状态与 `as_of_sequence` 一致读。
4. C-05：WorkerStatus 主键补 `attempt`。
5. C-12：SSE 保留窗口/每连接缓冲配置。

**M3 前（快照/引文/导出）：**
6. C-03：导出资源状态机 + `GET /exports/{id}` 权威 + 独立流/轮询。
7. C-07：证据字节包 + 稳定 resolver + 撤回通知。
8. C-08：fetch-based SSE 客户端 + 410/204 语义。
9. C-09：RFC 8785 JCS + 跨语言 golden vectors。
10. C-13：快照大小上限 + 删除 typed error。

**M4 前（可靠性/规模）：**
11. C-10：`score_version` 产物/运行时 + 缓存 + 容差测试。
12. C-11：权威结果表 + 重启重建 + fencing token。
13. C-14/C-16/C-17/C-19/C-21：P2 契约清理。
14. Alt A 决策：按快照量/合规需求；Alt B 只在可靠性数据证明需要时评估。

**不要做：**
- 不要为修 P0 引入 `gather`/`Promise.all`/全局屏障；INV-01 必须保持。
- 不要把 finalize 的 drain 放在首结果路径。
- 不要在没有用户批准的情况下改 INV-06/INV-07/ADR-007/DED-001；C-06/P-03/P-04 是提案。

---

## 14. 来源（本次实际检索/阅读）

| 来源 | 支持的事实 | URL |
|---|---|---|
| WHATWG HTML Standard: Server-sent events | 正常关闭会重连；204 停止重连；非 200/错误 Content-Type 失败且不重连；`last event ID` 语义；错误事件信息很少 | https://html.spec.whatwg.org/multipage/server-sent-events.html |
| MDN: EventSource | 构造函数只接受 URL/withCredentials；HTTP/1.1 每域名约 6 连接；HTTP/2 多路复用 | https://developer.mozilla.org/en-US/docs/Web/API/EventSource |
| microservices.io: Transactional outbox | 状态 + outbox 同事务；relay 可能重复；消费者幂等；顺序需跨实例保持 | https://microservices.io/patterns/data/transactional-outbox.html |
| Debezium: Outbox Event Router | `aggregateid` 作为 Kafka key 以保持分区顺序；outbox 只应 INSERT | https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html |
| PostgreSQL 18: Transaction Isolation | Sequence 变更立即可见、回滚不撤销；Read Committed 每条 SELECT 新快照；Repeatable Read 稳定快照 | https://www.postgresql.org/docs/current/transaction-iso.html |
| PostgreSQL 18: Sequence Functions | `nextval` 原子、不同会话不同值；不保证无间隙 | https://www.postgresql.org/docs/current/functions-sequence.html |
| PostgreSQL 18: REFRESH MATERIALIZED VIEW | 完全替换；`CONCURRENTLY` 需唯一索引；同一视图一次只能一个 REFRESH | https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html |
| pg_ivm README | 立即 IVM；AFTER 触发器；不支持 ORDER BY/LIMIT/窗口等；并发锁限制 | https://github.com/sraoss/pg_ivm |
| Apache Flink: Event Time and Watermarks | watermark 断言“截至 t 大概率完整”；allowed lateness；side output；迟到默认丢弃 | https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/streaming_analytics/ |
| IETF: Idempotency-Key Header (draft-08) | key 唯一、不同 payload 不复用；并发重复 409；过期策略；复合键 | https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/ |
| RFC 8785: JCS | 属性排序、数字/字符串序列化、无 Unicode 规范化、重复键/NaN 报错 | https://www.rfc-editor.org/rfc/rfc8785 |
| PyTorch: Reproducibility | 跨版本/平台/CPU-GPU 不保证完全复现；deterministic algorithms 更慢；SDPA 后端数值不同 | https://docs.pytorch.org/docs/stable/notes/randomness.html |

**未在本次核验的：** Materialize/RisingWave/差分数据流的具体语义、Temporal 的具体 API/限制、OpenAlex/Crossref 的解析配额与覆盖。相关内容在报告中标记为 `[假设]`，不作为事实依据。

---

## 15. 附录：需求 ID → 发现映射

| 需求 ID | 发现 | 说明 |
|---|---|---|
| EVT-001/003/004 | C-01、C-04、C-08、C-12 | 序列、outbox、重放、SSE 状态语义 |
| FSM-001/002/003 | C-02、C-05、C-19 | finalize 切点、attempt 键、final 幂等 |
| STR-005/006/010/011 | C-01、C-02、C-05、C-12 | 重试、队列、迟到、覆盖 |
| SNAP-001/002/003/004/005 | C-02、C-03、C-07、C-13、C-16 | 触发、不可变、迟到、导出 |
| CON-130/140/150/160 | C-05、C-07、C-09、C-16、C-21 | Recommendation/WorkerStatus/Coverage/Snapshot |
| API-001/003/004/006/007 | C-03、C-04、C-08、C-14、C-17、C-19 | 状态读、SSE、快照、导出、幂等 |
| RANK-001/002/004 | C-02、C-06、C-11 | 资格、插入、确定性、恢复 |
| SCR-002/003/005/009 | C-06、C-10、C-20 | 评分版本、可比性、舍入 |
| DED-001/002/004 | C-18、Alt C | 身份、去重、租户键 |
| CIT-001/004 | C-03 | 引用句生成/事件可达 |
| UI-001/003/006 | C-03、C-08、C-12 | 事件恢复、局部更新、导出 |
| SEC-006/007 | C-07、C-13、C-18 | 原文访问、删除、租户 |
| CFG-001 | C-10、C-12、C-13 | 保留、背压、快照、评分 |
| AT-SCR-001/AT-EVT-001/AT-SNP-001/AT-OPS-002 | C-01–C-10 | 验收场景需补并发/重启/删除用例 |

---

## 16. 评审限制与未验证项

1. **本次未执行代码、未运行测试、未做基准测试**；仓库当前没有源码/依赖/命令表（AGENTS.md §0、§11.3）。所有“通过标准”都是建议，不是已通过证据。
2. **未验证真实模型/真实外部库的延迟、成本、错误率**；报告中相关数字均为 `[假设]`。
3. **未验证 PostgreSQL 的实际部署版本/隔离级别**；C-01/C-04 的结论基于官方文档的通用语义，不依赖具体版本。
4. **未验证前端是否使用原生 EventSource**；C-08 是“契约允许的实现会导致问题”，不是“现有代码已经错了”。
5. **未验证 Alt A/Alt B 的实际运维成本**；采用条件需要团队/预算评估。
6. **未修改任何规格/代码/配置**；本文件是唯一写入。C-06/P-01–P-04 是提案，需按 AGENTS.md §4.11 取得用户批准后才能进入规格。
7. **行号基准**是当前 `文献推荐工具_AGENT_SPEC.md`（1704 行）和 `build_agent_spec.js`；如果生成链重新生成，行号可能漂移，需以需求 ID 为准。
8. **不声称全局最优**：本报告只给出风险排序与可验证修复；最终选型应由用户在质量、召回、延迟、成本、复杂度、安全和团队带宽之间权衡。

---

*报告结束。*
