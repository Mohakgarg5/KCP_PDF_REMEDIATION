"""
test_reversionfixes_2026_06_03.py — Katharine 2026-06-03 follow-up.

Two defects reported on the ``reversionfixes (4)/`` batch:

  A. Cotton (KEL348) — a font fails to embed.  PAC flags ONE
     "Font not embedded" error on page 11 (Exhibit 1 table).  The
     offending font is BaseFont ``/Arial`` (bare name, simple TrueType,
     WinAnsi).  ``_FONT_FILE_NAMES`` keys the Liberation fallback under
     the PostScript name ``ArialMT`` only, so on the Linux/Docker deploy
     (``fonts-liberation`` installed, no MS core fonts) a font literally
     named ``Arial`` resolves to nothing and stays un-embedded.  macOS
     dev boxes hide the bug because real ``Arial.ttf`` is present.

  B. Boston Metro — page-1 header logos.  The source wraps them in a
     ``/Artifact <</Type /Pagination /Subtype /Header>>`` content range
     (``Meta18`` form, aspect 1.87; ``Image19`` image, aspect 2.9).
     They fall below the banner gate AND the pipeline ignores the
     source's explicit /Artifact intent, so they get tagged as
     ``/Figure`` with generic alt ("Figure"/"Image") instead of staying
     decorative page chrome.

  C. (secondary) Source-derived /Alt strings carry a trailing NUL
     byte (UTF-16 terminator) that leaks into the output /Alt.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_06_03 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf

import pdf_tagger
from pdf_tagger import _is_banner_artifact


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
BOSTON_METRO = os.path.join(FIXTURES_DIR, "boston_metro.pdf")


# ---------------------------------------------------------------------------
# A. Font embedding — bare/family-based Liberation fallback
# ---------------------------------------------------------------------------

class TestFontLiberationFallback(unittest.TestCase):
    """Robust family/style → Liberation fallback for un-embedded fonts.

    The map only keyed ``ArialMT`` etc.; a bare ``Arial`` (or comma-style
    ``Arial,Bold``) had no Liberation fallback and stayed un-embedded on
    Linux.  ``_liberation_fallback`` classifies ANY recognizable family
    so nothing common is left un-embedded.
    """

    def _fb(self, name):
        from pdf_postprocess import _liberation_fallback
        return _liberation_fallback(name)

    def test_bare_arial_regular(self):
        self.assertEqual(self._fb("Arial"), "LiberationSans-Regular.ttf")

    def test_bare_arial_comma_bold(self):
        self.assertEqual(self._fb("Arial,Bold"), "LiberationSans-Bold.ttf")

    def test_bare_arial_comma_italic(self):
        self.assertEqual(self._fb("Arial,Italic"), "LiberationSans-Italic.ttf")

    def test_bare_arial_comma_bolditalic(self):
        self.assertEqual(self._fb("Arial,BoldItalic"),
                         "LiberationSans-BoldItalic.ttf")

    def test_subset_prefix_stripped(self):
        self.assertEqual(self._fb("ABCDEE+Arial"),
                         "LiberationSans-Regular.ttf")

    def test_helvetica_maps_to_sans(self):
        self.assertEqual(self._fb("Helvetica-Bold"),
                         "LiberationSans-Bold.ttf")

    def test_times_maps_to_serif(self):
        self.assertEqual(self._fb("Times-Roman"),
                         "LiberationSerif-Regular.ttf")
        self.assertEqual(self._fb("TimesNewRomanPSMT"),
                         "LiberationSerif-Regular.ttf")

    def test_courier_maps_to_mono(self):
        self.assertEqual(self._fb("Courier"), "LiberationMono-Regular.ttf")

    def test_unknown_symbol_font_has_no_sans_fallback(self):
        """Symbol/dingbat fonts must NOT be silently mapped to a Latin
        sans face — that would replace glyphs with wrong shapes."""
        self.assertIsNone(self._fb("Symbol"))
        self.assertIsNone(self._fb("ZapfDingbats"))


class TestFindSystemFontUsesLiberation(unittest.TestCase):
    """_find_system_font must resolve a bare ``Arial`` to Liberation when
    that is the only sans face present (the Linux/Docker deploy case)."""

    def setUp(self):
        from pdf_postprocess import _FONT_DIRS
        self._saved_dirs = list(_FONT_DIRS)
        self.tmp = tempfile.mkdtemp(prefix="fonts_")
        # Only Liberation present — emulate the Streamlit/Docker host.
        for fn in ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf",
                   "LiberationSerif-Regular.ttf", "LiberationMono-Regular.ttf"):
            with open(os.path.join(self.tmp, fn), "wb") as f:
                f.write(b"\x00\x01\x00\x00")  # path-resolution only
        import pdf_postprocess
        pdf_postprocess._FONT_DIRS[:] = [self.tmp]

    def tearDown(self):
        import pdf_postprocess
        pdf_postprocess._FONT_DIRS[:] = self._saved_dirs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bare_arial_resolves_to_liberation_sans(self):
        from pdf_postprocess import _find_system_font
        loc = _find_system_font("Arial")
        self.assertIsNotNone(
            loc, "bare 'Arial' must resolve to a Liberation face when only "
                 "Liberation is installed (Linux deploy)")
        self.assertTrue(str(loc).endswith("LiberationSans-Regular.ttf"))

    def test_comma_arial_bold_resolves(self):
        from pdf_postprocess import _find_system_font
        loc = _find_system_font("Arial,Bold")
        self.assertIsNotNone(loc)
        self.assertTrue(str(loc).endswith("LiberationSans-Bold.ttf"))


# ---------------------------------------------------------------------------
# C. Alt-text NUL/control-char sanitisation
# ---------------------------------------------------------------------------

class TestAltTextSanitisation(unittest.TestCase):
    def _san(self, s):
        from pdf_tagger import _sanitize_alt_text
        return _sanitize_alt_text(s)

    def test_strips_trailing_nul(self):
        self.assertEqual(self._san("A spreadsheet figure.\x00"),
                         "A spreadsheet figure.")

    def test_strips_interior_control_chars(self):
        self.assertEqual(self._san("line one\x00line two"), "line oneline two")

    def test_preserves_normal_text_and_whitespace(self):
        self.assertEqual(self._san("Normal alt, with punctuation."),
                         "Normal alt, with punctuation.")

    def test_none_safe(self):
        self.assertEqual(self._san(None), "")


# ---------------------------------------------------------------------------
# B. Boston Metro logos — end-to-end (honor source /Artifact intent)
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.path.exists(BOSTON_METRO),
                     "boston_metro.pdf fixture not present")
class TestBostonMetroLogos(unittest.TestCase):
    """Source marks the page-1 logos as /Artifact (Pagination/Header).
    The pipeline must keep them artifacts, not promote them to /Figure.
    The 3 real content figures (2 spreadsheet screenshots, 1 graph) must
    survive with their descriptive author alt — and no /S=/Artifact may
    appear in the output tree (INV-1)."""

    @classmethod
    def setUpClass(cls):
        from main import process_single_pdf
        cls.tmp = tempfile.mkdtemp(prefix="bm_e2e_")
        res = process_single_pdf(BOSTON_METRO, cls.tmp, skip_validation=True)
        cls.out = res.output_path if res.success else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _figures(self, pdf):
        figs = []
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
                alt = n.get("/Alt")
                figs.append(str(alt) if alt is not None else "")
            k = n.get("/K")
            if k is not None:
                walk(k)

        root = pdf.Root.get("/StructTreeRoot")
        if root is not None:
            walk(root.get("/K"))
        return figs

    def _struct_types(self, pdf):
        from collections import Counter
        c = Counter()
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
            s = n.get("/S")
            if s is not None:
                c[str(s)] += 1
            k = n.get("/K")
            if k is not None:
                walk(k)

        root = pdf.Root.get("/StructTreeRoot")
        if root is not None:
            walk(root.get("/K"))
        return c

    def test_processed_ok(self):
        self.assertIsNotNone(self.out, "Boston Metro failed to process")

    def test_only_three_content_figures_survive(self):
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = self._figures(pdf)
        self.assertEqual(
            len(figs), 3,
            f"Expected exactly 3 content Figures (2 spreadsheets + 1 graph); "
            f"the page-1 header logos must be artifacts. Got {len(figs)}: "
            f"{[a[:40] for a in figs]}")

    def test_no_generic_alt_figures(self):
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = self._figures(pdf)
        generic = [a for a in figs
                   if a.strip().lower() in ("", "figure", "image")]
        self.assertEqual(
            len(generic), 0,
            f"No surviving Figure may have generic alt; got {generic}")

    def test_no_struct_artifact_type(self):
        with pikepdf.Pdf.open(self.out) as pdf:
            types = self._struct_types(pdf)
        self.assertEqual(
            types.get("/Artifact", 0), 0,
            "/Artifact is a marked-content operator, never a struct type "
            "(INV-1).")

    def test_page1_logos_are_content_artifacts(self):
        """Meta18 (form) and Image19 (image) draws must be inside an
        /Artifact marked-content range, never a /Figure BDC."""
        with pikepdf.Pdf.open(self.out) as pdf:
            pg = pdf.pages[0]
            cs = pg.obj.get("/Contents")
            data = (b"".join(c.read_bytes() for c in cs)
                    if isinstance(cs, pikepdf.Array) else cs.read_bytes())
            txt = data.decode("latin-1")
        for name in ("/Meta18", "/Image19"):
            idx = txt.find(name + " Do")
            self.assertGreaterEqual(idx, 0, f"{name} Do not found")
            preceding = txt[max(0, idx - 120):idx]
            # The nearest BDC/BMC before the Do must be an Artifact one.
            last_artifact = preceding.rfind("/Artifact")
            last_figure = preceding.rfind("/Figure")
            self.assertGreater(
                last_artifact, last_figure,
                f"{name} is wrapped as /Figure; expected /Artifact "
                f"(source marked it Pagination/Header).")

    def test_alt_text_has_no_trailing_nul(self):
        with pikepdf.Pdf.open(self.out) as pdf:
            figs = self._figures(pdf)
        bad = [a[:40] for a in figs if a.endswith("\x00")]
        self.assertEqual(bad, [], f"Alt text retains trailing NUL: {bad}")


if __name__ == "__main__":
    unittest.main()
