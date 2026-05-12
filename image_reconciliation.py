"""
image_reconciliation.py — Source-aware, position-based image / alt-text reconciliation.

For each page of a PDF, reconcile every Image XObject occurrence in the content
stream with its source-PDF intent (alt text, decorative status, watermark
status) by walking the struct tree and the content stream and matching by
MCID first, then by bbox overlap, then by header/footer-band heuristic.

Public API:
    reconcile_page_images(pdf_path) -> dict[int, list[ResolvedImage]]

Replaces the index-based alt-text matching in pdf_extractor._extract_images,
which broke on horizontally adjacent images, watermarked documents, and
decoratively-marked logos.
"""
from dataclasses import dataclass, field
from typing import Optional
import logging

import pikepdf

from models import BBox

__all__ = ["ResolvedImage", "reconcile_page_images"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------

@dataclass
class ResolvedImage:
    """One Image XObject occurrence, fully classified for downstream tagging."""
    xobject_name: str
    bbox: BBox
    alt_text: str = ""
    is_decorative: bool = False
    is_watermark: bool = False


# ---------------------------------------------------------------------------
# Internal data shapes
# ---------------------------------------------------------------------------

@dataclass
class SourceStructElement:
    """One /Figure or /Artifact element from the source PDF struct tree."""
    struct_type: str                # "/Figure" or "/Artifact"
    alt_text: str                   # "" if /Alt is missing
    page_index: int
    mcids: list[int] = field(default_factory=list)  # leaf integer kids in /K subtree
    bbox: Optional[BBox] = None     # from element's /BBox if present, else None


@dataclass
class ImageOccurrence:
    """One Do operator on an Image XObject in a page's content stream."""
    xobject_name: str
    page_bbox: BBox                       # CTM applied to unit-square image space
    mcid: Optional[int] = None            # innermost active MCID at this Do
    in_watermark_ancestor: bool = False   # True if any ancestor Form is a watermark


# ---------------------------------------------------------------------------
# CTM math
# ---------------------------------------------------------------------------

def _apply_ctm(ctm: list[float], x: float, y: float) -> tuple[float, float]:
    """Apply 6-element CTM [a, b, c, d, e, f] to (x, y).

    Per PDF spec: x' = a*x + c*y + e;  y' = b*x + d*y + f.
    """
    a, b, c, d, e, f = ctm
    return (a * x + c * y + e, b * x + d * y + f)


def _bbox_from_ctm(ctm: list[float]) -> BBox:
    """Compute axis-aligned page-space bbox of the unit-square image after CTM.

    Maps all four image-space corners (0,0),(1,0),(0,1),(1,1) through the
    CTM and takes min/max — handles rotation, skew, and negative scales.
    """
    corners = [
        _apply_ctm(ctm, 0.0, 0.0),
        _apply_ctm(ctm, 1.0, 0.0),
        _apply_ctm(ctm, 0.0, 1.0),
        _apply_ctm(ctm, 1.0, 1.0),
    ]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return BBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))
