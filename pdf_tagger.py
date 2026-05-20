"""
pdf_tagger.py - Add PDF/UA structure tags directly to existing PDFs.

Instead of reconstructing PDFs from scratch (which alters layout),
this module adds accessibility structure tags directly to the original
PDF's content streams, preserving the exact visual appearance.

Hardened for diverse PDF types:
- Handles inherited Resources from page tree
- Handles inline images (BI/ID/EI)
- Supports multiple images per page
- Per-page error handling (one bad page doesn't kill the PDF)
- Safe float conversion for operands
- Adaptive position matching tolerance
- Full table structure tagging (/Table, /TR, /TH, /TD)
- Position-based image matching using CTM coordinates
"""
import logging
import re
import sys
from typing import Optional

import pikepdf

from models import DocumentContent, PageContent, TextBlock, ImageBlock, TableBlock, ElementType
from image_reconciliation import _read_source_struct_elements

logger = logging.getLogger(__name__)


def tag_pdf(input_path: str, output_path: str, doc_content: DocumentContent) -> str:
    """Add PDF/UA structure tags to the original PDF."""
    try:
        pdf = pikepdf.Pdf.open(input_path)
    except pikepdf.PasswordError:
        logger.error("PDF is encrypted/password-protected: %s", input_path)
        raise
    except Exception as e:
        logger.error("Could not open PDF for tagging: %s", e)
        raise

    # Harvest source /Figure MCID → alt-text BEFORE we tear down the source
    # struct tree.  Lets _insert_markers preserve diagrams that the source
    # wrapped as one /Figure (typical for InDesign flowcharts/maps drawn as
    # raw paths intermixed with text labels) — without this, the vector
    # detector fragments them into N pieces and drops the source alt.
    source_figure_alts = _build_source_figure_alt_map(pdf)

    _remove_existing_structure(pdf)

    all_page_elems = []

    for page_idx, page in enumerate(pdf.pages):
        page_content = (doc_content.pages[page_idx]
                        if page_idx < len(doc_content.pages) else None)
        mcid_counter = [0]  # Reset per page — ParentTree is indexed by MCID per page
        page_source_figure_alts = source_figure_alts.get(page_idx, {})
        try:
            page_elems = _tag_page(
                pdf, page, page_content, page_idx, mcid_counter,
                source_figure_alts=page_source_figure_alts,
            )
        except Exception as e:
            logger.warning("Page %d tagging failed (%s), wrapping as artifact", page_idx, e)
            mcid_counter = [0]
            page_elems = _tag_page_fallback(pdf, page, page_idx, mcid_counter)
        all_page_elems.append((page_idx, page_elems))

    _build_structure_tree(pdf, all_page_elems, doc_content)

    pdf.save(output_path)
    pdf.close()
    return output_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val, default=0.0) -> float:
    """Convert a pikepdf operand to float safely."""
    try:
        return float(val)
    except (ValueError, TypeError, OverflowError):
        return default


def _resolve_resources(page) -> Optional[pikepdf.Dictionary]:
    """Get Resources for a page, checking inheritance from the page tree."""
    res = page.get("/Resources")
    if res:
        return res
    # Walk up the page tree with circular reference protection
    parent = page.get("/Parent")
    seen = set()
    while parent:
        try:
            obj_id = parent.objgen
            if obj_id in seen:
                break
            seen.add(obj_id)
            res = parent.get("/Resources")
            if res:
                return res
            parent = parent.get("/Parent")
        except Exception:
            break
    return None


def _get_xobjects(page) -> Optional[pikepdf.Dictionary]:
    """Get XObject dictionary, handling inherited Resources."""
    res = _resolve_resources(page)
    if not res:
        return None
    return res.get("/XObject")


# ---------------------------------------------------------------------------
# Source struct-tree harvesting
# ---------------------------------------------------------------------------

def _build_source_figure_alt_map(pdf: pikepdf.Pdf) -> dict:
    """Build {page_idx: {source_mcid: (figure_id, alt_text)}} from the source struct tree.

    The ``figure_id`` is a stable per-source-/Figure identifier — every MCID
    that belongs to the same source ``/Figure`` StructElem shares one id.
    ``_insert_markers`` uses it to group multiple BDC ranges of a single
    source diagram into ONE output ``/Figure`` StructElem (instead of
    fragmenting them into one StructElem per BDC range, which produces the
    "overlapping empty figures, repeated alt text" pattern that screen
    readers announce N times).

    Figures with no /Alt are still tracked — grouping is needed regardless,
    and the alt-resolution fallback in ``_insert_markers`` fills in a value.
    """
    out: dict = {}
    try:
        elements_by_page = _read_source_struct_elements(pdf)
    except Exception:
        return out
    fig_id_counter = [0]
    for page_idx, elems in elements_by_page.items():
        page_map: dict = {}
        for elem in elems:
            if elem.struct_type != "/Figure":
                continue
            fid = fig_id_counter[0]
            fig_id_counter[0] += 1
            for mcid in elem.mcids:
                page_map.setdefault(mcid, (fid, elem.alt_text or ""))
        if page_map:
            out[page_idx] = page_map
    return out


# ---------------------------------------------------------------------------
# Structure tree removal
# ---------------------------------------------------------------------------

def _remove_existing_structure(pdf: pikepdf.Pdf):
    """Remove existing structure tree and related entries."""
    if "/StructTreeRoot" in pdf.Root:
        del pdf.Root[pikepdf.Name.StructTreeRoot]
    for page in pdf.pages:
        if "/StructParents" in page:
            del page[pikepdf.Name.StructParents]


# ---------------------------------------------------------------------------
# Page-level tagging
# ---------------------------------------------------------------------------

def _tag_page(pdf, page, page_content: Optional[PageContent],
              page_idx: int, mcid_counter: list,
              source_figure_alts: Optional[dict] = None) -> list:
    """Tag a single page's content stream with structure markers."""
    blocks = _build_block_index(page_content)

    try:
        raw_ops = list(pikepdf.parse_content_stream(page))
    except Exception:
        # Can't parse → wrap entire page as artifact
        return _tag_page_fallback(pdf, page, page_idx, mcid_counter)

    ops, source_artifact_mask, source_figure_regions = (
        _strip_markers_with_source_intent(raw_ops, source_figure_alts or {})
    )

    watermark_forms = _detect_watermark_forms(page)
    link_annots = _collect_link_annots(page)

    new_ops, struct_elems = _insert_markers(
        ops, blocks, page, watermark_forms, mcid_counter, link_annots, pdf=pdf,
        source_artifact_mask=source_artifact_mask,
        source_figure_regions=source_figure_regions,
    )

    new_stream_data = pikepdf.unparse_content_stream(new_ops)
    page.Contents = pikepdf.Stream(pdf, new_stream_data)
    page[pikepdf.Name.StructParents] = page_idx

    return struct_elems


def _tag_page_fallback(pdf, page, page_idx: int, mcid_counter: list) -> list:
    """Fallback: wrap entire page content in a single /P tag.

    Used when content stream parsing fails.
    """
    mcid = mcid_counter[0]
    mcid_counter[0] += 1

    try:
        ops = list(pikepdf.parse_content_stream(page))
    except Exception:
        page[pikepdf.Name.StructParents] = page_idx
        return [(mcid, "/P", "")]

    new_ops = [
        ([pikepdf.Name("/P"), pikepdf.Dictionary({"/MCID": mcid})],
         pikepdf.Operator("BDC")),
    ]
    new_ops.extend(ops)
    new_ops.append(([], pikepdf.Operator("EMC")))

    new_stream_data = pikepdf.unparse_content_stream(new_ops)
    page.Contents = pikepdf.Stream(pdf, new_stream_data)
    page[pikepdf.Name.StructParents] = page_idx

    return [(mcid, "/P", "")]


def _build_block_index(page_content: Optional[PageContent]) -> list:
    """Build a list of classified blocks with bboxes for position matching."""
    blocks = []
    if not page_content:
        return blocks

    for tb in page_content.text_blocks:
        struct_type = _element_to_struct_type(tb)
        blocks.append({
            "bbox": tb.bbox,
            "struct_type": struct_type,
            "text": tb.text,
            "is_artifact": tb.element_type in (
                ElementType.WATERMARK, ElementType.HEADER_FOOTER
            ),
        })

    for img in page_content.images:
        blocks.append({
            "bbox": img.bbox,
            "struct_type": "/Figure",
            "alt_text": img.alt_text or "Figure",
            "is_artifact": img.is_decorative,
            "is_vector": getattr(img, "is_vector_figure", False),
            "used": False,
        })

    for table in page_content.tables:
        table_id = id(table)  # globally unique across all pages
        for row_idx, row in enumerate(table.rows):
            is_header = row_idx < table.header_rows
            for cell in row:
                blocks.append({
                    "bbox": cell.bbox,
                    "struct_type": "/TH" if is_header else "/TD",
                    "text": cell.text,
                    "is_artifact": False,
                    "row_idx": row_idx,
                    "table_idx": table_id,
                })

    return blocks


