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
import re

import pikepdf

from models import BBox

__all__ = ["ResolvedImage", "reconcile_page_images", "detect_watermark_forms"]

logger = logging.getLogger(__name__)


# Alt strings that match this pattern are placeholders left by older versions
# of accessibility-tagging tools (including a prior buggy version of this
# pipeline). Treat them as "no useful alt" so they do not propagate forward.
_PLACEHOLDER_ALT_RE = re.compile(r"^Figure \d+ on page \d+$")


# ---------------------------------------------------------------------------
# Watermark detection
# ---------------------------------------------------------------------------

_WATERMARK_KEYWORDS = [
    # English
    "draft", "confidential", "copy", "do not",
    "sample", "watermark", "instructor", "reproduce",
    "preliminary", "internal", "restricted", "void",
    "duplicate", "unofficial", "not for distribution",
    "review", "not for publication", "internal use",
    "proprietary", "for review", "proof",
    # French
    "brouillon", "confidentiel", "copie", "ne pas",
    "projet", "filigrane",
    # German
    "entwurf", "vertraulich", "kopie", "muster",
    "wasserzeichen",
    # Spanish
    "borrador", "confidencial", "copia", "muestra",
]


def _resolve_page_xobjects(page):
    """Get a page's /XObject dictionary, walking inherited Resources if needed."""
    try:
        res = page.get("/Resources")
        if res is None:
            parent = page.get("/Parent")
            while parent is not None and res is None:
                res = parent.get("/Resources")
                parent = parent.get("/Parent")
        if res is None:
            return None
        return res.get("/XObject")
    except Exception:
        return None


def detect_watermark_forms(page) -> set:
    """Return set of XObject names that are watermark Form XObjects.

    A Form XObject is treated as a watermark when any of these match:
      - Adobe PieceInfo marks it /Watermark
      - Optional-Content layer name contains 'watermark'
      - Form content stream contains watermark keywords (DRAFT, COPY, ...)

    This logic was previously private in pdf_tagger; it now lives here so the
    image reconciliation extractor and the tagger share one source of truth.
    """
    wm_names: set[str] = set()
    xobjects = _resolve_page_xobjects(page)
    if not xobjects:
        return wm_names

    for name, obj in xobjects.items():
        try:
            if not hasattr(obj, "keys"):
                continue
            if obj.get("/Subtype") != pikepdf.Name.Form:
                continue

            piece_info = obj.get("/PieceInfo")
            if piece_info:
                compound = piece_info.get("/ADBE_CompoundType")
                if compound:
                    private = compound.get("/Private")
                    if private and str(private) == "/Watermark":
                        wm_names.add(str(name))
                        continue

            oc = obj.get("/OC")
            if oc:
                oc_name = ""
                try:
                    if "/Name" in oc:
                        oc_name = str(oc["/Name"])
                    elif "/OCGs" in oc:
                        for ocg in oc["/OCGs"]:
                            if "/Name" in ocg:
                                oc_name = str(ocg["/Name"])
                                break
                except Exception:
                    pass
                if "watermark" in oc_name.lower():
                    wm_names.add(str(name))
                    continue

            try:
                data = obj.read_bytes().decode("latin-1", errors="replace")
            except Exception:
                continue
            if len(data) > 10000:
                continue
            tj_texts = re.findall(r"\((.*?)\)", data)
            full_text = " ".join(tj_texts).strip().lower()

            if any(kw in full_text for kw in _WATERMARK_KEYWORDS):
                wm_names.add(str(name))
        except Exception as e:
            logger.debug("Watermark detection failed for XObject '%s': %s", name, e)
            continue

    return wm_names


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


def _collect_leaf_mcids(node, visited: set, _pinned: list | None = None) -> list[int]:
    """Walk node's /K subtree, return all MCID leaves.

    Handles both leaf encodings: direct (plain int in /K) and indirect
    (MCR dict). Uses _node_dedup_key for cycle detection, with optional
    _pinned list to keep inline-dict references alive so id() stays stable.
    """
    mcids: list[int] = []
    if _pinned is None:
        _pinned = []
    key = _node_dedup_key(node)
    if key in visited:
        return mcids
    visited.add(key)
    if key[0] == "id":
        _pinned.append(node)

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
                    mcids.extend(_collect_leaf_mcids(child, visited, _pinned))
        return mcids
    if hasattr(kids, "get"):
        mcr_id = _mcr_mcid(kids)
        if mcr_id is not None:
            return [mcr_id]
        return _collect_leaf_mcids(kids, visited, _pinned)
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


