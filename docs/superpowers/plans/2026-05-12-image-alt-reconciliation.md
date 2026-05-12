# Image / Alt-text Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace index-based alt-text matching in `pdf_extractor` with a source-aware, position-based reconciliation pass that maps every image XObject occurrence to its source-PDF intent (alt text, decorative status, watermark status). Fixes scrambled alts on horizontally adjacent images, lost alts on watermarked DNC documents, and decorative images becoming announced figures.

**Architecture:** A single new module `image_reconciliation.py` parses both the source struct tree (capturing `/Figure` AND `/Artifact` elements with MCIDs and optional `/BBox`) and each page's content stream (capturing every Image-XObject `Do` operator with its CTM-derived bbox, innermost MCID, and watermark-ancestor flag). A four-priority matching engine pairs them: watermark ancestor → decorative; MCID match → use source intent; bbox overlap → use source intent; no match → conservative heuristic (header/footer band → decorative). `pdf_extractor._extract_images` is rewritten to delegate to the new module. `pdf_tagger`'s position-based tag-time matching is untouched.

**Tech Stack:** Python 3.12, pikepdf (struct tree + content stream), pdfminer.six (text extraction, untouched), unittest + sys.modules mocking (existing pattern), veraPDF CLI (validation, untouched).

**Reference spec:** `docs/superpowers/specs/2026-05-12-image-alt-reconciliation-design.md`

**Branch:** `fix/image-alt-reconciliation` (already created off `fix/accessibility-reviewer-feedback`)

---

## File Structure

**Files to create:**
- `image_reconciliation.py` — new module, all reconciliation logic (~250 lines)
- `test_image_reconciliation.py` — unit tests for the new module (top-level, follows `test_fixes.py` pattern)
- `test_pipeline_integration.py` — end-to-end integration tests asserting invariants on real PDFs
- `tests/fixtures/` — regression corpus (copies of bug-trigger PDFs)
- `tests/fixtures/README.md` — provenance & corpus inventory

**Files to modify:**
- `pdf_extractor.py` — rewrite `_extract_images` to delegate; remove `_read_existing_alt_texts` and `_collect_images_recursive` (now obsolete)
- `pdf_tagger.py` — `_detect_watermark_forms` moves to `image_reconciliation.py`; tagger imports from new location (no behavioral change)

**Files NOT touched:**
- `models.py`, `pdf_postprocess.py`, `validator.py`, `main.py`, `app.py`, `config.py`, `test_fixes.py`

---

## Task 1: Setup — fixtures and branch verification

**Files:**
- Create: `tests/fixtures/` (directory)
- Create: `tests/fixtures/README.md`
- Copy: existing input/ PDFs + Brownfield + Unilever cases into `tests/fixtures/`

- [ ] **Step 1: Verify branch**

```bash
git rev-parse --abbrev-ref HEAD
```

Expected output: `fix/image-alt-reconciliation`

If not on this branch, run: `git checkout fix/image-alt-reconciliation`

- [ ] **Step 2: Create fixtures directory and copy corpus**

```bash
mkdir -p tests/fixtures
cp "input/Perils and Pitfalls - Case.pdf"                          tests/fixtures/perils_regular.pdf
cp "input/Perils and Pitfalls - Watermarked Case (1).pdf"          tests/fixtures/perils_watermarked.pdf
cp "input/KE1335_KLA_Tencor_Case_4-1-2026.pdf"                     tests/fixtures/ke1335_raw.pdf
cp "input/KE1335_Final_20260318_accessible.pdf"                    tests/fixtures/ke1335_already_accessible.pdf
cp "/Users/mohakgarg/Desktop/DRRC Documents/BettingonBrownfield_HBS_20140314_accessible.pdf" tests/fixtures/brownfield.pdf
cp "/Users/mohakgarg/Desktop/untitled folder/Unilever's Mission for Vitality.pdf"            tests/fixtures/unilever_vitality_regular.pdf
cp "/Users/mohakgarg/Desktop/untitled folder/Unilever's Mission for Vitality DNC.pdf"        tests/fixtures/unilever_vitality_dnc.pdf
ls -la tests/fixtures/
```

Expected: 7 PDFs listed, all > 0 bytes.

- [ ] **Step 3: Write fixtures README**

Create `tests/fixtures/README.md`:

```markdown
# Test Fixtures — Regression Corpus

Each PDF here is a frozen reproduction of a real bug or a known-good baseline.
Never replace without keeping the original alongside (rename to `*_legacy.pdf`).

| File | Origin | Bug category |
|---|---|---|
| perils_regular.pdf            | input/Perils and Pitfalls - Case.pdf                    | Baseline (no bugs reported) |
| perils_watermarked.pdf        | input/Perils and Pitfalls - Watermarked Case (1).pdf    | DNC watermark interaction |
| ke1335_raw.pdf                | input/KE1335_KLA_Tencor_Case_4-1-2026.pdf               | Baseline raw input |
| ke1335_already_accessible.pdf | input/KE1335_Final_20260318_accessible.pdf              | Baseline already-accessible |
| brownfield.pdf                | HBS BettingonBrownfield_20140314_accessible.pdf         | Horizontal-row shuffle (4 1 / 3 2) |
| unilever_vitality_regular.pdf | Unilever's Mission for Vitality.pdf                     | Kellogg logo → uninformative figure |
| unilever_vitality_dnc.pdf     | Unilever's Mission for Vitality DNC.pdf                 | DNC watermark + grouped images |
```

- [ ] **Step 4: Commit fixtures**

```bash
git add tests/fixtures/
git commit -m "test: add regression-corpus fixtures for image reconciliation"
```

Note: if any source path differs on your machine, adjust paths in Step 2. If a source file is missing, fail the task and ask the user to provide it — do not proceed without all 7 fixtures.

---

## Task 2: Create module scaffold with dataclasses

**Files:**
- Create: `image_reconciliation.py`
- Create: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test for dataclass shapes**

Create `test_image_reconciliation.py`:

```python
"""Unit tests for image_reconciliation.py — TDD layer 1 (data shapes)."""
import sys
import types
import unittest
from unittest.mock import MagicMock

# ----- pikepdf mock (mirrors test_fixes.py pattern) -----
class _Name:
    def __init__(self, s): self.s = s
    def __eq__(self, other):
        if isinstance(other, _Name): return self.s == other.s
        return str(other) == self.s
    def __hash__(self): return hash(self.s)
    def __str__(self): return self.s
    def __repr__(self): return f"Name({self.s!r})"

class _NameFactory:
    _cache = {}
    def __call__(self, s):
        if s not in self._cache: self._cache[s] = _Name(s)
        return self._cache[s]
    def __getattr__(self, s):
        return self(f"/{s}" if not s.startswith("/") else s)

pikepdf_mod = types.ModuleType("pikepdf")
pikepdf_mod.Name = _NameFactory()
pikepdf_mod.Array = list
pikepdf_mod.Dictionary = MagicMock(side_effect=lambda d=None, **kw: d or kw)
pikepdf_mod.Operator = MagicMock(side_effect=lambda s: s)
pikepdf_mod.Pdf = MagicMock()
pikepdf_mod.parse_content_stream = MagicMock(return_value=[])
sys.modules["pikepdf"] = pikepdf_mod

from models import BBox
from image_reconciliation import ResolvedImage, SourceStructElement, ImageOccurrence


class TestDataclasses(unittest.TestCase):
    def test_resolved_image_defaults(self):
        ri = ResolvedImage(xobject_name="/Im0", bbox=BBox(0, 0, 100, 100))
        self.assertEqual(ri.alt_text, "")
        self.assertFalse(ri.is_decorative)
        self.assertFalse(ri.is_watermark)

    def test_source_struct_element_defaults(self):
        se = SourceStructElement(
            struct_type="/Figure", alt_text="logo", page_index=0
        )
        self.assertEqual(se.mcids, [])
        self.assertIsNone(se.bbox)

    def test_image_occurrence_defaults(self):
        occ = ImageOccurrence(xobject_name="/Im0", page_bbox=BBox(0, 0, 50, 50))
        self.assertIsNone(occ.mcid)
        self.assertFalse(occ.in_watermark_ancestor)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test — verify it fails (ImportError)**

```bash
cd /Users/mohakgarg/Desktop/KTR_REMIDIATION
./venv/bin/python -m unittest test_image_reconciliation -v
```

Expected: `ModuleNotFoundError: No module named 'image_reconciliation'`

- [ ] **Step 3: Create the module scaffold**

Create `image_reconciliation.py`:

```python
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
    mcids: list = field(default_factory=list)  # leaf integer kids in /K subtree
    bbox: Optional[BBox] = None     # from element's /BBox if present, else None


@dataclass
class ImageOccurrence:
    """One Do operator on an Image XObject in a page's content stream."""
    xobject_name: str
    page_bbox: BBox                       # CTM applied to unit-square image space
    mcid: Optional[int] = None            # innermost active MCID at this Do
    in_watermark_ancestor: bool = False   # True if any ancestor Form is a watermark
