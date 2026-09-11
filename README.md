# 合同审查智能体

独立的合同审查应用：审查引擎源码已内置于本仓库 `src/contract_review`，不再通过 wheel 安装 `contract-review-agent`。扫描页和印章识别通过 **OCR 网关 HTTP 接口** 完成。

仓库不包含 `.env`（已加入 `.gitignore`，避免把密钥推到 GitHub）。克隆后必须先从模板生成自己的配置，否则进程能启动，但合同审查、OCR、异步任务都不可用。

## 与 OCR 网关的关系

- OCR 网关只负责证件/印章/通用印刷体识别。
- 本项目调用：
  - `POST /api/v1/general-basic-ocr`：扫描页补识别
  - `POST /api/v1/seal`：印章视觉证据
  - `GET /api/v1/health`：连通性检查

默认本服务端口 `8090`，本地运行只监听 `127.0.0.1`；OCR 网关默认指向本机 `http://127.0.0.1:8080`，请在 `.env` 里改成你的实际网关地址。Docker Compose 会把服务监听地址显式设为 `0.0.0.0`，以便容器端口映射。
除健康检查外的合同审查、预览、对比、规则和任务 API 都需要 `X-API-Token`（或 `AUTH_HEADER_NAME` 配置的请求头）。审查控制台右上角可以填写 Token，Token 只保存在当前浏览器会话中；若 OCR 网关开启了鉴权，把 `OCR_GATEWAY_TOKEN` 写在本服务 `.env` 即可，由后端代填。

## 配置（必做）

```bash
cp .env.example .env
```

`.env.example` 只是模板，里面的密钥都是空的。复制后至少填写下面几项，否则对应功能会静默降级或失败。

| 变量 | 作用 | 不填会怎样 |
| --- | --- | --- |
| `CONTRACT_REVIEW_ENDPOINT` | OpenAI 兼容的大模型接口 | 页面能打开，但 AI 审查 / 要素提取不会调用模型 |
| `CONTRACT_REVIEW_API_KEY` | 模型接口密钥 | 接口需要鉴权时审查失败 |
| `CONTRACT_REVIEW_MODEL` | 模型名 | 请求体缺少模型名，审查失败 |
| `OCR_GATEWAY_BASE_URL` | OCR 网关地址 | 扫描件/印章识别连不到网关；启动时只打 warning，不阻止进程 |
| `OCR_GATEWAY_TOKEN` | OCR 网关鉴权（网关开了鉴权才需要） | 扫描件/印章识别 401 |
| `API_TOKEN` | 本服务 API 鉴权 Token | 未配置时受保护 API 返回 503；缺少或错误 Token 返回 401 |
| `REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | 异步任务队列 | 默认同步审查仍可用；`--role all`（默认）会再拉 worker/beat，没 Redis 时子进程会退出 |
| `TASK_IDEMPOTENCY_TTL_SECONDS` / `TASK_STAGE_EVENT_LIMIT` | 幂等键保留时间 / 阶段事件账本长度 | 默认 72 小时 / 256 条；Redis 原子脚本负责最终待处理上限准入 |
| `CONTRACT_AI_PII_GATE_ENABLED` / `CONTRACT_AI_PII_MODE` | 外部模型 PII 门禁 | 默认 `true` / `block`；发现手机号、身份证号、邮箱、账号等高置信度信息，模型调用 fail-closed |

可选但建议一并填：

- `CONTRACT_REVIEW_EMBEDDING_ENDPOINT` / `CONTRACT_REVIEW_EMBEDDING_MODEL` / `CONTRACT_REVIEW_EMBEDDING_API_KEY`：启用向量检索；不填则退回引擎词法检索
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
- 合同拟定（要素提取）
- 规则引擎库
- 任务中心

## 架构与演进

当前采用证据优先的模块化单体：确定性审查引擎与 FastAPI/Celery 适配层分离，OCR、模型、Redis 和本地 SQLite 均可替换。组件职责、状态链、优秀项目借鉴和分阶段路线见 [架构说明](docs/ARCHITECTURE.md) 与 [ADR-001](docs/decisions/ADR-001-modular-evidence-first.md)。

## 测试

```bash
uv run --extra dev pytest
uv run ruff check src tests
uv run python -m compileall -q src tests
node --check src/contract_review_app/static/js/app.js
uv run python scripts/ci_api_smoke.py
uv run python scripts/evaluate_contract_fixtures.py
```

测试默认把 pytest 缓存写入 `.test-work/pytest-cache`，不会依赖本机的隐藏缓存目录。覆盖率报告可以按 CI 命令生成：

```bash
uv run pytest --cov=contract_review --cov=contract_review_app --cov-report=term-missing
```

`scripts/ci_api_smoke.py` 只启动本地 API 进程，验证根路径、未带 Token 的受保护接口和 OpenAPI 鉴权声明，不连接 Redis、OCR 网关或模型服务。Docker、Redis/Celery、OCR 和模型的真实联调仍需在具备对应运行环境时单独验收。

异步接口支持 `Idempotency-Key` 请求头。相同键在保留期内返回同一个任务，
不会重复落盘或入队；任务状态接口同时返回 append-only 阶段事件账本。固定的
离线合同夹具和评测约束见 [evals/README.md](evals/README.md)。
