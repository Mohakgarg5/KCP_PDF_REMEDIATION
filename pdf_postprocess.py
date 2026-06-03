"""
pdf_postprocess.py - Post-process PDF to fix remaining PDF/UA issues.

Uses pikepdf to ensure all catalog-level requirements are met:
- /MarkInfo /Marked true
- /Lang on catalog
- /ViewerPreferences /DisplayDocTitle true
- /Tabs /S on every page (tab order = structure)
- XMP metadata: dc:title, dc:language, pdfuaid:part
- Font fixes: ToUnicode CMap + embedding for non-embedded fonts
- RoleMap for structure types
"""
import logging
import os
import re
import sys
from io import BytesIO

import pikepdf

logger = logging.getLogger(__name__)


def postprocess_pdf(pdf_path: str, title: str, language: str,
                    source_path: str = None) -> str:
    """Fix catalog-level metadata in the PDF for PDF/UA-1 compliance."""
    try:
        pdf = pikepdf.Pdf.open(pdf_path, allow_overwriting_input=True)
    except pikepdf.PasswordError:
        logger.error("PDF is encrypted/password-protected: %s", pdf_path)
        raise
    except Exception as e:
        logger.error("Could not open PDF for postprocessing: %s", e)
        raise

    _ensure_mark_info(pdf)
    _ensure_language(pdf, language)
    _ensure_viewer_preferences(pdf)
    _ensure_tab_order(pdf)
    _ensure_xmp_metadata(pdf, title, language)
    _ensure_role_map(pdf)
    _fix_optional_content(pdf)
    _fix_fonts(pdf)
    _fix_cid_to_gid_map(pdf)
    _fix_cidset_streams(pdf)
    _fix_annotations(pdf)
    _cleanup_empty_markers(pdf)
    _heal_artifact_struct_elems(pdf)
    _sanitize_non_standard_struct_types(pdf)
    _check_pipeline_invariants(pdf)

    # PDF/UA-1 requires PDF 1.7+
    pdf.save(pdf_path, min_version="1.7")
    pdf.close()

    return pdf_path


# ---------------------------------------------------------------------------
# Catalog-level fixes
# ---------------------------------------------------------------------------

def _ensure_mark_info(pdf: pikepdf.Pdf):
    if "/MarkInfo" not in pdf.Root:
        pdf.Root.MarkInfo = pikepdf.Dictionary()
    # Preserve existing keys, only set /Marked
    pdf.Root.MarkInfo[pikepdf.Name.Marked] = True


def _ensure_language(pdf: pikepdf.Pdf, language: str):
    pdf.Root.Lang = pikepdf.String(language)


def _ensure_viewer_preferences(pdf: pikepdf.Pdf):
    if "/ViewerPreferences" not in pdf.Root:
        pdf.Root.ViewerPreferences = pikepdf.Dictionary()
    pdf.Root.ViewerPreferences[pikepdf.Name.DisplayDocTitle] = True


def _ensure_tab_order(pdf: pikepdf.Pdf):
    for page in pdf.pages:
        page.obj[pikepdf.Name.Tabs] = pikepdf.Name.S


_PLACEHOLDER_TITLES = frozenset({
    "title", "untitled", "document", "untitled document",
    "microsoft word", "microsoft word document", "word document",
    "powerpoint presentation", "microsoft powerpoint",
})

_PLACEHOLDER_PREFIXES = (
    "microsoft word", "microsoft powerpoint", "microsoft excel",
)


def _is_placeholder_title(title: str) -> bool:
    """Return True if title is a known placeholder that should be replaced."""
    t = title.strip().lower()
    return t in _PLACEHOLDER_TITLES or t.startswith(_PLACEHOLDER_PREFIXES)


def _ensure_xmp_metadata(pdf: pikepdf.Pdf, title: str, language: str):
    with pdf.open_metadata() as meta:
        # Overwrite blank/whitespace titles AND known Word/template placeholder
        # titles (e.g. 'Title', 'Untitled') — these display as useless in PAC
        # and cause a hard PDF/UA metadata failure (Matterhorn 06-003).
        existing = meta.get("dc:title") or ""
        if not existing.strip() or _is_placeholder_title(existing):
            meta["dc:title"] = title
        if not meta.get("dc:language"):
            meta["dc:language"] = language
        if not meta.get("pdfuaid:part"):
            meta["pdfuaid:part"] = "1"
        if not meta.get("pdf:Producer"):
            meta["pdf:Producer"] = "VAPT Accessibility Pipeline (pikepdf)"
        meta["xmp:CreatorTool"] = "VAPT PDF Accessibility Remediation Pipeline"


_STANDARD_STRUCT_TYPES = frozenset([
    "/Document", "/Part", "/Art", "/Sect", "/Div", "/BlockQuote",
    "/Caption", "/TOC", "/TOCI", "/Index", "/NonStruct", "/Private",
    "/P", "/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6",
    "/L", "/LI", "/Lbl", "/LBody",
    "/Table", "/TR", "/TH", "/TD", "/THead", "/TBody", "/TFoot",
    "/Span", "/Quote", "/Note", "/Reference", "/BibEntry", "/Code",
    "/Link", "/Annot", "/Ruby", "/Warichu", "/RB", "/RT", "/RP", "/WT", "/WP",
    "/Figure", "/Formula", "/Form",
    # Note: /Artifact is intentionally NOT here — it is a marked-content
    # operator (BDC /Artifact ... EMC), not a structure type.  PAC flags any
    # /S /Artifact struct element as non-standard.  Leaving it out ensures a
    # source RoleMap entry like /Artifact -> /Foo would be preserved rather
    # than silently removed.
])


def _ensure_role_map(pdf: pikepdf.Pdf):
    """Fix RoleMap: remove self-mappings of standard types, keep custom mappings.

    PDF/UA requires a RoleMap for non-standard structure types so they can
    be resolved to standard types. Standard types must NOT be remapped.
    """
    stroot = pdf.Root.get("/StructTreeRoot")
    if not stroot:
        return
    role_map = stroot.get("/RoleMap")
    if not role_map:
        return

    keys_to_remove = []
    for key in role_map.keys():
        key_name = str(key) if not str(key).startswith("/") else str(key)
        val_name = str(role_map[key])
        # Remove self-mappings and standard-to-standard mappings
        if key_name == val_name:
            keys_to_remove.append(key)
        elif key_name in _STANDARD_STRUCT_TYPES and val_name in _STANDARD_STRUCT_TYPES:
            keys_to_remove.append(key)

    for key in keys_to_remove:
        del role_map[key]

    # Remove empty RoleMap
    if len(role_map.keys()) == 0:
        del stroot[pikepdf.Name("/RoleMap")]


# ---------------------------------------------------------------------------
# Struct-type sanitizer (Robustness pass A)
# ---------------------------------------------------------------------------

