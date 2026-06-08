"""
test_reversionfixes_2026_06_08.py — Katharine 2026-06-08 (reversionfixes 6).

Three regressions reported on this batch:

1. KE1128 "A Landlord's Certainty" — the exhibit pages "come up as a full
   figure".  Root cause: the source already carries correct /Figure BDC
   ranges, but a range that wraps ONLY an image (``cm`` + ``Do`` with no
   path operators) accumulated no region bbox, so the tagger fell back to
   the whole MediaBox.  Pages whose source figure happened to include a
   clip rectangle (``re W n``) survived; the rest became whole-page
   figures.  Fix: accumulate the image/form ``Do`` placement rectangle
   into the figure-region bbox.

2. KEL142 "Apple iTunes" — PAC reports 163 "Characters in a text object
   cannot be mapped to Unicode".  Root cause: the bullet glyphs use an
   embedded Type0 / Identity-H Wingdings subset with no /ToUnicode, and
   _fix_fonts only synthesised ToUnicode for simple WinAnsi fonts.  Fix:
   synthesise a ToUnicode CMap for embedded Type0 fonts from the font
   program's own cmap.

3. KEL320 "AirFrance" — "parts of tables are marked as figures".  Root
   cause: _detect_vector_figure_regions grabs a table's ruling-line
   clusters (clipped single-segment strokes / thin filled rules) and
   wraps them as /Figure with a generic "Figure" alt.  Fix: demote
   auto-detected, generic-alt regions whose geometry is rule-like
   (degenerate sliver or thin high-aspect strip) to /Artifact.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_06_08 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
LANDLORD = os.path.join(FIXTURES_DIR, "ke1128_landlord.pdf")
ITUNES = os.path.join(FIXTURES_DIR, "kel142_itunes.pdf")
AIRFRANCE = os.path.join(FIXTURES_DIR, "kel320_airfrance.pdf")


def _figures(pdf):
    """Return [(alt, bbox_or_None)] for every /Figure struct element."""
    out = []
    seen = set()

    def walk(n):
        if isinstance(n, pikepdf.Array):
            for k in n:
                walk(k)
            return
        if not isinstance(n, pikepdf.Dictionary):
            return
        try:
            if n.objgen in seen:
                return
            seen.add(n.objgen)
        except Exception:
            pass
        if n.get("/S") == pikepdf.Name("/Figure"):
            a = n.get("/A")
            bb = None
            if isinstance(a, pikepdf.Dictionary):
                raw = a.get("/BBox")
                if raw is not None and len(raw) >= 4:
                    bb = [float(v) for v in raw]
            elif isinstance(a, pikepdf.Array):
                for it in a:
                    if isinstance(it, pikepdf.Dictionary) and it.get("/BBox"):
                        raw = it.get("/BBox")
                        bb = [float(v) for v in raw]
            alt = str(n.get("/Alt")) if n.get("/Alt") is not None else None
            out.append((alt, bb))
        k = n.get("/K")
        if k is not None:
            walk(k)

    st = pdf.Root.get("/StructTreeRoot")
    if st is not None:
        walk(st.get("/K"))
    return out


def _process(path):
    from main import process_single_pdf
    tmp = tempfile.mkdtemp(prefix="rev6_")
    res = process_single_pdf(path, tmp, skip_validation=True)
    return tmp, (res.output_path if res.success else None)


# --------------------------------------------------------------------------- #
# Bug 1 — Landlord: image-only source figures must keep a sub-page bbox
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(LANDLORD), "ke1128_landlord.pdf missing")
class TestLandlordNoFullPageFigures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(LANDLORD)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_full_page_figures(self):
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = _figures(pdf)
        full = [(alt, bb) for (alt, bb) in figs if bb
                and (bb[2] - bb[0]) >= 0.9 * 612
                and (bb[3] - bb[1]) >= 0.9 * 792]
        self.assertEqual(
            full, [],
            f"Exhibit pages must not be tagged as whole-page figures; "
            f"got {len(full)} full-page figures: {full}")

    def test_image_figures_keep_real_bbox(self):
        """Every real figure keeps a proper, on-page sub-page bbox."""
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = _figures(pdf)
        self.assertGreaterEqual(len([b for _, b in figs if b]), 5)
        for alt, bb in figs:
            self.assertIsNotNone(bb, f"figure {alt!r} lost its bbox")
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            self.assertTrue(0 < w < 0.9 * 612 and 0 < h < 0.9 * 792,
                            f"figure {alt!r} has non-content bbox {bb}")


# --------------------------------------------------------------------------- #
# Bug 2 — iTunes: embedded Type0 fonts must carry ToUnicode
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(ITUNES), "kel142_itunes.pdf missing")
class TestITunesType0ToUnicode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(ITUNES)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_all_type0_fonts_have_tounicode(self):
        missing = []
        seen = set()
        with pikepdf.Pdf.open(self.out) as pdf:
            for pi, page in enumerate(pdf.pages):
                res = page.get("/Resources")
                fonts = res.get("/Font") if res else None
                if not fonts:
                    continue
                for fname, font in fonts.items():
                    key = font.objgen
                    if key in seen:
                        continue
                    seen.add(key)
                    if str(font.get("/Subtype", "")) != "/Type0":
                        continue
                    if font.get("/ToUnicode") is None:
                        missing.append((pi, str(fname),
                                        str(font.get("/BaseFont"))))
        self.assertEqual(
            missing, [],
            f"Type0 fonts without /ToUnicode cause PAC "
            f"'cannot be mapped to Unicode' errors: {missing}")


# --------------------------------------------------------------------------- #
# Bug 3 — AirFrance: table ruling lines must not become figures
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(AIRFRANCE), "kel320_airfrance.pdf missing")
class TestAirFranceNoTableRuleFigures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(AIRFRANCE)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_degenerate_sliver_figures(self):
        """A /Figure 0.66pt wide is a table rule, not a figure."""
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = _figures(pdf)
        slivers = [(alt, bb) for (alt, bb) in figs if bb
                   and (abs(bb[2] - bb[0]) < 3 or abs(bb[3] - bb[1]) < 3)]
        self.assertEqual(slivers, [],
                         f"degenerate sliver figures (table rules): {slivers}")

    def test_no_generic_alt_rule_figures(self):
        """The spurious table-rule figures carry the generic alt "Figure";
        every real figure on this doc has a descriptive caption."""
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = _figures(pdf)
        generic = [(alt, bb) for (alt, bb) in figs
                   if (alt or "").strip().lower() in ("figure", "image", "")]
        self.assertEqual(
            generic, [],
            f"generic-alt figures are mis-tagged table ruling: {generic}")

    def test_real_image_figures_survive(self):
        """The legitimate spreadsheet / chart screenshots stay figures."""
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = _figures(pdf)
        descriptive = [a for a, _ in figs
                       if a and a.strip().lower() not in ("figure", "image", "")]
        self.assertGreaterEqual(
            len(descriptive), 9,
            f"real figures were over-suppressed; only {len(descriptive)} left")


if __name__ == "__main__":
    unittest.main()
