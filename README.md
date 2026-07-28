# 东软智慧商务 AI 助手平台（实训项目仓库）

面向企业客户、客服人员、系统管理员和决策者的教学演示项目。系统以 **Vue 3 + FastAPI** 为主线，提供智能对话、知识库检索、客服工单、运营看板和 AI 配置管理；默认使用可重复的本地 AI 回退链路，配置 OpenAI-compatible 或 Dify 服务后可按环境变量增强。

本项目对应《基于 Python 的 AI 应用开发》实训教学日历第 3–11 天：原生模型流、LCEL 与记忆、混合 RAG、LangGraph 多智能体、Dify 工作流、实时消息/偏好/缓存，以及 Docker/Kubernetes 与 DevOps。

本仓库同时包含上述平台的全部源码、部署配置，以及本次实训的产出材料（实习日记、报告、汇报 PPT）和用于生成本地实习文档的辅助脚本。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router |
| 后端 | FastAPI + SQLite + LangChain（RAG / LCEL）+ LangGraph（StateGraph） |
| AI | 本地可离线混合 RAG（FAISS + 稀疏关键词 RRF）、OpenAI-compatible 适配器、Dify 工作流增强 |
| 部署 | Docker Compose / Kubernetes（最小 K8s 清单） |

## 快速启动

### Docker Compose（推荐演示方式）

```powershell
Copy-Item .env.example .env
docker compose up --build
```

上面的命令启动课程应用（前端、后端、Redis）。若要把仓库内 `dify/` 的官方 Dify 环境也纳入同一次演示，先准备官方环境文件，再由隔离编排脚本启动两个 Compose 项目：

```powershell
Copy-Item dify\docker\.env.example dify\docker\.env
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stack.ps1 -Action config -WithDify
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stack.ps1 -Action up -WithDify
```

脚本不会填充模型、工作流、数据集或媒体服务凭据；首次启动后需在 `http://localhost:8081/install` 完成 Dify 初始化，并把已发布应用 Key 放入根 `.env`。它使用独立项目名，避免覆盖课程 Redis 和数据卷。若 Dify 已由其他目录/项目运行，请先设置 `DIFY_COMPOSE_DIR` 与 `DIFY_COMPOSE_PROJECT_NAME` 指向同一官方 checkout，再执行 `config/ps/health/smoke`，避免误启动第二套 Dify。

启动后访问：

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:5173 |
| 后端 API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/v1/health |

Compose 默认只将前后端端口绑定到本机回环地址。容器数据库固定使用命名卷中的 `/app/data/business_ai.db`；原生启动后端时则默认使用 `backend/data/business_ai.db`，两种路径互不混用。健康检查的 `security.token_secret` 会提示签名密钥是否仍为演示默认值，但不会返回密钥内容。

首次启动会自动写入 SQLite 演示数据。请先在根目录 `.env` 中设置 `DEMO_PASSWORD`，再使用以下帐号登录：

| 角色 | 帐号 | 演示入口 |
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

默认不需要任何云端密钥：系统使用 LangChain `Document`、`RecursiveCharacterTextSplitter`、确定性本地 Embedding、FAISS 与稀疏关键词加权 RRF 完成可离线验证的混合 RAG。LCEL 使用 `RunnableParallel`/`RunnableBranch` 组织证据与谨慎分支，并组合最近窗口和有界摘要记忆；LangGraph `StateGraph` 执行受控任务分解、分类/检索并行、条件工具路由和有界重试。真实只读 SQLite 工具只返回工单队列聚合，不暴露客户记录。

设置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 后，OpenAI-compatible 适配器会使用 `stream=true` 消费提供方 SSE delta，并将模型 Token 即时转发给前端；客户端取消会关闭上游流，异常会在首 Token 前回退或在部分 Token 后以 `reset` 修正。设置 `DIFY_API_URL` 和 `DIFY_ROUTER_API_KEY` 后，主对话入口会优先调用已发布的 Dify Router 工作流。Dify 未配置、超时、返回空结果或执行失败时，FastAPI 会在同一数据库会话中执行本地 LangGraph/RAG Agent，并返回真实 `citations` 与 `trace`，而不是固定的泛化文案。

`dify/` 中提供 Router、扩展客服、TTS 和文生图 DSL 模板；本机目标 Dify 已完成知识库配置，四个应用均已发布，TTS/文生图真实媒体也已通过播放/显示。`scripts/stack.ps1` 只负责容器编排，不代替 Dify 界面配置。

