# 多学科大库检索协议 v3

本版本继承 `protocol-v2.md` 的候选发现、证据边界、评价与失败报告规则，只扩大语料和开发任务，不改写任何 v2 金标或结果。

## 语料与任务

- `data/runtime/quality-corpus/large-corpus.json` 是 11,002 篇真实 Europe PMC 题录/摘要的版本化快照：保留 v2 的 493 篇，新增 10,509 篇，覆盖 65 次已完成主题查询。DOI 唯一；仍不是 Europe PMC 实时全库。
- `tasks-v3.json` 保留 v2 的 28 个基础任务，新增 8 个目标位于原 50 篇核心之外的开发/调参任务，覆盖参考基因组、跨语言空间组学、定量方法、蛋白质结构/序列设计、临床基因治疗、变异检测和群体基因组。
- 新任务由逐篇阅读真实摘要后建立，全部进入调参集，不是未见留出，也不证明跨领域统计优效。原留出 8 案没有重新标注；其既有结果已被使用前不得称为未见测试。
- 新增记录撤稿状态保持 `unknown`，候选不能因此冒充已核查引用。原始查询响应分主题缓存，抓取失败阻断最终快照；原子写入防止半成品替代完整文件。
- 公开 `large-corpus-manifest.json` 不含完整摘要；不重新分发 PDF。

## 实施与性能对照

- E5 文档仍采用 `query:` / `passage:`、归一化和固定 RRF，评分语义不变。向量缓存改为带模型、快照、ID 顺序绑定的 NumPy NPZ 矩阵，减少 JSON 向量解析和 Python 点积成本。
- 同一调参任务分别报告大库稀疏与混合检索的 Hit@5、nDCG@5、逐案候选与三次复现；若大库破坏原有目标，应保留失败并解释，而不是删除任务或调金标。
- 性能验收记录语料字节、向量字节、索引时间、查询时延及服务器内存。因单次小样本不能宣称生产容量，只用于当前版本回归。

## 命令

```bash
uv run python tools/quality_corpus_expand_v3.py --per-topic 250
uv run python tools/quality_suite_v3.py
uv run python -m tools.quality_benchmark prepare
uv run python -m tools.quality_benchmark.retrieval_eval \
  --split tuning --retriever sparse --repeat 3 \
  --output benchmark/quality/retrieval/<version>-sparse.json
uv run python -m tools.quality_benchmark.retrieval_eval \
  --split tuning --retriever hybrid --dense-threshold 0.80 --repeat 3 \
  --output benchmark/quality/retrieval/<version>-hybrid.json
```

Agent 与真实理由的正式运行仍需显式使用 `run`，会产生实际模型调用；不得用直接检索指标冒充端到端回答质量。

## 质量优先口径（quality-first-v1，2026-11-16）

本版本覆盖继承协议中的三轮检索限制：历史 `max_retrieval_rounds` 字段仅为兼容旧任务哈希保留，不再传给被测系统、不参与自动预期或科学硬门槛。召回、排序、约束、摘要忠实和范围/因果是质量指标；检索次数、耗时和成本只作资源指标。产品安全边界为 900 秒整体超时、32 次检索工具与 96 步递归，Kimi 为 900 秒进程超时与 96 步。达到技术上限或进程失败仍计完成失败，但不能把正确回答因补查较多判成科学失败。
