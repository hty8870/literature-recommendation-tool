# 文献推荐工具

面向科研写作的文献检索与引用辅助工具。可以用关键词、摘要或 PDF/DOCX 稿件发起检索。在连续对话中，各数据库流式返回候选，模型结合来源证据重排，用户可以核查依据并复制所需的引用格式。题录候选不是可直接引用的证据；没有合法原文或证据不足时，系统不会生成引用建议。

本仓库是**当前开发源码**，含项目、项目内共享选库和经用户审批的数据源发现功能。这些功能不代表[线上试用站](https://biodataagent.com/literature/)已经更新。匿名实例不用于未公开稿件或保密材料。

## 克隆后直接运行

安装 [uv](https://docs.astral.sh/uv/)。在仓库根目录运行一条命令，uv 会选择 Python 3.12 并按锁文件准备依赖：

```sh
uv run --frozen citation-tool
```

打开 **<http://127.0.0.1:8765/>**，默认选中三个真实数据源，不需要另下数据包或登录。没有模型 Key 时，点击首页「无需 Key，试用本地检索」，或打开 <http://127.0.0.1:8765/?local=1>，输入 `LFY2, soybean`。这个访客入口只查两套本地库，无需外网，不生成模型理由、规范引用或全文证据。

| 默认数据源 | 随仓库交付 |
| --- | --- |
| Europe PMC 在线 | 适配器。发起检索时由运行服务的机器实时访问 Europe PMC，不依赖试用站。 |
| 多学科文献快照 | 11,002 篇题录，其中 4,400 篇附可公开分发的摘要。 |
| aBiotech 老师样例库 | 50 篇题录，50 篇均附摘要。 |

两套本地库保留线上快照的全部文献标识。摘要按各条记录所列的 CC BY 或 CC0 及所链接的原文许可使用，保留题名、作者和来源；仅做 HTML 文本规范化。不满足公开分发条件的摘要不提交，条目标为「仅题录」，可按题名检索并跳转来源，不参与模型推荐排名。[Europe PMC 版权说明](https://europepmc.org/Copyright) · [语料与 SHA-256 清单](corpora/manifest.json)。仓库不含论文 PDF、模型密钥、账号或历史数据。

### 升级已有部署

先停止旧服务；有重要数据的实例先备份 SQLite 和稿件目录。在**同一个仓库目录**运行：

```sh
git pull --ff-only && uv run --frozen citation-tool
```

保留原环境变量和数据路径，无需重建账号、导入论文库或重装前端。Git 不能快进时会停下，不要强制覆盖本地修改。由 systemd 等管理的实例，拉取后用原服务管理器启动，不再额外启动第二个进程。原 Uvicorn factory 命令仍可使用；短命令可传 `--host` 和 `--port`。

`uv run --frozen citation-tool --check` 只验证启动：检查前端文件、语料、配置和显式启用的 E5 缓存，使用临时 SQLite，不接触旧数据库、不请求在线库或模型。正式启动后的 `/healthz` 同样不能证明真实检索或重排可用。

### 正式演示前的联网验收

在**实际演示服务器**设置好服务端默认模型后，从同一个仓库运行：

```sh
uv run --frozen citation-tool --check-live
```

它用公开关键词 `lfy2` 走正常 Agent 接口，以临时 SQLite 实际调用模型和 Europe PMC；验收真实助手答复、完整 LLM 排名、与论文原文绑定的推荐理由、可回放的流式事件，以及样例论文的 APA/Vancouver 书目。命令只报告本轮实际用过的来源；不要求自由 Agent 每次都查满三个库。使用与正式启动相同的配置，不写会话数据库、不读取用户稿件，不需要登录。会使用少量模型额度；模拟快慢库、旧 `pointwise` 模式、本地备用检索、上游失败或缺少实际名次均不会通过。裸关键词不预设科学论述，因此不把生成全文引用建议作为这项验收的要求。

### Linux 演示机一键启动或更新

已有 Git 克隆与 uv 的机器，可把 [`tools/update_linux.py`](tools/update_linux.py) 单独放到仓库根目录。首次若旧服务是手工或 systemd 启动的，用原方式停一次。以后每次在仓库根目录运行：

```sh
uv run --script update_linux.py
```

若直接使用仓库里的脚本，改用 `uv run --script tools/update_linux.py`。**已有默认模型**时，追加 `--check-live` 先进行联网验收，再切换；验收失败不停止脚本管理的旧服务。若要设置或更换脚本所管服务的默认 DeepSeek，请运行：

```sh
uv run --script tools/update_linux.py --set-deepseek --check-live
```

终端会隐藏输入 Key，预检通过且新版启动后将它加密保存，网页「服务端默认」即使用官方 `deepseek-flash`。不覆盖其他服务或原 `CITATION_MODEL_CONFIG` 文件。以后更新只需 `--check-live`，无需重输 Key。拷在根目录的旧脚本须替换为新版；对应命令是 `uv run --script update_linux.py --set-deepseek --check-live`。

追加 `status` 查看运行地址，追加 `stop` 安全停止。无须 sudo、Docker 或额外的服务管理器；不要在同一数据目录同时启动手工实例或系统服务。

- 只取当前 Git 分支的上游版本并检查可快进，不重置工作区。新版本和锁定依赖放在独立目录，预检通过才停旧实例并检查新实例 `/healthz`。默认只做离线启动检查，代码没变且服务健康时不重启。新版启动失败会尝试恢复之前运行的版本，且命令仍返回失败状态；不覆盖 SQLite、原稿或模型配置。重要数据仍应单独备份，新版一旦改了数据库结构，旧程序不一定兼容。
- 首启默认尝试 `127.0.0.1:8765–8775`，打印实际可用地址；之后沿用成功过的端口。已经配置反代、要固定端口时加 `--port 8765`，端口占用就报错，不改端口也不杀占用进程。保持同一套 `CITATION_*` 环境变量；脚本不复制凭据。通过 SSH 在电脑浏览器演示时，把端口转发目标改为脚本打印的实际端口。
- 旧系统若缺支持的 Python 或二进制依赖，安装或自检会报错；日志见 `data/runtime/linux-updater/update.log` 和 `service.log`。脚本不修改操作系统、反代、防火墙或开机自启设置。已纳入系统服务管理的正式部署请继续使用原管理器，不和此脚本混用。

### 接入真实 Agent 与部署配置

- 首页头像 → **模型设置**：选择协议、填写公网 HTTPS 地址、模型名和自己的 API Key。登录后点「同步到账号」，同一站点的其他设备登录后会自动用于新检索、续聊和引用建议；Key 不回传浏览器。旧标签仍有同账户配置时，直接打开设置保存一次即可迁移，不用重输。选「服务端默认模型」并应用则移除账号已保存的 Key。访客自定义 Key 仍只留在当前标签，不会成为服务器默认 Key。
- 服务器默认模型用 `CITATION_MODEL_CONFIG` 指向受保护的 JSON 凭据文件（默认位置为 `data/runtime/deepseek.local.json`）。例如 Luna 的字段为 `"provider": "openai"`、`"base_url": "https://api.openai.com/v1"`、`"api_type": "responses"`、`"model": "gpt-6-luna"` 和 `"api_key"`。快捷脚本也支持上面的 DeepSeek 配置。两种模型都须支持工具调用及结构化输出。部署不会继承试用站的默认 Key，不要把 Key 加进 Git。
- 账户 Key 使用 `cryptography`（Apache-2.0/BSD 许可）的 AES-256-GCM 存密文，首次保存会在数据库旁生成独立的 `*.account-model.key`（Linux 仅服务账号可读）。迁移或恢复时，除 SQLite 外还需**从单独受控的备份恢复这份密钥**。正式部署可用 `CITATION_ACCOUNT_MODEL_KEY_FILE` 指向由凭据管理器注入的 32 字节密钥文件。缺失时不会用服务器的默认 Key 代替已选账户模型。
- 默认本地召回使用 BM25，不下载模型。持有授权快照时，可设置 `CITATION_CORPUS_PATH`、`CITATION_SAMPLE_CORPUS_PATH` 覆盖两个默认文件；清单的 `original_sha256` 对应原始快照。若启用 `CITATION_EMBEDDING_ENABLED=1`，还需用 `CITATION_EMBEDDING_CACHE_DIR` 指向该语料匹配的预建 E5 模型和 `.npz` 索引。缺失或不匹配在启动时报错，不在首个请求里向量化整库。公开版默认检索排名不承诺与带完整摘要和 E5 的线上实例一致。
- 健康检查：`GET /healthz`；交互文档：`GET /docs`；MCP：`POST /mcp`。上述命令只监听回环；在远程服务器演示可经已有 HTTPS 入口反代，或用 SSH 端口转发。反代须允许 SSE、保留正确的 Host，并配置 `CITATION_TRUSTED_HOSTS`；按需设 `CITATION_BASE_PATH`、`CITATION_SECURE_COOKIES=1`。不直接公开裸 Uvicorn 或无安全扫描的稿件上传。
- 进程意外退出后，重启会标记未完成检索与对话轮次中断，不会自动重跑；已保存的候选、会话记录和快照仍可访问，也可继续该会话。
- 后台发现数据源需 `CITATION_LEARNING_ENABLED=1`、服务端默认模型以及项目内的主动授权；DeepSeek Search/Fetch 还需独立的 `CITATION_LEARNING_SEARCH_KEY`。arXiv 和 Zenodo 是当前已审配方，接入必须用户批准，且默认不勾选。不支持由模型自行接入任意网站。

### 实机演示的降级模式

使用仓库内置语料时默认启用 `CITATION_DEMO_CONTINUITY`；使用自备语料时默认关闭，可用 `CITATION_DEMO_CONTINUITY=1` 开启。访客的首轮关键词或摘要若已选本地库，且服务端默认模型在任何检索受理前遭遇连接、超时、限流、余额、鉴权或服务商故障，系统只按原输入查已选的本地库。原 Agent 仍如实记失败；已有检索保留原任务和结果，不换模型或凭据。正常答复最多额外等引用建议 5 秒，未完建议按原期限独立运行。

若模型配置有误导致服务不能启动，可另起一个回环端口的实例：用 `CITATION_MODEL_CONFIG` 指向不存在的专用路径，设 `CITATION_EUROPEPMC_ONLINE=0`、`CITATION_EMBEDDING_ENABLED=0`，并为 `CITATION_DB_PATH` 和 `CITATION_AUTH_DB_PATH` 使用独立的新文件。再打开备用实例的 `?local=1`，不覆盖现有数据。

### 模拟演示与界面

测试夹具可用 `CITATION_LIBRARY_MODE=fixture` 显式选择。也可运行 `uv run --frozen python -m tools.browser_fixture_server`，在 <http://127.0.0.1:8767/> 用固定模型回执体验流式榜单和对话。夹具不使用真实论文库，也不调用外部模型。

顶栏「界面」提供四套主题：`?theme=1|2|3|4` 分别为简洁经典（默认）、农科院门户、学术札记和研究工作台。四套共享检索、对话、历史、项目及引用功能，仅呈现不同；不使用院徽或冒充官方。[四套界面对比](docs/design/四种前端方案对比.png)。网页运行不需要 Node.js，运行前端测试需 Node.js 22 或更新版。

## 源码导览

| 位置 | 内容 |
| --- | --- |
| [`src/citation_tool/`](src/citation_tool/) | FastAPI、LangChain 父 Agent/来源子 Agent、检索、证据、重排、SQLite 和 SSE |
| [`web/`](web/) | 原生 HTML/CSS/ES 模块：检索、对话、历史、项目和引用详情 |
| [`.agents/skills/`](.agents/skills/) | 运行时检索、数据源发现及前端设计约束 |
| [`tests/`](tests/) 和 [`tools/`](tools/) | 测试、本地演示、语料准备、评测与维护工具 |
| [`benchmark/`](benchmark/) | 评测协议和题目定义；不含原始会话、语料及运行回执 |
| [实施规格](docs/spec/文献推荐工具_AGENT_SPEC.md) 和 [AGENTS.md](AGENTS.md) | 需求、接口、验收与开发约束；Markdown 规格是唯一真源 |

父 Agent 自己决定是否检索、改写或补查，不依赖固定检索流程。各来源返回的题录独立保存，同一论文按身份归并。榜单先用同尺度特征打基础分，再由模型盲排并复核；原文和引用资格另行核查。上传的稿件、账户库、会话记录、论文原件、生产配置和内部运行记录均不纳入仓库。第三方引用样式和图标的许可文件随源码保留。

## 验证与维护

```sh
uv run --frozen ruff check src tests tools
uv run --frozen pytest -rs
npm test
uv run --frozen python tools/smoke.py
uv run --frozen python tools/check_spec.py
```

账户模型回归覆盖独立登录会话、服务重启后读回、续聊和引用建议用同一模型、切回默认及账户隔离；更新脚本回归验证加密配置和真实本地 HTTP 启动。它们不调用付费模型；联网验收仍使用 `--check-live`。

运行上述本地夹具后，可另用 `npm run test:browser:themes` 验证四主题的桌面/手机首页、全文入口、双榜单、原生移动触控、引用和证据弹窗；它使用隔离的无头 Edge，不操作日常浏览器。可用 `THEMES=2,3 VIEWPORTS=mobile npm run test:browser:themes` 缩小范围，截图和报告写入 `data/runtime/theme-browser-acceptance/` 的新目录。

常规启动的回归测试使用无参数 `create_app()` 验证默认三库、真实本地检索和显式夹具模式，不需要设置 `CITATION_CANDIDATE_PIPELINE`。`tools/smoke.py` 使用真实本地 HTTP/SSE 验证早到结果、事件重放和快照不可变，不调用外部模型。缺少另行准备的语料快照或 E5 缓存时，仅依赖该文件的测试会显式跳过，并列出缺失文件；已有数据仍按原断言验证。评测工具的 `run` 命令可能花费 API 额度；先读对应协议并从 `prepare` 开始。仓库不含生产 SSH 目标和密钥，不触发部署。维护者若另获发布授权，须显式指定 `CITATION_DEPLOY_SSH_TARGET`（OpenSSH 别名或 `user@host`），用 SSH 配置或 `CITATION_DEPLOY_SSH_IDENTITY_FILE` 指定本机私钥路径；发布脚本仍面向既有服务器布局，不是通用部署器。