def _node_dedup_key(node):
    """Return a stable dedup key for a pikepdf struct node.

    Indirect objects get an ('og', objgen) key — stable across calls.
    Inline dicts share objgen=(0,0) in pikepdf, so they fall back to
    ('id', id(node)); callers must pin the node to prevent GC recycling
    the address (which would cause id() collisions between unrelated
    short-lived wrappers).
    """
    try:
        og = node.objgen
        if og != (0, 0):
            return ("og", og)
    except Exception:
        pass
    return ("id", id(node))


def _find_descendant_pg(node, _depth: int = 0):
    """Return the /Pg attribute from the first descendant that has one.

    Word and InDesign typically attach /Pg to the MCR leaf, not the
    Figure that contains it; this helper recovers the page reference by
    walking down a few levels.  Capped at depth 6 to avoid pathological
    cost on large subtrees.
    """
    if _depth > 6:
        return None
    try:
        kids = node.get("/K")
    except Exception:
        return None
    if kids is None:
        return None
    candidates = []
    if isinstance(kids, list) or isinstance(kids, pikepdf.Array):
        for child in kids:
            if hasattr(child, "get"):
                candidates.append(child)
    elif hasattr(kids, "get"):
        candidates.append(kids)
    for child in candidates:
        try:
            pg = child.get("/Pg")
            if pg is not None:
                return pg
        except Exception:
            pass
    for child in candidates:
        deeper = _find_descendant_pg(child, _depth + 1)
        if deeper is not None:
            return deeper
    return None


