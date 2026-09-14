# 合同审查智能体

独立的合同审查应用：审查引擎源码已内置于本仓库 `src/contract_review`，不再通过 wheel 安装 `contract-review-agent`。扫描页和印章识别通过 **OCR 网关 HTTP 接口** 完成。

仓库不包含 `.env`（已加入 `.gitignore`，避免把密钥推到 GitHub）。克隆后必须先从模板生成自己的配置，否则进程能启动，但合同审查、OCR、异步任务都不可用。

## 与 OCR 网关的关系

- OCR 网关只负责证件/印章/通用印刷体识别。
- 本项目调用：
  - `POST /api/v1/general-basic-ocr`：扫描页补识别
  - `POST /api/v1/seal`：印章视觉证据
  - `GET /api/v1/health`：连通性检查

默认本服务端口 `8090`，本地运行只监听 `127.0.0.1`；配置模板中的 OCR 网关指向 `http://172.20.1.147:8080`，不同环境请在 `.env` 里覆盖。Docker Compose 会把服务监听地址显式设为 `0.0.0.0`，以便容器端口映射。
除健康检查外的合同审查、预览、对比、规则和任务 API 都需要 `X-API-Token`（或 `AUTH_HEADER_NAME` 配置的请求头）。审查控制台右上角可以填写 Token，Token 只保存在当前浏览器会话中；若 OCR 网关开启了鉴权，把 `OCR_GATEWAY_TOKEN` 写在本服务 `.env` 即可，由后端代填。

## 配置（必做）

```bash
cp .env.example .env
```

`.env.example` 只是模板，里面的密钥都是空的。复制后至少填写下面几项，否则对应功能会静默降级或失败。

| 变量 | 作用 | 不填会怎样 |
| --- | --- | --- |
| `CONTRACT_REVIEW_ENDPOINT` | OpenAI 兼容的大模型接口 | 页面能打开，但语义审查不会调用外部模型；确定性规则和事实抽取仍可执行 |
| `CONTRACT_REVIEW_API_KEY` | 模型接口密钥 | 接口需要鉴权时审查失败 |
| `CONTRACT_REVIEW_MODEL` | 模型名 | 请求体缺少模型名，审查失败 |
| `OCR_GATEWAY_BASE_URL` | OCR 网关地址 | 扫描件/印章识别连不到网关；启动时只打 warning，不阻止进程 |
| `OCR_GATEWAY_TOKEN` | OCR 网关鉴权（网关开了鉴权才需要） | 扫描件/印章识别 401 |
| `API_TOKEN` | 本服务 API 鉴权 Token | 未配置时受保护 API 返回 503；缺少或错误 Token 返回 401 |
| `REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | 异步任务队列 | 默认同步审查仍可用；`--role all`（默认）会再拉 worker/beat，没 Redis 时子进程会退出 |
| `TASK_IDEMPOTENCY_TTL_SECONDS` / `TASK_STAGE_EVENT_LIMIT` | 幂等键保留时间 / 阶段事件账本长度 | 默认 72 小时 / 256 条；Redis 原子脚本负责最终待处理上限准入 |
| `CONTRACT_AI_PII_GATE_ENABLED` / `CONTRACT_AI_PII_MODE` | 外部模型 PII 门禁 | 默认 `true` / `block`；发现手机号、身份证号、邮箱、账号等高置信度信息，模型调用 fail-closed |
| `CONTRACT_RULES_PATH` / `CONTRACT_CORE_RULES_PATH` | 基础规则快照 / 核心业务扩展规则快照 | 默认加载 v0.14 基础规则和 v0.15 付款、交付、验收、续期、终止、违约、跨文档规则；任一快照校验失败则拒绝审查 |

可选但建议一并填：

- `CONTRACT_REVIEW_EMBEDDING_ENDPOINT` / `CONTRACT_REVIEW_EMBEDDING_MODEL` / `CONTRACT_REVIEW_EMBEDDING_API_KEY`：启用词法与向量混合召回；不填则使用引擎词法检索
- `CONTRACT_REVIEW_PROVIDER`：默认 `openai-compatible`
- `AUTH_HEADER_NAME`：本服务鉴权请求头名称，默认 `X-API-Token`
- `OTEL_ENABLED` / `OTEL_SERVICE_NAME`：可选 OpenTelemetry 链路追踪；先执行 `uv sync --frozen --extra otel`，未安装 SDK 或未开启时为 no-op，span 不包含合同正文、提示词或密钥

没有 `.env` 时，`pydantic-settings` 会用 `src/contract_review_app/config/settings.py` 里的默认值启动（端口 `8090`、回环监听、内网 OCR 地址等）。服务仍可启动，但因为没有配置 `API_TOKEN`，受保护 API 会 fail-closed 返回 503，不能用于实际审查。

## 启动

```bash
cp .env.example .env
# 按上一节填写 CONTRACT_REVIEW_*、OCR_GATEWAY_*、Redis