密钥只写入本机 `.env`，不要提交到仓库。详细方案见 `dify/README.md`。

## Dify、LangChain 与 LangGraph 的职责边界

三者位于不同层级，不是三个可互换的“模型框架”。前端始终只访问 FastAPI；FastAPI 负责认证、会话持久化、密钥隔离、结果归一化和失败降级，前端不保存或直接调用任何 Dify API Key。

| 组件 | 在本项目中的职责 | 不负责的内容 |
| --- | --- | --- |
| Dify | 可视化工作流与外部能力平台；Router 对请求做 A/B/C 意图分类，A 类执行 Dify 知识检索，B/C 类通过 HTTP 节点回调 FastAPI；另承载 TTS、文生图子工作流 | 不直接访问业务 SQLite，也不执行后端 `StateGraph` |
| LangChain | 后端 AI 组件层；提供 `Document`、文本切分、Embedding/FAISS、提示词、会话记忆以及 LCEL `RunnableParallel`/`RunnableBranch` | 不决定跨系统 A/B/C 路由，也不保存业务会话 |
| LangGraph | 后端复杂任务的状态编排层；用 `StateGraph` 组织任务规划、并行分类/检索、白名单工具路由、回复计划与有界质检重试 | 本身不是 LLM、知识库或 Dify 工作流 |

### 请求类型与实际执行位置

| 路由 | 典型请求 | Dify 执行内容 | FastAPI / LangGraph / LangChain 执行内容 |
| --- | --- | --- | --- |
| A：简单知识 | 企业制度、FAQ、稳定产品说明 | Dify Router 分类后直接执行知识库检索和回答生成 | FastAPI 只归一化并持久化结果；不回调 LangGraph |
| B：复杂任务 | 多步推理、工单统计、数据库聚合、故障处理、订单/合同/付款等需工具或人工复核的任务 | Dify Router 分类后调用受保护的 LangGraph 回调接口 | LangGraph 编排复杂任务；LangChain/FAISS 提供本地检索和提示词组件；白名单工具只返回获准的数据或聚合 |
| C：媒体生成 | “朗读上一条回复”“把这段话转成语音”“生成一张图片” | Dify Router 分类为 `tts` 或 `image`；后端再调用对应 Dify 媒体子工作流 | 回调进入专用异步媒体路径并返回 `artifacts`；当前媒体路径不执行同步 `StateGraph` |
| Router 降级 | Router 未配置、超时、失败、空输出 | 不再参与本次请求 | FastAPI 直接运行本地 `BusinessAgentOrchestrator`，由 LangGraph + LangChain/RAG 完成回答；图不可用时退到顺序编排 |

Dify 分类 JSON 无法解析或类别不合法时会安全降级到 B 类，让后端按复杂任务处理，避免把需要工具或人工复核的问题误送到简单知识分支。这里的 A/B/C 是 Dify 的外层路由；LangGraph 内部还会细分“工单统计”“系统故障”“合同咨询”“一般咨询”等业务类别，两组分类不可混用。

### B 类复杂任务完整调用链

```mermaid
sequenceDiagram
    participant UI as Vue 前端
    participant API as FastAPI 安全代理
    participant Dify as Dify Router
    participant Agent as BusinessAgentOrchestrator
    participant Graph as LangGraph StateGraph
    participant Chain as LangChain / 本地 RAG
    participant Model as 后端 LLM

    UI->>API: POST /api/v1/assistant/chat 或 /api/v1/assistant/chat/stream
    API->>Dify: run_router_workflow(query, context)
    Dify->>Dify: LLM 分类为 B / complex
    Dify->>API: POST /api/v1/tools/langgraph/run<br/>route=complex, route_depth=1
    API->>API: 校验共享 Secret、递归深度和会话归属
    API->>Agent: run_callback(route=complex)
    Agent->>Graph: invoke StateGraph
    Graph->>Chain: 并行分类、FAISS 检索、提示词/记忆组装
    Chain-->>Graph: 分类、引用与回复上下文
    Graph->>Graph: 白名单工具路由、回复计划校验与有界重试
    Graph-->>Agent: category / citations / response_plan
    Agent->>Model: 最终 completion（已配置时）
    Model-->>Agent: 回答文本
    Agent->>Agent: 最终质检与邮件草稿步骤
    Agent-->>API: answer / citations / trace / category
    API-->>Dify: 结构化回调响应
    Dify-->>API: Router 工作流统一输出
    API-->>UI: ChatResponse（并写入会话历史）
```

