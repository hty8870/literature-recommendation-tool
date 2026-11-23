## 跨库评分、增量去重、动态排序与结果快照

### 1. 范围与处理对象

本模块接收各 Worker 已完成原文证据读取和关系核验的候选，负责统一展示分、跨库去重、增量排序、实时结果状态及不可变快照。模块 MUST NOT 重新把相似度当作引用依据，也 MUST NOT 等待全部 Worker 返回后才开始处理。

**输入**

- `query_id`、`claim_id` 与规范化查询文本；
- 候选论文的书目信息、来源库和版本关系；
- 库内原始检索分及检索器信息；
- 已核验的 `relationship`、`verification_status` 和证据集合；
- `source_trace_valid`、`document_type_eligible`；
- 统一重排器、校准器及其版本配置。

**输出**

- 带 `display_score`、`score_version` 和 `provisional` 的有序实时结果；
- 新增、合并、排名变化和拒绝诊断；
- 带数据覆盖范围的不可变 `Snapshot`；
- 供早期排名评估使用的到达顺序、分数和排名历史。

处理优先级 MUST 为：**证据真实性与引用正确性 > 快照及编号一致性 > 跨库排序确定性 > 首结果延迟 > 调用成本**。

### 2. 评分信息分离

#### SCR-001：三类信息 MUST 独立保存和展示

| 字段 | 职责 | 使用限制 |
|---|---|---|
| `retrieval_score` | 保存库内 BM25、向量或第三方检索原始分，供库内排序和诊断。 | 异构来源之间 MUST NOT 直接比较或相加。 |
| `display_score` | 统一重排或校准后的 `0..100` 分，供跨库展示和动态排序。 | MUST 绑定 `score_version`；MUST NOT 解释为引用正确率。 |
| `verification_status` | 表示原文证据是否通过核验，并与 `relationship` 配合表达支持、补充或对比。 | MUST 作为独立资格信息，不能由分数替代。 |

界面 MUST 使用“统一相关性分”或等义名称，MUST NOT 使用“引用正确率”“应该引用的概率”等表述。只有经过代表性标注集概率校准时，才 MAY 补充“相关性概率”解释；该概率仍不是证据支持概率或专家采纳概率。

#### SCR-002：评分上下文 MUST 可重现

每次评分 MUST 记录：

- `reranker_version`；
- `calibrator_version`；
- `feature_schema_version`；
- `score_kind = global_rerank | source_calibration`；
- `calibration_status = bootstrap | calibrated | uncalibrated`；
- 输入论文、证据和查询的版本标识。

`score_version` MUST 至少由重排器、校准器和特征 Schema 三部分组成。同一实时结果集 MUST 使用同一可比较的 `score_version`；模型升级 SHOULD 创建新查询或新结果版本，MUST NOT 无说明地混排新旧分数。送入重排器的多个证据 MUST 先按 `evidence_id` 做确定性排序，再按统一特征规则组装，禁止把 Worker 到达顺序作为模型输入顺序。

### 3. 同口径与异构评分算法

#### SCR-003：同口径检索 MUST 使用共同评分口径

当各库使用相同查询表示、字段、分块、重排模型和特征版本时，Worker 或协调器 MUST 使用共同重排器计算：

```text
r = CommonReranker(query, title, abstract, evidence)
display_score = round1(100 × Calibrate_global(r))
```

- `round1` MUST 明确定义为保留一位小数的确定性舍入，服务端各语言实现 MUST 使用相同舍入规则。
- 独立索引产生的 BM25 原始分 MUST NOT 因“算法名称相同”就直接跨库比较，因为 IDF、平均文档长度和分片统计可能不同。
- 共同重排器的输入 MUST 使用相同字段顺序、截断策略、证据选择规则和文本清洗规则。

#### SCR-004：异构检索 SHOULD 通过统一重排取得可比性

只要外部库能够提供标题、摘要和证据文本，就 MUST 优先在 Worker 末端或协调器对每条新结果增量调用 `CommonReranker`，再使用 `Calibrate_global`。该调用只处理当前新结果，MUST NOT 等待其他库，也 MUST NOT 重算未发生变化的全部分数。

来源库原始检索器可以不同；统一展示分 MUST 来自共同重排上下文，而不是对原始分做表面换算。

#### SCR-005：无法统一重排时 MUST 使用来源校准或标记不可比

外部库不能提供统一重排所需文本时，使用：

