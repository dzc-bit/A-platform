# Dify 工作流接入说明

本目录提供三个 Dify DSL 模板：

- `business-support-workflow.yml`：风险分类 -> 高风险只读健康检查 -> 知识检索 -> 代码证据检查 -> 条件路由 -> 合规生成或人工转交。
- `text-to-speech-workflow.yml`：文本/音色参数校验 -> 外部 TTS HTTP API -> 返回真实服务响应。
- `text-to-image-workflow.yml`：提示词/画幅校验 -> 外部文生图 HTTP API -> 返回真实服务响应。

模板不包含服务凭据，也不包含静态音频、图片或伪造成功结果。仓库中的结构测试只能验证节点、分支和密钥边界；真实可用性必须在目标 Dify 版本中完成导入、绑定、发布和 API 调用验收。

## 当前本机配置状态（2026-07-18）

- 官方 Dify `1.15.0` Compose 与服务清单已通过配置检查；Docker Engine 29.6.1 当前可用，Dify Web 和 `/console/api/setup` 均 HTTP 200，API/PostgreSQL/Redis/Sandbox healthy。现有运行容器由 `D:\dify-runtime\docker` 启动，其 Compose 与仓库 `.runtime/dify/docker` 副本哈希相同；没有为路径一致性再启动第二套冲突实例。
- 本目录当前三份扩展 DSL 已在目标 Dify 环境完成相应配置；客服、TTS、文生图三个应用均已发布/可调用，FastAPI 三条链路均 `HTTP 200 / remote / degraded=false`，TTS/文生图真实媒体验收通过。模板 `0.3.0` 相对官方 `0.6.0` 的兼容性警告保留为 fresh workspace 边界。
- 本轮没有回显或写入任何私有 `.env` 值；Compose 加载私有配置后，客服、TTS、文生图 FastAPI 调用均为 `HTTP 200 / remote / degraded=false`，实际 WAV/PNG 已下载并通过播放/显示。
- 当前模板标记为 DSL `0.3.0`，而本机 Dify 1.15 导出的当前 DSL 版本为 `0.6.0`；导入可能带兼容警告。`dependencies` 也未包含 Tongyi marketplace 插件的精确版本与哈希，fresh workspace 必须先安装并配置 `langgenius/tongyi/tongyi`，或从已配置的目标实例重新导出模板后再纳入版本控制。
- 不要猜测插件版本或依赖哈希。只有目标实例重新导出的依赖记录、成功导入日志和发布后 API/媒体结果可以作为可复现证据。

## 本地 Dify 部署后的导入步骤

Docker 与 Dify 服务已经运行；第 8 天知识库索引、分段/TopK 和三条召回命中，以及 TTS/文生图发布和真实媒体响应均已完成。以下步骤保留为 fresh workspace 的复现说明，不表示当前实例仍缺这些配置。

1. 在浏览器打开 `http://localhost:8081`，按 Dify 首次使用界面创建本地管理员账户和工作空间。管理员账户、密码和恢复信息必须由你本人保管，不要写入项目文件或知识库。
2. 在 Dify 控制台的模型提供方设置中添加你有权使用的聊天模型，并在控制台中保存该模型提供方的凭据。模型名称、提供方类型和可用模型以你的 Dify 版本及模型服务商控制台为准。
3. 创建一个用于“商务服务助手”的知识库，并导入下列两份 Markdown 文件：
   - `dify/knowledge/official-business-support-sources.md`：公开权威来源的原创摘要、来源编号和合规边界。它为合同、发票、个人信息和人工复核类回答提供可检索依据。
   - `dify/knowledge/acceptance-questions.md`：验收问题、通过条件、必须引用的来源编号和禁止回答。上传后应关闭该文档的检索开关，仅把它作为人工回归清单，不能让标准答案参与生产问答召回。
4. 等待公开来源文档完成解析和索引，记录该知识库的数据集 ID。不要把数据集 ID、上传记录或任何敏感文件提交到 Git。
5. 导入 `business-support-workflow.yml`，在控制台把全零数据集占位符替换为第 4 步的知识库，并设置 `PLATFORM_API_BASE_URL`。该地址必须是 Dify 容器可访问的 FastAPI HTTPS 根地址，不能携带 API Key。若当前 Dify 版本拒绝导入或提示 DSL 字段不兼容，请在可视化编辑器中按相同节点关系重建工作流。
6. 打开工作流中的 LLM 节点，选择第 2 步已配置的模型提供方和模型；保留低随机性的回答设置，并确保系统提示词仍要求：只依据检索内容回答、信息不足时转人工、合同/付款/订单/故障等高风险事项需人工复核。
7. 在 Dify 控制台调试知识库检索和工作流。可用 `acceptance-questions.md` 中的问题逐项检查回答是否引用相应来源、是否拒绝接收敏感信息，以及是否在需要时提示人工处理。
8. 测试结果符合预期后，在 Dify UI 中发布工作流，并为已发布的应用生成调用凭据。服务地址和应用调用凭据只能保存到本机私有部署配置或受控的密钥管理中，不能写入本 README、知识库、截图或 Git 提交。