LangGraph 内部的主要节点顺序为：`task_planner` → `classification_agent` 与 `knowledge_query_agent` 并行 → `parallel_join` → 按分类选择 `business_tool_agent` → `response_agent` → `response_plan_quality` → `finish`。回复计划校验失败时最多重试一次，不允许无限循环。最终模型 completion、最终回答质检和邮件草稿步骤在同步 `StateGraph` 完成后执行，不应理解为所有 Agent 步骤都位于图内。

回调接口 `POST /api/v1/tools/langgraph/run` 是 Dify 内部 HTTP 节点使用的接口，不是前端接口。它要求 `X-Dify-Callback-Secret`，拒绝 `route_depth > 1`，并校验 `conversation_id` 与 `user_id` 的归属。复杂任务回调只运行后端 Orchestrator，不会再次调用 Dify Router，因此不会形成 `FastAPI → Dify Router → FastAPI → Dify Router` 的递归。C 类媒体路径可以调用 Dify TTS/文生图子工作流，但不会再次进入 Router。

B/C 分支收到 FastAPI 的结构化响应后只做 JSON 解析并进入 Dify End 节点，不会再经过第二个 Dify LLM 润色。当前实现采用一次调用完成的方案，`need_clarification` 固定为 `false`，尚未实现基于 Dify 会话变量的多轮追问回路。

最终回答缓存命中时会直接返回缓存并跳过 Dify Router 与 LangGraph。Router 启用时，前端虽然仍调用 SSE 接口，但后端会等待 Dify blocking 工作流完成，再发送 `trace`、`reset` 和 `done`；只有本地 Orchestrator 路径支持模型 Token 的逐段流式输出。

### 模型与凭据区分

| 配置位置 | 用途 |
| --- | --- |
| Dify 控制台的模型提供方 | 供 Router 意图分类和 A 类知识回答节点使用；凭据由 Dify 保存 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 供 FastAPI 内的 OpenAI-compatible 客户端生成 B 类复杂任务或本地降级回答；未配置时使用确定性的本地回退 |
| `DIFY_ROUTER_API_KEY` | FastAPI 调用已发布 Router 工作流的应用 Key；与普通客服、TTS、文生图应用 Key 分离 |
| `DIFY_CALLBACK_SECRET` | Dify HTTP 节点回调 FastAPI 时使用的共享密钥；两侧值必须一致 |
| `DIFY_ROUTER_TIMEOUT_SECONDS` | FastAPI 等待 Router 及其回调完成的总超时，默认 150 秒 |

`DIFY_API_KEY` 仍用于独立客服工作流，并作为 TTS/文生图未配置专用 Key 时的兼容回退，但它不替代 `DIFY_ROUTER_API_KEY`。媒体应用可分别配置 `DIFY_TTS_API_KEY` 和 `DIFY_IMAGE_API_KEY`。

## 仓库结构

