# 真实摘要检索与回答质量对照协议 v2

`suite_id=abiotech-abstract-ood-v2`。本协议在首次完整 v2 A/C 运行前建立。旧 v1 文件和结果保留，不覆盖。

## 1. 目的与范围

测量候选召回、查询内排序、研究问题回答深度、摘要证据忠实度、范围/因果/版本约束、行动经济性与跨库治理。真实题录/摘要只支持候选发现，不支持声称已读全文、PDF 坐标或可直接引用的全文证据。

外部方法参考：[BEIR](https://arxiv.org/html/2104.08663v4) 的词法强基线与领域外泛化，[BRIGHT](https://brightbenchmark.github.io/) 的推理型查询，[ALCE](https://aclanthology.org/2023.emnlp-main.398/) 的检索/引用支持分离，[RAGChecker](https://github.com/amazon-science/RAGChecker) 的检索与生成错误分解，以及 [RGB](https://arxiv.org/html/2309.01431v2) 的噪声和不可回答问题。这里不复制它们的标签，也不与公开榜单比较。

## 2. 语料与版本

- `extended-corpus.json` 固定 493 篇真实题录/摘要：50 篇 aBiotech 核心论文 + Europe PMC 六类近邻主题各 75 条原始查询结果去重后的 443 篇干扰论文。六类查询在本快照均为 `complete`。
- `core_work_ids` 标识原 50 篇，不能把新增干扰论文当成已有金标目标。所有目标、背景和排除项由逐篇阅读真实摘要后编写，是来源核查的便利金标，不是独立双人科学标注。
- 核心 50 篇的公开更正/撤稿字段来自 Europe PMC 快照，含核查时间。2 篇原文有更正，当前快照未标记撤稿；这永久不等于“绝无撤稿”。新增干扰项未核查则保留 `unknown`。
- 原始 PDF 不进入公开运行包，不重新分发。公开 manifest 不含完整摘要。新增记录没有撤稿核查不意味着已通过全文引用资格。
- `tools/quality_corpus_expand.py` 逐主题缓存公开查询；失败主题显式标 `failed`，不能把不完整快照称为完整。

## 3. 调参、开发诊断与留出

- **调参集 13 案**：v1 的 8 案 + 无语义同词提示、花生结瘤、油脂品质、中文表观组学 4 案 + 库外零答案案。其中 3 案重复三次，共 19 次/系统。允许据此修改实现与训练 LTR。
- **开发诊断集 7 案**：v1 原留出案中的 7 案。它们已在 v2 冻结前用于开发期检索诊断，不再称为未见留出；原零答案案已转为调参，用于向量阈值和凑数风险控制。
- **留出集 8 案**：新增方法定量、比较表型、跨界 RNAi、转录后调控、热激方向、单细胞、证据缺口与苜蓿编辑。只在实现冻结后正式运行。部分目标论文在其他任务出现，因此这是未见问题留出，**不是跨主题完全独立的证明**。
- 一旦使用留出结果修改实现，该案降级为开发诊断；后续需换未见任务。不得为胜负修改金标、删除失败样本或降低证据门槛。

## 4. 对照与模型

- 产品：Skill 引导的自由 LangChain Agent，真实 `search_literature`，SQLite，真实 DeepSeek 候选理由。稀疏和 E5+RRF 分别记录，不混用旧实现结果。注入真实理由生成器时必须声明 `provider=deepseek`，运行前置检查确认工具报告模型已配置；不得让仪表误报影响 Agent 回答。
- 对照：Kimi Code CLI 每个任务全新会话，官方 `deepseek-flash` 别名，正常用户工具/Skill 与全局 AGENTS.md；隔离目录只有 `corpus.json`，没有项目 Skill、SPEC、金标或产品接口。
- 双方及匿名评价使用同一官方 DeepSeek V4.1 Flash。产品 Agent/理由/评价关闭 thinking 并使用 temperature=0；Kimi 保留现实配置并记录实际模型与 thinking。资源包络、上下文和工具不同，不能把差异单独归因于框架。
- 双方最多三轮检索。读取已取得材料不算新一轮。Kimi 隔离子进程使用 `KIMI_LOOP_MAX_STEPS_PER_TURN=32`，避免语言/文件读取步骤上限与三轮检索上限混淆；不修改用户全局配置。记录工具动作、错误、时延、输入/输出/cache token、脱敏可见输出和实现哈希；真实秘密泄漏阻断发布。

## 5. 指标与限制

1. `retrieval_eval` 直接测量 Worker 候选 Recall/Hit@5、nDCG@5，不经过 Agent 或证据资格闸门。零答案任务单列噪声候选，不能伪造 Recall 分母。
2. 产品排名按各 query 候选顺序；Kimi 按最终可见答案候选顺序。不同接口的排名不是同接口纯检索器比较。
3. 自动检查：完成、模型一致、预算、必要检索、目标 DOI/稳定 ID、必要比较对象与摘要边界。明确要求的数值/术语只作辅助检查，语义上正确的等价表达需要人工/匿名评审核对。`excluded_dois_in_answer` 只检查是否出现排除项 DOI，出现时不作自动通过或失败判定，因为合法回答可能需要说明其更正/排除角色；是否真正误交付由匿名评审结合真实摘要核查，不能机械按字符串出现扣分。
4. 匿名六维评价：相关性/完成、证据忠实、范围/因果/冲突、约束/检索决策、行动经济性、表达/能力边界。A/B 标签按运行 ID 确定性交换，不合成全局总分。
5. 硬门槛：完成、模型一致、无秘密泄漏、预算内、无评审确认的伪造、无关推荐、因果/全称夸大或把摘要冒充全文。每个严重评审标记必须附该侧回答的逐字证据片段，结构校验确认片段真实存在；无具体越界句不得打严重标记。自动评判不是科学双人金标。
6. 比较 sparse、dense/RRF 与 LTR 时，LTR 只在调参集进行 leave-one-case-out 验证。保存特征版本、训练数据哈希、固定种子、权重和各折结果；无真实增益就保留简单基线。
7. 复现比较同一输入的候选 ID、分数、顺序与内容指纹，不比较随机 query_id、时间戳或要求自然语言理由逐字相同。Worker 到达顺序不同不得改变最终同 Work 分数、来源集合或排名。
8. 小样本均值须结合逐案结果和失败类型，不据此宣称普遍优于 Kimi。成本只作观测，不能抵消召回、证据、范围或安全失败。

## 6. 可复现命令

```bash
uv run python tools/quality_corpus_expand.py
uv run python tools/quality_suite_v2.py
uv run python -m tools.quality_benchmark prepare
uv run python -m tools.quality_benchmark.retrieval_eval \
  --split tuning --retriever sparse --repeat 3 \
  --output benchmark/quality/retrieval/<version>-sparse.json
uv run python -m tools.quality_benchmark.retrieval_eval \
  --split tuning --retriever hybrid --dense-threshold 0.80 --repeat 3 \
  --output benchmark/quality/retrieval/<version>-hybrid.json
uv run python -m tools.quality_benchmark run \
  --name <new-run-name> --split tuning --retriever <chosen> --concurrency 2
uv run python -m tools.quality_benchmark run \
  --name <new-run-name> --split holdout --retriever <chosen> --concurrency 2
```

结果目录不得覆盖；`--resume` 只复用同一实现哈希的逐次脱敏记录。`plan.json` 固定任务/语料/实现，`runs.jsonl` 保存输出，`judgments.json` 保存匿名映射，`summary.json` 和 `report.md` 汇总。失败或中断试跑不计入正式结果。`rescore` 保留原实现、语料、任务、检索次数和修正原因，不重复原 Agent 调用。
