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
pikepdf_mod.String = MagicMock(side_effect=lambda s: s)
pikepdf_mod.Stream = MagicMock()
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

    def test_collects_mcr_dict_child_as_mcid(self):
        """MCR dict ({/Type /MCR, /MCID 5}) is the indirect form of an MCID leaf.

        Acrobat-edited PDFs frequently use this form when content spans a
        referenced Form XObject. The walker must extract the /MCID instead of
        recursing into the MCR's (absent) /K subtree.
        """
        from image_reconciliation import _read_source_struct_elements
        Name = pikepdf_mod.Name
        page = MagicMock()
        page.objgen = (2, 0)

        # MCR dict — has /MCID but no /K
        mcr = MagicMock()
        mcr.get.side_effect = {
            "/Type": Name("/MCR"),
            "/MCID": 42,
            "/Pg": page,
        }.get
        mcr.objgen = (0, 0)  # inline — would collide on objgen-based dedup

        # Figure whose /K contains the MCR dict (not a plain int)
        fig = MagicMock()
        fig.get.side_effect = {
            "/S": Name("/Figure"),
            "/Pg": page,
            "/Alt": "Form-spanned figure",
            "/K": pikepdf_mod.Array([mcr]),
        }.get
        fig.objgen = (0, 0)  # also inline

        struct_root = MagicMock()
        struct_root.get.side_effect = {"/K": pikepdf_mod.Array([fig])}.get
        struct_root.objgen = (1, 0)

        pdf = MagicMock()
        pdf.Root.get.side_effect = {"/StructTreeRoot": struct_root}.get
        pdf.pages = [page]

        result = _read_source_struct_elements(pdf)
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[0][0].mcids, [42])

    def test_inline_struct_elements_not_conflated(self):
        """Two inline (objgen=(0,0)) Figures must both be collected.

        Regression guard for the bug where objgen-based cycle detection
        treated all inline dicts as a single visited node.
        """
        from image_reconciliation import _read_source_struct_elements
        Name = pikepdf_mod.Name
        page = MagicMock()
        page.objgen = (2, 0)

        fig1 = MagicMock()
        fig1.get.side_effect = {
            "/S": Name("/Figure"), "/Pg": page, "/Alt": "First",
            "/K": pikepdf_mod.Array([10]),
        }.get
        fig1.objgen = (0, 0)

        fig2 = MagicMock()
        fig2.get.side_effect = {
            "/S": Name("/Figure"), "/Pg": page, "/Alt": "Second",
            "/K": pikepdf_mod.Array([11]),
        }.get
        fig2.objgen = (0, 0)  # same objgen as fig1 — must not collide

        struct_root = MagicMock()
        struct_root.get.side_effect = {"/K": pikepdf_mod.Array([fig1, fig2])}.get
        struct_root.objgen = (1, 0)

        pdf = MagicMock()
        pdf.Root.get.side_effect = {"/StructTreeRoot": struct_root}.get
        pdf.pages = [page]

        result = _read_source_struct_elements(pdf)
        self.assertEqual(len(result[0]), 2)
        alts = sorted(e.alt_text for e in result[0])
        self.assertEqual(alts, ["First", "Second"])

    def test_nested_containers_recursed(self):
        """Figures nested inside non-Figure containers (e.g., Sect/Div) are found."""
        from image_reconciliation import _read_source_struct_elements
        Name = pikepdf_mod.Name
        page = MagicMock()
        page.objgen = (2, 0)

        fig = MagicMock()
        fig.get.side_effect = {
            "/S": Name("/Figure"), "/Pg": page, "/Alt": "Buried",
            "/K": pikepdf_mod.Array([7]),
        }.get
        fig.objgen = (3, 0)

        sect = MagicMock()
        sect.get.side_effect = {
            "/S": Name("/Sect"),
            "/K": pikepdf_mod.Array([fig]),
        }.get
        sect.objgen = (4, 0)

        struct_root = MagicMock()
        struct_root.get.side_effect = {"/K": pikepdf_mod.Array([sect])}.get
        struct_root.objgen = (1, 0)

        pdf = MagicMock()
        pdf.Root.get.side_effect = {"/StructTreeRoot": struct_root}.get
        pdf.pages = [page]

        result = _read_source_struct_elements(pdf)
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[0][0].alt_text, "Buried")