# ---------------------------------------------------------------------------
# Content stream manipulation
# ---------------------------------------------------------------------------

def _strip_markers(ops: list) -> list:
    """Remove all existing BDC/BMC/EMC markers from the content stream."""
    return [
        (operands, op)
        for operands, op in ops
        if str(op) not in ("BDC", "BMC", "EMC")
    ]


def _strip_markers_with_source_intent(raw_ops: list,
                                       source_figure_alts: dict) -> tuple:
    """Strip BDC/BMC/EMC and report the source's structural intent.

    Returns:
        (out_ops, artifact_mask, source_figure_regions)

    - ``out_ops`` is the op list with all marked-content operators removed.
    - ``artifact_mask[i]`` is True when ``out_ops[i]`` was originally nested
      inside a ``/Artifact`` BDC/BMC ... EMC range — used by the vector
      detector to skip page-template chrome the author already marked
      decorative.
    - ``source_figure_regions`` is a list of ``(start_idx, end_idx, alt_text)``
      tuples giving the post-strip op-index range of every ``/Figure`` BDC
      whose MCID is present in ``source_figure_alts`` (keyed by source MCID).
      The caller wraps each range as a single ``/Figure`` to keep diagrams
      that mix paths and text labels (flowcharts, maps) intact instead of
      letting the path-region detector fragment them.
    """
    out_ops: list = []
    mask: list = []
    artifact_depth = 0
    # bdc_stack frames: ('artifact', None) | ('figure', mcid_or_None) | ('other', None)
    bdc_stack: list = []
    # Track the deepest source /Figure we're inside: (out_start_idx, alt, figure_id).
    figure_open_stack: list = []
    source_figure_regions: list = []  # (start, end, alt, figure_id)
    for operands, op in raw_ops:
        s = str(op)
        if s in ("BDC", "BMC"):
            tag = str(operands[0]) if operands else ""
            if tag == "/Artifact":
                bdc_stack.append(("artifact", None))
                artifact_depth += 1
            elif tag == "/Figure":
                # Pull the MCID out of the BDC properties dict so we can
                # cross-reference against source_figure_alts.  BMC has no
                # properties dict; treat as untracked.
                mcid = None
                if s == "BDC" and len(operands) >= 2:
                    try:
                        props = operands[1]
                        if hasattr(props, "get"):
                            raw_mcid = props.get("/MCID")
                            if raw_mcid is not None:
                                mcid = int(raw_mcid)
                    except Exception:
                        mcid = None
                entry = (source_figure_alts.get(mcid)
                         if mcid is not None else None)
                if entry is not None:
                    fid, alt = entry
                    figure_open_stack.append((len(out_ops), alt, fid))
                    bdc_stack.append(("figure", mcid))
                else:
                    bdc_stack.append(("other", None))
            else:
                bdc_stack.append(("other", None))
            continue
        if s == "EMC":
            if bdc_stack:
                kind, _ = bdc_stack.pop()
                if kind == "artifact" and artifact_depth > 0:
                    artifact_depth -= 1
                elif kind == "figure" and figure_open_stack:
                    start_idx, alt, fid = figure_open_stack.pop()
                    end_idx = len(out_ops) - 1
                    if end_idx >= start_idx:
                        source_figure_regions.append(
                            (start_idx, end_idx, alt, fid)
                        )
            continue
        out_ops.append((operands, op))
        mask.append(artifact_depth > 0)
    return out_ops, mask, source_figure_regions


def _strip_markers_with_artifact_mask(raw_ops: list) -> tuple:
    """Back-compat shim — returns just (ops, artifact_mask). New callers
    should use _strip_markers_with_source_intent directly.
    """
    ops, mask, _ = _strip_markers_with_source_intent(raw_ops, {})
    return ops, mask


# Operator categories used by the vector-figure detector below.
_PATH_OPS = frozenset({
    "m", "l", "c", "v", "y", "re", "f", "F", "f*",
    "B", "b", "S", "s", "B*", "b*", "n", "h", "W", "W*",
})
_TEXT_OPS = frozenset({"Tj", "TJ", "'", '"', "BT", "ET"})
_XOBJECT_OPS = frozenset({"Do", "BI", "ID", "EI"})


def _detect_vector_figure_regions(
    ops: list,
    min_path_ops: int = 6,
    max_text_in_region: int = 3,
    merge_gap: int = 80,
) -> list:
    """Find op-index ranges to wrap as a single /Figure structure element.

    Many InDesign / Word PDFs draw charts (line, bar, scatter, pie) as
    raw path operators directly in the page content stream — not as
    Image XObjects, not as Form XObjects.  Without explicit handling
    every path defaults to /Artifact (decorative) and the graph becomes
    invisible to screen readers.

    Strategy:
      1. Identify top-level `q ... Q` graphics-state blocks dominated
         by path operators (>= `min_path_ops`) with little or no text
         and no XObject `Do` references.  These are typically a single
         chart primitive emitted by InDesign.
      2. Merge spatially adjacent blocks (separated by <= `merge_gap`
         ops with no Tj/TJ/Do between them) so a chart composed of
         hundreds of small q/Q primitives ends up as ONE Figure rather
         than hundreds of fragmented Figures.

    Returns a list of (start_op_idx, end_op_idx) tuples whose op
    positions refer to the input `ops` list (post-strip).  The caller
    wraps each range as `/Figure ... EMC` with an MCID.

    Implementation note: single pass, merging inline.  We track the
    index of the most recent text/XObject op (`last_significant_idx`)
    so merges are decided with an O(1) comparison instead of rescanning
    the gap — keeps memory flat on pages with thousands of path ops.
    """
    merged: list = []
    cur_start = cur_end = -1  # currently-extending region; -1 means none
    last_significant_idx = -1  # max idx of any text or XObject operator

    depth = 0
    q_idx = -1
    path_count = text_count = do_count = 0

    for i, (_, operator) in enumerate(ops):
        s = str(operator)
        if s == "q":
            if depth == 0:
                q_idx = i
                path_count = text_count = do_count = 0
            depth += 1
            continue
        if s == "Q":
            if depth > 0:
                depth -= 1
                if depth == 0 and q_idx >= 0:
                    qualifies = (
                        path_count >= min_path_ops
                        and text_count <= max_text_in_region
                        and do_count == 0
                    )
                    if qualifies:
                        if cur_end >= 0 and (q_idx - cur_end - 1) <= merge_gap \
                                and last_significant_idx <= cur_end:
                            cur_end = i
                        else:
                            if cur_end >= 0:
                                merged.append((cur_start, cur_end))
                            cur_start, cur_end = q_idx, i
                    q_idx = -1
            continue
        if s in _TEXT_OPS:
            last_significant_idx = i
            if depth > 0:
                text_count += 1
        elif s in _XOBJECT_OPS:
            last_significant_idx = i
            if depth > 0:
                do_count += 1
        elif depth > 0 and s in _PATH_OPS:
            path_count += 1

    if cur_end >= 0:
        merged.append((cur_start, cur_end))

    return merged


def _merge_figure_regions(source_regions: list, auto_regions: list) -> list:
    """Combine source ``/Figure`` BDC ranges with auto-detected vector
    regions, returning a sorted, non-overlapping list of
    ``(start, end, alt_or_None, figure_id_or_None)`` tuples.

    Source ranges are authoritative: any auto-detected region that
    overlaps a source range is dropped (the source range already covers
    the diagram and carries the correct alt).  ``figure_id`` is the
    source-/Figure StructElem identity for source regions and ``None``
    for auto-detected regions.
    """
    merged: list = []
    for s, e, alt, fid in source_regions:
        if e < s:
            continue
        merged.append((s, e, alt, fid))
    for s, e in auto_regions:
        overlaps = any(
            not (e < sr_s or s > sr_e) for sr_s, sr_e, _, _ in merged
        )
        if overlaps:
            continue
        merged.append((s, e, None, None))
    merged.sort(key=lambda t: t[0])
    return merged


def _region_is_source_artifact(start: int, end: int, mask: list) -> bool:
    """Return True if the op-range [start..end] was predominantly inside a
    /Artifact marker in the source content stream.

    Used to suppress vector-figure detection on regions the author had
    already marked decorative — page-template chrome, vector watermarks,
    etc. — so we don't promote them to /Figure.
    """
    if not mask or start >= len(mask):
        return False
    end = min(end, len(mask) - 1)
    span = end - start + 1
    if span <= 0:
        return False
    hits = sum(1 for i in range(start, end + 1) if mask[i])
    return hits * 2 >= span  # >= 50% of the region inside a source /Artifact


