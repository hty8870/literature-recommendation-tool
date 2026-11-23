# Astra v4 独立摘要候选发现评测协议

## 来源、身份与冻结

- 套件：`astra-independent-abstract-v4`；固定快照 `abiotech-with-europepmc-multidomain-v3-n11002`，11,002 条真实题录/摘要。只测候选发现、查询内排序和摘要忠实度，不测全文阅读、页码定位、引用资格或现实全库查全率。
- 命题使用 `packy_gpt/gpt-6-astra`，事件日志会话为 `session_c1ee263a-bf22-4ee9-9ef0-1038f902b978`。作者本地检查完成，但进程因 `provider.auth_error: 403 用户额度不足` 返回 1，没有独立 Astra 终审，不伪称正常完成。命题稿、报告、缺口筛查、失败流和会话凭据保存在 `authoring/`。
- 作者原文件 `tasks.authoring.json` 的根键为 `cases`，SHA-256 `b222e85c679f405efc8436efb73afdb8c6a46e3539bfb5f3702048a803f6b9f4`。交付 `tasks.json` 只把该根键规范为 `tasks`，SHA-256 `5d6c2549dc46be036498300ebf842bc2126ab818e1736989bf616b8ac60d7b4c`。`review/normalization.json` 记录深比较和两种哈希，科学任务对象未变。
- 独立终审使用另一个 `deepseek/deepseek-flash` 会话 `session_74944c97-a40c-4440-aa4e-04189c16f5b9`，返回码 0，`review/FINAL_REVIEW.md` 为 `freeze-approved`，0 blocker、0 major、3 个非阻断 minor。终审不是 Astra 终审，也不是人工双人金标。其 SHA-256 为 `fe3e859191230d291262d66d35aceb4b9a6a08901b68c705de2a767b2707994a`。
- 只读校验 `review/validation.normalized.json` 为 `no_local_structural_issues`，对应兼容 `cases/tasks` 的校验脚本。`review/validation.root-key-first-failure.json` 保留中间失败，不用成功输出覆盖历史。
- 任务在首次运行前未被用于修改实现。若运行后据此修改实现、提示、评分或检索策略，套件即转为开发证据，必须另命新题做独立确认。不得挑最好一次、丢弃失败案或为胜利改金标。

## 任务组成

- 恰好 16 案，14 个正目标案、恰好 2 个库内证据缺口案；17 个去重 required 目标，3 个双目标案；86 条逐字证据锚点，46 次角色标注，44 条不同记录。
- 非植物/农业 14 案，中文输入 9 案，keywords/abstract 各 8 案。覆盖精确身份、语义 OOD、跨语言、范围/因果、定量方法、多对象比较、文献角色与证据缺口。
- 稳定性案为 `v4-01`、`v4-03`、`v4-06`、`v4-14`，每案独立上下文重复三次；其他 12 案各一次，共 24 案次/系统。`tuning_case_ids=[]`、`development_case_ids=[]`。
- `corpus_path=data/runtime/quality-corpus/large-corpus.json`。语料原始字节 SHA-256 `8ee2bc9d6c3cad4f135fdaeb4642df54983d5fa977c0cc2002e851b7dc95e737`；records 的 RFC8785/JCS 规范化 SHA-256 `cb0395aab30acb937ca546cf53c0703b02e01f20c221ba1cef80b05b8502f3db`，两种哈希口径不得混用。
- 每个标注角色都有逐字存在于对应 title/abstract、至少 24 字符的锚点。`unknown` 撤稿状态既不证明撤稿也不证明未撤稿；缺字、截断、版本和文献角色不得靠模型记忆补齐。

## 输入与隔离

- 只把 `baseline_query` 和合法 `request` 交给被测系统，`case_id` 仅用于评测关联。不得提供 `expect`、金标、锚点、角色、缺口标签或命题/审校报告。
- keywords 保留原始关键词；abstract 保留完整研究论述。若接口只能文本输入，使用 `baseline_query`；支持结构化输入时同时提供完整约束。不得漏掉否定、比较、时间、方法或证据强度条件。
- 双方均使用同一模型、同一冻结语料与相同研究请求。Kimi Code 每次新建会话，不加载项目 Skill 或项目 AGENTS；保留正常通用工具能力，不限制其扫描方式制造优势。
- 预检命令：`uv run python -m tools.quality_benchmark prepare --suite benchmark/external/astra-v4/tasks.json --protocol benchmark/external/astra-v4/protocol.md`。

## 质量优先与资源边界

- 检索轮数、耗时、token 与成本只作资源指标，不作为自动预期、科学硬门槛或胜负抵消项。用户明确以质量为核心，不设置三轮淘汰。
- 双方技术上限均为 900 秒与 96 步；产品另有 32 次检索工具失控保护。进程失败、整体超时、无输出仍计完成失败，不把必要补查本身视为科学错误。
- 重复案使用新的 Agent 上下文；同案例同租户只共享持久化的真实理由缓存，不暴露旧回答或旧对话。不同案例/租户不共享私有缓存。模型、提示、证据、版本、撤稿及文献类型必须纳入缓存输入。
- 记录查询改写、各轮候选 ID/分数、工具调用、最终回答、实际模型与失败。不得将自主补查的次数本身当成结果不稳定或低质量。

## 候选、角色与评分

- 从最终自然语言回答的主要推荐清单提取最多五个有序、可解析的 work_id，按首次出现去重。明确作为背景、反例或排除说明的引用不占主要推荐位，但保留在原始回答中；另存工具阶段候选，区分未召回、排序靠后与读后答错。
- required 是必要核心记录，多目标必须分别覆盖。background 可解释背景但不能替代核心；forbidden 是本案不满足必要条件的近邻，正确说明其局限不算错误。未标注记录不自动无关，应另记合理替代候选和证据。
- 有 required 时，报告任一目标 Hit@5、全目标 Hit@5、required coverage@5 及 required 二元 nDCG@5。required=1，background/forbidden=0，未标注候选暂记零增益但显式标为未判断，不能据此直接算无关推荐错误。nDCG 只在每个查询内部解释，不是跨库概率。
- 缺口案的 Hit@5、coverage@5、all-required@5、nDCG@5 均为 N/A。单独评价是否明确说明当前快照摘要未提供全部条件的证据、近邻缺哪项、是否误荐不满足条件的记录；不得声称现实世界不存在研究。
- 完成度、伪造/无关推荐、范围或因果错误、文献身份、约束覆盖、证据边界分别评审。严重标签须附回答中的逐字证据。研究设计、分母、比较方向、文献角色、年份/版本和摘要未报告项必须逐篇绑定，不能跨记录拼接。
- `required_response_terms` 仅作自动定位辅助，等价科学表达由语义评审核对；字符串顺序不是完整科学判断。

## 稳定性与解释边界

- 保存 `stability.json`：比较实际查询哈希、候选集合、顺序、同尺度原始分、最终候选和回答差异。只在相同 work、query 序号和评分版本/语料组内比较原始分；缺失或不可比不能当作零分或相等。
- 查询次数变化、Agent 计划变化、检索/评分变化、文本变化分别记录。文本不同不自动等于科学漂移，需要回查原回答与来源，不能仅据哈希宣称语义稳定或不稳定。
- 报告自动预期、匿名科学评审、候选排序、最终覆盖、理由成功率和资源包络，不合成虚假的总分或普遍优效结论。
- 产品工具候选顺序与 Kimi 最终回答顺序不是同接口纯检索器比较；同模型匿名评审不等于独立人工标注。样本只有 16 案，目标标注不穷尽，语料采样有偏，不能外推到所有科研领域或全文引用能力。