# Common non-standard /S aliases produced by Word/InDesign/Acrobat that map
# cleanly to a PDF/UA-1 standard structure type. Adding entries here teaches
# the sanitizer to insert a RoleMap rule automatically rather than letting
# PAC fail with "non-standard structure type".
_STRUCT_TYPE_ALIASES = {
    "/Diagram":         "/Figure",
    "/Chart":           "/Figure",
    "/Drawing":         "/Figure",
    "/InlineShape":     "/Figure",
    "/Picture":         "/Figure",
    "/Image":           "/Figure",
    "/Title":           "/H1",
    "/Subtitle":        "/H2",
    "/StyleSpan":       "/Span",
    "/ParagraphSpan":   "/Span",
    "/CharacterSpan":   "/Span",
    "/Hyperlink":       "/Link",
    # /Footnote, /Note, /Reference, /BibEntry are already standard — see
    # _STANDARD_STRUCT_TYPES above.
}


def _walk_struct_elements(node, seen=None):
    """Depth-first generator over every struct element in the tree."""
    if seen is None:
        seen = set()
    if not isinstance(node, pikepdf.Dictionary):
        return
    try:
        key = node.objgen
    except Exception:
        key = id(node)
    if key in seen:
        return
    seen.add(key)
    yield node
    try:
        kids = node.get("/K")
    except Exception:
        return
    if kids is None:
        return
    if isinstance(kids, pikepdf.Array):
        for c in kids:
            if isinstance(c, pikepdf.Dictionary):
                yield from _walk_struct_elements(c, seen)
    elif isinstance(kids, pikepdf.Dictionary):
        yield from _walk_struct_elements(kids, seen)


def _heal_artifact_struct_elems(pdf: pikepdf.Pdf):
    """Rewrite any ``/S=/Artifact`` StructElem to a valid PDF/UA-1 type.

    Background: ``/Artifact`` is a marked-content operator, never a struct
    type.  PAC's "Role mapping of non-standard structure types" check fails
    on every ``/S=/Artifact`` node it sees, and there is no RoleMap fix
    (you cannot map ``/Artifact`` to anything sensible).  Pre-Phase-1 builds
    of this pipeline emitted these elements when a content-stream ``/Figure``
    BDC range got paired with a mis-typed struct element; the producer has
    since been hardened (see the assert in ``_build_structure_tree``) but
    output files from the older code path are still in circulation.

    This sanitizer is the cleanup pass for those files (and the catch-all
    safety net for any future regression).  For every offending node we:

    1. Look up the BDC tag of its referenced MCID(s) in the actual content
       stream.
    2. Align ``/S`` to that tag.  Stream said ``/Figure`` -> set ``/Figure``
       and fill in the PDF/UA-required ``/Alt`` and ``/A /BBox`` fallbacks.
       Stream said ``/Artifact`` (or anything non-standard) -> set
       ``/NonStruct`` (a standard PDF/UA-1 type that maps to "no semantic
       meaning, just a tagged region").
    3. Leave the content stream alone; the page's BDC markers and MCIDs are
       still valid — only the StructElem's /S was wrong.

    Runs BEFORE :func:`_sanitize_non_standard_struct_types` and INV-1 so the
    invariant pass sees a clean tree.
    """
    stroot = pdf.Root.get("/StructTreeRoot")
    if not stroot:
        return

    pages_by_og: dict = {}
    for p in pdf.pages:
        try:
            pages_by_og[p.objgen] = p
        except Exception:
            pass

    bdc_cache: dict = {}  # page_objgen -> {mcid: bdc_tag_str}

    def _bdc_tag_for(page_og, mcid):
        if page_og not in bdc_cache:
            page = pages_by_og.get(page_og)
            tags: dict = {}
            if page is not None:
                try:
                    ops = list(pikepdf.parse_content_stream(page))
                except Exception:
                    ops = []
                for operands, op in ops:
                    if str(op) != "BDC" or len(operands) < 2:
                        continue
                    tag = operands[0]
                    attrs = operands[1]
                    if not hasattr(attrs, "get"):
                        continue
                    try:
                        raw = attrs.get("/MCID")
                        if raw is None:
                            continue
                        tags[int(raw)] = str(tag)
                    except Exception:
                        continue
            bdc_cache[page_og] = tags
        return bdc_cache[page_og].get(mcid)

    def _mcrs_under(node):
        """Return [(page_objgen, mcid)] for /MCR children of node's /K."""
        k = node.get("/K") if hasattr(node, "get") else None
        if k is None:
            return []
        kids = (
            list(k) if isinstance(k, pikepdf.Array)
            else [k] if isinstance(k, pikepdf.Dictionary)
            else []
        )
        out = []
        for kk in kids:
            if not isinstance(kk, pikepdf.Dictionary):
                continue
            pg = kk.get("/Pg")
            mc = kk.get("/MCID")
            if pg is None or mc is None:
                continue
            try:
                out.append((pg.objgen, int(mc)))
            except Exception:
                continue
        return out

    healed = 0
    for node in _walk_struct_elements(stroot):
        try:
            s = node.get("/S")
        except Exception:
            continue
        if str(s) != "/Artifact":
            continue

        mcrs = _mcrs_under(node)
        bdc_tag = None
        for page_og, mcid in mcrs:
            t = _bdc_tag_for(page_og, mcid)
            if t:
                bdc_tag = t
                break

        if bdc_tag == "/Figure":
            node[pikepdf.Name("/S")] = pikepdf.Name("/Figure")
            if "/Alt" not in node:
                node[pikepdf.Name("/Alt")] = pikepdf.String("")
            if "/A" not in node and "/BBox" not in node and mcrs:
                page_og, _ = mcrs[0]
                page = pages_by_og.get(page_og)
                if page is not None:
                    try:
                        mb = page.obj.get("/MediaBox")
                        if mb is not None and len(mb) >= 4:
                            node[pikepdf.Name("/A")] = pikepdf.Dictionary({
                                "/O": pikepdf.Name("/Layout"),
                                "/BBox": pikepdf.Array([
                                    float(mb[0]), float(mb[1]),
                                    float(mb[2]), float(mb[3]),
                                ]),
                                "/Placement": pikepdf.Name("/Block"),
                            })
                    except Exception:
                        pass
        elif bdc_tag and bdc_tag in _STANDARD_STRUCT_TYPES:
            node[pikepdf.Name("/S")] = pikepdf.Name(bdc_tag)
        else:
            # /Artifact in stream, unknown tag, or missing — /NonStruct is
            # the standard PDF/UA-1 "no semantic role" type.
            node[pikepdf.Name("/S")] = pikepdf.Name("/NonStruct")
        healed += 1

    if healed:
        logger.warning(
            "Healed %d /S=/Artifact struct element(s) by aligning /S to the "
            "content-stream BDC tag.  These were produced by a pre-Phase-1 "
            "tagger; the current producer cannot emit them.",
            healed,
        )


