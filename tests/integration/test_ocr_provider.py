"""OCR 网关适配器单元测试（mock HTTP 客户端，无需真实服务）。"""

import pymupdf
import pytest

from contract_review.models import PageGeometry
from contract_review.ocr import OCRProviderError

from contract_review_app.services.ocr_client import OCRGatewayError
from contract_review_app.services.triton_ocr_provider import TritonOCRProvider


def _render_page_png(width: float = 612, height: float = 792) -> bytes:
    """按 parser 的方式渲染 2x PNG（图像像素 = 2 * PDF 点）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((72, 72), "测试文本")
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    png = pix.tobytes("png")
    doc.close()
    return png


class FakeOCRClient:
    def __init__(self, result: dict) -> None:
        self.result = result

    def recognize_text(self, image_bytes: bytes) -> dict:
        del image_bytes
        return self.result


def test_recognize_maps_pixel_polygons_to_pdf_coordinates(monkeypatch):
    fake = FakeOCRClient(
        {
            "rec_texts": ["合同金额一百万元", "   "],
            "rec_scores": [0.92, 0.5],
            "dt_polys": [
                [[100, 200], [300, 200], [300, 260], [100, 260]],
                [[0, 0], [10, 0], [10, 10], [0, 10]],
            ],
        }
    )
    provider = TritonOCRProvider()
    monkeypatch.setattr(
        "contract_review_app.services.triton_ocr_provider.ocr_gateway_client", fake
    )

    result = provider.recognize(
        _render_page_png(),
        page_number=1,
        geometry=PageGeometry(width=612, height=792),
    )

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.text == "合同金额一百万元"
    assert block.confidence == pytest.approx(0.92)
    assert block.bbox.x1 == pytest.approx(50.0)
    assert block.bbox.y1 == pytest.approx(100.0)
    assert block.bbox.x2 == pytest.approx(150.0)
    assert block.bbox.y2 == pytest.approx(130.0)
    assert provider.provider_version.startswith("ocr-gateway-general-basic")


def test_recognize_handles_flat_polygon_format(monkeypatch):
    fake = FakeOCRClient(
        {
            "rec_texts": ["标题"],
            "rec_scores": [0.8],
            "dt_polys": [[0, 0, 100, 0, 100, 20, 0, 20]],
        }
    )
    provider = TritonOCRProvider()
    monkeypatch.setattr(
        "contract_review_app.services.triton_ocr_provider.ocr_gateway_client", fake
    )

    result = provider.recognize(
        _render_page_png(),
        page_number=1,
        geometry=PageGeometry(width=612, height=792),
    )

    assert len(result.blocks) == 1
    assert result.blocks[0].bbox.x1 == pytest.approx(0.0)
    assert result.blocks[0].bbox.x2 == pytest.approx(50.0)
    assert result.blocks[0].bbox.y2 == pytest.approx(10.0)


def test_recognize_raises_ocr_provider_error_on_gateway_failure(monkeypatch):
    class BrokenOCRClient:
        def recognize_text(self, image_bytes: bytes) -> dict:
            del image_bytes
            raise OCRGatewayError("ocr gateway down")

    provider = TritonOCRProvider()
    monkeypatch.setattr(
        "contract_review_app.services.triton_ocr_provider.ocr_gateway_client",
        BrokenOCRClient(),
    )

    with pytest.raises(OCRProviderError):
        provider.recognize(
            _render_page_png(),
            page_number=1,
            geometry=PageGeometry(width=612, height=792),
        )
