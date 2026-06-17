"""
test_reversionfixes_2026_06_17_fonts.py — Katharine 2026-06-17 (reversionfixes 8).

veraPDF clause 7.21.5: "the glyph width information in the font dictionary and
in the embedded font program shall be consistent."

KEL334 PurplePill declares /Arial,Bold as a NON-embedded /TrueType font with a
/Widths array of standard Arial metrics (778, 611, 389 …).  The pipeline embeds
a metric-compatible substitute (Liberation Sans Bold, unitsPerEm 2048) whose
real glyph advances differ (722, 556, 333 …).  _try_embed_font only synthesised
/Widths when the font was flipped from Type1 or had no /Widths at all — so for
an already-/TrueType font that ALREADY had /Widths it left the inherited Arial
widths untouched, inconsistent with the substitute program → 7.21.5 fails (9
glyphs on page 10).

Fix: after embedding, reconcile the embedded program's hmtx advances to the
font dict's /Widths.  Simple (non-CID) fonts position glyphs by /Widths, never
by the program hmtx, so layout is preserved while the program becomes
self-consistent.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_06_17_fonts -v
"""
import io
import os
import shutil
import tempfile
import unittest

import pikepdf


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
PURPLEPILL = os.path.join(FIXTURES_DIR, "kel334_purplepill.pdf")

_WIDTH_TOLERANCE = 1.0  # veraPDF 7.21.5 allows <= 1 glyph-space unit


def _process(path):
    from main import process_single_pdf
    tmp = tempfile.mkdtemp(prefix="rev8font_")
    res = process_single_pdf(path, tmp, skip_validation=True)
    return tmp, (res.output_path if res.success else None)


def _width_inconsistencies(pdf):
    """Return [(basefont, char, dict_width, program_width)] for every embedded
    simple TrueType glyph whose program advance disagrees with /Widths."""
    from fontTools.ttLib import TTFont

    bad = []
    seen = set()
    for page in pdf.pages:
        fonts = (page.get("/Resources", {}) or {}).get("/Font", {})
        for _, fo in (fonts.items() if fonts else []):
            if str(fo.get("/Subtype", "")) != "/TrueType":
                continue
            bf = str(fo.get("/BaseFont", ""))
            widths = fo.get("/Widths")
            desc = fo.get("/FontDescriptor", {}) or {}
            ff = desc.get("/FontFile2")
            if widths is None or ff is None:
                continue
            key = (bf, id(fo))
            if key in seen:
                continue
            seen.add(key)
            try:
                tt = TTFont(io.BytesIO(bytes(ff.read_bytes())), lazy=True)
                upm = tt["head"].unitsPerEm
                scale = 1000.0 / upm
                cmap = tt.getBestCmap() or {}
                hmtx = tt["hmtx"]
            except Exception:
                continue
            first = int(fo.get("/FirstChar", 0))
            for i, w in enumerate(widths):
                try:
                    wv = float(w)
                except Exception:
                    continue
                if wv <= 0:
                    continue
                code = first + i
                gname = cmap.get(code)
                if not gname or gname not in hmtx.metrics:
                    continue
                prog = hmtx.metrics[gname][0] * scale
                if abs(prog - wv) > _WIDTH_TOLERANCE:
                    bad.append((bf, code, wv, round(prog, 2)))
    return bad


@unittest.skipUnless(os.path.exists(PURPLEPILL), "kel334_purplepill.pdf missing")
class TestPurplePillFontWidthConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(PURPLEPILL)
        cls.pdf = pikepdf.open(cls.out) if cls.out else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_embedded_widths_consistent_with_program(self):
        bad = _width_inconsistencies(self.pdf)
        self.assertEqual(
            bad, [],
            f"{len(bad)} glyph(s) violate veraPDF 7.21.5 "
            f"(/Widths != embedded program advance): {bad[:12]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