def _sanitize_non_standard_struct_types(pdf: pikepdf.Pdf):
    """Ensure every output struct element has a PDF/UA-resolvable /S.

    Walks the struct tree, collects the set of distinct /S values, and for
    any non-standard value that isn't already mapped in /RoleMap:
      * Known alias (Word/InDesign): insert RoleMap entry -> standard type.
      * /Artifact: log loudly. Artifact is a marked-content operator, never
        a struct type — its presence in the tree is a code bug, not data
        the user introduced. We do not auto-rewrite here (would require
        rewriting the content stream and ParentTree); the matching pipeline
        invariant raises so the regression is caught immediately.
      * Anything else: insert a defensive /Span mapping and warn so the
        tag still resolves to a known structure type for screen readers.

    This is a pure safety-net pass: if the tagger is healthy, the function
    walks the tree, finds nothing to fix, and returns.
    """
    stroot = pdf.Root.get("/StructTreeRoot")
    if not stroot:
        return

    types_in_tree: set = set()
    for node in _walk_struct_elements(stroot):
        try:
            s = node.get("/S")
        except Exception:
            continue
        if s is None:
            continue
        types_in_tree.add(str(s))

    non_standard = types_in_tree - _STANDARD_STRUCT_TYPES
    if not non_standard:
        return

    role_map = stroot.get("/RoleMap")
    if role_map is None:
        role_map = pikepdf.Dictionary()
        stroot[pikepdf.Name("/RoleMap")] = role_map

    for s_type in sorted(non_standard):
        # Already mapped by source or a prior run — leave alone.
        try:
            if role_map.get(s_type) is not None:
                continue
        except Exception:
            pass

        if s_type == "/Artifact":
            # Loud log — the invariant check below will fail the run so we
            # catch the regression immediately rather than shipping a bad
            # file.  No RoleMap entry: /Artifact has no valid struct meaning.
            logger.error(
                "Output struct tree contains /S /Artifact — this is never "
                "valid (Artifact is a marked-content operator, not a struct "
                "type).  Investigate the tagger; not auto-fixing here."
            )
            continue

        mapped = _STRUCT_TYPE_ALIASES.get(s_type, "/Span")
        role_map[pikepdf.Name(s_type)] = pikepdf.Name(mapped)
        logger.warning(
            "Added defensive RoleMap entry: %s -> %s (non-standard struct "
            "type encountered in output)",
            s_type, mapped,
        )


# ---------------------------------------------------------------------------
# Pre-save pipeline invariants (Robustness pass B)
# ---------------------------------------------------------------------------

class PipelineInvariantError(RuntimeError):
    """Raised when the final PDF violates an invariant we promised to uphold."""


def _check_pipeline_invariants(pdf: pikepdf.Pdf):
    """Last-line sanity check before save.

    These invariants are things the rest of the pipeline already guarantees
    — but a code regression elsewhere could quietly break them.  Surfacing
    the violation here turns silent bad output into a loud error during
    development/CI, while staying a no-op when the pipeline is healthy.

    Invariants:
      INV-1  No struct element has /S /Artifact.  Artifact is a marked-
             content operator and ends up flagged by PAC as a non-standard
             structure type with nothing visible to fix.
      INV-2  Every /S in the tree is either standard PDF/UA or appears in
             /RoleMap (the sanitizer pass above should have closed any gap).
      INV-3  Every /Figure struct element carries /BBox (directly or in /A).
             PDF/UA-1 requires /BBox on every page-local Figure so AT can
             locate/crop it.
      INV-4  Every MCID present in a page content stream is reachable from
             the struct tree (no orphan tagged content).

    Violations raise PipelineInvariantError — the failure stops the pipeline
    before writing a known-bad output PDF.
    """
    stroot = pdf.Root.get("/StructTreeRoot")
    if not stroot:
        return

    role_map_keys: set = set()
    try:
        rm = stroot.get("/RoleMap")
        if rm is not None:
            role_map_keys = {str(k) for k in rm.keys()}
    except Exception:
        pass

    bad_artifact = 0
    unresolved: set = set()
    figures_missing_bbox = 0
    reachable_mcids: dict = {}  # page-objgen -> set of MCIDs
    for node in _walk_struct_elements(stroot):
        try:
            s = node.get("/S")
        except Exception:
            continue
        if s is None:
            continue
        s_str = str(s)
        if s_str == "/Artifact":
            bad_artifact += 1
            continue
        if s_str not in _STANDARD_STRUCT_TYPES and s_str not in role_map_keys:
            unresolved.add(s_str)
        # INV-3: every /Figure must carry /BBox
        if s_str == "/Figure":
            if not _figure_has_bbox(node):
                figures_missing_bbox += 1
        # INV-4: collect reachable MCIDs (only for leaf /K = MCR/int)
        _collect_reachable_mcids(node, reachable_mcids)

    # INV-4: compare reachable vs actual content-stream MCIDs
    orphans_total = 0
    orphan_pages = []
    for page_idx, page in enumerate(pdf.pages):
        actual = _collect_page_mcids(page)
        reach = reachable_mcids.get(page.objgen, set())
        orphans = actual - reach
        if orphans:
            orphans_total += len(orphans)
            orphan_pages.append((page_idx + 1, sorted(orphans)[:5]))

    problems = []
    if bad_artifact:
        problems.append(
            f"INV-1 violated: {bad_artifact} struct element(s) have "
            f"/S /Artifact (must be marked-content, not a struct type)"
        )
    if unresolved:
        problems.append(
            f"INV-2 violated: non-standard struct type(s) without RoleMap: "
            f"{sorted(unresolved)}"
        )
    if figures_missing_bbox:
        problems.append(
            f"INV-3 violated: {figures_missing_bbox} /Figure element(s) "
            f"missing /BBox (PDF/UA-1 requires /BBox on page-local Figures)"
        )
    if orphans_total:
        sample = ", ".join(
            f"page {p}: MCIDs {ms}" for p, ms in orphan_pages[:3]
        )
        problems.append(
            f"INV-4 violated: {orphans_total} orphan MCID(s) in content "
            f"streams not reachable from struct tree (sample: {sample})"
        )

    if problems:
        msg = "Pipeline invariant check failed:\n  - " + "\n  - ".join(problems)
        logger.error(msg)
        raise PipelineInvariantError(msg)


def _figure_has_bbox(node) -> bool:
    """True if a /Figure StructElem has /BBox available (direct or in /A)."""
    try:
        if "/BBox" in node:
            return True
        a = node.get("/A")
    except Exception:
        return False
    if a is None:
        return False
    if isinstance(a, pikepdf.Dictionary):
        return "/BBox" in a
    if isinstance(a, pikepdf.Array):
        for entry in a:
            if isinstance(entry, pikepdf.Dictionary) and "/BBox" in entry:
                return True
    return False


def _collect_reachable_mcids(node, out: dict) -> None:
    """Walk a StructElem's /K and record every (page, MCID) reached."""
    try:
        k = node.get("/K")
    except Exception:
        return
    if k is None:
        return
    items = k if isinstance(k, pikepdf.Array) else [k]
    for item in items:
        if isinstance(item, int):
            # Bare int MCID on this StructElem's page (when /Pg is on the elem)
            try:
                pg = node.get("/Pg")
            except Exception:
                pg = None
            if pg is not None:
                out.setdefault(pg.objgen, set()).add(int(item))
            continue
        if isinstance(item, pikepdf.Dictionary):
            t = item.get("/Type")
            t_str = str(t) if t is not None else ""
            if t_str == "/MCR":
                mcid = item.get("/MCID")
                pg = item.get("/Pg") or node.get("/Pg")
                if mcid is not None and pg is not None:
                    out.setdefault(pg.objgen, set()).add(int(mcid))


_MCID_RE = None