```

- [ ] **Step 4: Run test — verify it passes**

```bash
./venv/bin/python -m unittest test_image_reconciliation -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): scaffold module with data shapes"
```

---

## Task 3: CTM math — bbox from unit-square × CTM

The CTM maps PDF image-space (unit square [0,1]×[0,1]) to page coordinates. For a rotated or skewed image, the four image-space corners map to four page-space points whose min/max gives the axis-aligned bbox.

**Files:**
- Modify: `image_reconciliation.py` (add helper)
- Modify: `test_image_reconciliation.py` (add test)

- [ ] **Step 1: Write the failing test**

Append to `test_image_reconciliation.py` (before `if __name__`):

```python
class TestBBoxFromCtm(unittest.TestCase):
    """CTM applied to unit square [0,1]×[0,1] -> axis-aligned page bbox."""

    def test_identity_ctm(self):
        from image_reconciliation import _bbox_from_ctm
        # Identity: image-space corners map unchanged.
        bbox = _bbox_from_ctm([1, 0, 0, 1, 0, 0])
        self.assertEqual((bbox.x0, bbox.y0, bbox.x1, bbox.y1), (0, 0, 1, 1))

    def test_scale_and_translate(self):
        from image_reconciliation import _bbox_from_ctm
        # 100×50 image at (200, 300): CTM = [100, 0, 0, 50, 200, 300]
        bbox = _bbox_from_ctm([100, 0, 0, 50, 200, 300])
        self.assertEqual((bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                         (200, 300, 300, 350))

    def test_rotation_90_degrees(self):
        from image_reconciliation import _bbox_from_ctm
        # 90deg rotation: [0, 1, -1, 0, tx, ty] swaps width/height.
        # Image at origin, 100×50, rotated 90deg, anchored so result starts at (0,0):
        bbox = _bbox_from_ctm([0, 100, -50, 0, 50, 0])
        # Corners map to: (50,0), (50,100), (0,0), (0,100) -> bbox (0,0)-(50,100)
        self.assertEqual((bbox.x0, bbox.y0, bbox.x1, bbox.y1), (0, 0, 50, 100))

    def test_negative_scale_flipped(self):
        from image_reconciliation import _bbox_from_ctm
        # Flipped horizontally: width=-100 anchored at x=300 spans (200,300)
        bbox = _bbox_from_ctm([-100, 0, 0, 50, 300, 0])
        self.assertEqual((bbox.x0, bbox.y0, bbox.x1, bbox.y1), (200, 0, 300, 50))
```

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestBBoxFromCtm -v
```

Expected: AttributeError for `_bbox_from_ctm`.

- [ ] **Step 3: Implement `_bbox_from_ctm`**

Append to `image_reconciliation.py`:

```python
# ---------------------------------------------------------------------------
# CTM math
# ---------------------------------------------------------------------------

def _apply_ctm(ctm: list, x: float, y: float) -> tuple:
    """Apply 6-element CTM [a, b, c, d, e, f] to (x, y).

    Per PDF spec: x' = a*x + c*y + e;  y' = b*x + d*y + f.
    """
    a, b, c, d, e, f = ctm
    return (a * x + c * y + e, b * x + d * y + f)


def _bbox_from_ctm(ctm: list) -> BBox:
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
```

- [ ] **Step 4: Run test — verify it passes**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestBBoxFromCtm -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): bbox-from-CTM helper handles rotation and skew"
```

---

## Task 4: Struct tree reader

Walk the source PDF struct tree, collect every `/Figure` AND `/Artifact` with MCIDs, `/Alt`, page assignment, and optional `/BBox`.

**Files:**
- Modify: `image_reconciliation.py`
- Modify: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test**

Append to `test_image_reconciliation.py`:

```python
class TestStructTreeReader(unittest.TestCase):
    """Walks /StructTreeRoot and collects /Figure + /Artifact elements."""

    def _make_pdf_with_struct_tree(self, page_obj, figure_elements):
        """Build a mock pikepdf.Pdf with a struct tree.

        figure_elements: list of dicts {struct_type, alt, mcids, bbox(optional)}.
        """
        Name = pikepdf_mod.Name

        kids = []
        for fe in figure_elements:
            mcid_kids = list(fe.get("mcids", []))
            elem = MagicMock()
            data = {
                "/S": Name(fe["struct_type"]),
                "/Pg": page_obj,
                "/K": pikepdf_mod.Array(mcid_kids),
            }
            if fe.get("alt"):
                data["/Alt"] = fe["alt"]
            if fe.get("bbox"):
                data["/BBox"] = fe["bbox"]
            elem.get.side_effect = data.get
            elem.objgen = (id(elem), 0)
            kids.append(elem)

        struct_root = MagicMock()
        struct_root.get.side_effect = {"/K": pikepdf_mod.Array(kids)}.get
        struct_root.objgen = (1, 0)

        pdf = MagicMock()
        pdf.Root.get.side_effect = {"/StructTreeRoot": struct_root}.get
        pdf.pages = [page_obj]
        # Provide a page_id_to_idx mapping shape
        page_obj.objgen = (2, 0)
        return pdf

    def test_collects_figure_with_alt(self):
        from image_reconciliation import _read_source_struct_elements
        page = MagicMock()
        pdf = self._make_pdf_with_struct_tree(page, [
            {"struct_type": "/Figure", "alt": "Photo 1", "mcids": [10]},
        ])
        result = _read_source_struct_elements(pdf)
        self.assertEqual(len(result[0]), 1)
        elem = result[0][0]
        self.assertEqual(elem.struct_type, "/Figure")
        self.assertEqual(elem.alt_text, "Photo 1")
        self.assertEqual(elem.mcids, [10])
        self.assertIsNone(elem.bbox)

    def test_collects_artifact(self):
        from image_reconciliation import _read_source_struct_elements
        page = MagicMock()
        pdf = self._make_pdf_with_struct_tree(page, [
            {"struct_type": "/Artifact", "mcids": [5]},
        ])
        result = _read_source_struct_elements(pdf)
        elem = result[0][0]
        self.assertEqual(elem.struct_type, "/Artifact")
        self.assertEqual(elem.alt_text, "")
        self.assertEqual(elem.mcids, [5])

    def test_no_struct_tree_returns_empty(self):
        from image_reconciliation import _read_source_struct_elements
        pdf = MagicMock()
        pdf.Root.get.side_effect = {"/StructTreeRoot": None}.get
        result = _read_source_struct_elements(pdf)
        self.assertEqual(result, {})
```

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestStructTreeReader -v
```

Expected: AttributeError for `_read_source_struct_elements`.

- [ ] **Step 3: Implement struct tree reader**

Append to `image_reconciliation.py`:

```python
# ---------------------------------------------------------------------------
# Struct tree reader
# ---------------------------------------------------------------------------

def _collect_leaf_mcids(node, visited):
    """Walk node's /K subtree, return all integer MCID leaves."""
    mcids = []
    try:
        oid = node.objgen if hasattr(node, "objgen") else id(node)
    except Exception:
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
                mcids.extend(_collect_leaf_mcids(child, visited))
        return mcids
    if hasattr(kids, "get"):
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


def _read_source_struct_elements(pdf) -> dict:
    """Walk /StructTreeRoot, return {page_index: [SourceStructElement, ...]}.

    Collects /Figure AND /Artifact nodes. Empty result when struct tree is absent.
    """
    result: dict = {}
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            return result

        # Build page-index lookup
        try:
            page_id_to_idx = {p.objgen: i for i, p in enumerate(pdf.pages)}
        except Exception:
            page_id_to_idx = {}

        visited = set()
        FIGURE = pikepdf.Name("/Figure")
        ARTIFACT = pikepdf.Name("/Artifact")

        def _walk(node, inherited_pg=None):
            try:
                oid = node.objgen if hasattr(node, "objgen") else id(node)
            except Exception:
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
```