def _collect_link_annots(page) -> list:
    """Collect link annotations with their rects for position matching during tagging.

    Skips annotations with abnormally large rects (> 300pt wide) to avoid
    greedily capturing unrelated text. Such annotations typically cover
    entire paragraphs and are better handled by the postprocessor.
    """
    annots = page.get("/Annots")
    if not annots:
        return []

    # Get page width for size guard
    try:
        mediabox = page.get("/MediaBox")
        page_width = float(mediabox[2]) - float(mediabox[0]) if mediabox else 612
    except Exception:
        page_width = 612

    max_annot_width = min(500.0, page_width * 0.8)

    result = []
    for annot in annots:
        try:
            if str(annot.get("/Subtype", "")) != "/Link":
                continue
            rect = annot.get("/Rect")
            if not rect or len(rect) < 4:
                continue
            r = [float(rect[i]) for i in range(4)]
            x0, y0 = min(r[0], r[2]), min(r[1], r[3])
            x1, y1 = max(r[0], r[2]), max(r[1], r[3])
            width = x1 - x0
            height = y1 - y0
            # Skip degenerate or abnormally large rects
            if width <= 0 or height <= 0:
                continue
            if width > max_annot_width:
                logger.debug("Skipping large link annotation (%.0fx%.0f)", width, height)
                continue
            result.append({
                "rect": (x0, y0, x1, y1),
                "annot": annot,
            })
        except Exception:
            continue
    return result


def _find_link_annot(x: float, y: float, link_annots: list) -> Optional[int]:
    """Find the link annotation whose rect contains position (x, y).

    Uses asymmetric tolerances:
    - Left/bottom: generous (text may start slightly before rect due to rounding)
    - Right: tight (text starting past the rect end is not part of the link;
      the text position is the START of the glyph, so text past x1 belongs
      to the next content, not the link)
    - Vertical: generous (text baseline varies relative to annotation rect)

    Does NOT skip already-matched annotations — consecutive text operators
    within the same annotation rect must all be tagged as /Link.
    """
    best_idx = None
    best_dist = float("inf")

    for i, la in enumerate(link_annots):
        x0, y0, x1, y1 = la["rect"]
        height = y1 - y0
        width = x1 - x0
        tol_y = max(3.0, height * 0.5)
        tol_x_left = max(2.0, width * 0.1)
        # Tight right tolerance: only for floating-point rounding (< 0.5pt)
        tol_x_right = 0.5
        if (x0 - tol_x_left <= x <= x1 + tol_x_right and
                y0 - tol_y <= y <= y1 + tol_y):
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            dist = abs(x - cx) + abs(y - cy)
            if dist < best_dist:
                best_dist = dist
                best_idx = i

    return best_idx


def _clean_form_xobject_markers(pdf: pikepdf.Pdf, page, xobj_name: str):
    """Strip all BDC/BMC/EMC markers from a Form XObject's content stream.

    When a Form XObject is wrapped as a <Figure> in the page stream, any
    Artifact or MCID markers inside the Form create PDF/UA Clause 7.1
    violations (Artifact inside tagged content, or tagged content inside
    Artifact).  Stripping them makes the Form's content "clean" so the
    outer Figure tag is the sole structure context.
    """
    try:
        xobjects = _get_xobjects(page)
        if xobjects is None:
            return
        obj = xobjects.get(xobj_name) or xobjects.get(xobj_name.lstrip("/"))
        if obj is None or not hasattr(obj, "get"):
            return
        if obj.get("/Subtype") != pikepdf.Name.Form:
            return
        try:
            ops = list(pikepdf.parse_content_stream(obj))
        except Exception:
            return
        stripped = _strip_markers(ops)
        if len(stripped) == len(ops):
            return  # nothing changed — avoid unnecessary stream rewrite
        new_data = pikepdf.unparse_content_stream(stripped)
        obj.write(new_data)
    except Exception as e:
        logger.debug("Could not clean Form XObject '%s' markers: %s", xobj_name, e)