def _collect_page_mcids(page) -> set:
    """Parse a page's content stream(s) and return the set of MCIDs present."""
    import re
    global _MCID_RE
    if _MCID_RE is None:
        _MCID_RE = re.compile(rb"/MCID\s+(\d+)")
    cs = page.get("/Contents")
    if cs is None:
        return set()
    streams = cs if isinstance(cs, pikepdf.Array) else [cs]
    mcids: set = set()
    for s in streams:
        try:
            data = s.read_bytes()
        except Exception:
            continue
        for m in _MCID_RE.finditer(data):
            mcids.add(int(m.group(1)))
    return mcids


def _fix_optional_content(pdf: pikepdf.Pdf):
    """Fix Optional Content (OCProperties) for PDF/UA-1 compliance.

    Clause 7.10 requires:
    - Each OC config dict (D key and Configs array) must have a /Name key
    - The /AS key must not appear in any OC config dict
    """
    oc_props = pdf.Root.get("/OCProperties")
    if not oc_props:
        return

    def _fix_config(config_dict):
        if not hasattr(config_dict, 'get'):
            return
        # Ensure /Name key exists
        if "/Name" not in config_dict or not str(config_dict.get("/Name", "")):
            config_dict[pikepdf.Name("/Name")] = pikepdf.String("Default")
        # Remove forbidden /AS key
        if "/AS" in config_dict:
            del config_dict[pikepdf.Name("/AS")]

    # Fix the default configuration (D key)
    d_config = oc_props.get("/D")
    if d_config:
        _fix_config(d_config)

    # Fix alternate configurations (Configs array)
    configs = oc_props.get("/Configs")
    if configs and isinstance(configs, pikepdf.Array):
        for cfg in configs:
            _fix_config(cfg)


# ---------------------------------------------------------------------------
# Content stream cleanup
# ---------------------------------------------------------------------------

def _cleanup_empty_markers(pdf: pikepdf.Pdf):
    """Remove empty BMC/BDC...EMC pairs that contain no content operators.

    These are created by the q/Q boundary fix and confuse some validators.
    """
    _CONTENT_OPS = frozenset([
        "Tj", "TJ", "'", '"',                          # text drawing
        "m", "l", "c", "v", "y", "h", "re",            # path construction
        "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n",  # path painting
        "W", "W*",                                       # clipping
        "Do",                                            # XObject
        "sh",                                            # shading
        "BI",                                            # inline image
        "BT",                                            # text object (has content inside)
    ])

    for page in pdf.pages:
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception as e:
            logger.debug("Could not parse content stream for cleanup: %s", e)
            continue

        # Find empty marker spans (open_idx, close_idx) to remove
        changed = True
        while changed:
            changed = False
            marker_starts = []  # stack of (index, has_content)
            remove_indices = set()

            for i, (operands, operator) in enumerate(ops):
                op = bytes(operator).decode()
                if op in ("BDC", "BMC"):
                    marker_starts.append((i, False))
                elif op == "EMC":
                    if marker_starts:
                        start_idx, has_content = marker_starts.pop()
                        if not has_content:
                            remove_indices.add(start_idx)
                            remove_indices.add(i)
                            changed = True
                elif op in _CONTENT_OPS:
                    if marker_starts:
                        # Mark the current (innermost) marker as having content
                        idx, _ = marker_starts[-1]
                        marker_starts[-1] = (idx, True)

            if remove_indices:
                ops = [op for j, op in enumerate(ops) if j not in remove_indices]

        new_data = pikepdf.unparse_content_stream(ops)
        page.Contents = pikepdf.Stream(pdf, new_data)


# ---------------------------------------------------------------------------
# Link annotation fixes (clauses 7.18.1, 7.18.5)
# ---------------------------------------------------------------------------

def _fix_annotations(pdf: pikepdf.Pdf):
    """Tag annotations (Link, Widget) as structure elements with Contents.

    PDF/UA-1 requires:
    - Every link annotation is tagged as /Link in structure tree (7.18.5)
    - Every widget annotation is tagged as /Form in structure tree (7.18.1)
    - Annotations have /Contents or /Alt text

    For veraPDF, the annotation's /StructParent must map in the ParentTree
    to the correct structure element. We assign each annotation a new unique
    StructParent key beyond ParentTreeNextKey and add the mapping.
    """
    stroot = pdf.Root.get("/StructTreeRoot")
    if not stroot:
        return

    doc_elem = stroot.get("/K")
    if not doc_elem:
        return

    kids = doc_elem.get("/K")
    if not isinstance(kids, pikepdf.Array):
        return

    # Get or create the ParentTree number tree
    parent_tree = stroot.get("/ParentTree")
    if not parent_tree:
        parent_tree = pdf.make_indirect(pikepdf.Dictionary({
            "/Nums": pikepdf.Array(),
        }))
        stroot[pikepdf.Name("/ParentTree")] = parent_tree

    nums = parent_tree.get("/Nums")
    if nums is None:
        nums = pikepdf.Array()
        parent_tree[pikepdf.Name("/Nums")] = nums

    # Determine the next available key
    next_key = int(stroot.get("/ParentTreeNextKey", 0))
    for i in range(0, len(nums) - 1, 2):
        try:
            k = int(nums[i])
            if k >= next_key:
                next_key = k + 1
        except Exception:
            pass

    # Annotation subtype → structure type mapping
    _ANNOT_STRUCT_MAP = {
        "/Link": "/Link",
        "/Widget": "/Form",
    }

    for page_idx, page in enumerate(pdf.pages):
        annots = page.get("/Annots")
        if not annots:
            continue

        page_ref = page.obj

        for annot in annots:
            try:
                subtype = str(annot.get("/Subtype", ""))
                struct_type = _ANNOT_STRUCT_MAP.get(subtype)
                if not struct_type:
                    continue

                # 1. Ensure /Contents key exists with descriptive text
                if "/Contents" not in annot or not str(annot.get("/Contents", "")):
                    contents_text = _derive_annot_contents(annot, subtype)
                    annot[pikepdf.Name("/Contents")] = pikepdf.String(contents_text)

                # 2. Check if already properly tagged in ParentTree
                existing_sp = annot.get("/StructParent")
                if _is_already_tagged(stroot, existing_sp, struct_type):
                    continue

                # 3. Create structure element with OBJR.
                # /Link and /Form are inline elements — they must not be direct
                # children of /Document. Wrap them in a /P block so PAC does
                # not warn about improper nesting (Matterhorn structure tree).
                objr = pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/OBJR"),
                    "/Pg": page_ref,
                    "/Obj": annot,
                })

                _INLINE_TYPES = {"/Link", "/Form"}
                if struct_type in _INLINE_TYPES:
                    elem = pdf.make_indirect(pikepdf.Dictionary({
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name(struct_type),
                        "/K": objr,
                    }))
                    p_elem = pdf.make_indirect(pikepdf.Dictionary({
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name("/P"),
                        "/P": doc_elem,
                        "/K": pikepdf.Array([elem]),
                    }))
                    elem[pikepdf.Name("/P")] = p_elem
                    kids.append(p_elem)
                else:
                    elem = pdf.make_indirect(pikepdf.Dictionary({
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name(struct_type),
                        "/P": doc_elem,
                        "/K": objr,
                    }))
                    kids.append(elem)

                # 4. Assign a new unique StructParent and add to ParentTree
                annot[pikepdf.Name("/StructParent")] = next_key
                nums.append(next_key)
                nums.append(elem)
                next_key += 1

            except Exception as e:
                logger.debug("Could not fix %s annotation on page %d: %s",
                             subtype, page_idx, e)
                continue

    # Update ParentTreeNextKey
    stroot[pikepdf.Name("/ParentTreeNextKey")] = next_key


