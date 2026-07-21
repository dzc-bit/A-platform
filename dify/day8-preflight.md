# 第 8 天 Dify 预检与验收

`day8_preflight.py` 将第 8 天能自动化的部分集中到一个可复现命令中。默认模式只读：它检查 `.runtime/dify/docker/docker-compose.yaml`、官方 `.env.example`、合成知识资料、分段/检索参数和三份 DSL 的版本兼容性，不会启动 Docker，也不会读取私有 `.env`。

## 只读预检

在项目根目录执行：

```powershell
backend\.venv\Scripts\python.exe dify\day8_preflight.py
```

预检应显示：

- 官方 Dify API/Web/Worker 镜像均为 `1.15.0`，并且 Compose 中存在 API、Worker、PostgreSQL、Redis、Weaviate 和 Nginx；
- `official-business-support-sources.md` 含 `S1-S8`，`acceptance-questions.md` 含 `AQ-01..AQ-16`，所有问题都能映射到来源编号；
- 建议使用 `high_quality` 索引、Dify 中已配置的文本向量模型、段落分隔符 `\\n\\n`、每段 500 tokens、重叠 50 tokens、语义检索 `top_k=3`、关闭重排；
- 验收问题文件是回归清单，不是生产召回材料。若将它导入知识库，导入完成后必须在 Dify 中禁用该文档；
- 三份模板目前是 DSL `0.3.0`，官方源码的 `CURRENT_APP_DSL_VERSION` 是 `0.6.0`。Dify 1.15 对同一主版本较旧的小版本返回 `completed-with-warnings`，这不是“已发布”证明。模板没有被擅自改写为 `0.6.0`，因为插件依赖、模型节点字段和目标实例导出结果必须以目标实例为准。

命令末尾的 `BLOCKED DSL import gate` 是有意的门禁：它只表示尚未取得目标实例的导入结果，并不表示静态预检失败。

## 需要本人完成的操作

下面的步骤会启动服务、写入 Dify 数据或使用凭据，不能由脚本代替。请在本机完成后只回复每一步的结果摘要，不要发送密码、API Key、数据集 ID 或截图中的敏感值。

1. 启动 Docker Desktop 的 Linux Engine。进入 `.runtime/dify/docker`，按官方说明从 `.env.example` 创建私有 `.env`，确认主机端口映射后执行 `docker compose up -d`。用 `docker compose ps` 和 Dify 页面/健康端点确认 API、Web、Worker、PostgreSQL、Redis、向量库和 Nginx 均为 healthy/running。
2. 在 Dify 控制台完成管理员、工作空间和模型提供方设置，至少启用一个聊天模型和一个文本向量模型。记录模型“提供方标识”和“模型标识”，但不要把凭据发到聊天。
3. 在 Dify 知识库界面创建一个仅自己可见的空知识库。可导入 `dify/knowledge/official-business-support-sources.md` 和 `dify/knowledge/acceptance-questions.md`；后者解析完成后关闭“启用检索”。在召回设置中确认 500/50 分段、`top_k=3`，并等待两个文档都达到 completed。
4. 在控制台导入三个 DSL。由于版本警告，必须查看导入响应：`completed` 可继续；`completed-with-warnings` 需要在编辑器中检查节点/模型/数据集绑定、保存一次并确认没有失败节点；`pending` 或 `failed` 不得继续。将实际状态作为 `--dsl-import-status` 传给预检，不要手工把文件中的 `version: 0.3.0` 改成 `0.6.0`。

## 明确授权后运行 API 验收

只有在上述服务、模型和知识库已经由本人配置后，才在私有 PowerShell 会话中设置变量并运行。这里使用的是知识库 Service API token，不是工作流 App token；所有变量仅存在于当前会话：

```powershell
$env:DIFY_DATASET_API_URL = "http://localhost:8081" # 按实际 Dify 主机端口修改
$env:DIFY_DATASET_API_KEY = "<private-dataset-service-token>"
$env:DIFY_EMBEDDING_PROVIDER = "<provider-id-from-dify>"
$env:DIFY_EMBEDDING_MODEL = "<embedding-model-id-from-dify>"
backend\.venv\Scripts\python.exe dify\day8_preflight.py `
  --apply --create-dataset `
  --dsl-import-status completed-with-warnings `
  --acknowledge-dsl-warning `
  --json-output .runtime\day8-dify-result.json
```

`--apply` 会在指定实例中创建（或使用）知识库，导入公开来源和回归清单，禁用回归清单文档，轮询索引状态，并逐条调用 `/v1/datasets/{id}/retrieve`。输出只保存是否存在数据集 ID、文档状态和来源命中统计，不写入 token 或实际 ID。若知识库已经存在，应改用 `--dataset-id <uuid>` 和 `--skip-upload`，避免重复导入。

脚本只验收“召回结果包含预期来源编号”。回答措辞、敏感信息拒答、人工转交和工作流发布仍须在 Dify 控制台按 `acceptance-questions.md` 手工回归；没有真实导入、索引完成和召回响应，不能在验收报告中写成 Dify 已运行或已发布。

