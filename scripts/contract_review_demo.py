"""合同审查演示脚本：对单个合同文件跑完整审查（向量检索 + 语义模型）。

用法（在 contract-review 项目根目录执行）：
    uv run python scripts/contract_review_demo.py <合同文件路径> [--contract-type 合同类型] [--package-id 包ID]

合同类型可选值：软件产品销售 / 软件开发/转让服务 / 一般商品销售合同 / 混合合同 / 其它服务合同
不传 --contract-type 时默认按"其它服务合同"审查。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

from contract_review import KnowledgeSourceKind, project_review_analysis
from contract_review_app.services.review_service import run_contract_review

CONTRACT_TYPES = {
    "软件产品销售",
    "软件开发/转让服务",
    "一般商品销售合同",
    "混合合同",
    "其它服务合同",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="合同审查演示")
    parser.add_argument("file", help="合同文件路径（PDF/DOCX/XLSX）")
    parser.add_argument(
        "--contract-type",
        default="其它服务合同",
        help="合同类型，可选: " + " / ".join(sorted(CONTRACT_TYPES)),
    )
    parser.add_argument("--package-id", default="pkg-demo-001")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(f"文件不存在: {path}")
        return 1

    print(f"开始审查: {path.name}")
    print(f"合同类型: {args.contract_type} | 包ID: {args.package_id}")
    t0 = time.time()
    try:
        result = run_contract_review(
            [(path.name, path.read_bytes())],
            package_id=args.package_id,
            contract_type=args.contract_type,
        )
    except Exception as exc:
        print(f"审查失败: {exc}")
        print("常见原因：模型/embedding 服务超时或不可达（可在 .env 调大 "
              "CONTRACT_REVIEW_TIMEOUT_SECONDS），"
              "或本地未配置模型端点（CONTRACT_REVIEW_ENDPOINT）。")
        return 2
    print(f"耗时: {time.time() - t0:.1f}s\n")

    for document in result.documents:
        print(f"文档: {document.filename} | 解析状态: {document.parse_status}")
        if document.quality_flags:
            print(f"  质量标记: {document.quality_flags}")

    chunks_by_id = {c.chunk_id: c for c in result.knowledge_chunks}
    doc_hits = sum(
        1
        for trace in result.retrieval_traces
        for hit in trace.hits
        if chunks_by_id[hit.chunk_id].source_kind == KnowledgeSourceKind.CONTRACT
    )
    total = sum(len(trace.hits) for trace in result.retrieval_traces)
    versions = sorted({trace.index_version for trace in result.retrieval_traces})
    print(f"检索: {doc_hits}/{total} 个命中为合同正文块 | 索引: {versions[0] if versions else '词法基线'}")

    print(f"\n总体状态: {result.report.overall_status.value}")
    print(f"发现统计: {dict(result.report.finding_counts)}")
    if result.semantic_response is not None:
        items = result.semantic_response.items
        print(f"模型判断分布: {dict(Counter(item.status.value for item in items))}")

    print("\n" + "=" * 74)
    analysis = project_review_analysis(result)
    print(f"★ 风险清单投影（共 {len(analysis.items)} 项；来源为 ReviewResult）")
    for item in analysis.items:
        mark = "★" if item.risk_level in ("BLOCK", "WARN") else " "
        print(f"{mark}[{item.risk_level:>14}] {item.title}")
        print(f"         {item.reason[:110]}")
        if item.quote:
            print(f"         原文: {item.quote[:70]}")
        if item.suggested_action:
            print(f"         建议: {item.suggested_action[:80]}")

    print(f"\n正式规则包: {result.rule_bundle.bundle_id}（{len(result.rule_bundle.rules)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
