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


# ---------------------------------------------------------------------------
# Struct tree reader
# ---------------------------------------------------------------------------

def _mcr_mcid(node) -> Optional[int]:
    """If node is an MCR dict ({/Type /MCR, /MCID int}), return its MCID; else None.

    MCR is the indirect form of an MCID leaf (PDF 1.7 §10.5.2). Plain int kids
    are the direct form. Both must be collected.
    """
    try:
        mcid_val = node.get("/MCID")
        if mcid_val is None:
            return None
        return int(mcid_val)
    except Exception:
        return None


def _collect_leaf_mcids(node, visited: set) -> list[int]:
    """Walk node's /K subtree, return all MCID leaves.

    Handles both leaf encodings: direct (plain int in /K) and indirect
    (MCR dict). Uses id(node) for cycle detection — pikepdf inline dicts
    share objgen=(0,0), so objgen-based dedup conflates unrelated nodes.
    """
    mcids: list[int] = []
    oid = id(node)
    if oid in visited:
        return mcids
    visited.add(oid)

    try:
        kids = node.get("/K")
    except Exception:
        return mcids
    if kids is None:
        return mcids

    if isinstance(kids, int):
        return [kids]
    if isinstance(kids, list) or isinstance(kids, pikepdf.Array):
        for child in kids:
            if isinstance(child, int):
                mcids.append(child)
            elif hasattr(child, "get"):
                # MCR dict leaf first; otherwise recurse into struct subtree
                mcr_id = _mcr_mcid(child)
                if mcr_id is not None:
                    mcids.append(mcr_id)
                else:
                    mcids.extend(_collect_leaf_mcids(child, visited))
        return mcids
    if hasattr(kids, "get"):
        mcr_id = _mcr_mcid(kids)
        if mcr_id is not None:
            return [mcr_id]
        return _collect_leaf_mcids(kids, visited)
    return mcids


def _bbox_from_pdf_array(arr) -> Optional[BBox]:
    """Convert a 4-element pikepdf Array [x0, y0, x1, y1] to BBox, or None."""
    try:
        if arr is None or len(arr) < 4:
            return None
        return BBox(x0=float(arr[0]), y0=float(arr[1]),
                    x1=float(arr[2]), y1=float(arr[3]))
    except Exception:
        return None


def _read_source_struct_elements(pdf) -> dict[int, list[SourceStructElement]]:
    """Walk /StructTreeRoot, return {page_index: [SourceStructElement, ...]}.

    Collects /Figure AND /Artifact nodes. Empty result when struct tree is absent.
    """
    result: dict[int, list[SourceStructElement]] = {}
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            return result

        # Build page-index lookup
        try:
            page_id_to_idx = {p.objgen: i for i, p in enumerate(pdf.pages)}
        except Exception:
            page_id_to_idx = {}
        if not page_id_to_idx:
            logger.warning(
                "Could not build page-index lookup; struct elements may default to page 0"
            )

        visited: set[int] = set()
        FIGURE = pikepdf.Name("/Figure")
        ARTIFACT = pikepdf.Name("/Artifact")

        def _walk(node, inherited_pg=None):
            # Use id() rather than objgen — inline dicts share objgen=(0,0)
            # in pikepdf, which would conflate unrelated nodes.
            oid = id(node)
            if oid in visited:
                return
            visited.add(oid)

            try:
                struct_type = node.get("/S")
            except Exception:
                struct_type = None
            pg = None
            try:
                pg = node.get("/Pg") or inherited_pg
            except Exception:
                pg = inherited_pg

            if struct_type == FIGURE or struct_type == ARTIFACT:
                # Resolve /Alt
                raw_alt = None
                try:
                    raw_alt = node.get("/Alt")
                except Exception:
                    pass
                alt_str = str(raw_alt).strip() if raw_alt else ""

                # Resolve /BBox (rare on Figures from Word; common on Acrobat-edited)
                bbox = None
                try:
                    bbox = _bbox_from_pdf_array(node.get("/BBox"))
                except Exception:
                    pass

                # MCIDs — fresh visited set so we walk every leaf even on shared dicts
                mcids = _collect_leaf_mcids(node, set())

                # Determine page
                page_idx = None
                if pg is not None:
                    try:
                        page_idx = page_id_to_idx.get(pg.objgen)
                    except Exception:
                        page_idx = None
                if page_idx is None and mcids:
                    # Fallback: use first page (rare)
                    page_idx = 0

                if page_idx is not None:
                    elem = SourceStructElement(
                        struct_type=("/Artifact" if struct_type == ARTIFACT else "/Figure"),
                        alt_text=alt_str,
                        page_index=page_idx,
                        mcids=mcids,
                        bbox=bbox,
                    )
                    result.setdefault(page_idx, []).append(elem)
                return  # Figure/Artifact subtree already harvested

            # Recurse into children
            try:
                kids = node.get("/K")
            except Exception:
                kids = None
            if kids is None:
                return
            if isinstance(kids, list) or isinstance(kids, pikepdf.Array):
                for child in kids:
                    if hasattr(child, "get"):
                        _walk(child, inherited_pg=pg)
            elif hasattr(kids, "get"):
                _walk(kids, inherited_pg=pg)

        _walk(struct_root)
    except Exception as e:
        logger.debug("Struct tree walk failed: %s", e)

    return result
