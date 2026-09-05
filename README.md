# 东软智慧商务 AI 助手平台

[![CI](https://github.com/dzc-bit/A-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/dzc-bit/A-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

面向企业客户、客服人员、系统管理员和决策者的全栈 AI 助手平台，提供智能对话、知识库检索、客服工单、运营看板与 AI 配置管理，主线为 **Vue 3 + FastAPI**。

它最大的特点是**默认零密钥、完全离线可运行**：没有云端 API Key 也能跑通"混合检索 + LangGraph 工作流 + 受控工具 Agent + 流式响应"的完整链路，并返回真实检索引用与工作流轨迹；配置 OpenAI-compatible 服务或 Dify 后，同一套代码按环境变量平滑增强，业务逻辑无需改动。这让整个系统的行为完全可复现，也便于离线环境演示与测试。

## 功能特性

- **智能助手**：SSE 逐 Token 流式输出；回答附带检索引用、工作流轨迹与 `used_fallback` 状态，AI 决策过程可解释
- **意图理解（两次独立 LLM 调用）**：`QueryRewriter` 先把口语化问题改写为知识库规范表述（只改写不回答、不添加信息、保留单号标识符），`IntentRouter` 再判断处理难度（knowledge / complex / media）并归类；两者共用同一份规则表——规则同时渲染进提示词并做输出后置校验，非法输出带错误重问一次，离线时降级为关键词规则
- **混合检索**：确定性本地 Embedding，FAISS 与稀疏关键词加权 RRF 融合的词面级检索，可离线验证；Embedding 实现了 LangChain `Embeddings` 契约，可单点替换为真实语义模型
- **受控工具 Agent**：模型在 3 个真工具（工单队列聚合、订单状态查询、创建人工复核工单）的白名单内自主选择工具、自拟参数；参数经 schema 严格校验，被拒调用会把原因回喂给模型自我纠正，循环硬上限 3 轮；无 LLM 时由类别映射确定性驱动同一白名单
- **回答质检（GroundednessGate）**：以回答与检索片段的一致度打分替代"字数达标"式检查，阈值可由管理员配置；未过阈值触发一次更严格的重试，仍不合格则降级为带转人工建议的模板回复
- **Dify 混合路由**：Router 工作流对请求做 A（简单知识）/ B（复杂任务）/ C（媒体生成）意图分类，与本地意图路由语义对齐；Dify 未配置、超时或失败时自动降级到本地工作流
- **媒体与语音**：TTS 朗读与文生图经 Dify 子工作流返回真实 artifacts；浏览器语音输入与朗读基于 Web Speech API，不支持时自动回退完整文本流程
- **客服工单**：受保护 SSE 实时更新；订单查询校验归属、他人数据 fail-closed 不可见，工具只读、不暴露客户明细，高风险问题自动建议转人工复核
- **运营看板**：近 7 日咨询与回答质检趋势按消息、工单时间戳聚合；质检分来自一致性门禁而非硬编码规则；无历史数据时明确显示"暂无历史对比"
- **管理与审计**：用户、知识库、AI 配置与审计日志；合同、付款、订单、故障等高风险问题不臆造业务事实
- **回答缓存**：按用户、会话、知识/工单版本、模型设置与偏好隔离；缓存命中使用明确的 `origin=cache` 标记，不会切片冒充模型 Token

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router |
| 后端 | FastAPI + SQLite（Alembic 迁移）+ LangChain（RAG / LCEL）+ LangGraph（StateGraph） |
| AI | 本地混合检索（FAISS + 稀疏关键词 RRF，Embedding 单点可替换）、OpenAI-compatible 适配器、Dify 工作流增强 |
| 部署 | Docker Compose、GitHub Actions CI/CD |

## 架构

前端始终只访问 FastAPI；FastAPI 统一负责认证、会话持久化、密钥隔离、结果归一化与失败降级，Dify API Key 不下发到浏览器。

**Agent 与 workflow 的取舍**：本项目刻意只保留一个"真 Agent"——工具调用。判断标准是"是否存在需要模型自主决策的环节"：改写、路由、检索、质检都是单次输入→输出的确定性步骤，做成 workflow 节点（规则进提示词 + 输出后置校验），可靠、可测试、可复现；只有工具选择需要模型在白名单内自主决定"调不调、调哪个、传什么参数"，因此做成有界 Agent 循环。由此 LLM 在链路中有四个独立、各自可追溯的出现位置：

```text
QueryRewriter（改写）→ IntentRouter（路由）→ [知识检索] → ToolAgent（工具循环，仅 complex 路由）→ 最终生成 → GroundednessGate（质检）
```

三层 AI 组件各司其职：

| 组件 | 职责 |
| --- | --- |
| Dify | 可视化工作流与外部能力平台：Router 对请求做 A/B/C 意图分类，A 类直接执行知识检索与回答生成，B/C 类经 HTTP 节点回调 FastAPI；另承载 TTS、文生图子工作流 |
| LangChain | 后端 AI 组件层：`Document`、文本切分、Embedding/FAISS、提示词与会话记忆，LCEL `RunnableParallel`/`RunnableBranch` 组织证据与谨慎分支 |
| LangGraph | 后端工作流编排层：`StateGraph` 组织改写 → 路由 → 检索 → 条件工具/提示词组装 → 计划门禁的确定性骨架；远程模型调用留在图外，保证超时不打断检索与工具安全 |

### 请求路由

| 路由 | 典型请求 | 执行位置 |
| --- | --- | --- |
| A：简单知识 | 企业制度、FAQ、稳定产品说明 | Dify Router 分类后直接执行知识库检索与回答生成；FastAPI 只归一化并持久化结果 |
| B：复杂任务 | 多步推理、工单统计、订单进度查询、故障/付款/合同等需工具或人工复核的任务 | Dify Router 分类后调用受保护的回调接口；本地 ToolAgent 在白名单内自主选择工具并执行，LangChain/FAISS 提供本地检索与提示词组件 |
| C：媒体生成 | 朗读上一条回复、把这段话转成语音、生成一张图片 | Dify Router 分类为 `tts` 或 `image`，由对应 Dify 媒体子工作流返回 `artifacts` |
| Router 降级 | Dify 未配置、超时、失败或空输出 | FastAPI 直接运行本地工作流：改写 → 路由 → 检索 → 受控工具 Agent → 生成 → 质检；图不可用时退到顺序编排 |

Dify 分类 JSON 无法解析或类别不合法时安全降级到 B 类，避免把需要工具或人工复核的问题误送到简单知识分支。这里的 A/B/C 是 Dify 的外层路由；本地 `IntentRouter` 输出的难度路由与它语义对齐，LangGraph 内部还会细分"工单统计""系统故障""合同咨询"等业务类别，两组分类不可混用。

回调接口 `POST /api/v1/tools/langgraph/run` 是 Dify HTTP 节点专用的内部接口：要求 `X-Dify-Callback-Secret`，拒绝 `route_depth > 1`，并校验 `conversation_id` 与 `user_id` 的归属。复杂任务回调只运行本地工作流、不会再次调用 Router，因此不存在 `FastAPI → Dify Router → FastAPI → Dify Router` 的递归；C 类媒体路径调用 TTS/文生图子工作流后也不会再进入 Router。

### B 类复杂任务完整调用链

```mermaid
sequenceDiagram
    participant UI as Vue 前端
    participant API as FastAPI 安全代理
    participant Dify as Dify Router
    participant WF as AssistantWorkflow
    participant Graph as LangGraph StateGraph
    participant Agent as ToolAgent（白名单循环）
    participant Chain as LangChain / 本地检索
    participant Model as 后端 LLM

    UI->>API: POST /api/v1/assistant/chat 或 /api/v1/assistant/chat/stream
    API->>Dify: run_router_workflow(query, context)
    Dify->>Dify: LLM 分类为 B / complex
    Dify->>API: POST /api/v1/tools/langgraph/run<br/>route=complex, route_depth=1
    API->>API: 校验共享 Secret、递归深度和会话归属
    API->>WF: run_callback(route=complex)
    WF->>WF: QueryRewriter 改写（LLM #1）<br/>IntentRouter 路由（LLM #2）
    WF->>Graph: invoke StateGraph
    Graph->>Chain: 知识检索、提示词组装
    WF->>Agent: 复杂任务进入有界循环
    Agent->>Model: 自选工具、自拟参数（LLM #3，≤3 轮）
    Model-->>Agent: 工具调用 / 最终回答
    Agent->>Agent: schema 校验，拒绝时回喂原因自纠
    Agent->>Model: 最终生成（LLM #4）
    Model-->>WF: 回答文本
    WF->>WF: GroundednessGate 一致性质检
    WF-->>API: answer / citations / trace / category
    API-->>Dify: 结构化回调响应
    Dify-->>API: Router 工作流统一输出
    API-->>UI: ChatResponse（并写入会话历史）
```

LangGraph 内部的确定性骨架为：`task_planner` → `query_rewriter` → `intent_router` → `knowledge_retrieval` → `route_dispatch` →（complex 时）`deterministic_tool_driver` → `prompt_composer` → `groundedness_plan_gate` → `finish`。远程模型调用不进入同步图：超时的模型无法打断检索、工具白名单校验和提示词组装；LLM 决策（改写与路由）在图外完成后作为状态传入，离线时图内关键词规则接管。

最终回答缓存命中时会直接返回缓存并跳过 Dify Router 与本地工作流。Router 启用时，前端仍调用 SSE 接口，但后端会等待 Dify blocking 工作流完成再发送 `trace`、`reset` 和 `done`；只有本地工作流路径支持模型 Token 的逐段流式输出。

## 快速启动

### Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

上面的命令启动前端、后端与 Redis。若要把仓库内 `dify/` 的官方 Dify 环境也纳入同一次演示，先准备官方环境文件，再由隔离编排脚本启动两个 Compose 项目：

```powershell
Copy-Item dify\docker\.env.example dify\docker\.env
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stack.ps1 -Action config -WithDify
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stack.ps1 -Action up -WithDify
```

`stack.ps1` 使用独立项目名，不会覆盖已有 Redis 和数据卷；脚本只负责容器编排，不代替 Dify 界面配置。首次启动后需在 `http://localhost:8081/install` 完成 Dify 初始化，并把已发布应用 Key 放入根 `.env`。若 Dify 已由其他目录/项目运行，请先设置 `DIFY_COMPOSE_DIR` 与 `DIFY_COMPOSE_PROJECT_NAME` 指向同一官方 checkout，再执行 `config/ps/health/smoke`，避免误启动第二套 Dify。

启动后访问：

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:5173 |
| 后端 API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/v1/health |

Compose 默认只将前后端端口绑定到本机回环地址。容器数据库固定使用命名卷中的 `/app/data/business_ai.db`；原生启动后端时则默认使用 `backend/data/business_ai.db`，两种路径互不混用。

### 演示账号

首次启动会自动写入 SQLite 演示数据。先在根目录 `.env` 中设置 `DEMO_PASSWORD`，再使用以下账号登录（密码统一为 `DEMO_PASSWORD`）：

| 角色 | 账号 | 功能入口 |
| --- | --- | --- |
| 企业用户 | `enterprise@neusoft.local` | 智能助手、知识库问答 |
| 客服人员 | `support@neusoft.local` | 客服工作台、工单处理 |
| 管理员 | `admin@neusoft.local` | 用户、知识库、AI 配置与审计 |
| 决策者 | `executive@neusoft.local` | 运营看板与分析报告 |

### 本地开发

```powershell
Copy-Item .env.example .env
Set-Location backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

新开一个终端：

```powershell
Set-Location frontend
corepack enable
pnpm install --frozen-lockfile
pnpm dev
```

前端开发服务器通过 Vite 代理将 `/api` 转发至 `http://localhost:8000`。生产静态站点由 Nginx 将同一路径代理给后端。

常用命令已封装在 `Makefile` 中，例如 `make up` / `make health` / `make backend-test` / `make frontend-build`，以及 Dify 隔离编排的 `make stack-up` / `make stack-smoke` 等。

## AI 运行模式

AI 能力按环境变量分三档渐进，从零配置的完全离线到 Dify 全量增强，切换不需要改动业务代码。

### 第 1 档：离线运行（默认，零密钥）

系统使用 LangChain `Document`、`RecursiveCharacterTextSplitter`、确定性本地 Embedding、FAISS 与稀疏关键词加权 RRF 完成可离线验证的混合检索。LCEL 使用 `RunnableParallel`/`RunnableBranch` 组织证据与谨慎分支，并组合最近窗口和有界摘要记忆；LangGraph `StateGraph` 执行改写降级、关键词路由、条件工具驱动和计划门禁。订单查询等真工具读取 SQLite 演示数据，只返回状态与聚合，不暴露客户明细。

### 第 2 档：OpenAI-compatible 模型

设置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 后，LLM 在链路的四个位置依次生效：`QueryRewriter` 规范化改写（5 秒超时，失败回退原始问题）、`IntentRouter` 难度路由与分类（非法输出带错误重问一次，仍失败走关键词规则）、`ToolAgent` 白名单工具循环（模型自选工具、自拟参数，schema 校验拒绝时回喂原因，上限 3 轮）、最终回答生成。首响前的改写与路由是精度换延迟的自觉选择，两者独立超时、独立降级。最终回答通过 `stream=true` 消费提供方 SSE delta 并即时转发给前端；客户端取消会关闭上游流，异常在首 Token 前回退或在部分 Token 后以 `reset` 修正。

### 第 3 档：Dify 工作流增强

设置 `DIFY_API_URL` 和 `DIFY_ROUTER_API_KEY` 后，主对话入口优先调用已发布的 Dify Router 工作流。Dify 未配置、超时、返回空结果或执行失败时，FastAPI 会在同一数据库会话中执行本地工作流（改写 → 路由 → 检索 → 受控工具 Agent → 生成 → 质检），并返回真实 `citations` 与 `trace`，而不是固定的泛化文案。

`dify/` 中提供 Router、扩展客服、TTS 和文生图四个工作流 DSL 模板；`scripts/stack.ps1` 只负责容器编排，不代替 Dify 界面配置。

密钥只写入本机 `.env`，不要提交到仓库。详细方案见 `dify/README.md`。

### 模型与凭据区分

| 配置位置 | 用途 |
| --- | --- |
| Dify 控制台的模型提供方 | 供 Router 意图分类和 A 类知识回答节点使用；凭据由 Dify 保存 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 供 FastAPI 内的 OpenAI-compatible 客户端生成 B 类复杂任务或本地降级回答；未配置时使用确定性的本地回退 |
| `DIFY_ROUTER_API_KEY` | FastAPI 调用已发布 Router 工作流的应用 Key；与普通客服、TTS、文生图应用 Key 分离 |
| `DIFY_CALLBACK_SECRET` | Dify HTTP 节点回调 FastAPI 时使用的共享密钥；两侧值必须一致 |
| `DIFY_ROUTER_TIMEOUT_SECONDS` | FastAPI 等待 Router 及其回调完成的总超时，默认 150 秒 |

`DIFY_API_KEY` 仍用于独立客服工作流，并作为 TTS/文生图未配置专用 Key 时的兼容回退，但它不替代 `DIFY_ROUTER_API_KEY`。媒体应用可分别配置 `DIFY_TTS_API_KEY` 和 `DIFY_IMAGE_API_KEY`。

## 测试与质量

- **测试套件**：`backend/tests` 下 20 个 pytest 测试模块，覆盖 API 契约、流式响应、意图路由与改写、受控工具循环、回答质检、Dify 路由回调、工单状态契约、实时交接、部署配置等
- **CI（GitHub Actions）**：后端编译检查与确定性 Agent/RAG 评测指标、前端类型检查与构建、Compose 全栈构建与健康冒烟测试
- **评测数据集**：`backend/evaluations` 提供 Agent 与检索评测数据，`python scripts/evaluate_agent.py` 输出可复现的确定性指标（分类准确率、Top-3 命中率、引用可溯源率、核心轨迹覆盖率、安全转人工率）
- **可解释性**：所有回答返回检索引用、工作流轨迹（改写 → 路由 → 工具调用 → 生成 → 质检）与 `used_fallback` 状态，便于解释 AI 决策过程

## 数据迁移

数据库 schema 由 Alembic 管理（`backend/migrations`），迁移脚本连接应用同一套配置（根 `.env` 或 `DATABASE_URL`，可用 `ALEMBIC_DATABASE_URL` 覆盖）：

```powershell
Set-Location backend
alembic upgrade head          # 应用迁移
alembic revision --autogenerate -m "describe change"   # 依据 models 生成新迁移
alembic downgrade -1          # 回滚上一版
```

零配置演示仍默认 `create_all` 建表；已存在的演示卷通过兼容引导补齐缺失列。生产或团队协作场景一律以 Alembic 为准；切换 PostgreSQL 时仅需更换连接串并重放迁移。

## 仓库结构

```text
.
├── README.md                     # 项目总览（本文件）
├── LICENSE                       # MIT 许可证
├── .env.example                  # 运行配置模板（复制为 .env 后填写，不提交）
├── compose.yaml                  # 前端、后端与 Redis 编排
├── Makefile                      # 常用命令（up/test/build/stack-* 等）
│
├── backend/                      # FastAPI 后端（应用源码与测试）
│   ├── app/
│   │   ├── api.py                # 聚合路由入口（领域模块见 routers/）
│   │   ├── routers/              # 按领域拆分的路由：auth / users / chat / knowledge / support / admin / dashboard / media / system
│   │   ├── main.py               # 应用入口
│   │   ├── models.py             # 数据库模型（含演示订单表）
│   │   ├── schemas.py            # Pydantic 数据契约
│   │   ├── config.py             # 配置加载
│   │   ├── database.py           # 数据库会话与零配置建表
│   │   ├── dependencies.py       # 依赖注入
│   │   ├── security.py           # 认证与密钥
│   │   └── services/             # 业务服务：workflow / rag / dify / llm / cache / vision / 审计 ...
│   ├── migrations/               # Alembic 迁移（env 读取应用配置，含基线迁移）
│   ├── alembic.ini
│   ├── tests/                    # pytest 测试套件
│   ├── evaluations/              # AI 评测脚本与数据集
│   ├── scripts/                  # 后端辅助脚本（如 evaluate_agent.py）
│   ├── requirements.txt          # 运行依赖
│   ├── requirements-ai.txt       # AI 相关依赖
│   └── Dockerfile
│
├── frontend/                     # Vue 3 + Vite 单页应用
│   ├── src/
│   │   ├── api/                  # 后端接口封装（client.ts）
│   │   ├── components/           # 通用组件（对话气泡 / 看板卡片 / 实时面板 ...）
│   │   ├── views/                # 页面视图（对话 / 知识库 / 工单 / 看板 / 管理 ...）
│   │   ├── stores/               # Pinia 状态（auth 等）
│   │   ├── composables/          # 组合式函数（如语音 useSpeech）
│   │   └── router/               # 路由
│   ├── vite.config.ts            # Vite 配置（含 /api 代理）
│   ├── nginx.conf                # 生产静态站点代理
│   └── Dockerfile
│
├── dify/                         # Dify 工作流样例与接入说明
│   ├── router-workflow.yml       # A/B/C 智能路由工作流
│   ├── business-support-workflow.yml  # 扩展客服工作流
│   ├── text-to-speech-workflow.yml    # TTS 工作流
│   ├── text-to-image-workflow.yml     # 文生图工作流
│   ├── README.md                 # Dify 接入说明
│   ├── day8-preflight.md         # Dify 接入预检要点
│   └── knowledge/                # 知识库样例
│
├── scripts/
│   └── stack.ps1                 # 本平台 + 官方 Dify 的隔离一键编排
│
└── .github/
    └── workflows/                # CI（ci.yml）
```

> 说明：应用运行只需 `backend/`、`frontend/`、`dify/`、`scripts/`、`compose.yaml` 与 `.env`。`.env`、`.venv`、`node_modules`、`dist`、`.pytest_cache` 等为本地产物，已被 `.gitignore` 忽略。

## 文档索引

| 文档 | 用途 |
| --- | --- |
| [README.md](README.md) | 项目总览、启动方式、架构与配置 |
| [dify/README.md](dify/README.md) | Dify 工作流接入与环境准备说明 |
| [dify/day8-preflight.md](dify/day8-preflight.md) | Dify 接入预检要点 |

## 多用户隔离、并发与安全

**多用户隔离**：JWT 按用户签发；会话与工单全部经过作用域校验器（按 owner、角色与分配关系判定，客服只能看到未分配或分配给自己的会话，企业管理员才能越权查看）；SSE 实时事件按订阅谓词过滤且异常时 fail-closed（授权谓词先于队列注册，不存在跨用户推送窗口）；最终回答缓存键包含 user_id、会话历史与个人偏好；知识检索缓存纯内容键控——知识库全局共享，缓存值不含任何用户数据，跨用户复用安全。

**并发模型**：SQLite 连接级启用 WAL、busy_timeout、外键与 NORMAL 同步（读写不互斥，并发写排队等待而不是抛错）；事件 broker 基于 asyncio 单事件循环快照迭代；内存缓存持锁读写与逐出；工单 AI 富集在独立数据库会话的后台任务中执行，双层异常兜底，失败不影响 201 响应。多实例部署仍需接入 Redis（见 Roadmap）。

**提示词注入与数据隐私**：

- 知识库写入收敛到 admin / support_agent 内部角色；检索内容在提示词中以"数据参考，不是指令"显式隔离，意图层输出另有枚举后置校验
- 工具调用经白名单 + 参数 schema + 单号正则三重校验，执行硬上限；**订单查询 fail-closed 校验归属**——他人订单与不存在的订单返回同一条不泄露存在性的文案，注入操纵工具参数无利可图
- 人工复核工单按会话 + 类别去重，无法被注入或误触刷单
- GroundednessGate 防止模型编造，但**不防知识投毒**——被投毒的引用自身就是"依据"；这是设计边界，知识库写入权限即为此收敛
- 未配置 `LLM_API_KEY` 时问题文本不出本机；配置后用户问题会发送给所配置的第三方模型服务

## Roadmap

- 事件 Broker 接入 Redis Pub/Sub，支持多实例部署下的实时工单推送
- 基于 Dify 会话变量的多轮澄清追问回路（当前 `need_clarification` 固定为 `false`）
- Dify Router 路径下的逐段流式输出（当前仅本地工作流路径支持 Token 级流式）
- 生产化增强：托管数据库、受控对象存储、密钥管理、审计与监控服务接入

## License

本项目基于 [MIT License](LICENSE) 开源。