```text
display_score = round1(100 × Calibrate_i(local_score_i, query_type, topic))
```

校准器 MUST 满足：

1. 在带人工相关性标签且与在线分布相容的验证集上拟合；可使用逻辑校准或保序回归等单调方法。
2. 按 `source + query_type + topic` 选择最具体的可用校准器；样本或误差不符合配置要求时，依次回退到 `source + query_type`、`source`、来源类别全局校准器。
3. 记录 `training_data_version`、`sample_count`、适用来源、主题、误差和失效条件。
4. 校准器选择及回退 MUST 离线固定并由 `calibrator_version` 标识，MUST NOT 根据当前在线候选临时拟合。
5. 无可靠校准器时 MUST 设置 `calibration_status=uncalibrated`，进入“不可比/待校准”分组；MUST NOT 将原始分伪装成统一分参与精确跨库排序。

来源校准配置 MUST 提供 `min_samples` 和 `max_calibration_error` 两项阈值。部署方应根据标注规模设置；未配置时系统 MUST 采用保守的 `uncalibrated` 行为，而不是自动宣称可比。

#### SCR-006：无概率校准器时的启动映射 MUST 明确标识

开发初期没有足够标注数据时，若共同重排器输出实数 logit，MAY 使用固定的 `sigmoid` 单调映射；若输出已验证处于 `[0,1]`，MAY 使用恒等映射。此时 MUST 设置：

```text
calibration_status = bootstrap
score_kind = global_rerank
```

启动映射只提供稳定排序和 `0..100` 展示，MUST NOT 被解释为概率。输入范围无法确认或映射失败时 MUST 返回评分失败，MUST NOT 默认赋值 `0`、平均分或随机分。

#### SCR-007：禁止的分数转换

实现和评审 MUST 拒绝以下做法：

- 对当前在线批次执行最小值—最大值归一化，并把结果作为可长期比较的展示分；
- 使用当前批次均值、标准差或 Z-score 让旧分随新结果到达而变化；
- 将库内排名、百分位或倒数排名直接解释为相关性概率；
- 直接相加 BM25、向量余弦、第三方黑盒分和证据核验标签；
- 仅因某库历史质量较高就提高当前论文的展示分；历史质量 MAY 用于候选预算，但不能替代当前证据；
- 以来源数量隐式加分。来源数量 MAY 作为独立展示信息，若用于排序特征，MUST 显式定义、版本化并评估。

#### SCR-008：分数稳定性与允许更新的条件

在同一 `score_version` 和同一结果证据集合下，**无关新结果到达 MUST NOT 改变旧结果的 `display_score`**；它只能改变旧结果的名次。

只有以下情况 MAY 更新旧结果分数：

1. 重复论文合并了新的证据，导致统一重排输入变化；
2. 原证据被判定无效或论文版本关系被纠正；
3. 用户修改了查询或目标论述；
4. 显式创建了新 `score_version` 的评分结果。

每次分数变化 MUST 增加 `result_version` 并记录机器可读原因：`evidence_merged`、`evidence_invalidated`、`query_changed` 或 `score_version_changed`。

#### SCR-009：混合版本与评分失败行为

- 新事件与当前结果集的 `score_version` 不一致时，MUST 先通过当前版本重排/校准转换；转换失败时进入不可比分组，MUST NOT 比较两个原始值。
- 评分服务超时或失败时，当前候选 MUST 标记 `score_status=failed` 并保留诊断，其他候选继续处理。
- 评分失败候选 MUST NOT 进入可排序的“建议引用”集合，也 MUST NOT 被当作零相关论文删除；修复评分后可按原候选身份重试。
- 最终快照 MUST 记录评分失败和不可比候选数量，避免将评分缺失误报为检索无结果。

### 4. 全局增量去重

#### DED-001：主身份键

协调器 MUST 维护 `by_dedup_key` 和 `by_result_id` 两个索引。DOI 规范化 MUST 至少执行：去除首尾空白、移除已知 DOI URL/`doi:` 前缀、统一小写、移除末尾句读符号。规范化 DOI 相同的论文 MUST 使用同一 `result_id`。

推荐主键为：

```text
dedup_key = "doi:" + normalized_doi
result_id = stable_id(dedup_key)
```

`stable_id` MAY 使用规范化键本身或带版本前缀的确定性哈希；同一部署内 MUST 固定一种算法。

#### DED-002：无 DOI 的回退身份