def _read_source_struct_elements(pdf) -> dict[int, list[SourceStructElement]]:
    """Walk /StructTreeRoot, return {page_index: [SourceStructElement, ...]}.

    Collects /Figure AND /Artifact nodes. The RoleMap is consulted to
    resolve aliased tags (e.g. /Diagram -> /Figure on Word-generated PDFs).
    Empty result when struct tree is absent.
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

        # Resolve type aliases via /RoleMap. Word/Excel PDFs frequently
        # tag illustrations as /Diagram or /InlineShape and rely on the
        # RoleMap to declare /Diagram -> /Figure.
        role_map_resolved: dict[str, str] = {}
        try:
            raw_role_map = struct_root.get("/RoleMap")
            if raw_role_map is not None:
                for src_tag, mapped_tag in raw_role_map.items():
                    try:
                        role_map_resolved[str(src_tag)] = str(mapped_tag)
                    except Exception:
                        continue
        except Exception:
            pass

        visited: set = set()
        # Hold strong refs to inline-keyed nodes so id() stays stable.
        _pinned: list = []
        FIGURE = pikepdf.Name("/Figure")
        ARTIFACT = pikepdf.Name("/Artifact")

        def _resolved_struct_type(raw_s):
            if raw_s is None:
                return None
            s_str = str(raw_s)
            # Single-hop role-map resolution (PDF/UA permits chains, but
            # standard structure types map to themselves and chains are rare).
            mapped = role_map_resolved.get(s_str, s_str)
            return mapped

        def _walk(node, inherited_pg=None):
            key = _node_dedup_key(node)
            if key in visited:
                return
            visited.add(key)
            if key[0] == "id":
                _pinned.append(node)

            try:
                struct_type = node.get("/S")
            except Exception:
                struct_type = None
            resolved_type_str = _resolved_struct_type(struct_type)
            pg = None
            try:
                pg = node.get("/Pg") or inherited_pg
            except Exception:
                pg = inherited_pg

            is_figure = (struct_type == FIGURE) or (resolved_type_str == "/Figure")
            is_artifact = (struct_type == ARTIFACT) or (resolved_type_str == "/Artifact")
            if is_figure or is_artifact:
                # Resolve /Alt
                raw_alt = None
                try:
                    raw_alt = node.get("/Alt")
                except Exception:
                    pass
                alt_str = str(raw_alt).strip() if raw_alt else ""
                if _PLACEHOLDER_ALT_RE.match(alt_str):
                    # Legacy placeholder from prior tooling — discard
                    alt_str = ""

                # Resolve /BBox (rare on Figures from Word; common on Acrobat-edited)
                bbox = None
                try:
                    bbox = _bbox_from_pdf_array(node.get("/BBox"))
                except Exception:
                    pass

                # MCIDs — fresh visited set so we walk every leaf even on shared dicts
                mcids = _collect_leaf_mcids(node, set())

                # Determine page. In Word/InDesign output /Pg typically
                # lives on MCR leaf nodes below the Figure (not on the Figure
                # itself), so when neither the figure nor an ancestor has /Pg
                # we have to peek down at the descendants.
                page_idx = None
                if pg is not None:
                    try:
                        page_idx = page_id_to_idx.get(pg.objgen)
                    except Exception:
                        page_idx = None
                if page_idx is None:
                    leaf_pg = _find_descendant_pg(node)
                    if leaf_pg is not None:
                        try:
                            page_idx = page_id_to_idx.get(leaf_pg.objgen)
                        except Exception:
                            page_idx = None
                if page_idx is None and mcids:
                    # Fallback: use first page (rare; only when /Pg lookup failed)
                    page_idx = 0

                if page_idx is not None:
                    elem = SourceStructElement(
                        struct_type=("/Artifact" if is_artifact else "/Figure"),
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


# ---------------------------------------------------------------------------
# Content stream parser
# ---------------------------------------------------------------------------

def _matrix_multiply(m1: list[float], m2: list[float]) -> list[float]:
    """Multiply two PDF CTM matrices (6-element form, returns m1 × m2)."""
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


def _classify_xobjects(page) -> tuple[set, dict]:
    """Return (image_xobject_names, form_xobjects_by_name) on this page/form."""
    image_names: set[str] = set()
    form_objs: dict[str, object] = {}
    try:
        resources = page.get("/Resources")
        if resources is None:
            return image_names, form_objs
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return image_names, form_objs
        for name, obj in xobjects.items():
            try:
                subtype = obj.get("/Subtype")
                if subtype == pikepdf.Name("/Image"):
                    image_names.add(str(name))
                elif subtype == pikepdf.Name("/Form"):
                    form_objs[str(name)] = obj
            except Exception:
                continue
    except Exception:
        pass
    return image_names, form_objs


def _walk_content_stream(
    stream_obj,
    current_ctm: list[float],
    in_watermark: bool,
    image_names: set,
    form_objs: dict,
    watermark_form_names: set,
    occurrences: list,
    visited_forms: set,
    depth: int = 0,
) -> None:
    """Walk one content stream and recurse into Form XObjects with concat CTM."""
    if depth > 10:
        logger.debug("Form XObject recursion depth limit hit; stopping")
        return
    try:
        ops = pikepdf.parse_content_stream(stream_obj)
    except Exception:
        return

    ctm_stack: list[list[float]] = [list(current_ctm)]
    mcid_stack: list[Optional[int]] = []

    for operands, op in ops:
        op_str = str(op)
        if op_str == "q":
            ctm_stack.append(list(ctm_stack[-1]))
        elif op_str == "Q":
            if len(ctm_stack) > 1:
                ctm_stack.pop()
        elif op_str == "cm" and len(operands) >= 6:
            local = [float(operands[i]) for i in range(6)]
            ctm_stack[-1] = _matrix_multiply(local, ctm_stack[-1])
        elif op_str in ("BDC", "BMC"):
            mcid: Optional[int] = None
            if op_str == "BDC" and len(operands) >= 2:
                props = operands[1]
                try:
                    raw = props.get(pikepdf.Name("/MCID"))
                    if raw is not None:
                        mcid = int(raw)
                except Exception:
                    mcid = None
            mcid_stack.append(mcid)
        elif op_str == "EMC":
            if mcid_stack:
                mcid_stack.pop()
        elif op_str == "Do" and operands:
            xobj_name = str(operands[0])
            if xobj_name in image_names:
                bbox = _bbox_from_ctm(ctm_stack[-1])
                innermost = next(
                    (m for m in reversed(mcid_stack) if m is not None), None
                )
                occurrences.append(ImageOccurrence(
                    xobject_name=xobj_name,
                    page_bbox=bbox,
                    mcid=innermost,
                    in_watermark_ancestor=in_watermark,
                ))
            elif xobj_name in form_objs:
                form_obj = form_objs[xobj_name]
                form_id = id(form_obj)
                if form_id in visited_forms:
                    continue
                visited_forms.add(form_id)
                form_is_watermark = in_watermark or (xobj_name in watermark_form_names)
                inner_images, inner_forms = _classify_xobjects(form_obj)
                _walk_content_stream(
                    stream_obj=form_obj,
                    current_ctm=ctm_stack[-1],
                    in_watermark=form_is_watermark,
                    image_names=inner_images,
                    form_objs=inner_forms,
                    watermark_form_names=watermark_form_names,
                    occurrences=occurrences,
                    visited_forms=visited_forms,
                    depth=depth + 1,
                )


def _parse_image_occurrences(page, watermark_form_names: set) -> list:
    """Parse page content stream + recurse into Forms; emit ImageOccurrence list.

    Tracks CTM (via q/Q/cm) and innermost MCID (via BDC/EMC). Descends into
    Form XObjects with concatenated CTM so nested Image XObjects are found.
    """
    image_names, form_objs = _classify_xobjects(page)
    if not image_names and not form_objs:
        return []

    occurrences: list[ImageOccurrence] = []
    _walk_content_stream(
        stream_obj=page,
        current_ctm=[1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        in_watermark=False,
        image_names=image_names,
        form_objs=form_objs,
        watermark_form_names=watermark_form_names,
        occurrences=occurrences,
        visited_forms=set(),
    )
    return occurrences


# ---------------------------------------------------------------------------
# Bbox derivation for source elements lacking /BBox
# ---------------------------------------------------------------------------

def _derive_source_bboxes(
    source_elements: list, occurrences: list
) -> None:
    """Fill in `bbox` on SourceStructElement entries that don't have one.

    For each element with bbox=None, look up any ImageOccurrence whose mcid
    is in the element's mcids list and adopt that occurrence's page_bbox.
    Mutates source_elements in place. Idempotent for elements that already
    have a bbox.
    """
    by_mcid = {o.mcid: o for o in occurrences if o.mcid is not None}
    for elem in source_elements:
        if elem.bbox is not None:
            continue
        for m in elem.mcids:
            occ = by_mcid.get(m)
            if occ is not None:
                elem.bbox = occ.page_bbox
                break


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

def _bbox_iou(a: BBox, b: BBox) -> float:
    """Intersection-over-union of two BBoxes; 0.0 if no overlap."""
    ix0 = max(a.x0, b.x0)
    iy0 = max(a.y0, b.y0)
    ix1 = min(a.x1, b.x1)
    iy1 = min(a.y1, b.y1)
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    aw = max(0.0, a.x1 - a.x0)
    ah = max(0.0, a.y1 - a.y0)
    bw = max(0.0, b.x1 - b.x0)
    bh = max(0.0, b.y1 - b.y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _match_occurrences_to_sources(
    occurrences: list,
    sources: list,
    page_height: float,
) -> list:
    """Pair each ImageOccurrence with a SourceStructElement via priority chain.

    Priority:
      1. in_watermark_ancestor    → is_decorative=True, is_watermark=True
      2. MCID match               → use source intent (Figure: announce alt;
                                                       Artifact: decorative)
      3. Bbox overlap (IoU >= 0.5), greedy + each source claimed at most once
      4. No match in header band (y_center > 90% page_height) or footer band
         (y_center < 10%)        → is_decorative=True (header/footer ornament)
      5. No match in body region  → is_decorative=False, alt_text=""
                                   (downstream tagger handles fallback)

    Returns list of ResolvedImage in same order as `occurrences`.
    """
    results: list[Optional[ResolvedImage]] = [None] * len(occurrences)
    claimed_source_ids: set[int] = set()

    mcid_to_src = {}
    for src in sources:
        for m in src.mcids:
            mcid_to_src[m] = src

    # Pass 1: watermark + MCID
    for i, occ in enumerate(occurrences):
        if occ.in_watermark_ancestor:
            results[i] = ResolvedImage(
                xobject_name=occ.xobject_name, bbox=occ.page_bbox,
                alt_text="", is_decorative=True, is_watermark=True,
            )
            continue
        if occ.mcid is not None and occ.mcid in mcid_to_src:
            src = mcid_to_src[occ.mcid]
            results[i] = ResolvedImage(
                xobject_name=occ.xobject_name, bbox=occ.page_bbox,
                alt_text=src.alt_text,
                is_decorative=(src.struct_type == "/Artifact"),
            )
            claimed_source_ids.add(id(src))

    # Pass 2: bbox overlap (IoU >= 0.5), greedy
    for i, occ in enumerate(occurrences):
        if results[i] is not None:
            continue
        best_iou = 0.0
        best_src = None
        for src in sources:
            if id(src) in claimed_source_ids or src.bbox is None:
                continue
            iou = _bbox_iou(occ.page_bbox, src.bbox)
            if iou >= 0.5 and iou > best_iou:
                best_iou = iou
                best_src = src
        if best_src is not None:
            results[i] = ResolvedImage(
                xobject_name=occ.xobject_name, bbox=occ.page_bbox,
                alt_text=best_src.alt_text,
                is_decorative=(best_src.struct_type == "/Artifact"),
            )
            claimed_source_ids.add(id(best_src))

    # Pass 3: heuristic fallback
    header_threshold = page_height * 0.9
    footer_threshold = page_height * 0.1
    for i, occ in enumerate(occurrences):
        if results[i] is not None:
            continue
        y_center = (occ.page_bbox.y0 + occ.page_bbox.y1) / 2
        in_band = y_center > header_threshold or y_center < footer_threshold
        results[i] = ResolvedImage(
            xobject_name=occ.xobject_name, bbox=occ.page_bbox,
            alt_text="", is_decorative=in_band,
        )

    return results  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Visual reading-order sort
# ---------------------------------------------------------------------------

def _sort_visual_order(items: list) -> list:
    """Sort ResolvedImages top-to-bottom, left-to-right (visual reading order).

    PDF y-axis is bottom-up: higher y = higher on page.  Row-banding groups
    items at similar y so a slight vertical drift within a visual row does
    not break left-to-right ordering.
    """
    def y_center(it): return (it.bbox.y0 + it.bbox.y1) / 2
    def x_center(it): return (it.bbox.x0 + it.bbox.x1) / 2
    def height(it):   return max(1.0, it.bbox.y1 - it.bbox.y0)

    by_y = sorted(items, key=lambda i: -y_center(i))
    rows: list[list] = []
    for it in by_y:
        placed = False
        for row in rows:
            row_y = y_center(row[0])
            tol = max(height(row[0]), height(it)) * 0.5
            if abs(y_center(it) - row_y) <= tol:
                row.append(it)
                placed = True
                break
        if not placed:
            rows.append([it])
    output: list = []
    for row in rows:
        output.extend(sorted(row, key=x_center))
    return output


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reconcile_page_images(pdf_path: str) -> dict[int, list]:
    """Reconcile every Image XObject occurrence in the PDF with source intent.

    Returns {page_index: [ResolvedImage, ...]} in visual reading order.

    Per-page try/except: any failure on page N is logged at DEBUG and that
    page is omitted from the result.  The caller falls back to its prior
    behavior (e.g. placeholder alts) for omitted pages.  Other pages still
    get the new robust reconciliation.
    """
    result: dict[int, list] = {}
    try:
        pdf = pikepdf.Pdf.open(pdf_path)
    except Exception as e:
        logger.warning("Could not open PDF for reconciliation: %s", e)
        return result

    try:
        source_by_page = _read_source_struct_elements(pdf)

        for page_idx, page in enumerate(pdf.pages):
            try:
                try:
                    media = page.get("/MediaBox")
                    page_height = float(media[3]) - float(media[1]) if media else 792.0
                except Exception:
                    page_height = 792.0

                wm_names = detect_watermark_forms(page)
                occurrences = _parse_image_occurrences(page, wm_names)
                sources = list(source_by_page.get(page_idx, []))

                if occurrences:
                    _derive_source_bboxes(sources, occurrences)
                    resolved = _match_occurrences_to_sources(
                        occurrences, sources, page_height
                    )
                else:
                    resolved = []

                # Vector-figure preservation: any source /Figure with a non-empty
                # /Alt that didn't get attached to an occurrence above is emitted
                # as a placeholder ResolvedImage (empty xobject_name).  This keeps
                # alt text for chart-like figures rendered entirely with path
                # operators (no Image Do) flowing through to the tagger.
                claimed_alts = {r.alt_text for r in resolved if r.alt_text}
                for src in sources:
                    if src.struct_type != "/Figure":
                        continue
                    if not src.alt_text:
                        continue
                    if src.alt_text in claimed_alts:
                        continue
                    resolved.append(ResolvedImage(
                        xobject_name="",
                        bbox=src.bbox or BBox(x0=0, y0=0, x1=0, y1=0),
                        alt_text=src.alt_text,
                        is_decorative=False,
                    ))

                if resolved:
                    result[page_idx] = _sort_visual_order(resolved)
            except Exception as e:
                logger.debug("Reconciliation failed on page %d: %s", page_idx, e)
                continue
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    return result