def _insert_markers(ops, blocks, page, watermark_forms, mcid_counter,
                    link_annots=None, pdf=None, source_artifact_mask=None,
                    source_figure_regions=None):
    """Walk through content stream ops, inserting BDC/EMC structure markers.

    Uses an "artifact-as-default" strategy so nothing is ever untagged.
    When link_annots is provided, text falling within a link annotation's
    rect is tagged as /Link with both MCR and annotation reference.
    pdf is required for cleaning Form XObject content streams.

    ``source_figure_regions`` is an optional list of
    ``(start_idx, end_idx, alt_text)`` ranges harvested from the source
    PDF's ``/Figure`` BDC markers.  Each range is wrapped as a single
    ``/Figure`` so diagrams the source author had already grouped (e.g.
    InDesign flowcharts mixing paths and text labels) stay intact instead
    of being fragmented by the vector-region detector.
    """
    if link_annots is None:
        link_annots = []
    new_ops = []
    struct_elems = []

    # -- state tracking --
    # Account for page /Rotate: adjust initial CTM so position calculations
    # remain correct for rotated pages
    ctm = [1, 0, 0, 1, 0, 0]
    try:
        rotate = int(page.get("/Rotate", 0)) % 360
    except (ValueError, TypeError):
        rotate = 0
    if rotate == 90:
        ctm = [0, 1, -1, 0, 0, 0]
    elif rotate == 180:
        ctm = [-1, 0, 0, -1, 0, 0]
    elif rotate == 270:
        ctm = [0, -1, 1, 0, 0, 0]
    ctm_stack = []
    in_text = False
    tm = [1, 0, 0, 1, 0, 0]
    tlm = [1, 0, 0, 1, 0, 0]
    leading = 0.0

    artifact_open = False
    struct_open = False
    current_block_idx = -1

    # Vector-figure cluster detection: pre-scan ops for top-level q/Q
    # blocks dominated by path operators and merge adjacent clusters.
    # When the main loop reaches a region start, it emits a /Figure BDC
    # before the `q`, suppresses normal artifact/struct handling until
    # the matching `Q`, then emits EMC.  This is what tags raw vector
    # graph content (line graphs, bar charts, pie charts) as figures
    # instead of decorative artifacts.
    auto_regions = _detect_vector_figure_regions(ops)
    # Filter out regions whose content was tagged /Artifact in the source —
    # respects authorial intent for decorative page-template chrome that
    # happens to be drawn with many path operators (InDesign cover flourish,
    # watermarks, etc.).  Only applies when we received a source mask.
    if source_artifact_mask is not None and auto_regions:
        auto_regions = [
            (s, e) for (s, e) in auto_regions
            if not _region_is_source_artifact(s, e, source_artifact_mask)
        ]
    # Source /Figure BDC ranges are authoritative — they preserve the
    # author's intended grouping (one diagram = one Figure with its alt).
    # Drop any auto-detected region that overlaps a source range so the
    # diagram isn't double-wrapped or fragmented.
    src_ranges = list(source_figure_regions or [])
    figure_regions = _merge_figure_regions(src_ranges, auto_regions)
    next_figure_idx = 0  # pointer into figure_regions; avoids a start→end dict
    in_figure_region = False
    figure_region_end_idx = -1
    figure_region_mcid = -1
    figure_region_bbox = None  # accumulated min/max in page coords
    figure_region_source_alt = None  # alt from source /Figure, if any
    figure_region_source_id = None   # source-/Figure id for grouping, or None

    def _open_artifact():
        nonlocal artifact_open
        if not artifact_open:
            new_ops.append((
                [pikepdf.Name("/Artifact")],
                pikepdf.Operator("BMC"),
            ))
            artifact_open = True

    def _close_artifact():
        nonlocal artifact_open
        if artifact_open:
            new_ops.append(([], pikepdf.Operator("EMC")))
            artifact_open = False

    current_link_idx = -1
    # Track which annotation indices already have a struct_elem with OBJR,
    # so q/Q splits don't create duplicate OBJR entries for the same annotation.
    linked_annot_indices = set()

    def _close_struct():
        nonlocal struct_open, current_block_idx, current_link_idx
        if struct_open:
            new_ops.append(([], pikepdf.Operator("EMC")))
            struct_open = False
            # Mark table cell blocks as used when closed so duplicate TD/TH
            # elements are not created for multi-line cell content.
            if (current_block_idx >= 0
                    and blocks[current_block_idx]["struct_type"] in ("/TD", "/TH")):
                blocks[current_block_idx]["used"] = True
            current_block_idx = -1
            current_link_idx = -1

    def _open_struct_for_block(bidx):
        nonlocal struct_open, current_block_idx

        if struct_open and current_block_idx == bidx:
            return

        _close_struct()
        _close_artifact()

        block = blocks[bidx]
        if block.get("is_artifact"):
            new_ops.append((
                [pikepdf.Name("/Artifact"),
                 pikepdf.Dictionary({"/Type": pikepdf.Name("/Pagination")})],
                pikepdf.Operator("BDC"),
            ))
            struct_open = True
            current_block_idx = bidx
        else:
            mcid = mcid_counter[0]
            mcid_counter[0] += 1
            new_ops.append((
                [pikepdf.Name(block["struct_type"]),
                 pikepdf.Dictionary({"/MCID": mcid})],
                pikepdf.Operator("BDC"),
            ))
            struct_open = True
            current_block_idx = bidx
            row_idx = block.get("row_idx")
            if row_idx is not None:
                # Record the first MCID so continuations can reference it
                if "first_mcid" not in block:
                    block["first_mcid"] = mcid
                struct_elems.append((
                    mcid, block["struct_type"], "", None, None,
                    row_idx, block.get("table_idx", 0),
                ))
            else:
                struct_elems.append((
                    mcid, block["struct_type"], block.get("alt_text", ""),
                ))

    def _open_struct_for_cell_cont(cidx):
        """Open a continuation BDC for a cell that was already emitted once.

        Creates a new MCID tagged with an 8-tuple so _build_structure_tree
        can attach the MCR to the *existing* TD/TH struct element instead of
        creating a duplicate, keeping cell content structurally unified.
        """
        nonlocal struct_open, current_block_idx

        if struct_open and current_block_idx == cidx:
            return  # same cell already open in this BT block

        _close_struct()
        _close_artifact()

        block = blocks[cidx]
        mcid = mcid_counter[0]
        mcid_counter[0] += 1
        new_ops.append((
            [pikepdf.Name(block["struct_type"]),
             pikepdf.Dictionary({"/MCID": mcid})],
            pikepdf.Operator("BDC"),
        ))
        struct_open = True
        current_block_idx = cidx
        # 8-tuple: index 7 holds the original MCID to merge this MCR into
        struct_elems.append((
            mcid, "/TD_CONT", "", None, None,
            block.get("row_idx"), block.get("table_idx", 0),
            block.get("first_mcid"),
        ))

    def _open_struct_for_link(lidx):
        nonlocal struct_open, current_block_idx, current_link_idx

        # Same link already open — keep it (handles consecutive text ops)
        if struct_open and current_link_idx == lidx:
            return

        _close_struct()
        _close_artifact()

        mcid = mcid_counter[0]
        mcid_counter[0] += 1
        new_ops.append((
            [pikepdf.Name("/Link"),
             pikepdf.Dictionary({"/MCID": mcid})],
            pikepdf.Operator("BDC"),
        ))
        struct_open = True
        current_block_idx = -3
        current_link_idx = lidx

        if lidx not in linked_annot_indices:
            # First time seeing this annotation — include OBJR reference
            linked_annot_indices.add(lidx)
            struct_elems.append((
                mcid, "/Link", "", None, link_annots[lidx]["annot"],
            ))
        else:
            # Re-opening after q/Q split — MCR-only /Span (no duplicate OBJR).
            # The primary /Link element already has the OBJR; this just
            # ensures the continuation text is still properly tagged.
            struct_elems.append((mcid, "/Span", ""))
            logger.debug("Link annot %d re-tagged as /Span (q/Q split)", lidx)

    def _open_struct_unmatched():
        nonlocal struct_open, current_block_idx

        if struct_open and current_block_idx == -2:
            return

        _close_struct()
        _close_artifact()

        mcid = mcid_counter[0]
        mcid_counter[0] += 1
        new_ops.append((
            [pikepdf.Name("/P"),
             pikepdf.Dictionary({"/MCID": mcid})],
            pikepdf.Operator("BDC"),
        ))
        struct_open = True
        current_block_idx = -2
        struct_elems.append((mcid, "/P", ""))

    # -- start with artifact wrapper --
    _open_artifact()

    for idx, (operands, operator) in enumerate(ops):
        op = str(operator)

        # ---- Vector-figure region: enter on q ----
        # If this op starts a detected figure region, emit /Figure BDC
        # before the q and suppress normal artifact/struct handling
        # until the matching Q.
        if (not in_figure_region
                and next_figure_idx < len(figure_regions)
                and idx == figure_regions[next_figure_idx][0]):
            _close_struct()
            _close_artifact()
            figure_mcid = mcid_counter[0]
            mcid_counter[0] += 1
            # Alt-text resolution priority:
            #   1. Source /Figure BDC range carries the author's own alt — use it.
            #   2. Otherwise, consume an unused vector-figure block carrying an
            #      alt forwarded by image_reconciliation (source /Figure with
            #      /Alt that has no matching Image Do).
            #   3. Otherwise fall back to the generic "Figure" placeholder.
            figure_region_source_alt = figure_regions[next_figure_idx][2]
            figure_region_source_id = figure_regions[next_figure_idx][3]
            if figure_region_source_alt:
                figure_region_alt = figure_region_source_alt
            else:
                vec_idx = _find_vector_figure_block(blocks)
                if vec_idx is not None and blocks[vec_idx].get("alt_text"):
                    blocks[vec_idx]["used"] = True
                    figure_region_alt = blocks[vec_idx]["alt_text"]
                else:
                    figure_region_alt = "Figure"
            new_ops.append((
                [pikepdf.Name("/Figure"),
                 pikepdf.Dictionary({"/MCID": figure_mcid})],
                pikepdf.Operator("BDC"),
            ))
            in_figure_region = True
            figure_region_end_idx = figure_regions[next_figure_idx][1]
            next_figure_idx += 1
            figure_region_mcid = figure_mcid
            figure_region_bbox = None
            # Fall through to emit the q (handled below in the figure branch)

        if in_figure_region:
            # Maintain ctm tracking even inside the figure so post-figure
            # ops have correct coordinates.  Also accumulate a bbox from
            # path-op coordinates transformed through the current CTM so
            # the resulting /Figure carries a usable /BBox.
            if op == "q":
                ctm_stack.append(ctm[:])
            elif op == "Q":
                if ctm_stack:
                    ctm = ctm_stack.pop()
            elif op == "cm" and len(operands) >= 6:
                m = [_safe_float(operands[j]) for j in range(6)]
                ctm = _mat_mul(m, ctm)
            elif op in ("m", "l") and len(operands) >= 2:
                try:
                    x = _safe_float(operands[0])
                    y = _safe_float(operands[1])
                    ux = ctm[0] * x + ctm[2] * y + ctm[4]
                    uy = ctm[1] * x + ctm[3] * y + ctm[5]
                    if figure_region_bbox is None:
                        figure_region_bbox = [ux, uy, ux, uy]
                    else:
                        figure_region_bbox[0] = min(figure_region_bbox[0], ux)
                        figure_region_bbox[1] = min(figure_region_bbox[1], uy)
                        figure_region_bbox[2] = max(figure_region_bbox[2], ux)
                        figure_region_bbox[3] = max(figure_region_bbox[3], uy)
                except Exception:
                    pass
            elif op == "re" and len(operands) >= 4:
                try:
                    x = _safe_float(operands[0])
                    y = _safe_float(operands[1])
                    w = _safe_float(operands[2])
                    h = _safe_float(operands[3])
                    for cx, cy in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                        ux = ctm[0] * cx + ctm[2] * cy + ctm[4]
                        uy = ctm[1] * cx + ctm[3] * cy + ctm[5]
                        if figure_region_bbox is None:
                            figure_region_bbox = [ux, uy, ux, uy]
                        else:
                            figure_region_bbox[0] = min(figure_region_bbox[0], ux)
                            figure_region_bbox[1] = min(figure_region_bbox[1], uy)
                            figure_region_bbox[2] = max(figure_region_bbox[2], ux)
                            figure_region_bbox[3] = max(figure_region_bbox[3], uy)
                except Exception:
                    pass

            new_ops.append((operands, operator))

            if idx == figure_region_end_idx:
                # Close the /Figure region.  The 5th element is the source-
                # /Figure id (None for auto-detected regions); when set, the
                # struct-tree builder groups every BDC range sharing this id
                # under ONE output /Figure StructElem with multiple /MCRs.
                new_ops.append(([], pikepdf.Operator("EMC")))
                struct_elems.append((
                    figure_region_mcid,
                    "/Figure",
                    figure_region_alt,
                    figure_region_bbox,
                    figure_region_source_id,
                ))
                in_figure_region = False
                figure_region_end_idx = -1
                figure_region_mcid = -1
                figure_region_bbox = None
                figure_region_source_id = None
                _open_artifact()
            continue

        # ---- Inline image (BI/ID/EI) → stays in artifact ----
        if op in ("BI", "ID", "EI"):
            if not artifact_open and not struct_open:
                _open_artifact()
            new_ops.append((operands, operator))
            continue

        # ---- Graphics state save/restore ----
        # CRITICAL: close markers before q/Q to prevent crossing boundaries.
        # PDF/UA requires BDC/EMC and q/Q to be properly nested (no crossing).
        if op == "q":
            _close_struct()
            _close_artifact()
            ctm_stack.append(ctm[:])
            new_ops.append((operands, operator))
            _open_artifact()
            continue
        if op == "Q":
            _close_struct()
            _close_artifact()
            if ctm_stack:
                ctm = ctm_stack.pop()
            new_ops.append((operands, operator))
            _open_artifact()
            continue
        if op == "cm" and len(operands) >= 6:
            m = [_safe_float(operands[j]) for j in range(6)]
            ctm = _mat_mul(m, ctm)
            new_ops.append((operands, operator))
            continue

        # ---- Text object begin/end ----
        if op == "BT":
            in_text = True
            tm = [1, 0, 0, 1, 0, 0]
            tlm = [1, 0, 0, 1, 0, 0]
            new_ops.append((operands, operator))
            continue

        if op == "ET":
            _close_struct()
            if not artifact_open:
                _open_artifact()
            in_text = False
            new_ops.append((operands, operator))
            continue

        # ---- Inside text object ----
        if in_text:
            if op == "Tm" and len(operands) >= 6:
                tm = [_safe_float(operands[j]) for j in range(6)]
                tlm = tm[:]
            elif op in ("Td", "TD") and len(operands) >= 2:
                tx = _safe_float(operands[0])
                ty = _safe_float(operands[1])
                tlm = _mat_mul([1, 0, 0, 1, tx, ty], tlm)
                tm = tlm[:]
                if op == "TD":
                    leading = -ty
            elif op == "T*":
                tlm = _mat_mul([1, 0, 0, 1, 0, -leading], tlm)
                tm = tlm[:]
            elif op == "TL" and operands:
                leading = _safe_float(operands[0])

            if op in ("Tj", "TJ", "'", '"'):
                if op == "'":
                    tlm = _mat_mul([1, 0, 0, 1, 0, -leading], tlm)
                    tm = tlm[:]
                elif op == '"':
                    tlm = _mat_mul([1, 0, 0, 1, 0, -leading], tlm)
                    tm = tlm[:]

                ux = ctm[0] * tm[4] + ctm[2] * tm[5] + ctm[4]
                uy = ctm[1] * tm[4] + ctm[3] * tm[5] + ctm[5]

                # Check link annotations first (higher priority)
                lidx = _find_link_annot(ux, uy, link_annots)
                if lidx is not None:
                    _open_struct_for_link(lidx)
                else:
                    # Try table cell match (tight tolerance) before general block match
                    cidx = _find_cell_block(ux, uy, blocks)
                    if cidx >= 0:
                        _open_struct_for_block(cidx)
                    else:
                        # Check if this is a continuation of an already-emitted cell
                        # (e.g. second BT block in a multi-format cell)
                        cont_idx = _find_cell_continuation(ux, uy, blocks)
                        if cont_idx >= 0:
                            _open_struct_for_cell_cont(cont_idx)
                        else:
                            bidx = _find_block(ux, uy, blocks)
                            if bidx >= 0:
                                _open_struct_for_block(bidx)
                            else:
                                _open_struct_unmatched()

            new_ops.append((operands, operator))
            continue

        # ---- XObject Do (outside text) ----
        if op == "Do" and operands:
            xobj_name = str(operands[0])

            if xobj_name in watermark_forms:
                _close_struct()
                _close_artifact()
                new_ops.append((
                    [pikepdf.Name("/Artifact"),
                     pikepdf.Dictionary({
                         "/Subtype": pikepdf.Name("/Watermark"),
                         "/Type": pikepdf.Name("/Pagination"),
                     })],
                    pikepdf.Operator("BDC"),
                ))
                new_ops.append((operands, operator))
                new_ops.append(([], pikepdf.Operator("EMC")))
                _open_artifact()
                continue

            xobj_type = _get_xobject_subtype(page, xobj_name)

            if xobj_type == "Image":
                # Position-based image matching using full CTM transform
                # The image is placed at (0,0)-(1,1) in image space; CTM maps
                # this to page space. Use CTM translation + half the scale
                # as the center position for matching.
                img_x = ctm[4] + ctm[0] * 0.5 + ctm[2] * 0.5
                img_y = ctm[5] + ctm[1] * 0.5 + ctm[3] * 0.5
                img_idx = _find_image_block_by_position(blocks, img_x, img_y)

                if img_idx is not None and not blocks[img_idx].get("is_artifact"):
                    _close_struct()
                    _close_artifact()
                    mcid = mcid_counter[0]
                    mcid_counter[0] += 1
                    blocks[img_idx]["used"] = True
                    new_ops.append((
                        [pikepdf.Name("/Figure"),
                         pikepdf.Dictionary({"/MCID": mcid})],
                        pikepdf.Operator("BDC"),
                    ))
                    new_ops.append((operands, operator))
                    new_ops.append(([], pikepdf.Operator("EMC")))
                    alt = blocks[img_idx].get("alt_text", "Image")
                    # Compute bbox from CTM (image space is unit square)
                    x0 = ctm[4]
                    y0 = ctm[5]
                    x1 = ctm[4] + ctm[0]
                    y1 = ctm[5] + ctm[3]
                    fig_bbox = [min(x0, x1), min(y0, y1),
                                max(x0, x1), max(y0, y1)]
                    struct_elems.append((mcid, "/Figure", alt, fig_bbox))
                    _open_artifact()
                    continue

            if xobj_type == "Form":
                # If a vector figure placeholder exists for this page, tag this
                # Form XObject as a Figure (vector chart/diagram from InDesign).
                vec_idx = _find_vector_figure_block(blocks)
                # Strip any Artifact/BDC/EMC markers from the Form's own content
                # stream before wrapping it as Figure.  Internal markers inside a
                # Figure-wrapped Form violate PDF/UA Clause 7.1 (Artifact inside
                # tagged content, and vice-versa).
                if pdf is not None:
                    _clean_form_xobject_markers(pdf, page, xobj_name)
                _close_struct()
                _close_artifact()
                mcid = mcid_counter[0]
                mcid_counter[0] += 1
                new_ops.append((
                    [pikepdf.Name("/Figure"),
                     pikepdf.Dictionary({"/MCID": mcid})],
                    pikepdf.Operator("BDC"),
                ))
                new_ops.append((operands, operator))
                new_ops.append(([], pikepdf.Operator("EMC")))
                if vec_idx is not None:
                    blocks[vec_idx]["used"] = True
                    alt = blocks[vec_idx].get("alt_text", "Figure")
                else:
                    # No alt-text placeholder — still wrap the whole Form XObject
                    # as a single Figure so its constituent paths are never exposed
                    # as individual tagged elements (reviewer request: don't break
                    # figures into their constituent components).
                    alt = "Figure"
                # Compute bbox from the Form XObject's /BBox and the
                # current CTM so the Figure struct element satisfies
                # PDF/UA bounding-box requirements.
                form_bbox = _compute_form_bbox(page, xobj_name, ctm)
                struct_elems.append((mcid, "/Figure", alt, form_bbox))
                _open_artifact()
                continue

            # Any other Do → stays in artifact wrapper
            if not artifact_open:
                _close_struct()
                _open_artifact()
            new_ops.append((operands, operator))
            continue

        # ---- Everything else stays in current wrapper ----
        if not artifact_open and not struct_open:
            _open_artifact()
        new_ops.append((operands, operator))

    _close_struct()
    _close_artifact()

    return new_ops, struct_elems