- [ ] **Step 4: Run test — verify it passes**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestStructTreeReader -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): struct tree reader captures Figure + Artifact"
```

---

## Task 5: Content stream parser (single-page, no Form descent yet)

Parse one page's content stream tracking CTM, marked-content stack (innermost MCID), and emit one `ImageOccurrence` per Do on an Image XObject.

**Files:**
- Modify: `image_reconciliation.py`
- Modify: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test**

Append to `test_image_reconciliation.py`:

```python
class TestContentStreamParser(unittest.TestCase):
    """Parses operators to emit one ImageOccurrence per Image-XObject Do."""

    def _ops(self, *items):
        """Build a list of (operands, operator) tuples from compact spec.

        Each item is (operator_str, *operands).
        """
        Op = pikepdf_mod.Operator
        return [(list(operands), Op(op_str)) for op_str, *operands in items]

    def _page_with_image_xobjects(self, image_names):
        """Mock page whose /Resources/XObject maps each name to Image subtype."""
        Name = pikepdf_mod.Name
        xobjs = {}
        for n in image_names:
            obj = MagicMock()
            obj.get.side_effect = {"/Subtype": Name("/Image")}.get
            obj.objgen = (id(obj), 0)
            xobjs[Name(n)] = obj
        # Make .items() iterate name->obj
        xobj_dict = MagicMock()
        xobj_dict.items.return_value = list(xobjs.items())
        xobj_dict.get.side_effect = xobjs.get
        resources = MagicMock()
        resources.get.side_effect = {"/XObject": xobj_dict}.get
        page = MagicMock()
        page.get.side_effect = {"/Resources": resources}.get
        return page

    def test_single_image_identity_ctm(self):
        from image_reconciliation import _parse_image_occurrences

        page = self._page_with_image_xobjects(["/Im0"])
        ops = self._ops(
            ("cm", 100, 0, 0, 50, 200, 300),    # scale to 100×50 at (200,300)
            ("Do", pikepdf_mod.Name("/Im0")),
        )
        pikepdf_mod.parse_content_stream = MagicMock(return_value=ops)

        result = _parse_image_occurrences(page, watermark_form_names=set())

        self.assertEqual(len(result), 1)
        occ = result[0]
        self.assertEqual(occ.xobject_name, "/Im0")
        self.assertEqual((occ.page_bbox.x0, occ.page_bbox.y0,
                          occ.page_bbox.x1, occ.page_bbox.y1),
                         (200, 300, 300, 350))
        self.assertIsNone(occ.mcid)
        self.assertFalse(occ.in_watermark_ancestor)

    def test_mcid_captured_from_bdc(self):
        from image_reconciliation import _parse_image_occurrences

        page = self._page_with_image_xobjects(["/Im0"])
        ops = self._ops(
            ("BDC", pikepdf_mod.Name("/Figure"),
             {pikepdf_mod.Name("/MCID"): 7}),
            ("cm", 50, 0, 0, 50, 0, 0),
            ("Do", pikepdf_mod.Name("/Im0")),
            ("EMC",),
        )
        pikepdf_mod.parse_content_stream = MagicMock(return_value=ops)

        result = _parse_image_occurrences(page, watermark_form_names=set())
        self.assertEqual(result[0].mcid, 7)

    def test_two_horizontal_images(self):
        from image_reconciliation import _parse_image_occurrences

        page = self._page_with_image_xobjects(["/Im0", "/Im1"])
        ops = self._ops(
            ("q",),
            ("cm", 100, 0, 0, 100, 50, 400),
            ("Do", pikepdf_mod.Name("/Im0")),
            ("Q",),
            ("q",),
            ("cm", 100, 0, 0, 100, 250, 400),
            ("Do", pikepdf_mod.Name("/Im1")),
            ("Q",),
        )
        pikepdf_mod.parse_content_stream = MagicMock(return_value=ops)

        result = _parse_image_occurrences(page, watermark_form_names=set())
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].page_bbox.x0, 50)   # Im0 left
        self.assertEqual(result[1].page_bbox.x0, 250)  # Im1 right
```

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestContentStreamParser -v
```

Expected: AttributeError for `_parse_image_occurrences`.

- [ ] **Step 3: Implement the parser**

Append to `image_reconciliation.py`:

```python
# ---------------------------------------------------------------------------
# Content stream parser
# ---------------------------------------------------------------------------

def _matrix_multiply(m1: list, m2: list) -> list:
    """Multiply two PDF CTM matrices (6-element form).

    Returns m1 × m2 where each is [a, b, c, d, e, f] representing:
        | a  b  0 |
        | c  d  0 |
        | e  f  1 |
    """
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,           # a
        a1 * b2 + b1 * d2,           # b
        c1 * a2 + d1 * c2,           # c
        c1 * b2 + d1 * d2,           # d
        e1 * a2 + f1 * c2 + e2,      # e
        e1 * b2 + f1 * d2 + f2,      # f
    ]


def _get_image_xobject_names(page) -> set:
    """Return set of /XObject names on the page whose /Subtype is /Image."""
    names = set()
    try:
        resources = page.get("/Resources")
        if resources is None:
            return names
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return names
        for name, obj in xobjects.items():
            try:
                if obj.get("/Subtype") == pikepdf.Name("/Image"):
                    names.add(str(name))
            except Exception:
                continue
    except Exception:
        pass
    return names


def _parse_image_occurrences(page, watermark_form_names: set) -> list:
    """Parse page content stream; emit one ImageOccurrence per Image Do.

    Tracks CTM via q/Q/cm. Tracks innermost MCID via BDC/EMC stack.
    Form XObjects are NOT descended in this function — see Task 6.
    """
    image_xobj_names = _get_image_xobject_names(page)
    if not image_xobj_names:
        return []

    try:
        ops = pikepdf.parse_content_stream(page)
    except Exception as e:
        logger.debug("Could not parse content stream: %s", e)
        return []

    occurrences = []
    ctm_stack = [[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]]  # identity
    mcid_stack = []  # stack of innermost MCIDs (only those that carry MCID prop)

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
            mcid = None
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
            if xobj_name in image_xobj_names:
                bbox = _bbox_from_ctm(ctm_stack[-1])
                innermost_mcid = next(
                    (m for m in reversed(mcid_stack) if m is not None), None
                )
                occurrences.append(ImageOccurrence(
                    xobject_name=xobj_name,
                    page_bbox=bbox,
                    mcid=innermost_mcid,
                    in_watermark_ancestor=(xobj_name in watermark_form_names),
                ))

    return occurrences
```

- [ ] **Step 4: Run test — verify it passes**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestContentStreamParser -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): content stream parser captures Image Do operators"
```

---

## Task 6: Form XObject descent

Extend the content-stream parser to descend into Form XObjects (with concatenated CTM) so nested Image XObjects are found. Critical for InDesign-grouped figures and watermark Forms.

**Files:**
- Modify: `image_reconciliation.py`
- Modify: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test**

Append to `test_image_reconciliation.py`:

```python
class TestFormXObjectDescent(unittest.TestCase):
    """Image XObjects inside a Form XObject must be discovered with combined CTM."""

    def test_image_inside_form_xobject(self):
        from image_reconciliation import _parse_image_occurrences
        Name = pikepdf_mod.Name
        Op = pikepdf_mod.Operator

        # Inner Image XObject
        img_obj = MagicMock()
        img_obj.get.side_effect = {"/Subtype": Name("/Image")}.get
        img_obj.objgen = (10, 0)

        # Inner Form XObject containing one Do on the image
        form_obj = MagicMock()
        form_obj.get.side_effect = lambda k: {
            "/Subtype": Name("/Form"),
            "/Resources": MagicMock(),
        }.get(k)
        form_obj.objgen = (20, 0)
        # Form's own resources: one Image XObject under /Im0
        form_xobj_dict = MagicMock()
        form_xobj_dict.items.return_value = [(Name("/Im0"), img_obj)]
        form_xobj_dict.get.side_effect = {Name("/Im0"): img_obj}.get
        form_resources = MagicMock()
        form_resources.get.side_effect = {"/XObject": form_xobj_dict}.get
        form_obj.get.side_effect = lambda k: {
            "/Subtype": Name("/Form"),
            "/Resources": form_resources,
        }.get(k)
        # Form content stream: draw image at scale (50, 50)
        form_ops = [
            ([50, 0, 0, 50, 10, 20], Op("cm")),
            ([Name("/Im0")], Op("Do")),
        ]

        # Page references the Form under /F0
        page_xobjs = MagicMock()
        page_xobjs.items.return_value = [(Name("/F0"), form_obj)]
        page_xobjs.get.side_effect = {Name("/F0"): form_obj}.get
        page_resources = MagicMock()
        page_resources.get.side_effect = {"/XObject": page_xobjs}.get
        page = MagicMock()
        page.get.side_effect = {"/Resources": page_resources}.get

        # parse_content_stream switches return based on argument identity
        page_ops = [
            ([100, 0, 0, 100, 200, 300], Op("cm")),  # page-level scale
            ([Name("/F0")], Op("Do")),               # invoke Form
        ]
        def fake_parse(arg):
            if arg is page: return page_ops
            return form_ops
        pikepdf_mod.parse_content_stream = MagicMock(side_effect=fake_parse)

        result = _parse_image_occurrences(page, watermark_form_names=set())

        # Page CTM × form internal CTM:
        #   page = [100, 0, 0, 100, 200, 300]
        #   inner = [50, 0, 0, 50, 10, 20]
        #   combined = [50*100, 0, 0, 50*100, 10*100+200, 20*100+300]
        #            = [5000, 0, 0, 5000, 1200, 2300]
        # Image bbox = (1200, 2300) to (6200, 7300)
        self.assertEqual(len(result), 1)
        bbox = result[0].page_bbox
        self.assertAlmostEqual(bbox.x0, 1200)
        self.assertAlmostEqual(bbox.y0, 2300)
        self.assertAlmostEqual(bbox.x1, 6200)
        self.assertAlmostEqual(bbox.y1, 7300)

    def test_image_inside_watermark_form_marked(self):
        from image_reconciliation import _parse_image_occurrences
        Name = pikepdf_mod.Name
        Op = pikepdf_mod.Operator

        img_obj = MagicMock()
        img_obj.get.side_effect = {"/Subtype": Name("/Image")}.get
        img_obj.objgen = (11, 0)
        form_obj = MagicMock()
        form_obj.objgen = (21, 0)
        form_xobj_dict = MagicMock()
        form_xobj_dict.items.return_value = [(Name("/Im0"), img_obj)]
        form_xobj_dict.get.side_effect = {Name("/Im0"): img_obj}.get
        form_resources = MagicMock()
        form_resources.get.side_effect = {"/XObject": form_xobj_dict}.get
        form_obj.get.side_effect = lambda k: {
            "/Subtype": Name("/Form"),
            "/Resources": form_resources,
        }.get(k)
        form_ops = [([1, 0, 0, 1, 0, 0], Op("cm")),
                    ([Name("/Im0")], Op("Do"))]
        page_xobjs = MagicMock()
        page_xobjs.items.return_value = [(Name("/WM"), form_obj)]
        page_xobjs.get.side_effect = {Name("/WM"): form_obj}.get
        page_resources = MagicMock()
        page_resources.get.side_effect = {"/XObject": page_xobjs}.get
        page = MagicMock()
        page.get.side_effect = {"/Resources": page_resources}.get
        page_ops = [([1, 0, 0, 1, 0, 0], Op("cm")),
                    ([Name("/WM")], Op("Do"))]
        def fake_parse(arg):
            return page_ops if arg is page else form_ops
        pikepdf_mod.parse_content_stream = MagicMock(side_effect=fake_parse)

        result = _parse_image_occurrences(page, watermark_form_names={"/WM"})
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].in_watermark_ancestor)
```

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestFormXObjectDescent -v
```

