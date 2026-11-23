# 公开诊断题：已有实物与尚待构建的部分

[能力与比较协议](protocol.md) · [逐题审计索引](public-index.jsonl) · [仅给评审培训的判分示例](calibration.json)

## 已经取得并按题编目：2,335 条，不是 2,335 个独立研究

| 发布源、取样边界 | 实际编目 | 先看哪一项 |
|---|---:|---|
| Ajith 等，[LitSearch](https://github.com/princeton-nlp/LitSearch)，[固定数据 revision](https://huggingface.co/datasets/princeton-nlp/LitSearch/tree/9573fb284a1026c998df47024b888a163f0f0e25) 的 `query/full` 全部 | **597** | 作者手写 246（ACL 155、ICLR 91）；引用上下文生成 351（ACL 98、非 ACL 253）。563 题只列一个目标，34 题有多个；共享目标连通后 534 组。这些是已知目标，不是穷尽相关文献。 |
| Dasigi 等，[QASPER](https://aclanthology.org/2021.naacl-main.365/)，[官方数据卡](https://huggingface.co/datasets/allenai/qasper)，v0.3 **test** 全部 | **1,451**，416 篇 | 原始全库为 5,049 问/1,585 篇，不据此夸大测试集。就“本文能否作答”而言：1,208 问所有已有标注认为可答，79 问均认为不可答，164 问意见不一。可答不意味着各答案或原文位置完全一致；有分歧不当作确定金标。 |
| DeYoung 等，[Evidence Inference 2.0](https://aclanthology.org/2020.bionlp-1.13/)，[作者发布](https://github.com/jayded/evidence-inference/tree/a661e8c14f973398380c8865cf2f27a535aaaf6d/annotations) 中以 **`ev2_test_article_ids.txt`** 限定 PMCID 的全部 prompts | **287**，94 篇 | 指定报告、干预、对照、结局；不把它叫整个 EI 合并测试集。有 524 条标注行。只看 `Valid Label=True` 后，257 题有一种明确方向，3 题方向冲突，27 题没有通过验证的方向；没有合格标签绝不是“治疗没有差异”。再检查 `Valid Reasoning`、对应原文与偏移，不能仅因方向一致便给引文积分。 |

题索引包含 `id`、仅供分簇的 `family_hint`、审计切层 `stratum`、原始输入指纹。LitSearch 用固定文件零基行号 `lit:NNNN`，QASPER 用原 `question_id`，EI 用原 `PromptID`。指纹为原题字段数组紧凑 JSON 的 UTF-8 SHA-256（不重写原文空白）；定义见 [离线编目器](build_index.py)。家族提示仅识别同组目标或同一给定论文，不替代跨语种、跨来源、同队列、不同版本的人工家族核查。

**研究者可直接翻的三个真例**（属于公开诊断，不是隐藏题）：

- `lit:0351`（手写）：问是否有利用稠密短语检索来自动整理实体词典的研究。发布者列出的 S2 `corpusid` 为 `252907578`。只找到一篇通用稠密检索论文不等同完成；也不能先验否定新找到的足够直接的替代研究。
- `qas:f7662b11e87c1e051e13799413f3db459ac3e19c`（`arxiv:1911.10742`）：问该工作胜过哪些基线。一个官方回答是 TransferTransfo 和 Hybrid。“We compare MISSA mainly with two baseline models:” 是真实原文，却没有列出它们是谁，也不能单凭该句证明胜过。这正是字符串命中不应当作科学通过的例子。
- `ei:11214`（`PMC1794625`）：问特定两种呼吸正压方案在**入睡潜伏期**上的比较。有效标注方向为 `no significant difference`。换成另一睡眠指标、写成“两个方案等效”、或给出一般临床建议，均不等于报告了本题所问的结果。

## 文件与权限

本地 `data/runtime/benchmark/capabilities-v1/source/` 有下列 5 个公开原始文件，总计 11,985,750 字节；含金标，**绝不挂载给任一被测方**。QASPER 压缩包内只读取 `qasper-test-v0.3.json`，未执行随包 evaluator。生产索引、应用依赖、服务配置未变。不在网页、仓库或 MCP 对外重新分发全文；论文许可与标注/代码许可分别核对。

| 原始文件 | 固定来源 / SHA-256 |
|---|---|
| [LitSearch 查询 Parquet](https://huggingface.co/datasets/princeton-nlp/LitSearch/resolve/9573fb284a1026c998df47024b888a163f0f0e25/query/full-00000-of-00001.parquet) | `38cdbe4a6b7a7f5776d08055bfbf6e4511000aa2a523866592b4bb273388b914`（与上游 LFS 对象一致） |
| [QASPER test v0.3 压缩包](https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-test-and-evaluator-v0.3.tgz) | `72a52a41193e2838b8074f80ac074b94f956b84886c36a61c58a7df4171bdd72`；内部 JSON `6e29ad410e6e39aa1936017fb965b30a20eb2e7751997f55b97c9d281aa884e5`。数据卡标 CC BY 4.0。 |
| [EI prompts](https://raw.githubusercontent.com/jayded/evidence-inference/a661e8c14f973398380c8865cf2f27a535aaaf6d/annotations/prompts_merged.csv) | `0d70d9e1e78d113fdbbd4919310eab7768d6a328368f7d8f9d5a3b48f99c25d3` |
| [EI annotation](https://raw.githubusercontent.com/jayded/evidence-inference/a661e8c14f973398380c8865cf2f27a535aaaf6d/annotations/annotations_merged.csv) | `a5dd1a3583e5105a0f77d3e1c695008879859425c9a93dfbc07054ef462e0255` |
| [EI 指定文章分割](https://raw.githubusercontent.com/jayded/evidence-inference/a661e8c14f973398380c8865cf2f27a535aaaf6d/annotations/splits/ev2_test_article_ids.txt) | `3010f0df3b322fe9d8b4bbc2b9c11a57e1f3586f70b06a5402255e62c310ba9e` |

审计索引 SHA-256：`b107536c67633adadf774d2cfbc7da55a6225b64b02ae3ff2255cdbe674cd887`。复核命令只重建内存中的索引并比对字节，不联网、不启动产品、不调用模型。本机使用已安装的 Python 3.14 / PyArrow 23.0.1 读取 Parquet，不向产品环境新增依赖。

```sh
py -3.14 -B benchmark/capabilities-v1/build_index.py data/runtime/benchmark/capabilities-v1/source
```

本地的[纯题面文件](../../data/runtime/benchmark/capabilities-v1/inputs/requests.jsonl)与审计索引分离，2,335 行，SHA-256 `43797d61363fb6c53d13ef062ce490741bcbce11cb69cd7f2b15fadad4b62ecb`：只有不透明追踪键和问题；**只在给定论文阅读题**另有指定文档标识。EI 用中性的比较问法渲染原始三元组；不把方向、答案、`Valid Label`、证据句、目标 ID 或注释当提示。分发时只发单题 `question` 与必要的 `document`/已清理材料，不发管理键，不能直接给系统读整个评测目录。用同一脚本加 `--emit-requests <尚不存在的文件>` 可在本地重建此文件，不覆盖原件。

## 已选题源或已设计，但不能写成“已建成”

- SciFact-Open 大库、SYNERGY+ 筛选集、ScholarQABench 综合题及 Crossref 纠撤状态作为定向题源，本轮核对了上游说明，未下载整库或逐篇核验。正式用前需固定数据/语料版本、许可、公布实际纳入和剔除原因；连续时间状态至少保存公告时间与观测时间。已用过的 SciFact 开发集不计入上述 2,335 条。
- 本轮未创建或发布 LitSearch 全文检索库、EI 全文包；QASPER 文件中虽附正文，也未为模型导出无标注正文。以后准备材料时 QASPER 只投影文档 ID、标题、正文与许可的图表，删除 `qas` 等金标；EI 从同版 `txt_files` / XML 取对应 PMCID，保留偏移在**上游指定文本表示**上的含义；LitSearch 用固定同版检索语料，不给任何目标提示。给定论文任务与全库检索任务必须分报。
- 48 家族先导及正式 320 家族是独立招募设计，不是已经拿到的稿件或专家签字。未独立采集、去重隔离、双审和冻结前，不能对外称为未见金标；产品特定的 96 对治理材料也是取材设计、未收齐。12 张校准卡是作者自编判分示例，不计入科学测试样本。
