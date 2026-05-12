"""
models.py - Shared data structures for the PDF accessibility pipeline.

Defines the data classes that flow between pipeline stages.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ElementType(Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    TABLE_HEADER = "table_header"
    IMAGE = "image"
    WATERMARK = "watermark"
    HEADER_FOOTER = "header_footer"


@dataclass
class FontInfo:
    name: str
    size: float
    is_bold: bool
    is_italic: bool
    color: tuple = (0.0, 0.0, 0.0)


@dataclass
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class TextBlock:
    """A contiguous block of text with uniform formatting."""
    text: str
    bbox: BBox
    font: FontInfo
    element_type: ElementType = ElementType.PARAGRAPH
    heading_level: Optional[int] = None
    rotation_degrees: float = 0.0
    page_number: int = 0


@dataclass
class ImageBlock:
    """An extracted image with metadata."""
    image_bytes: bytes
    format: str
    bbox: BBox
    page_number: int
    alt_text: str = ""
    is_decorative: bool = False
    is_vector_figure: bool = False  # True for vector/form figures (no raster image data)


@dataclass
class TableBlock:
    """A detected table structure."""
    rows: list = field(default_factory=list)
    header_rows: int = 1
    bbox: Optional[BBox] = None
    page_number: int = 0


@dataclass
class PageContent:
    """All extracted content for a single page."""
    page_number: int
    width: float
    height: float
    text_blocks: list = field(default_factory=list)
    images: list = field(default_factory=list)
    tables: list = field(default_factory=list)


@dataclass
class DocumentContent:
    """Complete extracted content for one PDF."""
    title: str
    language: str
    pages: list = field(default_factory=list)
    source_path: str = ""