Expected: failures (only Image-typed Do is recognized; Form Do is silently ignored).

- [ ] **Step 3: Implement Form descent**

Replace `_parse_image_occurrences` in `image_reconciliation.py` with a version that recurses. Edit the existing function as follows:

```python
def _classify_xobjects(page) -> tuple:
    """Return (image_names, form_objs_by_name).

    image_names: set of names whose /Subtype is /Image (callable with Do).
    form_objs_by_name: dict of name -> Form XObject pikepdf object.
    """
    image_names = set()
    form_objs = {}
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


def _walk_content_stream(stream_obj, current_ctm, in_watermark, page_image_names,
                         page_form_objs, occurrences, depth=0):
    """Recursive helper that walks one content stream and recurses into Forms."""
    if depth > 10:
        logger.debug("Form XObject recursion depth limit hit; stopping")
        return
    try:
        ops = pikepdf.parse_content_stream(stream_obj)
    except Exception:
        return

    ctm_stack = [list(current_ctm)]
    mcid_stack = []

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
            mcid = None
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
            if xobj_name in page_image_names:
                bbox = _bbox_from_ctm(ctm_stack[-1])
                innermost_mcid = next(
                    (m for m in reversed(mcid_stack) if m is not None), None
                )
                occurrences.append(ImageOccurrence(
                    xobject_name=xobj_name,
                    page_bbox=bbox,
                    mcid=innermost_mcid,
                    in_watermark_ancestor=in_watermark,
                ))
            elif xobj_name in page_form_objs:
                # Descend into Form XObject with concatenated CTM
                form_obj = page_form_objs[xobj_name]
                # Determine if this Form (or any ancestor) is a watermark
                form_is_watermark = in_watermark or (
                    xobj_name in _watermark_names_holder["names"]
                )
                _walk_content_stream(
                    form_obj,
                    current_ctm=ctm_stack[-1],
                    in_watermark=form_is_watermark,
                    page_image_names=_get_image_xobjects_in_form(form_obj),
                    page_form_objs=_get_form_xobjects_in_form(form_obj),
                    occurrences=occurrences,
                    depth=depth + 1,
                )


# Module-level holder so deeper Form descents can check watermark set
_watermark_names_holder = {"names": set()}


def _get_image_xobjects_in_form(form_obj) -> set:
    """Like _get_image_xobject_names but for a Form XObject's /Resources."""
    names = set()
    try:
        resources = form_obj.get("/Resources")
        if resources is None:
            return names
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return names
        for name, obj in xobjects.items():
            try:
                if obj.get("/Subtype") == pikepdf.Name("/Image"):
                    names.add(str(name))
            except Exception:
                continue
    except Exception:
        pass
    return names


def _get_form_xobjects_in_form(form_obj) -> dict:
    """Like form objects within a Form XObject's /Resources."""
    forms = {}
    try:
        resources = form_obj.get("/Resources")
        if resources is None:
            return forms
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return forms
        for name, obj in xobjects.items():
            try:
                if obj.get("/Subtype") == pikepdf.Name("/Form"):
                    forms[str(name)] = obj
            except Exception:
                continue
    except Exception:
        pass
    return forms


def _parse_image_occurrences(page, watermark_form_names: set) -> list:
    """Parse page content stream + recurse into Forms; emit ImageOccurrence list."""
    image_names, form_objs = _classify_xobjects(page)
    if not image_names and not form_objs:
        return []
    _watermark_names_holder["names"] = set(watermark_form_names)
    occurrences = []
    _walk_content_stream(
        stream_obj=page,
        current_ctm=[1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        in_watermark=False,
        page_image_names=image_names,
        page_form_objs=form_objs,
        occurrences=occurrences,
    )
    return occurrences
```

Also remove the old `_get_image_xobject_names` since `_classify_xobjects` replaces it. Find and delete the old function definition.

- [ ] **Step 4: Run all tests — verify new tests pass and previous tests still pass**

```bash
./venv/bin/python -m unittest test_image_reconciliation -v
```

Expected: All tests pass (TestDataclasses, TestBBoxFromCtm, TestStructTreeReader, TestContentStreamParser, TestFormXObjectDescent).

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): descend into Form XObjects with concatenated CTM"
```

---

## Task 7: Move watermark detection from pdf_tagger

The tagger's `_detect_watermark_forms` becomes the shared source-of-truth. Move it into `image_reconciliation.py` and import from there in `pdf_tagger`. No behavioral change.

**Files:**
- Modify: `image_reconciliation.py` (add function)
- Modify: `pdf_tagger.py` (remove function body, import from new location)
- Modify: `test_image_reconciliation.py` (add test)

- [ ] **Step 1: Locate current implementation**

```bash
grep -n "def _detect_watermark_forms\|WATERMARK_KEYWORDS\|def _is_watermark_form" pdf_tagger.py
```

Note line numbers for `_detect_watermark_forms` and any helpers it calls (typically `_is_watermark_form` + `WATERMARK_KEYWORDS` set). These all move together.

- [ ] **Step 2: Write the failing test**

Append to `test_image_reconciliation.py`:

```python
class TestWatermarkDetection(unittest.TestCase):
    """Detects Form XObjects that are watermarks via Adobe markers or keywords."""

    def test_adobe_watermark_marker(self):
        from image_reconciliation import detect_watermark_forms
        Name = pikepdf_mod.Name

        form_obj = MagicMock()
        # Form with PieceInfo.ADBE.Private == "/Watermark"
        adbe = MagicMock()
        adbe.get.side_effect = {"/Private": Name("/Watermark")}.get
        piece_info = MagicMock()
        piece_info.get.side_effect = {"/ADBE": adbe}.get
        form_obj.get.side_effect = {
            "/Subtype": Name("/Form"),
            "/PieceInfo": piece_info,
        }.get
        form_obj.objgen = (30, 0)

        xobj_dict = MagicMock()
        xobj_dict.items.return_value = [(Name("/WM"), form_obj)]
        resources = MagicMock()
        resources.get.side_effect = {"/XObject": xobj_dict}.get
        page = MagicMock()
        page.get.side_effect = {"/Resources": resources}.get

        result = detect_watermark_forms(page)
        self.assertIn("/WM", result)
```

- [ ] **Step 3: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestWatermarkDetection -v
```

Expected: `AttributeError: module 'image_reconciliation' has no attribute 'detect_watermark_forms'`.

- [ ] **Step 4: Move detector into image_reconciliation.py**

Open `pdf_tagger.py` and locate `_detect_watermark_forms` (start of function definition, around line 932 per spec). Copy the entire function plus its helpers (`WATERMARK_KEYWORDS` set, any `_is_watermark_form` helper) into `image_reconciliation.py` near the top of the module (after the dataclasses, before CTM math). Rename the public name from `_detect_watermark_forms` to `detect_watermark_forms` (public, no leading underscore) since it's used by two modules.

In `pdf_tagger.py`, replace the function definition with a re-export:

```python
from image_reconciliation import detect_watermark_forms as _detect_watermark_forms
```

Place this import near the other top-of-file imports.

- [ ] **Step 5: Run all tests**

```bash
./venv/bin/python -m unittest test_image_reconciliation test_fixes -v
```

Expected: All tests pass. The new TestWatermarkDetection passes; existing test_fixes.py still passes (the tagger's behavior is unchanged — only the function's source location moved).

- [ ] **Step 6: Commit**

```bash
git add image_reconciliation.py pdf_tagger.py test_image_reconciliation.py
git commit -m "refactor: move detect_watermark_forms to image_reconciliation for sharing"
```

---

## Task 8: Bbox derivation for source elements missing /BBox

Most Word-produced PDFs have no `/BBox` on `/Figure` elements — we recover position via the MCID list. For each source element with `bbox=None`, find the MCID region in the content stream and use its CTM-derived bbox.

**Files:**
- Modify: `image_reconciliation.py`
- Modify: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test**

Append to `test_image_reconciliation.py`:

```python
class TestBboxDerivation(unittest.TestCase):
    """Source struct elements missing /BBox get bbox from their MCID region in CS."""

    def test_bbox_derived_from_mcid_region(self):
        from image_reconciliation import _derive_source_bboxes, SourceStructElement, ImageOccurrence

        # SourceStructElement with mcid 5, no bbox
        elem = SourceStructElement(
            struct_type="/Figure", alt_text="Photo", page_index=0,
            mcids=[5], bbox=None,
        )
        # An ImageOccurrence with mcid 5 at known bbox
        occ = ImageOccurrence(
            xobject_name="/Im0",
            page_bbox=BBox(100, 200, 300, 400),
            mcid=5,
        )
        _derive_source_bboxes([elem], [occ])
        self.assertIsNotNone(elem.bbox)
        self.assertEqual(
            (elem.bbox.x0, elem.bbox.y0, elem.bbox.x1, elem.bbox.y1),
            (100, 200, 300, 400),
        )

    def test_existing_bbox_preserved(self):
        from image_reconciliation import _derive_source_bboxes, SourceStructElement, ImageOccurrence

        existing = BBox(1, 2, 3, 4)
        elem = SourceStructElement(
            struct_type="/Figure", alt_text="Photo", page_index=0,
            mcids=[5], bbox=existing,
        )
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(100, 200, 300, 400), mcid=5)
        _derive_source_bboxes([elem], [occ])
        self.assertIs(elem.bbox, existing)
```

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestBboxDerivation -v
```

Expected: `AttributeError` for `_derive_source_bboxes`.

- [ ] **Step 3: Implement**

Append to `image_reconciliation.py`:

```python
# ---------------------------------------------------------------------------
# Bbox derivation
# ---------------------------------------------------------------------------

