"""OCR provider contract for scanned pages.

The parser owns evidence construction. Providers only return validated text
regions in PDF page coordinates and must expose a version that identifies the
engine/model/configuration used for replay.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import Field, model_validator

from .models import BlockType, BoundingBox, ModelBase, PageGeometry


class OCRProviderError(RuntimeError):
    """Raised when an OCR provider cannot produce a page result safely."""


class OCRTextBlock(ModelBase):
    text: str = Field(min_length=1)
    bbox: BoundingBox
    confidence: float | None = None
    block_type: BlockType = BlockType.PARAGRAPH

    @model_validator(mode="after")
    def validate_text(self) -> "OCRTextBlock":
        if not self.text.strip():
            raise ValueError("OCR text block cannot be blank")
        return self


class OCRPageResult(ModelBase):
    blocks: list[OCRTextBlock] = Field(default_factory=list)


class OCRProvider(Protocol):
    provider_version: str

    def recognize(
        self,
        page_image: bytes,
        *,
        page_number: int,
        geometry: PageGeometry,
    ) -> OCRPageResult:
        """Recognize text using coordinates expressed in PDF page units."""


class StaticOCRProvider:
    """Deterministic test/replay provider; production uses an external adapter."""

    provider_version = "static-ocr-0.1.0"

    def __init__(self, pages: Mapping[int, OCRPageResult]) -> None:
        self.pages = dict(pages)

    def recognize(
        self,
        page_image: bytes,
        *,
        page_number: int,
        geometry: PageGeometry,
    ) -> OCRPageResult:
        del page_image, geometry
        return self.pages.get(page_number, OCRPageResult())