无 DOI 时，仅当以下三项同时满足才 MUST 自动合并：

- 标题经 Unicode、大小写、空白和标点规范化后完全相同；
- 发表年份相同；
- 第一作者姓氏及名字首字母相同，或结构化作者列表完全一致。

回退键 MUST 标明生成规则版本。标题模糊相似、主题相似或作者相似 MUST NOT 单独触发自动合并；此类候选进入 `possible_duplicate` 诊断，保留不同结果身份，等待强标识或人工确认。

#### DED-003：版本、更正与撤稿关系

原始论文、更正声明和更新版本 MUST 保留各自标识及显式关系，MUST NOT 仅因标题相似就合并证据。撤稿或不允许推荐类型 MUST 被资格门槛排除，并保留排除原因。更正声明 SHOULD 指向原论文；若原论文被推荐，界面 SHOULD 提供更正状态提示。

#### DED-004：重复结果的合并 MUST 幂等

同一论文再次到达时，协调器 MUST：

1. 保持原 `result_id`；
2. 对 `source_ids`、`evidence_ids` 和原文链接按 ID 求并集；
3. 保存每个来源的原始检索分，不覆盖来源审计信息；
4. 选择可定位且满足授权要求的证据，不丢弃可核查来源；书目字段冲突 MUST 按版本化的来源优先级和字段级合并规则处理，不得依赖到达顺序；
5. 若证据集合或评分上下文未变化，保持原分；否则用当前 `score_version` 对合并结果重新评分；
6. 仅在实际状态变化时增加 `result_version` 并发布合并/排名事件。

相同 `event_id` 重放 MUST 不产生新结果、不重复证据、不增加版本。多个来源命中同一论文 MUST NOT 在前端形成多张独立推荐卡片。

### 5. 动态插入与排名更新

#### RANK-001：进入有序推荐集的资格门槛

```text
eligible = verification_status == "verified"
           AND relationship IN {support, complement, contrast}
           AND source_trace_valid == true
           AND document_type_eligible == true
           AND score_status == "scored"
           AND calibration_is_comparable == true
```

不满足条件的结果 MUST 保留明确诊断，但 MUST NOT 进入可引用推荐列表。`insufficient` MUST NOT 通过提高相似度分重新获得资格。

#### RANK-002：有序数据结构与新结果插入

协调器 MUST 使用“哈希索引 + 有序列表/平衡树”或等价结构，默认排序键为：

```text
(display_score DESC, result_id ASC)
```

`result_id` 只用于同分时保证确定性，不包含人工来源优先级。若未来增加证据完整度等次级排序键，MUST 纳入独立 `ranking_policy_version`，不得在查询中途改变。

新结果处理 MUST 按以下顺序执行：

1. 校验事件、查询身份、必需字段和评分版本；
2. 幂等检查及全局去重；
3. 对新身份计算或验证统一展示分；
4. 以有序插入放入当前列表，时间复杂度 SHOULD 为 `O(log N)`，简单数组实现最迟不得超过 `O(N)`；
5. 先提交内部状态，再发布新增或排名变化事件；
6. 立即向当前订阅者可见，不等待任何其他 Worker。

#### RANK-003：迟到高分与合并后的重定位

迟到结果的 `display_score` 高于当前结果时 MUST 插入相应位置，而不是追加到末尾。重复论文合并新证据后若分数改变，MUST 从旧位置移除并按新分重新插入。

前端事件 SHOULD 携带 `previous_rank`、`current_rank` 和 `rank_change_reason`，但客户端 MUST 以服务端发送的当前权威状态或顺序为准，不能自行猜测全局排名。

#### RANK-004：完整事件集的顺序确定性

给定相同完整事件集合、配置、`score_version` 和 `ranking_policy_version`，无论 Worker 到达顺序如何，最终去重结果、分数和有序列表 MUST 一致。实现 MUST 使用确定性 ID、合并规则和 Tie-breaker，不能依赖系统时间、随机数或字典遍历偶然顺序决定最终排名。

中间排名 MAY 因到达顺序不同而不同，这正是流式展示的预期行为；系统 MUST 保存中间状态以评估早期排名质量。

#### RANK-005：展示排名与数字引文编号 MUST 分离

实时列表位置 MUST NOT 直接生成 `[1]`、`[2]` 等数字引文标号。展示分排名表示相关性；数字引文编号表示正文中首次出现顺序，两者是不同排序语义。