```text
.
├── README.md                     # 项目总览（本文件）
├── .env.example                  # 运行配置模板（复制为 .env 后填写，不提交）
├── compose.yaml                  # 课程前端、后端与 Redis 编排
├── Makefile                      # 常用命令（up/test/build/stack-* 等）
│
├── backend/                      # FastAPI 后端（应用源码与测试）
│   ├── app/
│   │   ├── api.py                # 路由与接口实现（API 主文件）
│   │   ├── main.py               # 应用入口
│   │   ├── models.py             # 数据库模型
│   │   ├── schemas.py            # Pydantic 数据契约
│   │   ├── config.py             # 配置加载
│   │   ├── database.py           # 数据库会话
│   │   ├── dependencies.py       # 依赖注入
│   │   ├── security.py           # 认证与密钥
│   │   └── services/             # 业务服务：agent / rag / dify / llm / cache / vision / 审计 ...
│   ├── tests/                    # pytest 测试套件
│   ├── evaluations/              # AI 评测脚本与数据集
│   ├── scripts/                  # 后端辅助脚本（如 evaluate_agent.py）
│   ├── data/                     # 本地 SQLite 数据（business_ai.db）
│   ├── requirements.txt          # 运行依赖
│   ├── requirements-ai.txt       # AI 相关依赖
│   ├── pytest.ini                # 测试配置
│   └── Dockerfile
│
├── frontend/                     # Vue 3 + Vite 单页应用
│   ├── src/
│   │   ├── api/                  # 后端接口封装（client.ts）
│   │   ├── components/           # 通用组件（对话气泡 / 看板卡片 / 实时面板 ...）
│   │   ├── views/                # 页面视图（对话 / 知识库 / 工单 / 看板 / 管理 ...）
│   │   ├── stores/               # Pinia 状态（auth 等）
│   │   ├── composables/          # 组合式函数（如语音 useSpeech）
│   │   ├── router/               # 路由
│   │   └── main.ts / App.vue / styles.css / types.ts
│   ├── index.html
│   ├── vite.config.ts            # Vite 配置（含 /api 代理）
│   ├── nginx.conf                # 生产静态站点代理
│   ├── package.json / pnpm-lock.yaml
│   └── Dockerfile
│
├── dify/                         # Dify 工作流样例与接入说明
│   ├── router-workflow.yml              # A/B/C 智能路由工作流
│   ├── business-support-workflow.yml     # 扩展客服工作流
│   ├── text-to-speech-workflow.yml       # TTS 工作流
│   ├── text-to-image-workflow.yml        # 文生图工作流
│   ├── README.md                 # Dify 接入说明
│   ├── day8-preflight.md / day8_preflight.py   # 第 8 天预检
│   └── knowledge/                # 知识库样例
│
├── deploy/
│   └── k8s/                      # 最小 Kubernetes 部署清单（backend / frontend / redis / ingress ...）
│
├── scripts/
│   └── stack.ps1                 # 课程项目 + 官方 Dify 的隔离一键编排
│
├── .github/
│   └── workflows/                # CI（ci.yml）、镜像发布（release.yml）
│
├── docs/
│   └── superpowers/
│       └── plans/                # 模块规划笔记（如管理员模块）
│
├── 实习日记/                      # 第 1–9 天实习日记（9 个 .docx）
├── 实习报告.docx                  # 实习总结报告
├── 实习周记.docx                  # 实习周记
├── 项目报告.docx                  # 项目总结报告
├── 汇报ppt.pptx                   # 答辩 / 汇报演示文稿
```

> 说明：应用运行只需 `backend/`、`frontend/`、`dify/`、`deploy/`、`scripts/`、`compose.yaml` 与 `.env`。根目录的实习文档与 `_build_diaries.py` 等脚本仅用于本地生成实训材料，不参与服务运行。`.env`、`.venv`、`node_modules`、`dist`、`.pytest_cache` 等为本地产物，已被 `.gitignore` 忽略。

## 文档索引

仓库内实际存在的说明文档如下：

| 文档 | 用途 |
| --- | --- |
| [README.md](README.md) | 项目总览、启动方式、仓库结构与边界 |
| [dify/README.md](dify/README.md) | Dify 工作流接入与环境准备说明 |
| [dify/day8-preflight.md](dify/day8-preflight.md) | 第 8 天 Dify 接入预检要点 |
| [docs/superpowers/plans/2026-07-21-admin-module.md](docs/superpowers/plans/2026-07-21-admin-module.md) | 管理员模块规划笔记 |

## 质量与边界

- 回答会返回检索引用、Agent 轨迹和 `used_fallback` 状态，便于解释 AI 决策过程。
- 最终回答缓存按用户、会话、知识/工单版本、模型设置与偏好隔离；缓存命中使用明确的 `origin=cache`，不会切片冒充模型 Token。
- 用户可保存回答风格、语言和自动朗读偏好；客服工单通过受保护 SSE 实时更新，但当前事件 Broker 仅适合单进程，多实例需接入 Redis Pub/Sub。
- 运营看板的近 7 日咨询与 AI 建议质检代理趋势按消息、工单时间戳聚合；`quality_score` 来自教学规则，不是用户满意度。没有前一周期数据时明确显示“暂无历史对比”。
- 对合同、付款、订单和故障等高风险问题，系统不臆造业务事实；信息不足时提示补充标识并转人工核验。
- 浏览器语音输入与朗读基于 Web Speech API；浏览器不支持时仍保留完整的文本流程。
- 本项目为教学演示，生产环境应改用托管数据库、受控对象存储、密钥管理、审计与监控服务。
