# 东软智慧商务 AI 助手平台

面向企业客户、客服人员、系统管理员和决策者的教学演示项目。系统以 Vue 3 + FastAPI 为主线，提供智能对话、知识库检索、客服工单、运营看板和 AI 配置管理；默认使用可重复的本地 AI 回退链路，配置 OpenAI-compatible 或 Dify 服务后可按环境变量增强。

本项目对应《基于 Python 的 AI 应用开发》实训教学日历第 3-11 天：原生模型流、LCEL 与记忆、混合 RAG、LangGraph 多智能体、Dify 工作流、实时消息/偏好/缓存，以及 Docker/Kubernetes 与 DevOps。

## 快速启动

### Docker Compose（推荐演示方式）

```powershell
Copy-Item .env.example .env
docker compose up --build
```

上面的命令启动课程应用（前端、后端、Redis）。若要把仓库内 `.runtime/dify` 的官方 Dify 环境也纳入同一次演示，先准备官方环境文件，再由隔离编排脚本启动两个 Compose 项目：

```powershell
Copy-Item .runtime\dify\docker\.env.example .runtime\dify\docker\.env
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

首次启动会自动写入 SQLite 演示数据。可使用以下帐号登录，密码均为 `Demo123!`：

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

## AI 运行模式

默认不需要任何云端密钥：系统使用 LangChain `Document`、`RecursiveCharacterTextSplitter`、确定性本地 Embedding、FAISS 与稀疏关键词加权 RRF 完成可离线验证的混合 RAG。LCEL 使用 `RunnableParallel`/`RunnableBranch` 组织证据与谨慎分支，并组合最近窗口和有界摘要记忆；LangGraph `StateGraph` 执行受控任务分解、分类/检索并行、条件工具路由和有界重试。真实只读 SQLite 工具只返回工单队列聚合，不暴露客户记录。

设置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 后，OpenAI-compatible 适配器会使用 `stream=true` 消费提供方 SSE delta，并将模型 Token 即时转发给前端；客户端取消会关闭上游流，异常会在首 Token 前回退或在部分 Token 后以 `reset` 修正。设置 `DIFY_API_URL` 和 `DIFY_API_KEY` 后，可将已发布的 Dify 工作流作为可选增强服务接入。Dify 未配置、超时或失败时，FastAPI API 层会用同一数据库执行本地 RAG/Agent，并返回真实 `citations` 与 `trace`，而不是固定的泛化文案。

`dify/` 中提供扩展客服、TTS 和文生图 DSL 模板；本机目标 Dify 已完成知识库配置，三个应用均已发布并通过真实 API 验收，TTS/文生图真实媒体也已通过播放/显示。`scripts/stack.ps1` 只负责容器编排，不代替 Dify 界面配置。

密钥只写入本机 `.env`，不要提交到仓库。详细方案见 [docs/ai-design.md](docs/ai-design.md)。

## 文档索引

| 文档 | 用途 |
| --- | --- |
| [需求规格](docs/requirements.md) | 用户角色、功能、非功能需求与验收条件 |
| [总体架构](docs/architecture.md) | 分层、数据流、接口边界和演进策略 |
| [AI 设计](docs/ai-design.md) | RAG、Agent、提示词、Dify 与降级策略 |
| [知识点与代码对照](docs/knowledge-points-map.md) | 实训教学日历第 3-11 天的实现文件、验证证据与完成边界 |
| [接口说明](docs/api.md) | API 分组、认证和典型请求 |
| [部署手册](docs/deployment.md) | 本地、Compose、Kubernetes 与运维检查 |
| [演示脚本](docs/demo-script.md) | 答辩演示顺序、讲解要点和回退预案 |
| [测试与评估](docs/test-evaluation.md) | 测试矩阵、AI 评估集和评分证据 |
| [验收执行报告](docs/acceptance-report.md) | 本次实际命令、AI 指标、浏览器路径和运行限制 |
| [方案完成度审计](docs/completion-audit.md) | 八项评分对照及必须本人完成的证据 |
| [项目计划](docs/project-plan.md) | 角色分工、迭代节奏和协作规范 |
| [需要你完成的事项](docs/your-actions.md) | 外部账号、资料、部署留证与答辩材料清单 |

## 仓库结构

```text
backend/                 FastAPI、SQLite、RAG 与 AI 编排
frontend/                Vue 3 + Vite 单页应用
docs/                    需求、设计、测试与答辩材料
dify/                    Dify 工作流样例与接入说明
deploy/k8s/               最小 Kubernetes 部署清单
.github/workflows/       CI、镜像发布与受保护环境部署工作流
compose.yaml             课程前端、后端与 Redis 编排
scripts/stack.ps1        课程项目 + 官方 Dify 的隔离一键编排
```

## 质量与边界

- 回答会返回检索引用、Agent 轨迹和 `used_fallback` 状态，便于解释 AI 决策过程。
- 最终回答缓存按用户、会话、知识/工单版本、模型设置与偏好隔离；缓存命中使用明确的 `origin=cache`，不会切片冒充模型 Token。
- 用户可保存回答风格、语言和自动朗读偏好；客服工单通过受保护 SSE 实时更新，但当前事件 Broker 仅适合单进程，多实例需接入 Redis Pub/Sub。
- 运营看板的近 7 日咨询与 AI 建议质检代理趋势按消息、工单时间戳聚合；`quality_score` 来自教学规则，不是用户满意度。没有前一周期数据时明确显示“暂无历史对比”。
- 对合同、付款、订单和故障等高风险问题，系统不臆造业务事实；信息不足时提示补充标识并转人工核验。
- 浏览器语音输入与朗读基于 Web Speech API；浏览器不支持时仍保留完整的文本流程。
- 本项目为教学演示，生产环境应改用托管数据库、受控对象存储、密钥管理、审计与监控服务。
- 当前本轮自动验收为后端 `81 passed`、Dify/媒体与 Day 8 预检定向 21 条通过、12/12 离线评测全阈值通过。课程镜像已构建，backend/frontend/Redis healthy，Dify Web/API 200，课程 smoke 有 3 条引用且 `used_fallback=false`；第 8/9 天知识库、客服、TTS/文生图真实链路均已通过，详见 [验收报告](docs/acceptance-report.md)。
