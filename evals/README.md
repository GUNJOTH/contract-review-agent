# 固定合同离线评测集

`contract_review_cases.json` 是版本化的最小合同夹具集。每个夹具包含固定
文字、合同类型、最低发现数、最低条款/义务数量、问题结论覆盖率、报告状态和
证据类型；带 PII 的夹具还验证外部模型门禁必须返回 `block`。

评测不会访问 OCR、Redis、Celery 或外部模型：脚本在临时目录生成文字 PDF，
运行确定性审查，然后检查 Schema v2、条款与义务、规则问题覆盖、证据审计、
阶段事件账本和结果指纹重放。

```powershell
$env:UV_CACHE_DIR = '.uv-cache'
uv run --no-sync python scripts/evaluate_contract_fixtures.py
```

夹具变更应与规则快照变更一起评审；不要把真实合同或真实个人信息提交到
该目录。