# ---------------------------------------------------------------------------
# Position matching
# ---------------------------------------------------------------------------

def _find_block(x: float, y: float, blocks: list) -> int:
    """Find the text block whose bbox best matches position (x, y).

    Uses adaptive tolerance based on the block's own size.
    """
    best_idx = -1
    best_dist = float("inf")

    for idx, block in enumerate(blocks):
        if block["struct_type"] in ("/Figure", "/TD", "/TH"):
            continue  # Figures and table cells use dedicated matchers
        bbox = block["bbox"]
        # Adaptive tolerance: 20pt or 30% of block height, whichever is larger
        bh = max(bbox.y1 - bbox.y0, 1.0)
        bw = max(bbox.x1 - bbox.x0, 1.0)
        tol_y = max(20.0, bh * 0.3)
        tol_x = max(20.0, bw * 0.15)

        if (bbox.x0 - tol_x <= x <= bbox.x1 + tol_x and
                bbox.y0 - tol_y <= y <= bbox.y1 + tol_y):
            cx = (bbox.x0 + bbox.x1) / 2
            cy = (bbox.y0 + bbox.y1) / 2
            dist = abs(x - cx) + abs(y - cy)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

    return best_idx


def _find_cell_block(x: float, y: float, blocks: list) -> int:
    """Find the table cell (TD/TH) whose bbox strictly contains position (x, y).

    Uses a tight tolerance (4pt) so only text that is genuinely inside the
    cell bbox gets matched.  Already-used cells are still matched so that all
    operators inside one cell stay inside the same open struct (no re-open).
    Returns -1 if nothing matches.
    """
    TOL = 4.0
    best_idx = -1
    best_dist = float("inf")

    for idx, block in enumerate(blocks):
        if block["struct_type"] not in ("/TD", "/TH"):
            continue
        if block.get("used"):
            continue  # cell already fully emitted — don't re-open
        bbox = block["bbox"]
        if (bbox.x0 - TOL <= x <= bbox.x1 + TOL and
                bbox.y0 - TOL <= y <= bbox.y1 + TOL):
            cx = (bbox.x0 + bbox.x1) / 2
            cy = (bbox.y0 + bbox.y1) / 2
            dist = abs(x - cx) + abs(y - cy)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

    return best_idx