数字编号只能在用户选择文献并基于快照导出时，由引文模块结合稿件插入位置统一计算。实时卡片 MAY 显示作者—年份预览，但 MUST 保证同一论文身份一致。

### 6. 实时结果与不可变快照

#### SNAP-001：快照触发与覆盖状态

快照 MUST 支持以下触发值：

- `all_completed`：全部计划 Worker 完成；
- `deadline`：达到配置的截止时间；
- `sufficient_evidence`：满足证据充分性策略并停止剩余低收益任务；
- `user_requested`：用户在任意实时状态主动创建；
- `cancelled`：用户或系统取消后保留当前状态。

每个快照 MUST 记录各库 `completed/failed/timed_out/cancelled/pending` 状态、实际覆盖范围、评分失败数、不可比候选数和终止原因。存在未完成或失败库时 MUST 设置 `is_partial=true`，界面和导出 MUST 显示部分覆盖提示。

#### SNAP-002：快照内容 MUST 不可变

快照至少冻结：

- `snapshot_id`、`query_id`、创建时间和触发原因；
- 当前有序结果及每项 `result_id`、`result_version`、`display_score`、排名和证据引用；
- `score_version`、`ranking_policy_version`、检索及索引版本；
- Worker 覆盖状态、错误和终止原因；
- 用户选择状态及后续导出所需稿件上下文版本。

快照创建后 MUST 以新 ID 持久化，MUST NOT 被 Worker 事件、校准器升级、用户后续操作或迟到结果原地修改。快照中的结果顺序已固定，但 `is_partial=true` 仍明确表示未覆盖全部计划数据源。

#### SNAP-003：实时结果与快照的关系

实时结果的 `provisional` MUST 为 `true`，提示仍可能插入或重排。快照中的项目和顺序不再变化；其完整性由快照级 `is_partial` 表达，MUST NOT 通过继续修改快照项目来表示“最终”。

用户 MAY 在全部 Worker 完成前创建并导出快照。系统 MUST 保留该提前快照，不得因为后续出现更高分结果而撤回或改变其内容。

#### SNAP-004：迟到结果 MUST 进入补充版本或迟到诊断

- 查询仍活跃且允许继续接收结果时，迟到结果进入实时状态；之后创建的快照使用新 `snapshot_id` 和新的覆盖状态。
- 查询已关闭时，迟到结果只写入 `late_result` 诊断，MUST NOT 修改任何既有快照。
- 新快照 SHOULD 通过 `supersedes_snapshot_id` 指向旧快照，但旧快照及其导出 MUST 保持可重现。
- 已复制或导出的内容 MUST 记录 `snapshot_id`；后续更新 MUST NOT 静默替换。

#### SNAP-005：导出一致性

导出模块 MUST 只读取快照，不得直接读取仍在变化的全局实时表。相同快照、用户选择、稿件上下文和引文样式 MUST 产生相同正文标记、文末条目和编号变更。

展示分顺序 MAY 用于用户选择文献，数字引文顺序 MUST 由正文首次出现位置决定。迟到高分论文只能出现在后续快照或补充导出中。

### 7. 评分失败、去重冲突与快照异常

| 异常 | MUST 行为 |
|---|---|
| 新分数缺少 `score_version` | 拒绝进入可比列表，记录 `invalid_score_version`，不猜测版本。 |
| 外部原始分无法校准 | 标记 `uncalibrated`，进入不可比分组，不与统一分直接比较。 |
| 同一 DOI 对应冲突书目 | 保留可信元数据来源及冲突详情；身份可合并，但不得静默覆盖权威书目。 |
| 无 DOI 且仅标题近似 | 标记 `possible_duplicate`，不得自动合并。 |
| 合并时证据重复 | 按 `evidence_id` 幂等去重，不增加无效版本。 |
| 排名事件重复或乱序 | 通过 `event_id`、事件序号和 `result_version` 幂等处理；无法确定当前状态时触发状态同步而非重复插入。 |
| 快照创建时仍有 Worker 运行 | 允许创建，但必须 `is_partial=true` 并列出未覆盖库。 |
| 快照后收到新事件 | 不修改旧快照；按查询活跃状态进入实时补充版本或 `late_result`。 |

### 8. 早期 TopM 与分数质量评估

#### EVAL-001： MUST 建立可控到达顺序测试集