def _derive_annot_contents(annot, subtype: str) -> str:
    """Derive descriptive Contents text for an annotation."""
    if subtype == "/Link":
        action = annot.get("/A")
        if action:
            uri = action.get("/URI")
            if uri:
                return str(uri)
            # GoTo action
            s_type = str(action.get("/S", ""))
            if s_type == "/GoTo":
                return "Internal link"
            if s_type == "/GoToR":
                f = action.get("/F")
                return f"Link to {f}" if f else "External document link"
            if s_type == "/Named":
                n = action.get("/N")
                return str(n) if n else "Named action"
            dest = action.get("/D")
            if dest:
                return "Internal link"
        dest = annot.get("/Dest")
        if dest:
            return "Internal link"
        return "Link"

    if subtype == "/Widget":
        # Try field name (T), tooltip (TU), or alternate description
        tu = annot.get("/TU")
        if tu:
            return str(tu)
        t = annot.get("/T")
        if t:
            return f"Form field: {str(t)}"
        return "Form field"

    return "Annotation"


def _is_already_tagged(stroot, struct_parent, expected_type: str) -> bool:
    """Check if an annotation with given StructParent is already tagged correctly."""
    if struct_parent is None:
        return False

    parent_tree = stroot.get("/ParentTree")
    if not parent_tree:
        return False

    nums = parent_tree.get("/Nums")
    if not nums:
        return False

    sp_val = int(struct_parent)
    for i in range(0, len(nums) - 1, 2):
        try:
            if int(nums[i]) == sp_val:
                elem = nums[i + 1]
                if isinstance(elem, pikepdf.Array):
                    for e in elem:
                        if hasattr(e, 'get') and str(e.get("/S", "")) == expected_type:
                            return True
                elif hasattr(elem, 'get'):
                    if str(elem.get("/S", "")) == expected_type:
                        return True
                return False
        except Exception:
            continue

    return False



# ---------------------------------------------------------------------------
# CIDSet stream fix (clause 7.21.4.2)
# ---------------------------------------------------------------------------

def _fix_cidset_streams(pdf: pikepdf.Pdf):
    """Remove CIDSet streams from CID font descriptors.

    PDF/UA-1 clause 7.21.4.2 requires that if a CIDSet is present, it must
    identify ALL CIDs in the font. Since CIDSet is optional, the safest
    compliant fix is to simply remove it.
    """
    seen_objgen = set()

    for page in pdf.pages:
        res = _resolve_page_resources(page)
        if not res:
            continue
        font_dict = res.get("/Font")
        if not font_dict:
            continue

        for name, font_obj in font_dict.items():
            try:
                objgen = font_obj.objgen
                if objgen in seen_objgen:
                    continue
                seen_objgen.add(objgen)

                font_type = str(font_obj.get("/Subtype", ""))

                # Collect all font descriptors to check
                descriptors = []
                if font_type == "/Type0":
                    descendants = font_obj.get("/DescendantFonts")
                    if descendants:
                        for desc_font in descendants:
                            d = desc_font.get("/FontDescriptor")
                            if d:
                                descriptors.append(d)
                desc = font_obj.get("/FontDescriptor")
                if desc:
                    descriptors.append(desc)

                for d in descriptors:
                    if "/CIDSet" in d:
                        del d[pikepdf.Name("/CIDSet")]
                        logger.debug("Removed CIDSet from font '%s'", name)

            except Exception as e:
                logger.debug("CIDSet fix failed for font '%s': %s", name, e)
                continue


# ---------------------------------------------------------------------------
# CIDToGIDMap fix (clause 7.21.3.2)
# ---------------------------------------------------------------------------

def _fix_cid_to_gid_map(pdf: pikepdf.Pdf):
    """Add CIDToGIDMap /Identity to embedded Type 2 CIDFont dicts missing it.

    ISO 32000-1 Table 117 requires embedded CIDFontType2 fonts to have a
    CIDToGIDMap entry (either /Identity or a stream). Without it veraPDF
    fails clause 7.21.3.2. Only adds the entry when it is absent.
    """
    seen_objgen = set()

    for page in pdf.pages:
        res = _resolve_page_resources(page)
        if not res:
            continue
        font_dict = res.get("/Font")
        if not font_dict:
            continue

        for name, font_obj in font_dict.items():
            try:
                if str(font_obj.get("/Subtype", "")) != "/Type0":
                    continue

                objgen = font_obj.objgen
                if objgen in seen_objgen:
                    continue
                seen_objgen.add(objgen)

                descendants = font_obj.get("/DescendantFonts")
                if not descendants:
                    continue

                for desc_font in descendants:
                    try:
                        if str(desc_font.get("/Subtype", "")) != "/CIDFontType2":
                            continue
                        if "/CIDToGIDMap" not in desc_font:
                            desc_font[pikepdf.Name("/CIDToGIDMap")] = \
                                pikepdf.Name("/Identity")
                            logger.debug(
                                "Added CIDToGIDMap /Identity to '%s'", name)
                    except Exception as e:
                        logger.debug(
                            "CIDToGIDMap fix failed for descendant of '%s': %s",
                            name, e)
            except Exception as e:
                logger.debug("CIDToGIDMap fix failed for font '%s': %s", name, e)


# ---------------------------------------------------------------------------
# Font fixes — ToUnicode CMap + embedding
# ---------------------------------------------------------------------------

# Windows-1252 byte values 0x80-0x9F that map to non-obvious Unicode points
_WIN1252_SPECIAL = {
    0x80: 0x20AC, 0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E,
    0x85: 0x2026, 0x86: 0x2020, 0x87: 0x2021, 0x88: 0x02C6,
    0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039, 0x8C: 0x0152,
    0x8E: 0x017D, 0x91: 0x2018, 0x92: 0x2019, 0x93: 0x201C,
    0x94: 0x201D, 0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014,
    0x98: 0x02DC, 0x99: 0x2122, 0x9A: 0x0161, 0x9B: 0x203A,
    0x9C: 0x0153, 0x9E: 0x017E, 0x9F: 0x0178,
}