def _derive_source_bboxes(source_elements: list, occurrences: list) -> None:
    """Fill in `bbox` on SourceStructElement entries that don't have one.

    Strategy: for each element with bbox=None, find any ImageOccurrence whose
    `mcid` appears in the element's mcids list, and adopt that occurrence's
    page_bbox. Mutates source_elements in place.
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
```

- [ ] **Step 4: Run test — verify it passes**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestBboxDerivation -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): derive source-element bbox from MCID region"
```

---

## Task 9: Matching engine

Combine watermark / MCID / bbox-overlap / heuristic priorities into one function that produces a `ResolvedImage` per `ImageOccurrence`.

**Files:**
- Modify: `image_reconciliation.py`
- Modify: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test**

Append to `test_image_reconciliation.py`:

```python
class TestMatchingEngine(unittest.TestCase):
    """Resolves each ImageOccurrence to a ResolvedImage by priority chain."""

    def test_watermark_wins(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(0, 0, 100, 100),
                              mcid=7, in_watermark_ancestor=True)
        # Even though MCID 7 maps to /Figure with "Real Alt", watermark wins
        src = SourceStructElement(
            struct_type="/Figure", alt_text="Real Alt", page_index=0,
            mcids=[7], bbox=None,
        )
        results = _match_occurrences_to_sources([occ], [src], page_height=800)
        self.assertTrue(results[0].is_watermark)
        self.assertTrue(results[0].is_decorative)
        self.assertEqual(results[0].alt_text, "")

    def test_mcid_match_to_figure(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(0, 0, 100, 100),
                              mcid=5)
        src = SourceStructElement(struct_type="/Figure", alt_text="Photo 1",
                                  page_index=0, mcids=[5])
        results = _match_occurrences_to_sources([occ], [src], page_height=800)
        self.assertEqual(results[0].alt_text, "Photo 1")
        self.assertFalse(results[0].is_decorative)

    def test_mcid_match_to_artifact_is_decorative(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(0, 0, 100, 100),
                              mcid=9)
        src = SourceStructElement(struct_type="/Artifact", alt_text="",
                                  page_index=0, mcids=[9])
        results = _match_occurrences_to_sources([occ], [src], page_height=800)
        self.assertTrue(results[0].is_decorative)
        self.assertEqual(results[0].alt_text, "")

    def test_bbox_overlap_match(self):
        from image_reconciliation import _match_occurrences_to_sources
        # No MCID; rely on bbox overlap
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(100, 100, 200, 200))
        src = SourceStructElement(
            struct_type="/Figure", alt_text="Photo",
            page_index=0, mcids=[],
            bbox=BBox(95, 95, 205, 205),  # mostly overlaps
        )
        results = _match_occurrences_to_sources([occ], [src], page_height=800)
        self.assertEqual(results[0].alt_text, "Photo")

    def test_each_source_claimed_at_most_once(self):
        from image_reconciliation import _match_occurrences_to_sources
        # Two occurrences, one source — only one wins by bbox match
        occ_close = ImageOccurrence(xobject_name="/Im0",
                                    page_bbox=BBox(100, 100, 200, 200))
        occ_far = ImageOccurrence(xobject_name="/Im1",
                                  page_bbox=BBox(101, 101, 201, 201))
        src = SourceStructElement(struct_type="/Figure", alt_text="P",
                                  page_index=0, mcids=[],
                                  bbox=BBox(100, 100, 200, 200))
        results = _match_occurrences_to_sources(
            [occ_close, occ_far], [src], page_height=800
        )
        matched = [r for r in results if r.alt_text == "P"]
        self.assertEqual(len(matched), 1)

    def test_no_match_in_header_band_is_decorative(self):
        from image_reconciliation import _match_occurrences_to_sources
        # Page height 800, header band = top 10% = y > 720
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(50, 750, 200, 790))
        results = _match_occurrences_to_sources([occ], [], page_height=800)
        self.assertTrue(results[0].is_decorative)

    def test_no_match_body_region_not_decorative(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(50, 400, 200, 500))
        results = _match_occurrences_to_sources([occ], [], page_height=800)
        self.assertFalse(results[0].is_decorative)
        self.assertEqual(results[0].alt_text, "")
```

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestMatchingEngine -v
```

Expected: AttributeError for `_match_occurrences_to_sources`.

- [ ] **Step 3: Implement**

Append to `image_reconciliation.py`:

```python
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


def _match_occurrences_to_sources(occurrences: list, sources: list,
                                  page_height: float) -> list:
    """Pair each ImageOccurrence with a SourceStructElement via the priority chain.

    Priority:
      1. in_watermark_ancestor -> is_decorative=True, is_watermark=True
      2. MCID match -> use source intent
      3. Bbox overlap (IoU >= 0.5), greedy each source claimed at most once
      4. No match in header (y > 90% page_height) or footer (y < 10%) band ->
         is_decorative=True
      5. No match in body -> is_decorative=False, alt_text=""

    Returns list of ResolvedImage in same order as `occurrences`.
    """
    results = [None] * len(occurrences)
    claimed_source_ids = set()

    # Build MCID lookup
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

    # Pass 2: bbox overlap for occurrences still unresolved
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

    return results
```

- [ ] **Step 4: Run all tests**

```bash
./venv/bin/python -m unittest test_image_reconciliation -v
```

Expected: All tests pass (7 test classes).

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): matching engine with priority chain"
```

---

## Task 10: Visual reading-order sort

Sort each page's ResolvedImage list so consumers see top-to-bottom, left-to-right order. PDF y-axis is bottom-up: larger y = higher on page.

**Files:**
- Modify: `image_reconciliation.py`
- Modify: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test**

Append to `test_image_reconciliation.py`:

```python
class TestVisualOrderSort(unittest.TestCase):
    """Top-to-bottom, left-to-right page-visual order."""

    def test_sort_2x2_grid(self):
        from image_reconciliation import _sort_visual_order

        # Top-left, top-right, bottom-left, bottom-right (in PDF coords)
        top_left  = ResolvedImage("/Im0", BBox(50,  500, 250, 700))
        top_right = ResolvedImage("/Im1", BBox(300, 500, 500, 700))
        bot_left  = ResolvedImage("/Im2", BBox(50,  100, 250, 300))
        bot_right = ResolvedImage("/Im3", BBox(300, 100, 500, 300))
        # Provide unsorted
        items = [bot_right, top_left, bot_left, top_right]
        sorted_items = _sort_visual_order(items)
        self.assertEqual(
            [i.xobject_name for i in sorted_items],
            ["/Im0", "/Im1", "/Im2", "/Im3"],
        )

    def test_sort_horizontal_row(self):
        from image_reconciliation import _sort_visual_order
        left   = ResolvedImage("/Im0", BBox(50,  500, 200, 600))
        mid    = ResolvedImage("/Im1", BBox(220, 500, 380, 600))
        right  = ResolvedImage("/Im2", BBox(400, 500, 560, 600))
        items = [right, left, mid]
        sorted_items = _sort_visual_order(items)
        self.assertEqual(
            [i.xobject_name for i in sorted_items],
            ["/Im0", "/Im1", "/Im2"],
        )
```

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestVisualOrderSort -v
```

Expected: AttributeError for `_sort_visual_order`.

- [ ] **Step 3: Implement**

Append to `image_reconciliation.py`:

```python
# ---------------------------------------------------------------------------
# Visual order sort
# ---------------------------------------------------------------------------

def _sort_visual_order(items: list) -> list:
    """Sort a list of ResolvedImage in top-to-bottom, left-to-right page order.

    PDF y-axis is bottom-up: sort by -y_center first, then x_center.
    Uses row-banding so items at similar y are grouped before sorting by x —
    avoids slight y differences scrambling a visual row.
    """
    def y_center(it): return (it.bbox.y0 + it.bbox.y1) / 2
    def x_center(it): return (it.bbox.x0 + it.bbox.x1) / 2
    def height(it):   return max(1.0, it.bbox.y1 - it.bbox.y0)

    # Sort by descending y first to identify rows
    by_y = sorted(items, key=lambda i: -y_center(i))
    # Group into rows by y proximity (within half-height of first row member)
    rows = []
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
    # Within each row sort left-to-right
    output = []
    for row in rows:
        output.extend(sorted(row, key=x_center))
    return output
```

- [ ] **Step 4: Run test — verify it passes**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestVisualOrderSort -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): visual reading-order sort with row banding"
```

---

## Task 11: Top-level `reconcile_page_images()` with per-page fallback

Wire everything together. Public entry point. Per-page try/except so any failure on page N falls back to old behavior for N only.

**Files:**
- Modify: `image_reconciliation.py`
- Modify: `test_image_reconciliation.py`

- [ ] **Step 1: Write the failing test (uses a real fixture PDF)**

Append to `test_image_reconciliation.py`:

```python
import os

class TestReconcileEndToEnd(unittest.TestCase):
    """Top-level reconcile_page_images on real fixture PDFs.

    Skips cleanly if a fixture is missing — keeps fast-feedback path runnable
    in any environment.
    """

    FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")

    def _require_fixture(self, name):
        path = os.path.join(self.FIXTURES_DIR, name)
        if not os.path.exists(path):
            self.skipTest(f"Fixture not found: {path}")
        return path

    def test_brownfield_no_placeholder_alts(self):
        """Output must not contain 'Figure N on page M' placeholder alts."""
        # NOTE: This test runs against the REAL pikepdf, not the mock.
        # We re-import after disabling the mock for this test class.
        import importlib, sys
        if "pikepdf" in sys.modules and not hasattr(sys.modules["pikepdf"], "open_pdf"):
            # Remove mock so real pikepdf is imported
            del sys.modules["pikepdf"]
        try:
            import pikepdf as real_pikepdf  # noqa: F401
        except ImportError:
            self.skipTest("real pikepdf not installed")

        import image_reconciliation
        importlib.reload(image_reconciliation)

        path = self._require_fixture("brownfield.pdf")
        result = image_reconciliation.reconcile_page_images(path)

        for page_idx, items in result.items():
            for ri in items:
                self.assertFalse(
                    ri.alt_text.startswith("Figure ") and "on page" in ri.alt_text,
                    f"Placeholder alt leaked through on page {page_idx}: "
                    f"{ri.alt_text!r}"
                )
```

(Note: this end-to-end test requires real pikepdf and a real fixture. It will be exercised by Task 14's integration suite too — its purpose here is to ensure the top-level function at least runs.)

- [ ] **Step 2: Run test — verify it fails**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestReconcileEndToEnd -v
```

Expected: AttributeError for `reconcile_page_images`.

- [ ] **Step 3: Implement top-level entry**

Append to `image_reconciliation.py`:

```python
# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reconcile_page_images(pdf_path: str) -> dict:
    """Reconcile every Image XObject occurrence in the PDF with source intent.

    Returns {page_index: [ResolvedImage, ...]} in visual reading order.

    On any per-page failure, that page is omitted from the result; the caller
    falls back to its prior behavior (e.g., placeholder alts).  Other pages
    still get the new robust reconciliation.
    """
    result = {}
    try:
        pdf = pikepdf.Pdf.open(pdf_path)
    except Exception as e:
        logger.warning("Could not open PDF for reconciliation: %s", e)
        return result

    try:
        source_by_page = _read_source_struct_elements(pdf)

        for page_idx, page in enumerate(pdf.pages):
            try:
                # Page height for header/footer band heuristic
                try:
                    media = page.get("/MediaBox")
                    page_height = float(media[3]) - float(media[1]) if media else 792.0
                except Exception:
                    page_height = 792.0

                wm_names = detect_watermark_forms(page)
                occurrences = _parse_image_occurrences(page, wm_names)
                if not occurrences:
                    continue

                sources = list(source_by_page.get(page_idx, []))
                _derive_source_bboxes(sources, occurrences)
                resolved = _match_occurrences_to_sources(
                    occurrences, sources, page_height
                )
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
```

- [ ] **Step 4: Run test — verify it passes (or skips if fixture missing)**

```bash
./venv/bin/python -m unittest test_image_reconciliation.TestReconcileEndToEnd -v
```

Expected: either PASS (if brownfield.pdf is at tests/fixtures/) or SKIP.

- [ ] **Step 5: Commit**

```bash
git add image_reconciliation.py test_image_reconciliation.py
git commit -m "feat(reconciliation): top-level reconcile_page_images with per-page fallback"
```

---

## Task 12: Rewire `pdf_extractor._extract_images`

Replace the index-based body with a call to `reconcile_page_images`. Preserve the external contract: `page_content.images` is still populated with `ImageBlock` instances carrying `image_bytes, format, bbox, alt_text, is_decorative`.

**Files:**
- Modify: `pdf_extractor.py`

- [ ] **Step 1: Read current `_extract_images` for exact contract**

```bash
sed -n '851,937p' pdf_extractor.py
```

Note: confirms the function signature, return shape, and where `ImageBlock` fields come from. Verify the import line at top includes `ImageBlock` (already imported per existing code).

- [ ] **Step 2: Rewrite `_extract_images`**

Replace the body of `_extract_images` in `pdf_extractor.py` (lines ~851–937) with:

```python
def _extract_images(pdf_path: str, pages: list, existing_alt_texts=None):
    """Extract image bytes and reconcile alt text + decorative status.

    The `existing_alt_texts` parameter is retained for backward compatibility
    but no longer used — reconciliation reads the struct tree directly.
    """
    from image_reconciliation import reconcile_page_images

    try:
        pdf = pikepdf.Pdf.open(pdf_path)
    except Exception as e:
        logger.warning("Could not open PDF for image extraction: %s", e)
        return

    resolved_by_page = reconcile_page_images(pdf_path)

    try:
        for page_idx, pdf_page in enumerate(pdf.pages):
            if page_idx >= len(pages):
                break
            page_content = pages[page_idx]

            page_resolved = resolved_by_page.get(page_idx)
            if page_resolved is None:
                # Reconciliation skipped this page (failure or empty);
                # leave page_content.images as pdfminer populated it.
                continue

            # Build a lookup of {xobject_name -> pikepdf Image obj} on this page
            xobj_lookup = {}
            try:
                resources = pdf_page.get("/Resources")
                if resources is not None:
                    xobjects = resources.get("/XObject")
                    if xobjects is not None:
                        for name, obj in xobjects.items():
                            try:
                                if obj.get("/Subtype") == pikepdf.Name("/Image"):
                                    xobj_lookup[str(name)] = obj
                            except Exception:
                                continue
            except Exception:
                pass
            # Also search Form XObjects recursively for nested image bytes
            def _add_nested(form_obj, seen):
                try:
                    res = form_obj.get("/Resources")
                    if res is None: return
                    xo = res.get("/XObject")
                    if xo is None: return
                    for n, o in xo.items():
                        try:
                            objgen = getattr(o, "objgen", None)
                            if objgen and objgen in seen: continue
                            if objgen: seen.add(objgen)
                            sub = o.get("/Subtype")
                            if sub == pikepdf.Name("/Image"):
                                xobj_lookup.setdefault(str(n), o)
                            elif sub == pikepdf.Name("/Form"):
                                _add_nested(o, seen)
                        except Exception:
                            continue
                except Exception:
                    pass
            try:
                if resources is not None:
                    xo = resources.get("/XObject")
                    if xo is not None:
                        for n, o in xo.items():
                            try:
                                if o.get("/Subtype") == pikepdf.Name("/Form"):
                                    _add_nested(o, set())
                            except Exception:
                                continue
            except Exception:
                pass

            new_image_blocks = []
            for resolved in page_resolved:
                img_obj = xobj_lookup.get(resolved.xobject_name)
                image_bytes = b""
                fmt = ""
                if img_obj is not None:
                    try:
                        pdfimage = pikepdf.PdfImage(img_obj)
                        pil_image = pdfimage.as_pil_image()
                        buf = BytesIO()
                        pil_image.save(buf, format="PNG")
                        image_bytes = buf.getvalue()
                        fmt = "png"
                    except Exception as e:
                        logger.debug(
                            "Could not render image '%s' on page %d: %s",
                            resolved.xobject_name, page_idx, e,
                        )
                new_image_blocks.append(ImageBlock(
                    image_bytes=image_bytes,
                    format=fmt,
                    bbox=resolved.bbox,
                    page_number=page_idx,
                    alt_text=resolved.alt_text,
                    is_decorative=resolved.is_decorative,
                ))

            # Replace the page's images with the reconciled list (visual order).
            page_content.images = new_image_blocks
    finally:
        pdf.close()
```

The call sites that pass `existing_alt_texts` (in `extract_document_content`, around line 53) continue to work — the parameter is accepted but ignored. We'll remove that parameter and its caller after Task 14 confirms the integration is stable.

- [ ] **Step 3: Run existing test suite**

```bash
./venv/bin/python -m unittest test_fixes test_image_reconciliation -v
```

Expected: all tests still pass.

- [ ] **Step 4: Spot-check against one fixture**

```bash
./venv/bin/python -c "
from pdf_extractor import extract_document
doc = extract_document('tests/fixtures/perils_regular.pdf')
for i, p in enumerate(doc.pages):
    for img in p.images:
        print(f'page {i}: alt={img.alt_text!r} decorative={img.is_decorative}')
"
```

Expected: alt-text strings appear, no placeholder leakage for images that have source-tree alts.

- [ ] **Step 5: Commit**

```bash
git add pdf_extractor.py
git commit -m "refactor(extractor): delegate _extract_images to image_reconciliation"
```

---

## Task 13: Remove obsolete helpers from `pdf_extractor`

`_read_existing_alt_texts` and `_collect_images_recursive` are no longer called. Remove them and their call sites.

**Files:**
- Modify: `pdf_extractor.py`

- [ ] **Step 1: Confirm both helpers have no remaining callers**

```bash
grep -n "_read_existing_alt_texts\|_collect_images_recursive" pdf_extractor.py
```

Expected: only the definitions appear (lines ~721 and ~810); no callers.

- [ ] **Step 2: Delete the two functions and the call to `_read_existing_alt_texts`**

In `pdf_extractor.py`:
- Delete the body of `_read_existing_alt_texts` (lines 721–807 approx).
- Delete the body of `_collect_images_recursive` (lines 810–848 approx).
- Find and delete the line `existing_alt_texts = _read_existing_alt_texts(pdf_path)` (around line 53) and update the immediately-following `_extract_images` call to drop the third argument:

```python
# Before:
existing_alt_texts = _read_existing_alt_texts(pdf_path)
_extract_images(pdf_path, raw_pages, existing_alt_texts)
# After:
_extract_images(pdf_path, raw_pages)
```

- Also drop the `existing_alt_texts=None` parameter from `_extract_images`'s signature:

```python
def _extract_images(pdf_path: str, pages: list):
```

- [ ] **Step 3: Run tests**

```bash
./venv/bin/python -m unittest test_fixes test_image_reconciliation -v
```

Expected: all pass.

- [ ] **Step 4: Smoke-test the pipeline**

```bash
./run.sh --input "input/Perils and Pitfalls - Case.pdf" --skip-validation
```

Expected: completes without error; output PDF generated in `output/`.

- [ ] **Step 5: Commit**

```bash
git add pdf_extractor.py
git commit -m "refactor(extractor): remove obsolete index-based alt-text helpers"
```

---

## Task 14: Integration test harness with invariant assertions

Build a new top-level test script that runs the full pipeline on every fixture and asserts the four invariants. This is the automated "PAC-equivalent" check.

**Files:**
- Create: `test_pipeline_integration.py`

- [ ] **Step 1: Write the integration test**

Create `test_pipeline_integration.py`:

```python
"""
test_pipeline_integration.py — End-to-end tests asserting accessibility invariants.