评估数据 MUST 包含同一完整事件集合的多种 Worker 到达顺序：快库优先、慢库优先、随机顺序、重复论文交错、迟到高分和评分失败。每个测试用例 MUST 能重放到同一最终快照。

对时间点或已处理事件序号 `t`，记录：

```text
TopMOverlap(t) = |TopM(t) ∩ TopM(final)| / M
```

`M` MUST 大于零，并分别报告早期覆盖比例，避免只凭重合率误判检索完整性。对于中间状态和最终状态共同拥有的 `N` 个结果，SHOULD 计算成对排名反转率：

```text
RankInversionRate(t) =
  中间顺序与最终顺序相反的结果对数量 / (N × (N - 1) / 2)
```

#### EVAL-002：确定性验收门槛

以下属于 MUST 全通过的不变量，不以平均分抵消失败：

1. 同一 `score_version` 下，无关结果到达前后，旧结果 `display_score` 变化次数为 `0`。
2. 相同完整事件集合的所有到达顺序，最终去重数量、结果 ID、分数和排名完全一致。
3. 跨库同 DOI 最终只存在一个 `result_id`，重复证据无重复项，重放不增加 `result_version`。
4. 批次中新增极高或极低分结果，不重写其他结果的分数。
5. 未校准的异构原始分不会进入精确跨库排序。
6. 快照创建后，其序列化内容在迟到事件和事件重放后保持字节级或规范化 JSON 等价。
7. 实时展示排名不会改变已导出快照中的数字引文编号。

#### EVAL-003：校准与早期预判指标 MUST 分开报告

- 对 `calibrated` 分数，报告 Brier 分数、校准误差及分来源/主题切片；对 `bootstrap` 或 `uncalibrated`，MUST NOT 报告为已验证概率。
- 报告早期 `TopMOverlap`、`RankInversionRate`、首条有效结果时间及达到指定覆盖比例所需时间。
- 早期 TopM 指标 SHOULD 用于设定回归预算；未建立人工标注基线前，MUST 先报告原始值和置信区间，不得编造质量提升百分比。
- 评估 MUST 分别覆盖同口径全局重排、异构统一重排、来源校准和不可比回退四条路径。

### 9. 最小可执行示例

以下示例用于实现单元测试，不代表生产分数：

1. 库 A 返回论文甲，`display_score=91.4`，立即插入第 1 位并推送，不等待库 B。
2. 库 B 返回论文乙，`display_score=95.0`，乙插入第 1 位，甲变为第 2 位；甲的 `display_score` 仍为 `91.4`。
3. 库 C 返回同一 DOI 的甲及新证据，系统保留甲的原 `result_id`，合并来源与证据；若重评后为 `92.0`，只更新甲的 `result_version` 和位置，不产生新卡片。
4. 用户此刻创建快照 S1 并导出，S1 标记未覆盖库 D；随后库 D 返回 `display_score=99.0` 的论文丁，丁只能进入实时结果或补充快照 S2，S1 与其导出保持不变。
5. 使用相同事件集合按 A、B、C、D 与 D、C、B、A 两种顺序重放，最终 S2 的结果身份、分数和排名 MUST 相同。

### 10. 跨模块接口点

- **Worker → 本模块**：必须提供论文身份、来源原始分、证据集合、关系核验结果、文献类型资格及可追溯状态；只有满足资格门槛的结果才进入推荐集。
- **评分服务 → 本模块**：必须提供确定性 `display_score`、`score_version`、`score_kind`、`calibration_status` 和可诊断错误；禁止只返回无量纲浮点数。
- **本模块 → 数据契约层**：需要 `result_id`、`result_version`、`dedup_key`、`display_score`、`provisional`、`score_status`、`source_ids`、`evidence_ids` 和快照覆盖字段。
- **本模块 → API/事件层**：使用 `recommendation_created`、`recommendation_merged`、`recommendation_reranked` 和 `search_finalized`；状态先持久化再发事件，事件重放必须幂等。
- **本模块 → 前端**：提供统一展示分、当前名次、暂定状态、分数/排名变化原因、不可比状态及 Worker 覆盖进度。
- **快照 → 引文与导出模块**：只消费不可变快照、用户选择和稿件上下文；数字编号不按实时名次生成。
- **配置层**：必须提供共同重排器/校准器版本、来源校准阈值、排序策略版本、快照截止条件和迟到结果策略；所有影响结果的配置均纳入快照。
