"""合同审查引擎的 OCR 提供方适配器。

实现 ``contract_review.ocr.OCRProvider`` 协议，把 OCR 网关通用印刷体识别
转成带 PDF 页坐标的文字块。扫描页由审查引擎的 parser 渲染为 2x PNG 传入。
"""

from __future__ import annotations

import pymupdf

from contract_review.models import BoundingBox, PageGeometry
from contract_review.ocr import OCRPageResult, OCRProviderError, OCRTextBlock

from contract_review_app.config import settings
from contract_review_app.services.ocr_client import OCRGatewayError, ocr_gateway_client


class TritonOCRProvider:
    """基于 OCR 网关通用印刷体识别的合同扫描页识别提供方。"""

    def __init__(self, confidence_threshold: float | None = None) -> None:
        threshold = (
            settings.CONTRACT_OCR_CONFIDENCE_THRESHOLD
            if confidence_threshold is None
            else confidence_threshold
        )
        self.confidence_threshold = threshold
        self.provider_version = (
            f"ocr-gateway-general-basic-{settings.APP_VERSION}-conf{threshold}"
        )

    def recognize(
        self,
        page_image: bytes,
        *,
        page_number: int,
        geometry: PageGeometry,
    ) -> OCRPageResult:
        del page_number
        image_width, image_height = self._image_size(page_image)
        scale_x = geometry.width / image_width
        scale_y = geometry.height / image_height
        try:
            raw = ocr_gateway_client.recognize_text(page_image)
        except OCRGatewayError as exc:
            raise OCRProviderError(f"OCR 网关调用失败: {exc}") from exc
        except Exception as exc:
            raise OCRProviderError(f"OCR 识别失败: {exc}") from exc

        blocks: list[OCRTextBlock] = []
        for text, score, poly in zip(
            raw.get("rec_texts", []),
            raw.get("rec_scores", []),
            raw.get("dt_polys", []),
        ):
            normalized = str(text).strip()
            if not normalized:
                continue
            confidence = float(score) if score is not None else None
            if confidence is not None and confidence < self.confidence_threshold:
                continue
            bbox = self._polygon_bbox(poly, scale_x, scale_y)
            if bbox is None:
                continue
            blocks.append(
                OCRTextBlock(text=normalized, bbox=bbox, confidence=confidence)
            )
        return OCRPageResult(blocks=blocks)

    @staticmethod
    def _image_size(image_bytes: bytes) -> tuple[int, int]:
        try:
            pixmap = pymupdf.Pixmap(image_bytes)
            return pixmap.width, pixmap.height
        except Exception as exc:
            raise OCRProviderError(f"无法解码 OCR 页面图像: {exc}") from exc

    @staticmethod
    def _polygon_bbox(
        poly: object, scale_x: float, scale_y: float
    ) -> BoundingBox | None:
        """兼容嵌套 [[x,y],...] 与扁平 [x1,y1,x2,y2,...] 两种多边形格式。"""
        try:
            points = list(poly)  # type: ignore[arg-type]
        except TypeError:
            return None
        if not points:
            return None
        if isinstance(points[0], (int, float)):
            values = [float(value) for value in points]
            xs, ys = values[0::2], values[1::2]
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
