"""
test_reversionfixes_2026_08_15_fonts.py — Charlotte 2026-08-15 (reversionfixes 11).

KEL072 CPEF (A) declares /Arial-Black as a NON-embedded /TrueType font.  The
pipeline must embed something, and it was embedding *regular* Arial — a visible
weight loss on a face whose whole purpose is being heavy.

Two independent defects, one per host type:

1. ``_find_system_font`` (any host that has the real MS faces, e.g. macOS).
   ``Arial-Black`` has no ``_FONT_FILE_NAMES`` entry, so lookup fell through to
   the hyphen rule, which splits on "-" and keeps only the FAMILY — appending
   ``Arial.ttf`` and discarding the weight entirely.  ``Arial Black.ttf`` sits
   in the same directory and was never a candidate.  The last-resort substring
   scan missed it too: "arial-black" is not a substring of "arialblack.ttf"
   because the scan strips spaces but not hyphens.

2. ``_liberation_fallback`` (the Linux/Streamlit deploy, which ships only
   fonts-liberation).  Weight is detected with ``"bold" in low``, and "black"
   does not contain "bold", so Arial Black resolved to LiberationSans-*Regular*.

Both fixes preserve weight rather than silently dropping to the family default.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_08_15_fonts -v
"""
import os
import tempfile
import unittest


def _touch(path):
    with open(path, "wb") as fh:
        fh.write(b"\x00\x01\x00\x00")  # enough to exist; never parsed here


class TestArialBlackSystemLookup(unittest.TestCase):
    """Defect 1 — style must survive the hyphen split."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fontdirs_")
        # A directory shaped like macOS /System/Library/Fonts/Supplemental
        for name in ("Arial.ttf", "Arial Bold.ttf", "Arial Black.ttf",
                     "Arial Narrow.ttf", "Times New Roman.ttf"):
            _touch(os.path.join(self.tmp, name))

        import pdf_postprocess
        self.mod = pdf_postprocess
        self._saved = list(pdf_postprocess._FONT_DIRS)
        pdf_postprocess._FONT_DIRS[:] = [self.tmp]

    def tearDown(self):
        self.mod._FONT_DIRS[:] = self._saved

    def test_arial_black_resolves_to_arial_black_not_regular(self):
        got = self.mod._find_system_font("Arial-Black")
        self.assertIsNotNone(got, "Arial-Black resolved to nothing")
        self.assertEqual(
            os.path.basename(got), "Arial Black.ttf",
            f"Arial-Black embedded the wrong face: {got!r} "
            "(regular Arial is a visible weight loss)",
        )

    def test_hyphen_style_preserved_generally(self):
        """The defect is the hyphen rule, not this one font name."""
        got = self.mod._find_system_font("Arial-Narrow")
        self.assertIsNotNone(got)
        self.assertEqual(os.path.basename(got), "Arial Narrow.ttf")

    def test_mapped_names_still_resolve(self):
        """Regression: names in _FONT_FILE_NAMES must be unaffected."""
        self.assertEqual(
            os.path.basename(self.mod._find_system_font("ArialMT")), "Arial.ttf")
        self.assertEqual(
            os.path.basename(self.mod._find_system_font("Arial-BoldMT")),
            "Arial Bold.ttf")

    def test_family_fallback_still_works_when_no_style_face(self):
        """A style with no matching file must still fall back to the family."""
        got = self.mod._find_system_font("Arial-Condensed")
        self.assertIsNotNone(got)
        self.assertEqual(os.path.basename(got), "Arial.ttf")


class TestArialBlackLiberationFallback(unittest.TestCase):
    """Defect 2 — the Streamlit/Linux deploy path."""

    def setUp(self):
        import pdf_postprocess
        self.fb = pdf_postprocess._liberation_fallback

    def test_black_maps_to_bold_weight(self):
        self.assertEqual(self.fb("Arial-Black"), "LiberationSans-Bold.ttf")

    def test_heavy_maps_to_bold_weight(self):
        self.assertEqual(self.fb("Helvetica-Heavy"), "LiberationSans-Bold.ttf")

    def test_black_italic_maps_to_bold_italic(self):
        self.assertEqual(
            self.fb("Arial-BlackItalic"), "LiberationSans-BoldItalic.ttf")

    def test_existing_weights_unchanged(self):
        """Regression over the June-3 comma-style and canonical names."""
        self.assertEqual(self.fb("Arial,Bold"), "LiberationSans-Bold.ttf")
        self.assertEqual(self.fb("ArialMT"), "LiberationSans-Regular.ttf")
        self.assertEqual(
            self.fb("TimesNewRomanPSMT"), "LiberationSerif-Regular.ttf")
        self.assertEqual(
            self.fb("TimesNewRomanPS-BoldMT"), "LiberationSerif-Bold.ttf")
        self.assertEqual(
            self.fb("CourierNewPSMT"), "LiberationMono-Regular.ttf")

    def test_symbol_faces_still_excluded(self):
        """A black-weight symbol face must not become a Latin sans."""
        self.assertIsNone(self.fb("Wingdings-Regular"))
        self.assertIsNone(self.fb("SymbolMT"))


class TestArialBlackEndToEnd(unittest.TestCase):
    """The real document, if the real face is present on this host."""

    FIXTURE = os.path.join(
        os.path.dirname(__file__), "tests", "fixtures", "kel072_cpef_a.pdf")

    def test_cpef_embeds_a_black_weight(self):
        if not os.path.exists(self.FIXTURE):
            self.skipTest("kel072_cpef_a.pdf fixture not present")

        import io
        import shutil
        import pikepdf
        from fontTools.ttLib import TTFont
        from main import process_single_pdf

        tmp = tempfile.mkdtemp(prefix="cpef_")
        try:
            res = process_single_pdf(self.FIXTURE, tmp, skip_validation=True)
            self.assertTrue(res.success, f"pipeline failed: {res.error}")

            pdf = pikepdf.Pdf.open(res.output_path)
            found = None
            for page in pdf.pages:
                res_dict = page.get("/Resources")
                fonts = res_dict.get("/Font") if res_dict is not None else None
                if fonts is None:
                    continue
                for _, fobj in fonts.items():
                    if "Arial-Black" not in str(fobj.get("/BaseFont", "")):
                        continue
                    fd = fobj.get("/FontDescriptor")
                    if fd is None or "/FontFile2" not in fd:
                        continue
                    tt = TTFont(io.BytesIO(bytes(fd["/FontFile2"].read_bytes())),
                                fontNumber=0, lazy=True)
                    found = tt["name"].getDebugName(4) or ""
            pdf.close()

            if found is None:
                self.skipTest("Arial-Black not embedded on this host")
            low = found.lower()
            self.assertTrue(
                "black" in low or "bold" in low or "heavy" in low,
                f"Arial-Black embedded as {found!r} — weight was dropped",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
