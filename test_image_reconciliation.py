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