def _find_cell_continuation(x: float, y: float, blocks: list) -> int:
    """Find a *used* TD/TH cell whose bbox contains (x, y).

    Called only after _find_cell_block returns -1 (no unused cell matches).
    When a cell spans multiple BT blocks (mixed formatting), subsequent blocks
    hit this function and get merged into the same struct element via the
    /TD_CONT mechanism in _build_structure_tree.
    Returns -1 if no used cell matches.
    """
    TOL = 4.0
    best_idx = -1
    best_dist = float("inf")

    for idx, block in enumerate(blocks):
        if block["struct_type"] not in ("/TD", "/TH"):
            continue
        if not block.get("used"):
            continue  # only match already-used cells
        bbox = block["bbox"]
        if (bbox.x0 - TOL <= x <= bbox.x1 + TOL and
                bbox.y0 - TOL <= y <= bbox.y1 + TOL):
            cx = (bbox.x0 + bbox.x1) / 2
            cy = (bbox.y0 + bbox.y1) / 2
            dist = abs(x - cx) + abs(y - cy)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

    return best_idx


def _find_image_block_by_position(blocks: list, x: float, y: float) -> Optional[int]:
    """Find the closest unused image block by CTM position.

    Falls back to the next sequential unused image if no position match.
    """
    best_idx = None
    best_dist = float("inf")

    for idx, block in enumerate(blocks):
        if block["struct_type"] != "/Figure" or block.get("used"):
            continue
        if block.get("is_vector"):
            continue  # vector figures are matched by Form XObject, not position
        bbox = block["bbox"]
        # Use generous tolerance since CTM position may not perfectly align
        tol = max(50.0, max(bbox.width, bbox.height) * 0.5)
        cx = (bbox.x0 + bbox.x1) / 2
        cy = (bbox.y0 + bbox.y1) / 2
        dist = abs(x - cx) + abs(y - cy)
        if dist < tol and dist < best_dist:
            best_dist = dist
            best_idx = idx

    # Fallback: next unused image block if position matching fails
    if best_idx is None:
        for idx, block in enumerate(blocks):
            if (block["struct_type"] == "/Figure"
                    and not block.get("used")
                    and not block.get("is_vector")):
                best_idx = idx
                break

    return best_idx


def _find_vector_figure_block(blocks: list) -> Optional[int]:
    """Find the first unmatched vector figure block (placeholder from original structure tree)."""
    for idx, block in enumerate(blocks):
        if (block["struct_type"] == "/Figure"
                and block.get("is_vector")
                and not block.get("used")):
            return idx
    return None


# ---------------------------------------------------------------------------
# Watermark detection — moved to image_reconciliation.py so the extractor and
# tagger share one implementation.  Local alias preserves the private name
# already used inside this module.
# ---------------------------------------------------------------------------

from image_reconciliation import detect_watermark_forms as _detect_watermark_forms


# ---------------------------------------------------------------------------
# XObject helpers
# ---------------------------------------------------------------------------

def _compute_form_bbox(page, xobj_name: str, ctm: list) -> Optional[list]:
    """Compute page-space bounding box for a Form XObject.

    Reads the Form's /BBox (and optional /Matrix), composes with the
    current CTM, and transforms all four corners to page coordinates.
    Returns [x0, y0, x1, y1] or None if the bbox cannot be determined.
    """
    xobjects = _get_xobjects(page)
    if not xobjects:
        return None
    obj = xobjects.get(xobj_name) or xobjects.get(xobj_name.lstrip("/"))
    if obj is None:
        return None
    try:
        raw_bbox = obj.get("/BBox")
        if raw_bbox is None:
            return None
        fb = [float(raw_bbox[i]) for i in range(4)]
        # Compose Form's own /Matrix (if any) with the current CTM
        form_matrix = [1, 0, 0, 1, 0, 0]
        raw_matrix = obj.get("/Matrix")
        if raw_matrix is not None:
            form_matrix = [float(raw_matrix[i]) for i in range(6)]
        composed = _mat_mul(form_matrix, ctm)
        # Transform all 4 corners of the Form's BBox to page space
        corners = [
            (fb[0], fb[1]), (fb[2], fb[1]),
            (fb[2], fb[3]), (fb[0], fb[3]),
        ]
        xs, ys = [], []
        for cx, cy in corners:
            px = composed[0] * cx + composed[2] * cy + composed[4]
            py = composed[1] * cx + composed[3] * cy + composed[5]
            xs.append(px)
            ys.append(py)
        return [min(xs), min(ys), max(xs), max(ys)]
    except Exception:
        return None


def _get_xobject_subtype(page, xobj_name: str) -> str:
    """Return 'Image', 'Form', or '' for the named XObject.

    Handles inherited Resources from the page tree.
    """
    xobjects = _get_xobjects(page)
    if not xobjects:
        return ""
    obj = xobjects.get(xobj_name)
    if obj is None:
        # Try without leading /
        obj = xobjects.get(xobj_name.lstrip("/"))
    if obj is None:
        return ""
    try:
        subtype = obj.get("/Subtype")
        if subtype == pikepdf.Name.Image:
            return "Image"
        elif subtype == pikepdf.Name.Form:
            return "Form"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Structure tree construction — with table support
# ---------------------------------------------------------------------------