class TestContentStreamParser(unittest.TestCase):
    """Parses operators to emit one ImageOccurrence per Image-XObject Do."""

    def _ops(self, *items):
        Op = pikepdf_mod.Operator
        return [(list(operands), Op(op_str)) for op_str, *operands in items]

    def _page_with_image_xobjects(self, image_names):
        Name = pikepdf_mod.Name
        xobjs = {}
        for n in image_names:
            obj = MagicMock()
            obj.get.side_effect = {"/Subtype": Name("/Image")}.get
            obj.objgen = (id(obj), 0)
            xobjs[Name(n)] = obj
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
            ("cm", 100, 0, 0, 50, 200, 300),
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
        props = MagicMock()
        props.get.side_effect = {pikepdf_mod.Name("/MCID"): 7}.get
        ops = self._ops(
            ("BDC", pikepdf_mod.Name("/Figure"), props),
            ("cm", 50, 0, 0, 50, 0, 0),
            ("Do", pikepdf_mod.Name("/Im0")),
            ("EMC",),
        )
        pikepdf_mod.parse_content_stream = MagicMock(return_value=ops)
        result = _parse_image_occurrences(page, watermark_form_names=set())
        self.assertEqual(result[0].mcid, 7)

    def test_two_horizontal_images_distinct_ctm(self):
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
        self.assertEqual(result[0].page_bbox.x0, 50)
        self.assertEqual(result[1].page_bbox.x0, 250)


class TestWatermarkDetection(unittest.TestCase):
    """Form XObjects with Adobe /Watermark marker are detected."""

    def test_adobe_watermark_marker(self):
        from image_reconciliation import detect_watermark_forms
        Name = pikepdf_mod.Name

        compound = MagicMock()
        compound.get.side_effect = {"/Private": Name("/Watermark")}.get
        piece_info = MagicMock()
        piece_info.get.side_effect = {"/ADBE_CompoundType": compound}.get

        form_obj = MagicMock(spec=["get", "keys"])
        form_obj.keys = MagicMock(return_value=["/Subtype", "/PieceInfo"])
        form_obj.get.side_effect = {
            "/Subtype": pikepdf_mod.Name.Form,
            "/PieceInfo": piece_info,
        }.get

        xobj_dict = MagicMock()
        xobj_dict.items.return_value = [(Name("/WM"), form_obj)]
        resources = MagicMock()
        resources.get.side_effect = {"/XObject": xobj_dict}.get
        page = MagicMock()
        page.get.side_effect = {"/Resources": resources}.get

        result = detect_watermark_forms(page)
        self.assertIn("/WM", result)


class TestFormXObjectDescent(unittest.TestCase):
    """Image XObjects inside a Form XObject must be discovered with combined CTM."""

    def test_image_inside_form_xobject(self):
        from image_reconciliation import _parse_image_occurrences
        Name = pikepdf_mod.Name
        Op = pikepdf_mod.Operator

        img_obj = MagicMock()
        img_obj.get.side_effect = {"/Subtype": Name("/Image")}.get
        img_obj.objgen = (10, 0)

        form_xobj_dict = MagicMock()
        form_xobj_dict.items.return_value = [(Name("/Im0"), img_obj)]
        form_xobj_dict.get.side_effect = {Name("/Im0"): img_obj}.get
        form_resources = MagicMock()
        form_resources.get.side_effect = {"/XObject": form_xobj_dict}.get

        form_obj = MagicMock()
        form_obj.get.side_effect = lambda k: {
            "/Subtype": Name("/Form"),
            "/Resources": form_resources,
        }.get(k)
        form_obj.objgen = (20, 0)

        form_ops = [
            ([50, 0, 0, 50, 10, 20], Op("cm")),
            ([Name("/Im0")], Op("Do")),
        ]

        page_xobjs = MagicMock()
        page_xobjs.items.return_value = [(Name("/F0"), form_obj)]
        page_xobjs.get.side_effect = {Name("/F0"): form_obj}.get
        page_resources = MagicMock()
        page_resources.get.side_effect = {"/XObject": page_xobjs}.get
        page = MagicMock()
        page.get.side_effect = {"/Resources": page_resources}.get

        page_ops = [
            ([100, 0, 0, 100, 200, 300], Op("cm")),
            ([Name("/F0")], Op("Do")),
        ]
        def fake_parse(arg):
            return page_ops if arg is page else form_ops
        pikepdf_mod.parse_content_stream = MagicMock(side_effect=fake_parse)

        result = _parse_image_occurrences(page, watermark_form_names=set())
        # Page CTM × form CTM: combined scale = 50×100 = 5000;
        # combined translate = (10×100+200, 20×100+300) = (1200, 2300)
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
        form_xobj_dict = MagicMock()
        form_xobj_dict.items.return_value = [(Name("/Im0"), img_obj)]
        form_xobj_dict.get.side_effect = {Name("/Im0"): img_obj}.get
        form_resources = MagicMock()
        form_resources.get.side_effect = {"/XObject": form_xobj_dict}.get
        form_obj = MagicMock()
        form_obj.get.side_effect = lambda k: {
            "/Subtype": Name("/Form"),
            "/Resources": form_resources,
        }.get(k)
        form_obj.objgen = (21, 0)
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


