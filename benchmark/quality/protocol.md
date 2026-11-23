# aBiotech 真实摘要开发集外挑战协议

版本：`abiotech-abstract-ood-v1`。本协议和 `tasks.json` 在首次完整 A/C 运行前建立。旧 fixture 基准只作回归，不用其结果宣称真实检索质量。

## 1. 目标与外部依据

测五类能力：候选召回、查询内排序、最终回答的相关性、摘要证据忠实度、范围与文献类型约束。OOD 指**未参与本轮实现决策的任务与主题**，不意味着已覆盖整个生物文献分布。

设计参考：

- [BEIR](https://arxiv.org/html/2104.08663v4)：领域外泛化、词法强基线、分任务 nDCG/Recall，而非只测分布内样本。
- [BRIGHT](https://brightbenchmark.github.io/)：表面词汇不足以表达相关性的推理型查询。
- [ALCE](https://aclanthology.org/2023.emnlp-main.398/)：检索与逐条引用支持分开评估。
- [RAGChecker](https://github.com/amazon-science/RAGChecker)：把错误定位到检索或生成，不只看最终印象。
- [RGB](https://arxiv.org/html/2309.01431v2)：噪声、不可回答、冲突信息鲁棒性。

这里没有直接复用这些外部数据集的标签，也不把本项目的分数与它们的公开榜单比较。

## 2. 语料与金标边界

- `tools/quality_corpus.py prepare` 从本地 50 份 PDF 的文件名得到 DOI，使用 Europe PMC `core` 元数据补齐标题、摘要、作者、类型与 PubMed/PMC 标识。50/50 均有摘要。
- 原始 PDF 不进入公开运行包，不重新分发。公开 manifest 只记录元数据与摘要长度；模型运行使用 `data/runtime/quality-corpus/real-corpus.json`。
- 仅对一篇更正 PDF 做了 MinerU 文字层抽查，**没有完成 50 篇全文解析、逐页阅读顺序或证据定位验收**。
- 金标由逐篇阅读真实标题/摘要后编写，包含目标、背景、排除项和理由。它是来源核查的便利金标，不是独立双人科学标注；同模型盲评也不能把它升级为人工金标。
- 更正声明保留在库中以测试身份区分，不作为独立原始研究。摘要中的引文 DOI 可以是真实参考，不等同于推荐了该文献。

## 3. 对照与隔离

- C：真实 LangChain Agent、项目 Skill、`search_literature`、真实 `MetadataCorpusAdapter`、SQLite、真实 DeepSeek 候选理由。
- A：Kimi Code CLI，每个任务全新会话，官方 `deepseek-flash` 别名，正常用户 Skill、文件/命令/联网工具与全局 AGENTS.md。隔离目录只有 `corpus.json`，没有项目 Skill、SPEC、金标或产品接口。
- 双方与评判均只用官方 DeepSeek V4.1 Flash。产品 Agent、候选理由和评判关闭 thinking；Kimi 使用其现实模型配置并记录实际 thinking。资源包络不同，不能把差异归因于框架。
- 双方最多三轮检索。每次运行保留模型、工具动作、时延、输入/输出/cache token、错误和脱敏可见输出。真实密钥泄漏阻断发布。
- 8 个调参基础任务（含重复共 14 次/系统）用于形成性修改；其余 8 个留出任务只在实现冻结后用于最终检验。留出集一旦用于改实现，就降级为后续调参集，下一轮必须新增未见任务。

## 4. 指标与判定

1. **候选阶段**：`retrieval_eval` 直接记录目标是否已在 Worker 候选中丢失、Recall/Hit@5、nDCG@5；不经过 Agent 或证据资格闸门。
2. **端到端自动检查**：完成、模型一致、预算、必要检索、目标 DOI/稳定 ID、指定排除 DOI、必要比较对象、摘要/全文边界披露。
3. **排序**：产品使用每个 query 内的候选顺序，不跨 query 比原始分；Kimi 使用最终可见答案中的候选顺序。二者不是同接口纯检索器比较。
4. **匿名六维评价**：相关性/完成、证据忠实、范围/因果/冲突、约束/检索决策、行动经济性、表达/能力边界。A/B 标签按运行 ID 确定性交换，不合成全局总分。
5. **硬门槛**：完成、模型一致、无真实秘密泄漏、预算内、无评审确认的伪造、无关推荐、因果/全称夸大、把摘要冒充全文等。
6. **零答案与相关非因果**：不能只用“目标词没出现”判定；由带真实摘要的评审区分拒绝、背景候选和错误推荐，并人工抽查原始输出。

成本只作观测，不能抵消召回、范围、证据或安全失败。不同 thinking、上下文与结果数量意味着成本差异不能解释为纯框架效率。

## 5. 复现与产物

```bash
uv run python tools/quality_corpus.py prepare
uv run python -m tools.quality_benchmark prepare
uv run python -m tools.quality_benchmark.retrieval_eval --split tuning \
  --output benchmark/quality/retrieval/<version>.json
uv run python -m tools.quality_benchmark run \
  --name <new-run-name> --split tuning --concurrency 2
uv run python -m tools.quality_benchmark run \
  --name <new-run-name> --split holdout --concurrency 2
```

结果目录不得覆盖；`--resume` 只复用相同实现哈希的逐次脱敏记录。调参和完整运行使用不同名称。`plan.json` 固定任务/语料/实现哈希，`runs.jsonl` 保存逐次输出，`judgments.json` 保存匿名映射与评价，`summary.json` 和 `report.md` 是汇总。失败或中断的批次移到运行时无效试跑区，不计入正式结果。

评分器错误可以修复，但必须记录原因、保存原始 Agent 输出、离线测试修正，并用新运行对照；不得看到结果后调整金标或门槛来制造胜利。可用下面的 `rescore` 只重跑评判，不重复原 Agent 调用；新报告必须写入新目录，保留原 Agent 实现哈希、评分器实现哈希、原检索计数及修正原因，不覆盖旧报告：

```bash
uv run python -m tools.quality_benchmark rescore \
  --source-name <original-run> --name <new-run-name> \
  --reason "<可审计的通用计量/评判修正>" --concurrency 2
```