# Map PostScript font names to possible TTF file names.
# Each list is tried in order — first match wins.
# Liberation fonts (apt-get install fonts-liberation) are metrically compatible
# substitutes for Arial/Times/Courier on Linux/Docker where MS fonts are absent.
_FONT_FILE_NAMES = {
    "TimesNewRomanPSMT": ["Times New Roman.ttf", "times.ttf", "LiberationSerif-Regular.ttf"],
    "TimesNewRomanPS-BoldMT": ["Times New Roman Bold.ttf", "timesbd.ttf", "LiberationSerif-Bold.ttf"],
    "TimesNewRomanPS-ItalicMT": ["Times New Roman Italic.ttf", "timesi.ttf", "LiberationSerif-Italic.ttf"],
    "TimesNewRomanPS-BoldItalicMT": ["Times New Roman Bold Italic.ttf", "timesbi.ttf", "LiberationSerif-BoldItalic.ttf"],
    "ArialMT": ["Arial.ttf", "arial.ttf", "LiberationSans-Regular.ttf"],
    "Arial-BoldMT": ["Arial Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf"],
    "Arial-ItalicMT": ["Arial Italic.ttf", "ariali.ttf", "LiberationSans-Italic.ttf"],
    "Arial-BoldItalicMT": ["Arial Bold Italic.ttf", "arialbi.ttf", "LiberationSans-BoldItalic.ttf"],
    "CourierNewPSMT": ["Courier New.ttf", "cour.ttf", "LiberationMono-Regular.ttf"],
    "CourierNewPS-BoldMT": ["Courier New Bold.ttf", "courbd.ttf", "LiberationMono-Bold.ttf"],
    "Verdana": ["Verdana.ttf", "verdana.ttf"],
    "Verdana-Bold": ["Verdana Bold.ttf", "verdanab.ttf"],
    "Georgia": ["Georgia.ttf", "georgia.ttf"],
    "Georgia-Bold": ["Georgia Bold.ttf", "georgiab.ttf"],
    "Tahoma": ["Tahoma.ttf", "tahoma.ttf"],
    "Tahoma-Bold": ["Tahoma Bold.ttf", "tahomabd.ttf"],
    "Calibri": ["Calibri.ttf", "calibri.ttf"],
    "Calibri-Bold": ["Calibri Bold.ttf", "calibrib.ttf"],
    "Cambria": ["Cambria.ttf", "cambria.ttf"],
    # Helvetica → Arial / Liberation Sans fallback (PDF base-14 PostScript names)
    "Helvetica": ["Arial.ttf", "arial.ttf", "LiberationSans-Regular.ttf"],
    "Helvetica,Bold": ["Arial Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf"],
    "Helvetica-Bold": ["Arial Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf"],
    "Helvetica,Italic": ["Arial Italic.ttf", "ariali.ttf", "LiberationSans-Italic.ttf"],
    "Helvetica-Oblique": ["Arial Italic.ttf", "ariali.ttf", "LiberationSans-Italic.ttf"],
    "Helvetica,BoldItalic": ["Arial Bold Italic.ttf", "arialbi.ttf", "LiberationSans-BoldItalic.ttf"],
    "Helvetica-BoldOblique": ["Arial Bold Italic.ttf", "arialbi.ttf", "LiberationSans-BoldItalic.ttf"],
    # Times (PDF base-14 PostScript names — distinct from MS TimesNewRoman*MT)
    "Times-Roman": ["Times New Roman.ttf", "times.ttf", "LiberationSerif-Regular.ttf"],
    "Times-Bold": ["Times New Roman Bold.ttf", "timesbd.ttf", "LiberationSerif-Bold.ttf"],
    "Times-Italic": ["Times New Roman Italic.ttf", "timesi.ttf", "LiberationSerif-Italic.ttf"],
    "Times-BoldItalic": ["Times New Roman Bold Italic.ttf", "timesbi.ttf", "LiberationSerif-BoldItalic.ttf"],
    # Courier (PDF base-14 PostScript names)
    "Courier": ["Courier New.ttf", "cour.ttf", "LiberationMono-Regular.ttf"],
    "Courier-Bold": ["Courier New Bold.ttf", "courbd.ttf", "LiberationMono-Bold.ttf"],
    "Courier-Oblique": ["Courier New Italic.ttf", "couri.ttf", "LiberationMono-Italic.ttf"],
    "Courier-BoldOblique": ["Courier New Bold Italic.ttf", "courbi.ttf", "LiberationMono-BoldItalic.ttf"],
}

# Map PostScript font names to TTC (TrueType Collection) files + font index
# Used when a standalone TTF is not available
_FONT_TTC_MAP = {}
if sys.platform == "darwin":
    _FONT_TTC_MAP = {
        "Helvetica": ("/System/Library/Fonts/Helvetica.ttc", 0),
        "Helvetica,Bold": ("/System/Library/Fonts/Helvetica.ttc", 1),
        "Helvetica-Bold": ("/System/Library/Fonts/Helvetica.ttc", 1),
        "Helvetica,Italic": ("/System/Library/Fonts/Helvetica.ttc", 2),
        "Helvetica-Oblique": ("/System/Library/Fonts/Helvetica.ttc", 2),
        "Helvetica,BoldItalic": ("/System/Library/Fonts/Helvetica.ttc", 3),
        "Helvetica-BoldOblique": ("/System/Library/Fonts/Helvetica.ttc", 3),
    }

# Platform-specific font directories
_FONT_DIRS = []
if sys.platform == "darwin":
    _FONT_DIRS = [
        "/System/Library/Fonts/Supplemental",
        "/System/Library/Fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
    ]
elif sys.platform.startswith("linux"):
    _FONT_DIRS = [
        "/usr/share/fonts/truetype",
        "/usr/share/fonts/truetype/msttcorefonts",
        "/usr/share/fonts/truetype/liberation",
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/.local/share/fonts"),
    ]
elif sys.platform == "win32":
    _FONT_DIRS = [
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    ]


def _resolve_page_resources(page):
    """Get Resources for a page, checking inheritance from page tree."""
    res = page.get("/Resources")
    if res:
        return res
    parent = page.get("/Parent")
    seen = set()
    while parent:
        try:
            obj_id = parent.objgen
            if obj_id in seen:
                break  # Circular reference protection
            seen.add(obj_id)
            res = parent.get("/Resources")
            if res:
                return res
            parent = parent.get("/Parent")
        except Exception:
            break
    return None


def _fix_fonts(pdf: pikepdf.Pdf):
    """Fix all non-embedded fonts: add ToUnicode CMap and embed font data."""
    seen_objgen = set()

    for page in pdf.pages:
        res = _resolve_page_resources(page)
        if not res:
            continue
        font_dict = res.get("/Font")
        if not font_dict:
            continue

        for name, font_obj in font_dict.items():
            try:
                objgen = font_obj.objgen
                if objgen in seen_objgen:
                    continue
                seen_objgen.add(objgen)

                has_tounicode = "/ToUnicode" in font_obj
                desc = font_obj.get("/FontDescriptor")
                embedded = False
                if desc:
                    embedded = any(k in desc for k in
                                   ["/FontFile", "/FontFile2", "/FontFile3"])

                # For Type0 (CID) fonts, check DescendantFonts for descriptor
                # and embedding status — Type0 wrappers don't have their own
                # FontDescriptor; it lives on the CIDFont descendant.
                font_type = str(font_obj.get("/Subtype", ""))
                if font_type == "/Type0" and not embedded:
                    descendants = font_obj.get("/DescendantFonts")
                    if descendants:
                        for desc_font in descendants:
                            d = desc_font.get("/FontDescriptor")
                            if d and any(k in d for k in
                                         ["/FontFile", "/FontFile2",
                                          "/FontFile3"]):
                                embedded = True
                                break

                if has_tounicode and embedded:
                    continue

                encoding_obj = font_obj.get("/Encoding")
                encoding = str(encoding_obj) if encoding_obj else ""
                base_font_raw = str(font_obj.get("/BaseFont", "")).lstrip("/")
                # Strip subset prefix like ABCDEF+
                is_subset = "+" in base_font_raw
                base_font = base_font_raw.split("+", 1)[1] if is_subset else base_font_raw

                # Add ToUnicode CMap for simple WinAnsi fonts.
                # Skip if encoding has /Differences (custom remapping) or if
                # font is Type0/CID (uses its own CMap), or if already present.
                if not has_tounicode and _is_simple_winansi(encoding_obj):
                    _add_tounicode_cmap(pdf, font_obj)

                if not embedded:
                    _try_embed_font(pdf, font_obj, base_font)

            except Exception as e:
                logger.warning("Font fix failed for '%s': %s", name, e)
                continue


