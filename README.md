# 文献推荐工具

面向科研写作的文献检索与引用辅助工具。可以用关键词、摘要或 PDF/DOCX 稿件发起检索。在连续对话中，各数据库流式返回候选，模型结合来源证据重排，用户可以核查依据并复制所需的引用格式。题录候选不是可直接引用的证据；没有合法原文或证据不足时，系统不会生成引用建议。

本仓库是**当前开发源码**，含项目、项目内共享选库和经用户审批的数据源发现功能。这些功能不代表[线上试用站](https://biodataagent.com/literature/)已经更新。匿名实例不用于未公开稿件或保密材料。

## 五分钟本地试用（不需要 API Key）

安装 [uv](https://docs.astral.sh/uv/) 和 Python 3.12（项目支持 3.12–3.14）。在仓库根目录运行：

```sh
uv sync --frozen --python 3.12
uv run --frozen python -m tools.browser_fixture_server
```

打开 **<http://127.0.0.1:8767/>**，用关键词模式提交 `LFY2, soybean`。这条路径使用明确标识的模拟论文和固定模型回执，可体验流式榜单、对话和页面操作；不连接真实论文库、不调用付费模型，也不会把模拟结果当成科研证据。进程只监听本机，按 `Ctrl+C` 停止。需要检查前端源码或运行前端测试时，安装 Node.js 22 或更新版；网页运行无第三方 JavaScript 依赖。

顶栏“界面”提供四套完整主题：[1 简洁经典](http://127.0.0.1:8767/?theme=1)、[2 农科院门户](http://127.0.0.1:8767/?theme=2)、[3 学术札记](http://127.0.0.1:8767/?theme=3) 和 [4 研究工作台](http://127.0.0.1:8767/?theme=4)。四套共享检索、对话、历史、账户、项目与引用功能，仅呈现不同；农科院主题不使用院徽或官方身份。主题选择只保存数字编号，旧英文主题链接继续兼容，切换后由服务端恢复原会话。结果卡片与证据详情展示名次而不展示数值分数。[四种前端方案对比图](docs/design/四种前端方案对比.png)展示同一已登录账户、真实多轮对话和服务端历史在各主题下的主页与对话页。

## 运行常规服务

```sh
uv run --frozen uvicorn citation_tool.api:create_app --factory --host 127.0.0.1 --port 8765
```

- 网页：<http://127.0.0.1:8765/>。健康检查：`GET /healthz`；API 交互文档：`GET /docs`；MCP：`POST /mcp`。
- 缺省使用模拟文献。无需登录即可浏览首页；要运行真实 Agent，需在头像 → **模型设置**填入自己的模型协议、HTTPS 地址、模型名和 Key，或由维护者配置服务端默认模型。兼容 OpenAI Chat Completions 和 Responses；所选模型须支持工具调用和结构化输出。没有模型时服务会明确提示，不冒充真实运行。
- 本机服务端模型配置可放在 `data/runtime/deepseek.local.json`，或用 `CITATION_MODEL_CONFIG` 指定一个未纳入版本控制的路径。文件包含 `base_url`、`model`、`api_key`；使用原生 Responses 时另设 `api_type` 为 `responses`。不要将真实 Key 写入仓库。浏览器里配置的 Key 只在当前标签页使用，不写入服务器数据库。
- 要复现真实多库检索，设置 `CITATION_LIBRARY_MODE=metadata`，用 `CITATION_CORPUS_PATH` 和 `CITATION_SAMPLE_CORPUS_PATH` 分别指向有许可的题录/摘要 JSON（格式见 `src/citation_tool/runtime.py`）；默认同时启用 Europe PMC 在线检索。可选 `CITATION_EMBEDDING_ENABLED=1` 开启 E5 与 BM25 混合召回。真实语料、模型缓存和论文原件需要另外准备，不随源码分发。
- 后台发现数据源需 `CITATION_LEARNING_ENABLED=1`、服务端默认模型以及项目内的主动授权；DeepSeek Search/Fetch 还需独立的 `CITATION_LEARNING_SEARCH_KEY`。arXiv 和 Zenodo 是当前已审配方，接入必须用户批准，且默认不勾选。暂不支持由模型自行接入任意网站。

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

运行上述本地夹具后，可另用 `npm run test:browser:themes` 验证四主题的桌面/手机首页、全文入口、双榜单、原生移动触控、引用和证据弹窗；它使用隔离的无头 Edge，不操作日常浏览器。可用 `THEMES=2,3 VIEWPORTS=mobile npm run test:browser:themes` 缩小范围，截图和报告写入 `data/runtime/theme-browser-acceptance/` 的新目录。

`tools/smoke.py` 使用真实本地 HTTP/SSE 验证早到结果、事件重放和快照不可变，不调用外部模型。缺少另行准备的语料快照或 E5 缓存时，仅依赖该文件的测试会显式跳过，并列出缺失文件；已有数据仍按原断言验证。评测工具的 `run` 命令可能花费 API 额度；先读对应协议并从 `prepare` 开始。仓库不含生产 SSH 目标和密钥，不触发部署。维护者若另获发布授权，须显式指定 `CITATION_DEPLOY_SSH_TARGET`（OpenSSH 别名或 `user@host`），用 SSH 配置或 `CITATION_DEPLOY_SSH_IDENTITY_FILE` 指定本机私钥路径；发布脚本仍面向既有服务器布局，不是通用部署器。
