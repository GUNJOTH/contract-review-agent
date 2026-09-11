# 贡献指南

## 开发环境

项目要求 Python 3.11 或 3.12，依赖由 `uv.lock` 锁定。首次准备环境：

```bash
uv sync --frozen --extra dev
```

测试和静态检查：

```bash
uv run pytest
uv run ruff check src tests
uv run python -m compileall -q src tests
node --check src/contract_review_app/static/js/app.js
```

当前提交的 Ruff 门禁先固定在语义/运行时错误规则（E4、E7、E9、F），避免把历史格式差异一次性混入业务 PR；格式化会随后续触碰文件逐步收敛。

测试只使用仓库内的最小夹具、临时目录和测试 Token，不连接生产数据库、OCR 网关或模型服务。

## Pull Request 要求

- 每个行为修复都应带一个可稳定复现的回归测试。
- 说明未执行的 Docker、Redis、Celery、OCR 或模型验证，以及阻塞原因。
- 不提交 `.env`、密钥、客户合同、运行时数据库和本机缓存。
- API、鉴权、文件上传和异步任务改动需要说明兼容性与回滚方式。
- 合并前应确认 `uv.lock` 与 `pyproject.toml` 一致。