## TTS 与文生图工作流绑定

1. 分别导入 `text-to-speech-workflow.yml` 和 `text-to-image-workflow.yml`；两份模板的结束节点先经过 URL 提取代码节点，避免把 provider JSON 拼接到临时签名 URL 后面。
2. 在 Dify 环境变量中配置 `TTS_API_URL` / `IMAGE_API_URL`，在 Secret 类型变量中配置 `TTS_API_KEY` / `IMAGE_API_KEY`。地址、认证头、请求体和响应字段需按已获授权的服务商协议调整。
3. TTS 调试必须检查响应确实包含可播放音频或受控音频 URL，并验证媒体类型、大小、有效期和域名白名单；DashScope `audio.url` 只有效 24 小时，复制时只复制 URL 本身，不能包含后续 JSON 文本。文生图调试必须检查返回真实图片或受控图片 URL，并执行同样的来源与大小校验。
4. 当前目标实例已取得授权并完成媒体验收；fresh workspace 若没有提供方账号、配额和调用授权，这两个文件仍只能标记为可导入模板。

后端调用工作流时使用服务根地址，并自行追加工作流 API 路径；不要把重复的版本路径写入服务根地址。当前项目后端运行在 Docker Desktop 容器中，因此本机 Dify 地址应写为 `http://host.docker.internal:8081`；只有原生运行后端时才使用 `http://127.0.0.1:8081`。不同 Dify 版本的菜单名称或 DSL 字段可能略有不同，应以该版本控制台提示为准。

FastAPI 的调用映射如下：

| Dify 应用 | FastAPI 路径 | 输入 | 凭据变量 |
| --- | --- | --- | --- |
| 客服工作流 | `POST /api/v1/dify/customer-service` | `query` | `DIFY_API_KEY` |
| TTS 工作流 | `POST /api/v1/dify/text-to-speech`（`/dify/tts`） | `text`, `voice` | `DIFY_TTS_API_KEY`，空时回退 `DIFY_API_KEY` |
| 文生图工作流 | `POST /api/v1/dify/text-to-image`（`/dify/image`） | `prompt`, `size` | `DIFY_IMAGE_API_KEY`，空时回退 `DIFY_API_KEY` |

客服工作流在远程不可用时可返回带引用的本地 RAG 回退；当前目标实例的客服、TTS、文生图均已远程通过。TTS 和文生图没有安全的本地媒体回退，只有 Dify 输出经过外部 URL/data URL、媒体类型、大小和公网主机校验后才返回 `200`。未配置返回 `503`，上游失败或无真实媒体返回 `502`。`DIFY_MEDIA_ALLOWED_HOSTS` 可选限制受控媒体 URL 的主机名。模板结构测试和 FastAPI mock 测试仍不替代 fresh workspace 的导入/发布验收。

## 重新部署或轮换密钥时仍需手工完成

这些操作涉及账户授权、外部服务凭据和真实业务资料，不能由项目代码替代，也不应伪造：

1. 管理员账户、通义模型凭据和工作流 API Key 只能由本人保管，不得写入文档、截图或提交历史。
2. API Key 轮换后，只在私有 `.env` 更新 `DIFY_API_KEY`，再执行 `docker compose up -d --force-recreate backend`。
3. 补充业务资料时，只能上传经批准且已脱敏的内容；验收问题和标准答案不得启用为业务检索文档。
4. 每次更换嵌入模型或替换知识文档后，应在 Dify“召回测试”中复验来源编号和官方链接，再发布工作流更新。

## 运行原则

- Dify 是可选的增强服务，不作为本地 Compose 的必需依赖。
- 后端是 AI 能力的统一入口；前端只调用 `/api/v1/dify/customer-service`，不直连 Dify。
- Dify Gateway 本身不访问业务数据库；它只返回远程回答或降级原因。
- Dify 不可用、调用凭据缺失、返回空回答或模型调用失败时，FastAPI API 层使用请求数据库会话运行 `BusinessAgentOrchestrator`，返回真实本地 RAG 回答、`citations` 和包含 Gateway 原因的 `trace`，不使用固定泛化文案。
- 远程 Dify 成功时当前工作流只承诺答案文本，因此统一 API 的 `citations` 和 `trace` 可以为空数组。
- 不要把调用凭据、数据集 ID、客户文件、生产提示词或未脱敏业务资料提交到 Git。

## 建议节点参数

| 节点 | 推荐参数 | 目的 |
| --- | --- | --- |
| 知识检索 | Top K=3，阈值按评测集调整 | 兼顾证据数量与噪声 |
| 文本向量 | 选择目标 Dify 中已验证可用的中文 embedding 模型（记录实际提供方/模型名） | 避免猜测模型或插件版本；更换后必须重新索引并复验召回 |
| LLM | 温度=0.2，要求给出下一步 | 保持客服答复稳定、可执行 |
| 答复 | 返回答案和来源名称 | 前端可展示依据 |
| 条件分支（扩展） | 无来源/故障 -> 转人工 | 高风险和未知问题不自动承诺 |
