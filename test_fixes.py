"""
test_fixes.py — Tests for the three reviewer-requested fixes:

  Fix 1: First-page header/footer text is NOT artifacted
          (case #, byline, date stay readable on page 1)

  Fix 2: Form XObjects are always wrapped as a single <Figure>
          (vector figures are never broken into constituent paths)

  Fix 3: Alt text alignment — figures without /Alt don't shift indices,
          and existing alt text is never overwritten with a generic placeholder
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Minimal stubs so we can import the modules without the full dependency tree
# ---------------------------------------------------------------------------

class _Name:
    """Minimal pikepdf.Name substitute that supports equality comparison."""
    def __init__(self, s):
        self.s = s
    def __eq__(self, other):
        if isinstance(other, _Name):
            return self.s == other.s
        return str(other) == self.s
    def __hash__(self):
        return hash(self.s)
    def __str__(self):
        return self.s
    def __repr__(self):
        return f"Name({self.s!r})"

class _NameFactory:
    """Acts as both a callable (Name("/Figure")) and attribute accessor (Name.Image)."""
    _cache = {}
    def __call__(self, s):
        if s not in self._cache:
            self._cache[s] = _Name(s)
        return self._cache[s]
    def __getattr__(self, s):
        return self(f"/{s}" if not s.startswith("/") else s)

# pikepdf stub
pikepdf_mod = types.ModuleType("pikepdf")
pikepdf_mod.Name = _NameFactory()
pikepdf_mod.String = MagicMock(side_effect=lambda s: s)
pikepdf_mod.Dictionary = MagicMock(side_effect=lambda d=None, **kw: d or kw)
pikepdf_mod.Array = list
pikepdf_mod.Operator = MagicMock(side_effect=lambda s: s)
pikepdf_mod.Stream = MagicMock()
pikepdf_mod.PasswordError = Exception

class _FakePdf:
    def __init__(self):
        self.Root = MagicMock()
        self.pages = []
    def make_indirect(self, obj):
        return obj
    def save(self, *a, **kw): pass
    def close(self): pass

pikepdf_mod.Pdf = MagicMock()
pikepdf_mod.Pdf.open = MagicMock(return_value=_FakePdf())
pikepdf_mod.parse_content_stream = MagicMock(return_value=[])
pikepdf_mod.unparse_content_stream = MagicMock(return_value=b"")
sys.modules["pikepdf"] = pikepdf_mod

# Other stubs
for mod in ["pdfminer", "pdfminer.high_level", "pdfminer.layout",
            "langdetect", "streamlit", "PIL", "PIL.Image"]:
    sys.modules[mod] = types.ModuleType(mod)

sys.modules["pdfminer.layout"].LAParams = MagicMock()
sys.modules["pdfminer.layout"].LTTextBox = MagicMock()
sys.modules["pdfminer.layout"].LTTextLine = MagicMock()
sys.modules["pdfminer.layout"].LTChar = MagicMock()
sys.modules["pdfminer.layout"].LTAnno = MagicMock()
sys.modules["pdfminer.layout"].LTFigure = MagicMock()
sys.modules["pdfminer.layout"].LTImage = MagicMock()
sys.modules["pdfminer.layout"].LTPage = MagicMock()
sys.modules["pdfminer.high_level"].extract_pages = MagicMock(return_value=[])
sys.modules["langdetect"].detect = MagicMock(return_value="en")
sys.modules["PIL.Image"].open = MagicMock()

import config  # real config

from models import (
    PageContent, TextBlock, ImageBlock, FontInfo, BBox, ElementType
)


# ---------------------------------------------------------------------------
# Fix 1 — _classify_elements: first-page headers must NOT be artifacted
# ---------------------------------------------------------------------------

class TestFix1FirstPageHeaders(unittest.TestCase):
    """First-page header text must remain readable even when it repeats."""

    def _make_page(self, page_number, text, y_top):
        """Create a minimal PageContent with one text block in header zone."""
        font = FontInfo(name="Arial", size=10.0, is_bold=False, is_italic=False)
        tb = TextBlock(
            text=text,
            bbox=BBox(x0=50, y0=y_top - 12, x1=200, y1=y_top),
            font=font,
            page_number=page_number,
        )
        page = PageContent(
            page_number=page_number,
            width=612,
            height=792,
            text_blocks=[tb],
        )
        return page, tb

    def test_first_page_header_kept_readable(self):
        """Page 0 header text matching a repeating signature must NOT be artifacted."""
        from pdf_extractor import _classify_elements

        page, tb = self._make_page(page_number=0, text="KE1335", y_top=780)
        # Signature that repeats: ("ke1335", "header")
        hf_signatures = {("ke1335", "header")}
        _classify_elements(page, body_font_size=10.0, hf_signatures=hf_signatures)

        self.assertNotEqual(
            tb.element_type, ElementType.HEADER_FOOTER,
            "Page-1 case # should NOT be artifacted"
        )

    def test_subsequent_page_header_artifacted(self):
        """Page 1+ header text matching a repeating signature MUST be artifacted."""
        from pdf_extractor import _classify_elements

        page, tb = self._make_page(page_number=1, text="KE1335", y_top=780)
        hf_signatures = {("ke1335", "header")}
        _classify_elements(page, body_font_size=10.0, hf_signatures=hf_signatures)

        self.assertEqual(
            tb.element_type, ElementType.HEADER_FOOTER,
            "Page-2+ case # SHOULD be artifacted"
        )

    def test_first_page_small_footer_still_artifacted(self):
        """Small-font copyright text in footer must still be artifacted on page 0."""
        from pdf_extractor import _classify_elements

        font = FontInfo(name="Arial", size=7.0, is_bold=False, is_italic=False)
        tb = TextBlock(
            text="© 2026 Kellogg School of Management",
            bbox=BBox(x0=50, y0=5, x1=500, y1=15),
            font=font,
            page_number=0,
        )
        page = PageContent(page_number=0, width=612, height=792, text_blocks=[tb])
        _classify_elements(page, body_font_size=10.0, hf_signatures=set())

        self.assertEqual(
            tb.element_type, ElementType.HEADER_FOOTER,
            "Small-font footer copyright must still be artifacted on page 1"
        )


# ---------------------------------------------------------------------------
# Fix 2 — _insert_markers: unmatched Form XObjects wrapped as Figure
# ---------------------------------------------------------------------------

class TestFix2FormXObjectAssFigure(unittest.TestCase):
    """Every Form XObject (that isn't a watermark) must become a single Figure tag."""

    def _run_insert_markers(self, xobj_type, has_vector_block=False):
        """
        Drive _insert_markers with a single Do operator and return struct_elems.
        xobj_type: "Form" or "Image"
        has_vector_block: whether a vector figure placeholder exists in blocks
        """
        import pikepdf as pk
        from pdf_tagger import _insert_markers

        # Minimal page mock
        page = MagicMock()
        page.get = MagicMock(return_value=None)

        # Content stream: just one Do operator
        op_Do = MagicMock()
        op_Do.__str__ = lambda self: "Do"
        operands = [MagicMock()]
        operands[0].__str__ = lambda self: "/Fm0"
        ops = [(operands, op_Do)]

        # Patch _get_xobject_subtype to return the desired type
        with patch("pdf_tagger._get_xobject_subtype", return_value=xobj_type):
            blocks = []
            if has_vector_block:
                blocks.append({
                    "bbox": BBox(0, 0, 100, 100),
                    "struct_type": "/Figure",
                    "alt_text": "Existing vector alt",
                    "is_artifact": False,
                    "is_vector": True,
                    "used": False,
                })

            _, struct_elems = _insert_markers(
                ops=ops,
                blocks=blocks,
                page=page,
                watermark_forms=set(),
                mcid_counter=[0],
                link_annots=[],
            )
        return struct_elems

    def test_form_xobject_without_placeholder_becomes_figure(self):
        """An unmatched Form XObject must be tagged as /Figure, not dropped to artifact."""
        struct_elems = self._run_insert_markers("Form", has_vector_block=False)
        types_found = [e[1] for e in struct_elems]
        self.assertIn("/Figure", types_found,
                      "Unmatched Form XObject must produce a /Figure struct element")

    def test_form_xobject_with_placeholder_uses_existing_alt(self):
        """A matched Form XObject must use the existing alt text from the placeholder."""
        struct_elems = self._run_insert_markers("Form", has_vector_block=True)
        figure_elems = [e for e in struct_elems if e[1] == "/Figure"]
        self.assertTrue(figure_elems, "Should have a /Figure element")
        self.assertEqual(figure_elems[0][2], "Existing vector alt")

    def test_form_xobject_without_placeholder_gets_generic_alt(self):
        """An unmatched Form XObject must get 'Figure' as fallback alt text."""
        struct_elems = self._run_insert_markers("Form", has_vector_block=False)
        figure_elems = [e for e in struct_elems if e[1] == "/Figure"]
        self.assertTrue(figure_elems, "Should have a /Figure element")
        self.assertEqual(figure_elems[0][2], "Figure",
                         "Fallback alt text should be 'Figure'")


# ---------------------------------------------------------------------------
# Fix 3 — _read_existing_alt_texts + _extract_images: alt text alignment
# ---------------------------------------------------------------------------

# TestFix3AltTextAlignment was removed in the image-alt-reconciliation
# refactor — _read_existing_alt_texts and the index-based _extract_images
# it tested were replaced by struct-tree intent matching in
# image_reconciliation.  Equivalent coverage now lives in
# test_image_reconciliation (TestStructTreeReader + TestMatchingEngine).


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestFix1FirstPageHeaders))
    suite.addTests(loader.loadTestsFromTestCase(TestFix2FormXObjectAssFigure))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