def _build_structure_tree(pdf: pikepdf.Pdf, all_page_elems: list,
                          doc_content: DocumentContent):
    """Build StructTreeRoot -> Document -> elements hierarchy.

    Groups elements that require parent containers:
    - Consecutive /LI elements are wrapped in /L (List)
    - Consecutive /TD|/TH elements are wrapped in /Table -> /TR
    - All other types (/P, /H1-/H6, /Figure, etc.) go directly under /Document
    """
    # Pass 1: sequence-based heading normalization (PDF/UA Clause 7.4.2).
    # The rule: when heading level increases, it must go up by exactly 1.
    # Walk headings in document order; if a heading jumps more than +1 from
    # the previous heading, clamp it to prev_level + 1.
    # e.g. sequence H3,H1,H4,H2,H3 → H1,H1,H2,H2,H3 (no forward jumps > 1).
    _heading_re = re.compile(r'^/H(\d+)$')
    _elem_overrides: dict = {}   # (page_idx, elem_idx) → new struct_type
    _prev_level = 0
    for _pi, _se in all_page_elems:
        for _ei, _ed in enumerate(_se):
            _m = _heading_re.match(_ed[1])
            if _m:
                _lvl = int(_m.group(1))
                if _lvl > _prev_level + 1:
                    _lvl = _prev_level + 1
                    _elem_overrides[(_pi, _ei)] = f"/H{_lvl}"
                _prev_level = _lvl

    def _remap_struct_type(page_idx: int, elem_idx: int, st: str) -> str:
        return _elem_overrides.get((page_idx, elem_idx), st)

    # Build Document → Art hierarchy to match InDesign's exported structure.
    # InDesign automatically wraps all body content in an <Art> element; our
    # flat Document → P... output was missing this container.
    outer_doc_kids = pikepdf.Array()
    outer_doc_elem = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/StructElem"),
        "/S": pikepdf.Name("/Document"),
        "/K": outer_doc_kids,
    }))

    # <Art> sits directly under <Document>; all content elements go under <Art>.
    # The local names doc_elem / doc_kids are kept so the rest of the function
    # (grouping helpers, ParentTree wiring) continues to work unchanged.
    doc_kids = pikepdf.Array()
    doc_elem = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/StructElem"),
        "/S": pikepdf.Name("/Art"),
        "/P": outer_doc_elem,
        "/K": doc_kids,
    }))
    outer_doc_kids.append(doc_elem)

    parent_tree_nums = pikepdf.Array()
    all_leaf_elems = []  # (elem_ref, struct_type) for grouping
    annot_parent_entries = []  # (annot_obj, elem_ref) for annotation ParentTree

    for page_idx, struct_elems in all_page_elems:
        if not struct_elems:
            continue

        if page_idx >= len(pdf.pages):
            continue

        page_ref = pdf.pages[page_idx].obj
        page_elem_refs = []
        mcid_to_elem = {}     # mcid -> struct elem, for continuation lookup
        pending_conts = []    # (cont_mcid, original_mcid) processed after main pass
        # source-/Figure id -> existing StructElem on this page, so multiple
        # BDC ranges of one diagram collapse into one StructElem with N MCRs.
        figure_groups: dict = {}

        for elem_idx, elem_data in enumerate(struct_elems):
            # Tuples: (mcid, type, alt) or (mcid, type, alt, bbox)
            #     or: (mcid, "/Link", alt, None, annot_obj)
            #     or: (mcid, "/Figure", alt, bbox, source_fig_id_or_None)
            #     or: (mcid, "/TD"|"/TH", "", None, None, row_idx, table_idx)
            #     or: (mcid, "/TD_CONT", "", None, None, row_idx, table_idx, orig_mcid)
            mcid = elem_data[0]
            struct_type = _remap_struct_type(page_idx, elem_idx, elem_data[1])

            if struct_type == "/TD_CONT":
                # Continuation of an already-created TD/TH — defer to second pass
                original_mcid = elem_data[7] if len(elem_data) > 7 else None
                pending_conts.append((mcid, original_mcid))
                page_elem_refs.append(None)  # placeholder; filled in second pass
                continue

            alt_text = elem_data[2]
            fig_bbox = elem_data[3] if len(elem_data) > 3 else None
            # elem_data[4] is overloaded by tuple type: annot_obj for /Link,
            # source-/Figure id for /Figure.  Disambiguate explicitly.
            annot_obj = (elem_data[4]
                         if struct_type == "/Link" and len(elem_data) > 4
                         else None)
            source_fig_id = (elem_data[4]
                             if struct_type == "/Figure" and len(elem_data) > 4
                             else None)
            row_idx   = elem_data[5] if len(elem_data) > 5 else None
            table_idx = elem_data[6] if len(elem_data) > 6 else None

            mcr = pikepdf.Dictionary({
                "/Type": pikepdf.Name("/MCR"),
                "/Pg": page_ref,
                "/MCID": mcid,
            })

            # Source-/Figure regrouping: every BDC range that shared one
            # source /Figure becomes additional /MCRs on the existing elem
            # instead of a duplicate StructElem.  Fixes the "N overlapping
            # figures with the same alt text" UX bug PAC doesn't catch.
            if struct_type == "/Figure" and source_fig_id is not None:
                existing = figure_groups.get(source_fig_id)
                if existing is not None:
                    existing_k = existing.get("/K")
                    if isinstance(existing_k, pikepdf.Array):
                        existing_k.append(mcr)
                    else:
                        existing[pikepdf.Name("/K")] = pikepdf.Array(
                            [existing_k, mcr]
                        )
                    if fig_bbox:
                        a = existing.get("/A")
                        if isinstance(a, pikepdf.Dictionary):
                            old_b = a.get("/BBox")
                            if old_b is not None and len(old_b) >= 4:
                                a[pikepdf.Name("/BBox")] = pikepdf.Array([
                                    min(float(old_b[0]), fig_bbox[0]),
                                    min(float(old_b[1]), fig_bbox[1]),
                                    max(float(old_b[2]), fig_bbox[2]),
                                    max(float(old_b[3]), fig_bbox[3]),
                                ])
                    mcid_to_elem[mcid] = existing
                    page_elem_refs.append(existing)
                    continue  # already in all_leaf_elems from first occurrence

            # Defensive: /Artifact is a marked-content role, never a valid
            # /S value.  If it ever reaches this point a producer upstream is
            # broken — fail loudly here rather than emit a malformed PDF that
            # only PAC will catch later.
            assert struct_type != "/Artifact", (
                f"Refusing to emit StructElem with /S=/Artifact "
                f"(mcid={mcid}, page={page_idx}); fix the producer"
            )
            elem_dict = {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name(struct_type),
            }

            if struct_type == "/Link" and annot_obj is not None:
                # /Link with annotation: K = [MCR, OBJR]
                objr = pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/OBJR"),
                    "/Pg": page_ref,
                    "/Obj": annot_obj,
                })
                elem_dict["/K"] = pikepdf.Array([mcr, objr])
            else:
                elem_dict["/K"] = mcr

            if struct_type == "/Figure" and alt_text:
                elem_dict["/Alt"] = pikepdf.String(alt_text)

            # PDF/UA clause 7.5: TH elements must have a /Scope attribute
            if struct_type == "/TH":
                elem_dict["/A"] = pikepdf.Dictionary({
                    "/O": pikepdf.Name("/Table"),
                    "/Scope": pikepdf.Name("/Column"),
                })

            if struct_type == "/Figure":
                # PDF/UA-1: every /Figure on a single page must carry /BBox.
                # Fall back to the page MediaBox so the /A dict is always
                # present — better a coarse BBox than none at all.
                bbox = fig_bbox
                if not bbox:
                    try:
                        mb = pdf.pages[page_idx].obj.get("/MediaBox")
                        if mb is not None and len(mb) >= 4:
                            bbox = [float(mb[0]), float(mb[1]),
                                    float(mb[2]), float(mb[3])]
                    except Exception:
                        bbox = None
                if bbox:
                    elem_dict["/A"] = pikepdf.Dictionary({
                        "/O": pikepdf.Name("/Layout"),
                        "/BBox": pikepdf.Array([
                            bbox[0], bbox[1], bbox[2], bbox[3],
                        ]),
                        "/Placement": pikepdf.Name("/Block"),
                    })

            elem = pdf.make_indirect(pikepdf.Dictionary(elem_dict))
            mcid_to_elem[mcid] = elem
            page_elem_refs.append(elem)
            all_leaf_elems.append((elem, struct_type, row_idx, table_idx))

            # First occurrence of this source-/Figure id: register so
            # subsequent BDC ranges fold into this same StructElem.
            if struct_type == "/Figure" and source_fig_id is not None:
                figure_groups[source_fig_id] = elem

            if struct_type == "/Link" and annot_obj is not None:
                annot_parent_entries.append((annot_obj, elem))

        # Second pass: attach continuation MCRs to their original TD/TH elements.
        # The ParentTree maps each continuation MCID to the *same* struct element
        # as the original, so the cell content stays unified under one /TD or /TH.
        for cont_mcid, original_mcid in pending_conts:
            original_elem = mcid_to_elem.get(original_mcid)
            if original_elem is not None:
                cont_mcr = pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/MCR"),
                    "/Pg": page_ref,
                    "/MCID": cont_mcid,
                })
                existing_k = original_elem.get("/K")
                if isinstance(existing_k, pikepdf.Array):
                    existing_k.append(cont_mcr)
                else:
                    original_elem[pikepdf.Name("/K")] = pikepdf.Array(
                        [existing_k, cont_mcr]
                    )
                page_elem_refs[cont_mcid] = original_elem
            else:
                # Fallback: original not found — create a standalone TD so this
                # MCID is never orphaned (avoids veraPDF clause 7.1/3 failure).
                fallback_mcr = pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/MCR"),
                    "/Pg": page_ref,
                    "/MCID": cont_mcid,
                })
                fallback_elem = pdf.make_indirect(pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/TD"),
                    "/K": fallback_mcr,
                }))
                page_elem_refs[cont_mcid] = fallback_elem
                all_leaf_elems.append((fallback_elem, "/TD", None, None))

        parent_tree_nums.append(page_idx)
        parent_tree_nums.append(pikepdf.Array(page_elem_refs))

    # Group elements under proper parent containers
    _group_and_add_children(pdf, doc_elem, doc_kids, all_leaf_elems)

    # Determine next key for annotation StructParent entries
    max_key = -1
    for i in range(0, len(parent_tree_nums) - 1, 2):
        try:
            k = int(parent_tree_nums[i])
            if k > max_key:
                max_key = k
        except Exception:
            pass
    next_key = max_key + 1

    # Add annotation StructParent entries to ParentTree
    for annot_obj, elem in annot_parent_entries:
        annot_obj[pikepdf.Name("/StructParent")] = next_key
        parent_tree_nums.append(next_key)
        parent_tree_nums.append(elem)
        next_key += 1

    parent_tree = pdf.make_indirect(pikepdf.Dictionary({
        "/Nums": parent_tree_nums,
    }))

    struct_tree_root = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/StructTreeRoot"),
        "/K": outer_doc_elem,
        "/ParentTree": parent_tree,
        "/ParentTreeNextKey": next_key,
    }))

    outer_doc_elem["/P"] = struct_tree_root
    pdf.Root[pikepdf.Name.StructTreeRoot] = struct_tree_root