def _is_simple_winansi(encoding_obj) -> bool:
    """Check if encoding is plain /WinAnsiEncoding without /Differences.

    Returns False for:
    - None / missing encoding
    - Encoding dictionaries with /Differences array (custom glyph remapping)
    - Non-WinAnsi encodings (/MacRomanEncoding, /Identity-H, etc.)
    """
    if encoding_obj is None:
        return False
    # Simple Name: /WinAnsiEncoding
    if isinstance(encoding_obj, pikepdf.Name):
        return str(encoding_obj) == "/WinAnsiEncoding"
    # Dictionary: check /BaseEncoding and /Differences
    if isinstance(encoding_obj, pikepdf.Dictionary):
        base = str(encoding_obj.get("/BaseEncoding", ""))
        if "WinAnsi" not in base:
            return False
        # If /Differences is present, encoding is customized — skip
        if "/Differences" in encoding_obj:
            return False
        return True
    # String or other: check for WinAnsi substring
    return "WinAnsi" in str(encoding_obj)


def _add_tounicode_cmap(pdf: pikepdf.Pdf, font_obj):
    """Generate and attach a ToUnicode CMap for WinAnsiEncoding."""
    cmap_str = _generate_winansi_tounicode()
    cmap_stream = pikepdf.Stream(pdf, cmap_str.encode("latin-1"))
    font_obj[pikepdf.Name("/ToUnicode")] = cmap_stream


def _generate_winansi_tounicode() -> str:
    """Generate a standard ToUnicode CMap for WinAnsiEncoding (Windows-1252)."""
    entries = []
    for code in range(0x20, 0x100):
        if code == 0x7F:
            continue
        if 0x80 <= code <= 0x9F:
            if code in _WIN1252_SPECIAL:
                entries.append((code, _WIN1252_SPECIAL[code]))
            # Skip undefined codes (0x81, 0x8D, 0x8F, 0x90, 0x9D)
        else:
            entries.append((code, code))

    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
    ]

    # Split into chunks of 100 (PDF CMap limit per block)
    for i in range(0, len(entries), 100):
        chunk = entries[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for byte_code, unicode_val in chunk:
            lines.append(f"<{byte_code:02X}> <{unicode_val:04X}>")
        lines.append("endbfchar")

    lines.extend([
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ])
    return "\n".join(lines)


def _try_embed_font(pdf: pikepdf.Pdf, font_obj, base_font: str):
    """Try to find and embed a system font file."""
    font_location = _find_system_font(base_font)
    if not font_location:
        logger.warning(
            "Could not find system font file for '%s' — font will NOT be embedded. "
            "On Linux, install: apt-get install fonts-liberation ttf-mscorefonts-installer",
            base_font,
        )
        return

    try:
        from fontTools.ttLib import TTFont
        from fontTools.subset import Subsetter
    except ImportError:
        msg = (
            "fontTools is not installed — font embedding skipped for '%s'. "
            "Run: pip install fonttools" % base_font
        )
        logger.error(msg)
        print(f"ERROR: {msg}")
        return

    try:
        if isinstance(font_location, tuple):
            # TTC file: (path, index)
            ttc_path, ttc_index = font_location
            from fontTools.ttLib import TTCollection
            ttc = TTCollection(ttc_path)
            tt = ttc.fonts[ttc_index]
        else:
            tt = TTFont(font_location)
    except Exception as e:
        logger.debug("Could not open font file '%s': %s", font_location, e)
        return

    try:
        head = tt.get("head")
        os2 = tt.get("OS/2")
        post = tt.get("post")
        if not head or not os2:
            tt.close()
            return

        units_per_em = head.unitsPerEm
        scale = 1000.0 / units_per_em

        # Subset to characters used (from FirstChar/LastChar)
        first_char = int(font_obj.get("/FirstChar", 0))
        last_char = int(font_obj.get("/LastChar", 255))
        unicodes = set()
        for code in range(first_char, last_char + 1):
            if code in _WIN1252_SPECIAL:
                unicodes.add(_WIN1252_SPECIAL[code])
            elif 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
                unicodes.add(code)

        try:
            subsetter = Subsetter()
            subsetter.populate(unicodes=unicodes)
            subsetter.subset(tt)
        except Exception:
            # Reload full font if subsetting fails
            tt.close()
            if isinstance(font_location, tuple):
                from fontTools.ttLib import TTCollection as _TTC
                _ttc = _TTC(font_location[0])
                tt = _ttc.fonts[font_location[1]]
            else:
                tt = TTFont(font_location)
            head = tt["head"]
            os2 = tt["OS/2"]
            post = tt.get("post")

        buf = BytesIO()
        tt.save(buf)
        font_data = buf.getvalue()

        # Ensure FontDescriptor exists
        desc = font_obj.get("/FontDescriptor")
        if desc is None:
            desc = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/FontDescriptor"),
            }))
            font_obj[pikepdf.Name("/FontDescriptor")] = desc

        # Only set metrics that are MISSING from the existing descriptor.
        # Overwriting existing metrics causes text layout corruption because
        # the original metrics match the document's text positioning.
        def _set_if_missing(key, value):
            if key not in desc:
                desc[pikepdf.Name(key)] = value

        _set_if_missing("/FontName", font_obj.get(
            "/BaseFont", pikepdf.Name("/Unknown")))
        if "/Flags" not in desc:
            flags = 32  # Nonsymbolic
            if post and post.italicAngle != 0:
                flags |= 64  # Italic
            desc[pikepdf.Name("/Flags")] = flags
        _set_if_missing("/FontBBox", pikepdf.Array([
            int(head.xMin * scale),
            int(head.yMin * scale),
            int(head.xMax * scale),
            int(head.yMax * scale),
        ]))
        _set_if_missing("/ItalicAngle",
                         int(post.italicAngle) if post else 0)
        _set_if_missing("/Ascent", int(os2.sTypoAscender * scale))
        _set_if_missing("/Descent", int(os2.sTypoDescender * scale))
        _set_if_missing("/CapHeight", int(
            getattr(os2, "sCapHeight", 700) * scale))
        _set_if_missing("/StemV", 80)

        # Embed font data
        font_stream = pikepdf.Stream(pdf, font_data)
        font_stream[pikepdf.Name("/Length1")] = len(font_data)
        desc[pikepdf.Name("/FontFile2")] = font_stream

        # FontFile2 carries a TrueType program — the font's Subtype must
        # agree.  When the source font was Type1 (e.g. the PDF base-14
        # "Times-Roman" PostScript name), veraPDF rejects FontFile2 under
        # a /Type1 Subtype.  Flip Subtype to /TrueType so the embedded
        # program matches the declared font type.
        subtype = str(font_obj.get("/Subtype", ""))
        flipped_to_truetype = subtype == "/Type1"
        if flipped_to_truetype:
            font_obj[pikepdf.Name("/Subtype")] = pikepdf.Name("/TrueType")

        # Type1 base-14 fonts (Times-Roman, Helvetica, Courier, …) often
        # omit /FirstChar /LastChar /Widths because Adobe Reader hard-codes
        # the canonical widths for those fonts.  TrueType has no such
        # convention — without an explicit /Widths array veraPDF rule
        # 7.21.5 fires when the glyph widths in the embedded program don't
        # match the (missing/zero) dictionary widths.  Synthesize the
        # array from the embedded font's hmtx table.
        if flipped_to_truetype or "/Widths" not in font_obj:
            try:
                cmap = tt.getBestCmap() or {}
                hmtx = tt.get("hmtx")
                first_char = 0x20
                last_char = 0xFF
                widths_array = []
                for code in range(first_char, last_char + 1):
                    if code in _WIN1252_SPECIAL:
                        unicode_cp = _WIN1252_SPECIAL[code]
                    elif 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
                        unicode_cp = code
                    else:
                        unicode_cp = None
                    if unicode_cp is None or unicode_cp not in cmap:
                        widths_array.append(0)
                        continue
                    glyph_name = cmap[unicode_cp]
                    metric = hmtx.metrics.get(glyph_name) if hmtx else None
                    if metric is None:
                        widths_array.append(0)
                    else:
                        widths_array.append(int(round(metric[0] * scale)))
                font_obj[pikepdf.Name("/FirstChar")] = first_char
                font_obj[pikepdf.Name("/LastChar")] = last_char
                font_obj[pikepdf.Name("/Widths")] = pikepdf.Array(widths_array)
            except Exception as e:
                logger.debug("Widths synthesis failed for %s: %s", base_font, e)

    except Exception as e:
        logger.warning("Font embedding failed for %s: %s", base_font, e)
    finally:
        tt.close()