class TestBboxDerivation(unittest.TestCase):
    """SourceStructElements without /BBox get one from their MCID-matched occurrence."""

    def test_bbox_derived_from_mcid_region(self):
        from image_reconciliation import _derive_source_bboxes
        elem = SourceStructElement(
            struct_type="/Figure", alt_text="Photo", page_index=0,
            mcids=[5], bbox=None,
        )
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
        from image_reconciliation import _derive_source_bboxes
        existing = BBox(1, 2, 3, 4)
        elem = SourceStructElement(
            struct_type="/Figure", alt_text="Photo", page_index=0,
            mcids=[5], bbox=existing,
        )
        occ = ImageOccurrence(xobject_name="/Im0",
                              page_bbox=BBox(100, 200, 300, 400), mcid=5)
        _derive_source_bboxes([elem], [occ])
        self.assertIs(elem.bbox, existing)


class TestMatchingEngine(unittest.TestCase):
    """Resolves ImageOccurrence → ResolvedImage via 4-priority chain."""

    def test_watermark_wins(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence("/Im0", BBox(0, 0, 100, 100), 7, True)
        src = SourceStructElement("/Figure", "Real Alt", 0, [7])
        results = _match_occurrences_to_sources([occ], [src], page_height=800)
        self.assertTrue(results[0].is_watermark)
        self.assertTrue(results[0].is_decorative)
        self.assertEqual(results[0].alt_text, "")

    def test_mcid_match_to_figure(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence("/Im0", BBox(0, 0, 100, 100), mcid=5)
        src = SourceStructElement("/Figure", "Photo 1", 0, [5])
        results = _match_occurrences_to_sources([occ], [src], 800)
        self.assertEqual(results[0].alt_text, "Photo 1")
        self.assertFalse(results[0].is_decorative)

    def test_mcid_match_to_artifact_is_decorative(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence("/Im0", BBox(0, 0, 100, 100), mcid=9)
        src = SourceStructElement("/Artifact", "", 0, [9])
        results = _match_occurrences_to_sources([occ], [src], 800)
        self.assertTrue(results[0].is_decorative)
        self.assertEqual(results[0].alt_text, "")

    def test_bbox_overlap_match(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence("/Im0", BBox(100, 100, 200, 200))
        src = SourceStructElement("/Figure", "Photo", 0, [],
                                  bbox=BBox(95, 95, 205, 205))
        results = _match_occurrences_to_sources([occ], [src], 800)
        self.assertEqual(results[0].alt_text, "Photo")

    def test_source_claimed_at_most_once(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ_a = ImageOccurrence("/Im0", BBox(100, 100, 200, 200))
        occ_b = ImageOccurrence("/Im1", BBox(101, 101, 201, 201))
        src = SourceStructElement("/Figure", "P", 0, [],
                                  bbox=BBox(100, 100, 200, 200))
        results = _match_occurrences_to_sources([occ_a, occ_b], [src], 800)
        self.assertEqual(sum(1 for r in results if r.alt_text == "P"), 1)

    def test_no_match_header_band_decorative(self):
        from image_reconciliation import _match_occurrences_to_sources
        # Page 800, header band = y > 720
        occ = ImageOccurrence("/Im0", BBox(50, 750, 200, 790))
        results = _match_occurrences_to_sources([occ], [], 800)
        self.assertTrue(results[0].is_decorative)

    def test_no_match_body_not_decorative(self):
        from image_reconciliation import _match_occurrences_to_sources
        occ = ImageOccurrence("/Im0", BBox(50, 400, 200, 500))
        results = _match_occurrences_to_sources([occ], [], 800)
        self.assertFalse(results[0].is_decorative)
        self.assertEqual(results[0].alt_text, "")


if __name__ == "__main__":
    unittest.main()