def _group_and_add_children(pdf: pikepdf.Pdf, doc_elem, doc_kids,
                             all_leaf_elems: list):
    """Group leaf structure elements under proper parent containers.

    - Consecutive /LI elements → wrapped in /L (List)
    - Consecutive /TD or /TH elements → wrapped in /Table -> /TR
    - Inline elements (/Link, /Span) → wrapped in /P (cannot be direct /Document children)
    - Everything else → direct child of /Document
    """
    _NEEDS_LIST = frozenset(["/LI"])
    _NEEDS_TABLE = frozenset(["/TD", "/TH"])
    # Inline-level elements must not be direct children of /Document.
    # PAC warns (Matterhorn structure tree check) about each one.
    # Wrap them in a /P block so they're properly nested.
    _NEEDS_P_WRAP = frozenset(["/Link", "/Span"])

    def _flush_list(items):
        """Wrap accumulated /LI elements in an /L container."""
        l_kids = pikepdf.Array()
        l_elem = pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/L"),
            "/P": doc_elem,
            "/K": l_kids,
        }))
        for item_elem in items:
            item_elem[pikepdf.Name("/P")] = l_elem
            l_kids.append(item_elem)
        doc_kids.append(l_elem)

    def _flush_table(cell_pairs):
        """Wrap accumulated (elem, row_idx) pairs in /Table -> /TR... structure.

        Normalises all rows to the median column count so veraPDF clause 7.2/42
        (equal column spans) passes.  Rows that are longer are truncated; rows
        that are shorter are padded with content-free /TD placeholders.
        """
        # --- Group cells by row first so we can normalise column counts ---
        rows_ordered: list = []       # list of (row_idx, [cell_elem, ...])
        row_map: dict = {}            # row_idx -> cell list
        for cell_elem, row_idx in cell_pairs:
            ridx = row_idx if row_idx is not None else 0
            if ridx not in row_map:
                row_map[ridx] = []
                rows_ordered.append((ridx, row_map[ridx]))
            row_map[ridx].append(cell_elem)

        if not rows_ordered:
            return

        # Determine target column count: maximum across all rows.
        # All rows are padded to this count with empty TD placeholders so that
        # (a) every MCID-tagged cell stays in the tree (no orphaned MCIDs →
        #     veraPDF clause 7.1/3) and (b) every row has the same column count
        #     (veraPDF clause 7.2/42).  We never truncate — truncation would
        #     drop struct elements from the tree while their MCIDs remain in
        #     the content stream, causing "content not tagged" failures.
        counts = [len(cells) for _, cells in rows_ordered]
        target_cols = max(counts) if counts else 1
        target_cols = max(target_cols, 1)

        table_kids = pikepdf.Array()
        table_elem = pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/P": doc_elem,
            "/K": table_kids,
        }))

        for _, cells in rows_ordered:
            tr_kids = pikepdf.Array()
            tr_elem = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/TR"),
                "/P": table_elem,
                "/K": tr_kids,
            }))

            # Include ALL cells for this row (never truncate)
            for cell_elem in cells:
                cell_elem[pikepdf.Name("/P")] = tr_elem
                tr_kids.append(cell_elem)

            # Pad rows that are shorter than target with empty TD placeholders
            for _ in range(target_cols - len(cells)):
                placeholder = pdf.make_indirect(pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/TD"),
                    "/P": tr_elem,
                }))
                tr_kids.append(placeholder)

            table_kids.append(tr_elem)

        doc_kids.append(table_elem)

    def _wrap_in_p(child_elem):
        """Wrap a single inline element in a /P block under /Document."""
        p_kids = pikepdf.Array([child_elem])
        p_elem = pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/P"),
            "/P": doc_elem,
            "/K": p_kids,
        }))
        child_elem[pikepdf.Name("/P")] = p_elem
        doc_kids.append(p_elem)

    # --- Pass 1: pre-collect all table cells grouped by (table_idx, row_idx) ---
    # This lets us emit each table completely even when the content stream
    # interleaves cells with paragraphs (common in InDesign column-major PDFs).
    all_table_cells: dict = {}       # table_idx -> {row_idx -> [cell_elem, ...]}
    table_first_pos: dict = {}       # table_idx -> first index in all_leaf_elems
    for i, (elem, struct_type, row_idx, table_idx) in enumerate(all_leaf_elems):
        if struct_type in _NEEDS_TABLE and table_idx is not None:
            if table_idx not in all_table_cells:
                all_table_cells[table_idx] = {}
                table_first_pos[table_idx] = i
            ridx = row_idx if row_idx is not None else 0
            all_table_cells[table_idx].setdefault(ridx, []).append(elem)

    # --- Pass 2: emit elements in order ---
    pending_list = []
    emitted_tables: set = set()

    def _flush_list_pending():
        if pending_list:
            _flush_list(pending_list)
            pending_list.clear()

    for i, (elem, struct_type, row_idx, table_idx) in enumerate(all_leaf_elems):
        if struct_type in _NEEDS_LIST:
            # Accumulate consecutive list items — do NOT flush the list here
            pending_list.append(elem)
        elif struct_type in _NEEDS_TABLE:
            _flush_list_pending()
            tidx = table_idx if table_idx is not None else id(elem)
            if tidx not in emitted_tables:
                # Emit the complete table now (all rows/cells collected in pass 1)
                emitted_tables.add(tidx)
                rows_dict = all_table_cells.get(tidx, {})
                if not rows_dict:
                    ridx_key = row_idx if row_idx is not None else 0
                    rows_dict = {ridx_key: [elem]}
                cell_pairs = []
                for ridx_key in sorted(rows_dict.keys()):
                    for cell_elem in rows_dict[ridx_key]:
                        cell_pairs.append((cell_elem, ridx_key))
                _flush_table(cell_pairs)
            # Subsequent cells of the same table are already emitted — skip
        elif struct_type in _NEEDS_P_WRAP:
            # Inline element — flush pending list, then wrap in /P
            _flush_list_pending()
            _wrap_in_p(elem)
        else:
            # Block-level or neutral element — flush pending list, add directly
            _flush_list_pending()
            elem[pikepdf.Name("/P")] = doc_elem
            doc_kids.append(elem)

    # Flush remaining list
    _flush_list_pending()



# ---------------------------------------------------------------------------
# Element type mapping
# ---------------------------------------------------------------------------

def _element_to_struct_type(tb: TextBlock) -> Optional[str]:
    """Map a TextBlock's ElementType to a PDF structure tag name.

    Returns ``None`` for element types that must NOT receive a StructElem
    (WATERMARK, HEADER_FOOTER) — those are routed through the content-stream
    /Artifact BDC path in ``_open_struct_for_block`` instead.  ``/Artifact``
    is a marked-content role, never a valid /S value; returning it here was a
    contract bug that the downstream ``is_artifact`` flag intercepted, but the
    cleaner contract is to never produce it in the first place.
    """
    if tb.element_type == ElementType.HEADING:
        level = max(1, min(tb.heading_level or 1, 6))
        return f"/H{level}"
    elif tb.element_type == ElementType.LIST_ITEM:
        return "/LI"
    elif tb.element_type == ElementType.TABLE_CELL:
        return "/TD"
    elif tb.element_type == ElementType.TABLE_HEADER:
        return "/TH"
    elif tb.element_type in (ElementType.WATERMARK, ElementType.HEADER_FOOTER):
        return None
    else:
        return "/P"


# ---------------------------------------------------------------------------
# Matrix math
# ---------------------------------------------------------------------------

def _mat_mul(m1: list, m2: list) -> list:
    """Multiply two 2D affine matrices [a, b, c, d, e, f]."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    ]