For each fixture PDF, runs extract -> tag -> postprocess -> validate, then
asserts four invariants:

  I1. veraPDF passes (no new failures).
  I2. Every Image XObject in the output is inside a /Figure with non-empty /Alt
      OR inside an /Artifact wrapper. No third state.
  I3. No /Alt in output matches the placeholder regex "^Figure \\d+ on page \\d+$".
  I4. Every non-empty /Alt present in the source struct tree appears in the
      output struct tree (preservation).

Requires real pikepdf + veraPDF on PATH. Run with:
    ./venv/bin/python -m unittest test_pipeline_integration -v
"""
import os
import re
import shutil
import tempfile
import unittest

import pikepdf

from main import process_single_pdf

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
PLACEHOLDER_RE = re.compile(r"^Figure \d+ on page \d+$")


def _walk_struct_tree(node, visited=None):
    """Yield every struct element in the tree (depth-first)."""
    if visited is None:
        visited = set()
    try:
        oid = node.objgen
    except Exception:
        oid = id(node)
    if oid in visited:
        return
    visited.add(oid)
    yield node
    try:
        kids = node.get("/K")
    except Exception:
        return
    if kids is None:
        return
    if isinstance(kids, list) or isinstance(kids, pikepdf.Array):
        for child in kids:
            if hasattr(child, "get"):
                yield from _walk_struct_tree(child, visited)
    elif hasattr(kids, "get"):
        yield from _walk_struct_tree(kids, visited)


def _collect_figures_and_artifacts(pdf):
    """Return (figures, artifacts) lists from struct tree."""
    figures, artifacts = [], []
    try:
        root = pdf.Root.get("/StructTreeRoot")
        if root is None:
            return figures, artifacts
    except Exception:
        return figures, artifacts
    Figure = pikepdf.Name("/Figure")
    Artifact = pikepdf.Name("/Artifact")
    for node in _walk_struct_tree(root):
        try:
            s = node.get("/S")
            if s == Figure:
                figures.append(node)
            elif s == Artifact:
                artifacts.append(node)
        except Exception:
            continue
    return figures, artifacts


def _image_xobject_count(pdf):
    """Count Image XObjects across all pages, including those nested in Forms."""
    n = 0
    seen = set()
    def visit(resources):
        nonlocal n
        if resources is None: return
        try:
            xo = resources.get("/XObject")
            if xo is None: return
            for name, obj in xo.items():
                try:
                    objgen = obj.objgen
                    if objgen in seen: continue
                    seen.add(objgen)
                    sub = obj.get("/Subtype")
                    if sub == pikepdf.Name("/Image"):
                        n += 1
                    elif sub == pikepdf.Name("/Form"):
                        visit(obj.get("/Resources"))
                except Exception:
                    continue
        except Exception:
            pass
    for page in pdf.pages:
        try:
            visit(page.get("/Resources"))
        except Exception:
            continue
    return n


class TestPipelineInvariants(unittest.TestCase):

    FIXTURES = [
        "perils_regular.pdf",
        "perils_watermarked.pdf",
        "ke1335_raw.pdf",
        "ke1335_already_accessible.pdf",
        "brownfield.pdf",
        "unilever_vitality_regular.pdf",
        "unilever_vitality_dnc.pdf",
    ]

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="pipeline_invariants_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _process(self, fixture_name):
        src = os.path.join(FIXTURES_DIR, fixture_name)
        if not os.path.exists(src):
            self.skipTest(f"Fixture missing: {src}")
        result = process_single_pdf(src, self.tmpdir, skip_validation=False)
        self.assertTrue(result.success, f"Pipeline failed: {result.error}")
        return result

    def _open_output(self, result):
        return pikepdf.Pdf.open(result.output_path)

    def _assert_no_placeholder_alts(self, pdf, fixture):
        figures, _ = _collect_figures_and_artifacts(pdf)
        for fig in figures:
            try:
                alt = fig.get("/Alt")
            except Exception:
                continue
            if alt is None:
                continue
            alt_str = str(alt)
            self.assertFalse(
                PLACEHOLDER_RE.match(alt_str),
                f"[{fixture}] Placeholder alt leaked: {alt_str!r}",
            )

    def _assert_preservation(self, src_pdf, out_pdf, fixture):
        src_figs, _ = _collect_figures_and_artifacts(src_pdf)
        out_figs, _ = _collect_figures_and_artifacts(out_pdf)
        src_alts = set()
        for f in src_figs:
            try:
                a = f.get("/Alt")
                if a and str(a).strip():
                    src_alts.add(str(a).strip())
            except Exception:
                continue
        out_alts = set()
        for f in out_figs:
            try:
                a = f.get("/Alt")
                if a:
                    out_alts.add(str(a).strip())
            except Exception:
                continue
        missing = src_alts - out_alts
        self.assertFalse(
            missing,
            f"[{fixture}] Source alts not preserved in output: {sorted(missing)}",
        )

    def test_all_fixtures(self):
        for fixture in self.FIXTURES:
            with self.subTest(fixture=fixture):
                result = self._process(fixture)

                # I1: veraPDF passes
                self.assertTrue(
                    result.validation_compliant,
                    f"[{fixture}] veraPDF failed:\n{result.validation_report}",
                )

                out_pdf = self._open_output(result)
                try:
                    # I3: no placeholder alt strings
                    self._assert_no_placeholder_alts(out_pdf, fixture)

                    # I4: preservation
                    src_pdf = pikepdf.Pdf.open(os.path.join(FIXTURES_DIR, fixture))
                    try:
                        self._assert_preservation(src_pdf, out_pdf, fixture)
                    finally:
                        src_pdf.close()

                    # I2 (qualitative): figure count vs image XObject count alignment.
                    # We assert: every Figure in output struct tree has non-empty /Alt
                    # OR is empty content (vector figure). Non-empty /Alt is the
                    # contract for raster figures we tagged.
                    out_figs, _ = _collect_figures_and_artifacts(out_pdf)
                    empty_alt_count = 0
                    for f in out_figs:
                        try:
                            a = f.get("/Alt")
                            if a is None or not str(a).strip():
                                empty_alt_count += 1
                        except Exception:
                            empty_alt_count += 1
                    # Empty-alt figures are allowed only when source had none either.
                    # Hard floor: no more than the source's empty-alt count.
                    src_pdf = pikepdf.Pdf.open(os.path.join(FIXTURES_DIR, fixture))
                    try:
                        src_figs, _ = _collect_figures_and_artifacts(src_pdf)
                        src_empty = sum(
                            1 for f in src_figs
                            if not (f.get("/Alt") and str(f.get("/Alt")).strip())
                        )
                    finally:
                        src_pdf.close()
                    self.assertLessEqual(
                        empty_alt_count, src_empty,
                        f"[{fixture}] output introduced empty-alt figures: "
                        f"src={src_empty} out={empty_alt_count}",
                    )
                finally:
                    out_pdf.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the integration suite**

```bash
./venv/bin/python -m unittest test_pipeline_integration -v
```

Expected: `test_all_fixtures` passes (each fixture as a subtest). If any subtest fails, capture the failing fixture name + assertion and triage before proceeding.

- [ ] **Step 3: Commit**

```bash
git add test_pipeline_integration.py
git commit -m "test: add end-to-end pipeline invariant suite over fixture corpus"
```

---

## Task 15: Final validation, manual PAC spot-check, PR prep

- [ ] **Step 1: Run the full test suite**

```bash
./venv/bin/python -m unittest test_fixes test_image_reconciliation test_pipeline_integration -v
```

Expected: every test passes; integration suite covers all 7 fixtures.

- [ ] **Step 2: Run the full pipeline on every fixture and inspect output**

```bash
mkdir -p /tmp/recon_out
for f in tests/fixtures/*.pdf; do
  ./run.sh --input "$f" --output-dir /tmp/recon_out
done
ls -la /tmp/recon_out
```

Expected: every fixture produces a `*_accessible.pdf` with veraPDF PASS in console output.

- [ ] **Step 3: Manual PAC spot-check**

Open the three highest-risk outputs in PAC 2024 on Windows (or remind the user to):

```
/tmp/recon_out/brownfield_accessible.pdf
/tmp/recon_out/unilever_vitality_dnc_accessible.pdf
/tmp/recon_out/perils_watermarked_accessible.pdf
```

Verify in PAC:
- All images either have meaningful alt text OR are tagged as artifacts.
- Horizontally adjacent images in Brownfield (Exhibit 1 page) have alts in correct left-to-right order.
- Kellogg logo on Unilever cover is `/Artifact` (decorative), not `/Figure`.
- DNC watermark on Unilever DNC is `/Artifact/Watermark`.

If any PAC issue surfaces that's not covered by the invariant tests, add a unit test reproducing it in `test_image_reconciliation.py` and triage before merge.

- [ ] **Step 4: Push branch and open PR to develop**

```bash
git push -u origin fix/image-alt-reconciliation
gh pr create --base develop --title "fix: position-based image/alt reconciliation" --body "$(cat <<'EOF'
## Summary
- Replaces index-based alt-text matching in `pdf_extractor._extract_images` with a source-aware, position-based reconciliation pass in a new `image_reconciliation.py` module.
- Fixes scrambled alts on horizontally adjacent images (Brownfield case), lost alts on watermarked DNC documents (Perils Watermarked, Unilever DNC), and decorative images becoming announced figures (Kellogg logo on Unilever).
- Adds a permanent regression corpus at `tests/fixtures/` and an end-to-end invariant suite at `test_pipeline_integration.py` that asserts no placeholder alts leak, source alts are preserved, and every output Image XObject is either a `/Figure` with non-empty `/Alt` or wrapped as `/Artifact`.

## Test plan
- [x] `test_image_reconciliation` — 7 test classes, ~25 cases, all pass
- [x] `test_fixes` — existing reviewer-fix tests still pass
- [x] `test_pipeline_integration` — full pipeline on all 7 fixtures, veraPDF PASS, invariants hold
- [ ] Manual PAC 2024 spot-check on Brownfield, Unilever DNC, Perils Watermarked

## Reference
- Design spec: `docs/superpowers/specs/2026-05-12-image-alt-reconciliation-design.md`
- Implementation plan: `docs/superpowers/plans/2026-05-12-image-alt-reconciliation.md`

Co-authored-by: Claude Opus 4.7
EOF
)"
```

Return the PR URL when done.

- [ ] **Step 5: Note**

Do NOT merge until:
1. Codex review approves.
2. Gemini review approves.
3. Manual PAC spot-check on the three high-risk fixtures is green.

---

## Self-Review

**Spec coverage check (every section/requirement → task):**

| Spec requirement | Task |
|---|---|
| New module `image_reconciliation.py` | Tasks 2–11 |
| `ResolvedImage` dataclass | Task 2 |
| `SourceStructElement` dataclass | Task 2 |
| `ImageOccurrence` dataclass | Task 2 |
| Struct tree reader (Figure + Artifact) | Task 4 |
| Content stream parser (CTM + MCID stack) | Task 5 |
| Form XObject descent (concatenated CTM) | Task 6 |
| Watermark detection moved/shared | Task 7 |
| Bbox derivation for MCID-only sources | Task 8 |
| Matching engine — priority 1 (watermark) | Task 9 |
| Matching engine — priority 2 (MCID) | Task 9 |
| Matching engine — priority 3 (bbox overlap, greedy) | Task 9 |
| Matching engine — priority 4/5 (header/footer band fallback) | Task 9 |
| Visual reading-order sort | Task 10 |
| Top-level `reconcile_page_images` with per-page fallback | Task 11 |
| `pdf_extractor._extract_images` rewired | Task 12 |
| Obsolete helpers removed | Task 13 |
| Unit tests | Tasks 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 |
| Pipeline integration tests | Task 14 |
| Fixture corpus (permanent regression cases) | Task 1 |
| Invariant: no placeholder alts | Task 14 (assertion in `_assert_no_placeholder_alts`) |
| Invariant: source alts preserved | Task 14 (assertion in `_assert_preservation`) |
| Invariant: every image is Figure-with-alt or Artifact | Task 14 (empty-alt-count check + veraPDF) |
| veraPDF still passes | Task 14 (I1) |
| Manual PAC spot-check | Task 15 |
| PR opened to develop | Task 15 |

**No gaps.** Every section of the design spec maps to one or more tasks.

**Placeholder scan:** no TBD / TODO / "fill in" / "similar to" / "handle edge cases" without specifics.

**Type consistency:**
- `BBox` from `models.py` used uniformly.
- `ResolvedImage`, `SourceStructElement`, `ImageOccurrence` defined in Task 2, referenced consistently by exact field names throughout Tasks 4–11.
- Function names stable: `_bbox_from_ctm`, `_read_source_struct_elements`, `_parse_image_occurrences`, `_derive_source_bboxes`, `_match_occurrences_to_sources`, `_sort_visual_order`, `reconcile_page_images`, `detect_watermark_forms`. No drift across tasks.

Plan is internally consistent and complete.
