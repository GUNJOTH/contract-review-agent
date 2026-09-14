"""OCR 网关 HTTP 客户端：扫描页文字识别与印章识别。"""

from __future__ import annotations

import httpx
from loguru import logger

from contract_review_app.config import settings


class OCRGatewayError(RuntimeError):
    """调用 OCR 网关失败。"""


class OCRGatewayClient:
    """通过 OCR 网关的通用印刷体 / 印章接口补识别，不直连 Triton。"""

    def __init__(self) -> None:
        self.base_url = settings.OCR_GATEWAY_BASE_URL.rstrip("/")
        self.api_prefix = settings.OCR_GATEWAY_API_PREFIX.rstrip("/") or "/api/v1"
        self.token = settings.OCR_GATEWAY_TOKEN
        self.timeout = settings.OCR_GATEWAY_TIMEOUT_SECONDS

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers[settings.OCR_GATEWAY_AUTH_HEADER] = self.token
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{self.api_prefix}{path}"

    def health(self) -> dict:
        try:
            response = httpx.get(
                self._url("/health"),
                headers=self._headers(),
                timeout=2.0,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise OCRGatewayError(f"OCR 网关健康检查失败: {exc}") from exc

    def recognize_text(self, image_bytes: bytes) -> dict:
        """调用通用印刷体识别，返回 rec_texts / rec_scores / dt_polys。"""
        data = self._post_multipart(
            "/general-basic-ocr",
            form_data={"IsPdf": "false"},
            file_name="page.png",
            file_content=image_bytes,
        )
        detections = (data.get("Response") or {}).get("TextDetections") or []
        rec_texts: list[str] = []
        rec_scores: list[float] = []
        dt_polys: list[list[list[float]]] = []
        for item in detections:
            text = str(item.get("DetectedText") or "").strip()
            rec_texts.append(text)
            confidence = item.get("Confidence")
            rec_scores.append((float(confidence) / 100.0) if confidence is not None else 0.0)
            polygon = item.get("Polygon") or []
            if polygon:
                dt_polys.append(
                    [[float(point.get("X", 0)), float(point.get("Y", 0))] for point in polygon]
                )
                continue
            box = item.get("ItemPolygon") or {}
            x = float(box.get("X") or 0)
            y = float(box.get("Y") or 0)
            width = float(box.get("Width") or 0)
            height = float(box.get("Height") or 0)
            dt_polys.append(
                [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
            )
        return {"rec_texts": rec_texts, "rec_scores": rec_scores, "dt_polys": dt_polys}

    def recognize_seal(self, image_bytes: bytes, *, page_number: int = 1) -> dict | None:
        try:
            data = self._post_multipart(
                "/seal",
                form_data={
                    "EnablePdf": "false",
                    "PdfPageNumber": str(page_number),
                    "UseVL": "true",
                },
                file_name="page.png",
                file_content=image_bytes,
            )
        except OCRGatewayError as exc:
            logger.warning(f"OCR 网关印章识别失败: {exc}")
            return None
        result = data.get("Response") or {}
        seal_infos = result.get("SealInfos") or []
        if not seal_infos and result.get("SealBody"):
            seal_infos = [
                {
                    "SealBody": result.get("SealBody"),
                    "Location": result.get("Location"),
                    "OtherTexts": result.get("OtherTexts") or [],
                    "SealShape": result.get("SealShape"),
                }
            ]
        if not seal_infos:
            return None
        return {
            "count": len(seal_infos),
            "seal_infos": seal_infos,
            "source": result.get("Source") or "ocr-gateway",
        }

    def _post_multipart(
        self,
        path: str,
        *,
        form_data: dict[str, str],
        file_name: str,
        file_content: bytes,
    ) -> dict:
        """按 OCR 网关契约提交文件和表单字段。

        OCR 网关的识别接口声明为 ``multipart/form-data``。页面和印章识别
        调用方已经提供 PNG 字节，因此直接使用 ``file`` 字段，避免 JSON
        请求体无法被网关的表单参数解析器绑定。
        """
        try:
            response = httpx.post(
                self._url(path),
                data=form_data,
                files={"file": (file_name, file_content, "image/png")},
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise OCRGatewayError(
                f"OCR 网关 HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OCRGatewayError(f"OCR 网关请求失败: {exc}") from exc
        except ValueError as exc:
            raise OCRGatewayError("OCR 网关返回了无效 JSON") from exc


ocr_gateway_client = OCRGatewayClient()
