"""合同印章视觉证据检测器。

对合同包内 PDF 逐页调用 OCR 网关印章识别接口，把识别结果登记为
``VISUAL_REGION`` 证据，供审查引擎的视觉规则引用。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pymupdf
from loguru import logger

from contract_review.models import (
    BoundingBox,
    Document,
    Evidence,
    EvidenceType,
    SourceLocator,
)

from contract_review_app.config import settings
from contract_review_app.services.ocr_client import ocr_gateway_client

SEAL_DETECTOR_VERSION = "seal-detector-0.2.0"


class SealEvidenceError(RuntimeError):
    """印章证据检测失败。"""


class SealEvidenceDetector:
    """把印章识别结果转换为带页面定位的视觉证据。"""

    def __init__(self, max_pages: int | None = None) -> None:
        self.max_pages = (
            max_pages if max_pages is not None else settings.CONTRACT_SEAL_MAX_PAGES
        )

    def detect(
        self,
        paths: Sequence[str | Path],
        documents: Sequence[Document],
        document_filenames: Sequence[str] | None = None,
    ) -> list[Evidence]:
        """对 PDF 逐页检测印章并生成证据；识别服务不可用时返回空列表。"""

        if document_filenames is not None and len(document_filenames) != len(paths):
            raise ValueError(
                "document_filenames must contain one logical filename per input path"
            )
        if not settings.CONTRACT_SEAL_DETECTION_ENABLED:
            return []
        by_name = {document.filename: document for document in documents}
        evidence: list[Evidence] = []
        for index, raw_path in enumerate(paths):
            path = Path(raw_path)
            if path.suffix.lower() != ".pdf":
                continue
            logical_filename = (
                document_filenames[index]
                if document_filenames is not None
                else path.name
            )
            document = by_name.get(logical_filename) or by_name.get(path.name)
            if document is None:
                continue
            evidence.extend(self._detect_pdf(path, document))
        return evidence

    def _detect_pdf(self, path: Path, document: Document) -> list[Evidence]:
        evidence: list[Evidence] = []
        try:
            with pymupdf.open(str(path)) as pdf:
                page_count = min(pdf.page_count, self.max_pages)
                for page_number in range(1, page_count + 1):
                    evidence.extend(
                        self._detect_page(path, document, pdf, page_number)
                    )
        except Exception as exc:
            logger.warning(
                f"印章证据检测失败（{path.name}），跳过该文件: {exc}"
            )
        return evidence

    def _detect_page(
        self,
        path: Path,
        document: Document,
        pdf: pymupdf.Document,
        page_number: int,
    ) -> list[Evidence]:
        del path
        evidence: list[Evidence] = []
        page = pdf[page_number - 1]
        page_rect = page.rect
        pixmap = page.get_pixmap(
            dpi=settings.SEAL_PDF_RENDER_DPI, colorspace=pymupdf.csRGB
        )
        scale_x = page_rect.width / pixmap.width
        scale_y = page_rect.height / pixmap.height

        result = ocr_gateway_client.recognize_seal(
            pixmap.tobytes("png"), page_number=page_number
        )
        if not result:
            return evidence
        source = result.get("source") or "ocr-gateway"
        seal_infos = result.get("seal_infos") or []
        for index, seal_info in enumerate(seal_infos):
            seal_body = seal_info.get("SealBody") or ""
            bbox = self._location_bbox(
                seal_info.get("Location"), scale_x, scale_y
            )
            evidence.append(
                Evidence(
                    evidence_id=(
                        f"seal-{document.document_id}-p{page_number}-{index}"
                    ),
                    evidence_type=EvidenceType.VISUAL_REGION,
                    package_id=document.package_id,
                    document_id=document.document_id,
                    source_sha256=document.source_sha256,
                    locator=SourceLocator(
                        locator_type="page",
                        page_number=page_number,
                        bbox=bbox,
                    ),
                    raw_excerpt=seal_body,
                    display_excerpt=seal_body or "检测到印章",
                    extraction_method=f"seal-{source}",
                    extraction_version=(
                        f"{SEAL_DETECTOR_VERSION}-{source}"
                    ),
                    confidence=None,
                )
            )
        return evidence

    @staticmethod
    def _location_bbox(
        location: object, scale_x: float, scale_y: float
    ) -> BoundingBox | None:
        """印章 Location 可能是 4 点四边形或 bbox；换算到 PDF 点坐标。"""
        if not location:
            return None
        try:
            points = list(location)
        except TypeError:
            return None
        if not points:
            return None
        if isinstance(points[0], (int, float)):
            values = [float(value) for value in points]
            xs, ys = values[0::2], values[1::2]
        elif isinstance(points[0], dict):
            xs = [float(point.get("X", 0)) for point in points]
            ys = [float(point.get("Y", 0)) for point in points]
        else:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
        if not xs or not ys:
            return None
        try:
            return BoundingBox(
                x1=min(xs) * scale_x,
                y1=min(ys) * scale_y,
                x2=max(xs) * scale_x,
                y2=max(ys) * scale_y,
            )
        except ValueError:
            return None