_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")


def _liberation_fallback(base_font: str):
    """Map ANY recognizable Latin text font to its metric-compatible
    Liberation face (``fonts-liberation`` ships on the Streamlit/Docker
    deploy).

    The explicit ``_FONT_FILE_NAMES`` table only keys the canonical
    PostScript names (``ArialMT``, ``Helvetica``, …).  A font named with
    a bare family (``Arial``) or comma-style weight (``Arial,Bold`` — how
    Excel/Office embeds spreadsheet tables) had no entry, so on a host
    without the real MS face it stayed *un-embedded* — the KEL348 Cotton
    page-11 "Font not embedded" PAC error.

    This classifier is family + style based so no common Latin font slips
    through:

      * serif  (Times / *Serif* / Georgia / Cambria)  -> LiberationSerif
      * mono   (Courier / *Mono* / Consolas)           -> LiberationMono
      * sans   (everything else Latin)                 -> LiberationSans

    Symbol / dingbat / wingding faces return ``None`` — substituting a
    Latin sans there would paint the wrong glyphs, so they fall through to
    the other ``_find_system_font`` strategies (or stay flagged).

    Returns a Liberation filename (e.g. ``LiberationSans-Bold.ttf``) or
    ``None``.
    """
    if not base_font:
        return None
    name = _SUBSET_PREFIX_RE.sub("", str(base_font))
    low = name.lower()

    # Symbol / pictographic faces have no Latin metric equivalent.
    if any(tok in low for tok in ("symbol", "dingbat", "wingding",
                                  "webding", "zapf")):
        return None

    if any(tok in low for tok in ("times", "serif", "georgia", "cambria",
                                  "roman", "minion", "garamond")):
        family = "Serif"
    elif any(tok in low for tok in ("courier", "mono", "consol")):
        family = "Mono"
    else:
        family = "Sans"

    bold = "bold" in low
    italic = "italic" in low or "oblique" in low
    if bold and italic:
        style = "-BoldItalic"
    elif bold:
        style = "-Bold"
    elif italic:
        style = "-Italic"
    else:
        style = "-Regular"
    return f"Liberation{family}{style}.ttf"


def _find_system_font(base_font: str):
    """Find a system font file matching the PDF BaseFont name.

    Uses multiple strategies:
    1. Exact match from known font name → filename map
    2. Direct filename match (BaseFont.ttf)
    3. Fuzzy match: strip style suffixes, try common variants
    4. Family/style Liberation fallback (Linux/Docker deploy)
    5. TTC (TrueType Collection) map for macOS system fonts
    6. fc-match on Linux

    Returns:
        str path for TTF files, or (str path, int index) tuple for TTC files,
        or None if not found.
    """
    candidates = list(_FONT_FILE_NAMES.get(base_font, []))
    # Also try the base font name directly
    candidates.append(base_font + ".ttf")
    candidates.append(base_font + ".TTF")

    # Try fuzzy variants: strip PS suffixes like MT, PS, PSMT
    clean = base_font
    for suffix in ("PSMT", "PSMt", "PS-BoldMT", "PS-ItalicMT", "PS-BoldItalicMT",
                   "-Roman", "MT", ",Regular"):
        clean = clean.replace(suffix, "")
    # Add space-separated variants (e.g. "TimesNewRoman" -> "Times New Roman")
    import re as _re
    spaced = _re.sub(r'([a-z])([A-Z])', r'\1 \2', clean)
    if spaced != clean:
        candidates.append(spaced + ".ttf")
        candidates.append(spaced + ".TTF")
    # Try with hyphen variants
    for sep in ("-", ","):
        if sep in base_font:
            family = base_font.split(sep)[0]
            candidates.append(family + ".ttf")
            candidates.append(family + ".TTF")

    # Family/style Liberation fallback — listed LAST so a real same-name
    # face (e.g. macOS Arial.ttf) still wins, but on a Liberation-only
    # Linux/Docker host a bare "Arial" / "Arial,Bold" still resolves.
    lib_fallback = _liberation_fallback(base_font)
    if lib_fallback:
        candidates.append(lib_fallback)

    for font_dir in _FONT_DIRS:
        if not os.path.isdir(font_dir):
            continue
        for candidate in candidates:
            path = os.path.join(font_dir, candidate)
            if os.path.isfile(path):
                return path

    # Check TTC (TrueType Collection) map
    if base_font in _FONT_TTC_MAP:
        ttc_path, ttc_index = _FONT_TTC_MAP[base_font]
        if os.path.isfile(ttc_path):
            return (ttc_path, ttc_index)

    # Scan font dirs for case-insensitive partial match as last resort
    clean_lower = clean.lower().replace(" ", "")
    for font_dir in _FONT_DIRS:
        if not os.path.isdir(font_dir):
            continue
        try:
            for fname in os.listdir(font_dir):
                if not fname.lower().endswith((".ttf", ".otf")):
                    continue
                if clean_lower in fname.lower().replace(" ", ""):
                    return os.path.join(font_dir, fname)
        except OSError:
            continue

    # Fallback: try fc-match on Linux
    if sys.platform.startswith("linux"):
        try:
            import subprocess
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", base_font],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                path = result.stdout.strip()
                if os.path.isfile(path):
                    return path
        except Exception as e:
            logger.debug("fc-match failed for '%s': %s", base_font, e)

    return None