uv sync --frozen --extra dev
python -m contract_review_app.main
```

浏览器打开 [http://127.0.0.1:8090/ui](http://127.0.0.1:8090/ui)。

只跑 API（无 Redis 时同步审查仍可用）：

```bash
python -m contract_review_app.main --role api
```

默认 `--role all` 会同时拉起 API、Celery worker、beat，需要本机 Redis 先起来（默认 `localhost:6380`）。Docker 启动同样依赖仓库根目录的 `.env`：

```bash
docker compose up --build
```

## 主要页面

- 合同审查
- 正式 RuleBundle
- 任务中心

## 架构与演进

当前采用证据优先的模块化单体：确定性审查引擎与 FastAPI/Celery 适配层分离，OCR、模型、Redis、文件存储和规则快照均可替换。组件职责、状态链、优秀项目借鉴和分阶段路线见 [架构说明](docs/ARCHITECTURE.md) 与 [ADR-001](docs/decisions/ADR-001-modular-evidence-first.md)。

合同审查核心接口的业务前提通过 `ReviewContext` 统一传递：同步/异步上传接口支持 `ContractType`、`PartyPosition`、`Jurisdiction`、`TransactionContext`、`TransactionTags`、`TransactionAmount`、`DocumentKinds`、`DocumentPrecedence` 和 `ReviewScope`。`ContractType` 应优先填写正式 `RuleBundle` 中的规范名称（如 `软件开发/转让服务`）；已登记的 `software`、`software_development` 仅作为输入短名称在规则解析层统一归一，未登记类型不会被猜测为适用。`TransactionTags`、`TransactionAmount` 和合同包文档角色会进入规则 `ApplicabilitySpec` 的结构化条件；缺失条件保持 `UNKNOWN`，例外条件由 Playbook/Rule 集中裁决。`ReviewScope` 可填写规则 ID 或规则 category；缺省表示执行完整规则快照。所有适用规则统一经过 `RetrievalQuery → RetrievalTrace → CandidateEvidence → EvidenceAssessment`，关键词/BM25 和向量只是候选生成器；`CandidateEvidence` 不能直接产生审核结论，确定性模块只能消费 `EvidenceAssessment.outcome=ACCEPT` 的候选。结果中的 `ContractClause`、`ClauseRelation`、`ContractObligation`、`Finding`、`ReviewQuestion`、`QuestionAssessment`、`ContractVersionComparison`、`ContractRevisionSet` 均保留证据引用，规则适用性与 Playbook 动作由领域引擎统一判断。`ClauseRelation` 对已解析的层级/引用建立目标条款，对未找到目标的交叉引用保留 `UNRESOLVED`，不默认为已满足。金额、税率、付款、交付、验收、续期、终止、违约和发票检查器通过规则快照中的 `checker` 显式绑定到领域实现；人工确认通过 `POST /api/v1/contract-review/decision` 和 `POST /api/v1/contract-review/finalize` 更新同一个 `ReviewResult`。`GET /api/v1/contract-review/rule-bundle` 直接返回当前正式 `RuleBundle`；`POST /api/v1/contract-compare` 必须提交 `ReviewResultPayload`，将版本差异、业务义务影响、风险方向、文件优先效力和需重触发的 Playbook 回写同一结果；`POST /api/v1/contract-review/revision-set` 同时返回挂载了红线/修订建议的 `ReviewResult`。应用层不再返回独立风险清单、要素抽取或历史规则列表投影，合同标准要素统一读取 `ReviewResult.facts`。

除基础上下文外，`TransactionTags`、`TransactionAmount`、`DocumentKinds` 和 `DocumentPrecedence` 分别描述交易标签、金额区间输入、合同包文档角色和文件优先顺序；交易背景只参与规则适用性和上下文留痕，不作为合同正文的 BM25 词项。每条适用规则统一沿 `RetrievalQuery → RetrievalTrace → CandidateEvidence → EvidenceAssessment` 获取候选和资格裁决；BM25/向量命中只是候选证据，确定性检查器与语义模型共享同一候选集合，不能从候选之外直接扫描正文或生成通过结论。

异步审查只通过 `POST /api/v1/contract-review-async` 创建合同包任务；`GET /api/v1/tasks`、`GET /api/v1/tasks/{task_id}` 和 `GET /api/v1/tasks/{task_id}/result` 只负责任务查询与读取核心 `ReviewResult`，不再接受通用 OCR、Base64 或 URL 任务输入。

业务规则集中在 `data/contract_core_rules_v0.15.json`，加载时执行规则 ID、Playbook 立场、checker 绑定和 `ReviewResult` Schema 兼容门禁；草稿或不兼容快照不能进入审查。`publish_playbook_bundle` 只生成新的发布快照和指纹，不覆盖源文件。专家评测种子集位于 `evals/expert_contract_review_cases.json`，每个案例由完整合同包和专家标注闭环组成，严格区分条款定位、证据引用、规则判断、`UNKNOWN`、金额事实、金额计算、版本比较和红线建议；其中 `unknown_false_pass` 专门拦截证据不足却自动通过。数据契约和离线评测边界见 [评测说明](evals/README.md)。

## 测试

```bash
uv run --extra dev pytest
uv run ruff check src tests
uv run python -m compileall -q src tests
node --check src/contract_review_app/static/js/app.js
uv run python scripts/ci_api_smoke.py
uv run python scripts/evaluate_contract_fixtures.py
uv run python scripts/evaluate_expert_contract_cases.py
```

真实 Redis 验收不会在普通测试中自动连接外部服务；在验收机显式设置
`CONTRACT_REVIEW_LIVE_REDIS_URL` 后运行下面的并发回归，验证幂等键、原子准入和阶段事件账本：

```powershell
$env:CONTRACT_REVIEW_LIVE_REDIS_URL = 'redis://172.20.1.147:6380/2'
uv run --no-sync pytest -q tests/integration/test_redis_task_store_live.py
```

测试默认把 pytest 缓存写入 `.test-work/pytest-cache`，不会依赖本机的隐藏缓存目录。覆盖率报告可以按 CI 命令生成：

```bash
uv run pytest --cov=contract_review --cov=contract_review_app --cov-report=term-missing
```

`scripts/ci_api_smoke.py` 只启动本地 API 进程，验证根路径、未带 Token 的受保护接口和 OpenAPI 鉴权声明，不连接 Redis、OCR 网关或模型服务。Docker、Redis/Celery、OCR 和模型的真实联调仍需在具备对应运行环境时单独验收。

异步接口支持 `Idempotency-Key` 请求头。相同键在保留期内返回同一个任务，
Redis 原子准入保证同一键只登记一条任务，只有胜者会落盘并入队。任务状态接口
返回 append-only 阶段事件账本，审计结果另存独立事件 JSONL。审查结果采用
`schema_version=2.0`，旧结果和旧审计存储版本会被明确拒绝，不再自动迁移。
worker 领取任务时会获得按任务递增的 fencing token；心跳、进度、成功/失败写入
和释放锁均校验 token，租约过期后的旧 worker 不能覆盖新 worker 的状态。
固定的
离线合同夹具和评测约束见 [evals/README.md](evals/README.md)。
