"""
test_reversionfixes_2026_11_10.py — Katharine 2026-11-10 (reversionfixes 13).

Two cases, THREE independent root causes.  Katharine reported Sony as one
problem ("double tagging … causing a content error in PAC, I'm assuming due to
the live text on the table that's being labeled as a figure"); it is two, and
the PAC content error is not caused by the tagging at all.

A. ONTELA — clipping paths counted as drawn content
   `_PATH_OPS` included "n", "W" and "W*".  Those paint NOTHING: W/W* set a
   clipping region and n ends the path without filling or stroking.  A clipped
   table cell is `q re W* n BT … ET Q` = 3 path ops per clip, so two clips hit
   `min_path_ops` (6) exactly and the cell was promoted to a /Figure while
   drawing not a single mark.  Ontela p16 clips every cell: 23 such regions,
   merged into a phantom /Figure sitting over the table's row-label column
   ("Segment size", "Willingness to pay", "Cost to identify, attract, and
   serve").  Rendering that region produces a blank image — proof it paints
   nothing.

   Katharine thought this was leftover ornaments and remade the document to
   remove them.  It could never have worked: the ornaments are not what is
   being marked, which is why re-feeding the rebuilt file changed nothing.

   Fix: a path block must contain an actual PAINTING operator
   (f F f* B B* b b* S s) to count as a figure.

B. SONY — landscape template frame tagged /Figure
   InDesign draws a template frame /Fm0 on every page.  On portrait pages its
   bbox is the full 612x792, so `_form_is_full_page_background` artifacts it.
   On LANDSCAPE pages InDesign insets it to the margin box:
   [89.8, 51.3, 702.2, 559.6] on a 792x612 page = 77.3% x 83.1%.
   `_bbox_is_full_page` demands >= 90% in BOTH dimensions, so the frame slips
   through and becomes a /Figure on all five landscape pages (17, 23, 24, 29,
   30) — over live table text on p29/p30, and doubling up with a real
   described figure on p17/p23/p24.

   Fix: a Form that carries no raster image and whose bbox REPEATS on 2+ pages
   is page-template chrome regardless of the 90% threshold.  Repetition is the
   signal that separates a template frame from a one-off large diagram.

C. SONY — Type0 font with no ToUnicode at all (clause 7.21.7)
   /C0_0 CMIUKZ+ArialMT is a /CIDFontType0 whose program is a BARE CFF in
   /FontFile3 (Subtype /CIDFontType0C) with Ordering=Identity.
   `_build_type0_tounicode_map` parses with fontTools.ttLib.TTFont, which
   rejects a bare CFF ("Not a TrueType or OpenType font"), returns 0 entries,
   and no ToUnicode is written.  The clause-7.21.7 completion pass then skips
   the font twice over, because BOTH its registration gate and its per-run
   check require an existing ToUnicode (`has_tu`) — a font with none is
   invisible to the pass built to fix exactly this.  Result: 50 unmapped
   glyphs, which is the "content error" PAC reported.

   All 84 runs in that font sit inside the /Figure Katharine described, so the
   existing /ActualText mechanism carries the real reading.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_11_10 -v
"""
import os
import unittest

import pikepdf

import pdf_tagger as T
import pdf_postprocess as PP


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
SONY = os.path.join(FIXTURES_DIR, "ke1165_sony_csl.pdf")
ONTELA = os.path.join(FIXTURES_DIR, "kel452_ontela_tn.pdf")

SONY_FRAME_BBOX = [89.8, 51.3, 702.2, 559.6]
SONY_LANDSCAPE_PAGES = [17, 23, 24, 29, 30]


def _ops(page):
    return list(pikepdf.parse_content_stream(page))


def _page_regions(path, pageno):
    pdf = pikepdf.open(path)
    page = pdf.pages[pageno - 1]
    sfa = T._build_source_figure_alt_map(pdf).get(pageno - 1, {})
    stripped, mask, src = T._strip_markers_with_source_intent(_ops(page), sfa)
    return stripped, T._detect_vector_figure_regions(stripped)


# ---------------------------------------------------------------------------
# A. Clipping paths paint nothing
# ---------------------------------------------------------------------------

class TestClipPathsAreNotFigures(unittest.TestCase):

    def test_clip_operators_are_not_painting_operators(self):
        for op in ("W", "W*", "n"):
            self.assertNotIn(op, T._PAINT_OPS,
                             f"{op} paints nothing and must not count")

    def test_real_paint_operators_are_recognised(self):
        for op in ("f", "F", "f*", "B", "B*", "b", "b*", "S", "s"):
            self.assertIn(op, T._PAINT_OPS)

    def test_a_clipped_text_cell_is_not_a_vector_figure(self):
        # Exactly Ontela's shape: two clip rects around a text run.
        def mk(op, *operands):
            return ([*operands], pikepdf.Operator(op))
        ops = [
            mk("q"), mk("re"), mk("W*"), mk("n"),
            mk("q"), mk("re"), mk("W*"), mk("n"),
            mk("BT"), mk("Tf"), mk("Tm"), mk("TJ"), mk("ET"),
            mk("Q"), mk("Q"),
        ]
        self.assertEqual(
            T._detect_vector_figure_regions(ops), [],
            "a block that only clips and draws text is not a figure",
        )

    def test_a_block_that_actually_strokes_is_still_a_figure(self):
        def mk(op, *operands):
            return ([*operands], pikepdf.Operator(op))
        ops = [mk("q")] + [mk("m"), mk("l"), mk("l")] * 3 + [mk("S"), mk("Q")]
        self.assertTrue(T._detect_vector_figure_regions(ops),
                        "a real stroked path must still be detected")

    def test_ontela_page16_yields_no_vector_figures(self):
        _stripped, regions = _page_regions(ONTELA, 16)
        self.assertEqual(
            regions, [],
            f"p16 is a clipped table, not a figure; got {len(regions)} regions",
        )

    def test_ontela_page3_yields_no_vector_figures(self):
        _stripped, regions = _page_regions(ONTELA, 3)
        self.assertEqual(regions, [])


# ---------------------------------------------------------------------------
# B. Repeating inset template frames are chrome
# ---------------------------------------------------------------------------

class TestRepeatingFormChrome(unittest.TestCase):

    def test_the_sony_frame_falls_under_the_full_page_gate(self):
        # Precondition: this is exactly why the existing rule misses it.
        page_box = [0.0, 0.0, 792.0, 612.0]
        self.assertFalse(T._bbox_is_full_page(SONY_FRAME_BBOX, page_box))

    def test_the_repeating_frame_is_detected_across_pages(self):
        pdf = pikepdf.open(SONY)
        repeats = T._detect_repeating_form_bboxes(pdf)
        self.assertTrue(
            T._bbox_matches_any(SONY_FRAME_BBOX, repeats, tol=2.0),
            f"template frame should be found as repeating; got {repeats}",
        )

    def test_a_form_appearing_on_one_page_only_is_not_chrome(self):
        pdf = pikepdf.open(ONTELA)
        repeats = T._detect_repeating_form_bboxes(pdf)
        self.assertNotIn(tuple(SONY_FRAME_BBOX), repeats)

    def test_classifier_accepts_a_repeating_imageless_frame(self):
        page_box = [0.0, 0.0, 792.0, 612.0]
        repeats = {tuple(SONY_FRAME_BBOX)}
        self.assertTrue(T._form_is_template_chrome(
            SONY_FRAME_BBOX, page_box, repeats, has_image=False))

    def test_classifier_rejects_a_frame_holding_a_raster_image(self):
        # A repeated full-bleed photo is content, not chrome.
        page_box = [0.0, 0.0, 792.0, 612.0]
        repeats = {tuple(SONY_FRAME_BBOX)}
        self.assertFalse(T._form_is_template_chrome(
            SONY_FRAME_BBOX, page_box, repeats, has_image=True))

    def test_classifier_rejects_a_small_repeating_form(self):
        # Small repeating marks are logos — the banner rule owns those.
        page_box = [0.0, 0.0, 792.0, 612.0]
        logo = [40.0, 560.0, 200.0, 600.0]
        self.assertFalse(T._form_is_template_chrome(
            logo, page_box, {tuple(logo)}, has_image=False))


# ---------------------------------------------------------------------------
# C. Type0 fonts carrying no ToUnicode
# ---------------------------------------------------------------------------

_PDF_KEEPALIVE = []


def _page18_font(path, key="/C0_0"):
    # Hold the Pdf open: pikepdf objects die with their owning document.
    pdf = pikepdf.open(path)
    _PDF_KEEPALIVE.append(pdf)
    return (pdf.pages[17].get("/Resources") or {})["/Font"][key]


class TestType0WithoutToUnicode(unittest.TestCase):

    def test_fixture_precondition_bare_cff_without_tounicode(self):
        f = _page18_font(SONY)
        self.assertIsNone(f.get("/ToUnicode"))
        df = f["/DescendantFonts"][0]
        self.assertEqual(str(df.get("/Subtype")), "/CIDFontType0")
        self.assertIn("/FontFile3", df["/FontDescriptor"])

    def test_ttfont_cannot_parse_it_which_is_why_synthesis_returned_nothing(self):
        self.assertEqual(PP._build_type0_tounicode_map(_page18_font(SONY)), {})

    def test_bare_cff_is_parsed_for_glyph_names(self):
        names = PP._cff_glyph_names(_page18_font(SONY))
        self.assertTrue(names, "the CFF must be readable even though TTFont fails")
        self.assertIn("cid00020", names.values())

    def test_fonts_without_tounicode_are_considered_for_completion(self):
        # The gates that made such a font invisible to the 7.21.7 pass.
        self.assertTrue(PP._needs_tounicode_completion(_page18_font(SONY)))


class TestSonyEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tempfile
        import config
        from main import process_single_pdf
        cls._tmp = tempfile.mkdtemp(prefix="revfix1110_")
        old = getattr(config, "ALT_TEXT_DRAFTING", False)
        config.ALT_TEXT_DRAFTING = False
        try:
            r = process_single_pdf(SONY, cls._tmp, skip_validation=True)
            assert r.success, r.error
            cls.out = r.output_path
            r2 = process_single_pdf(ONTELA, cls._tmp, skip_validation=True)
            assert r2.success, r2.error
            cls.ontela_out = r2.output_path
        finally:
            config.ALT_TEXT_DRAFTING = old

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    @staticmethod
    def _figures(path):
        pdf = pikepdf.open(path)
        pm = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}

        def page_of(n, d=0):
            if d > 6:
                return None
            g = n.get("/Pg")
            if g is not None:
                return pm.get(g.objgen)
            k = n.get("/K")
            if k is None:
                return None
            for kid in (k if isinstance(k, pikepdf.Array) else [k]):
                if isinstance(kid, pikepdf.Dictionary):
                    gg = kid.get("/Pg")
                    if gg is not None:
                        return pm.get(gg.objgen)
                    r = page_of(kid, d + 1)
                    if r:
                        return r
            return None

        out = []

        def walk(n):
            if not isinstance(n, pikepdf.Dictionary):
                return
            if str(n.get("/S")) == "/Figure":
                a = n.get("/A")
                bb = None
                if a is not None:
                    try:
                        e = a[0] if isinstance(a, pikepdf.Array) else a
                        v = e.get("/BBox")
                        if v is not None:
                            bb = [round(float(x), 1) for x in v]
                    except Exception:
                        pass
                alt = n.get("/Alt")
                out.append((page_of(n), bb,
                            str(alt) if alt is not None else None))
            k = n.get("/K")
            if k is None:
                return
            for kid in (k if isinstance(k, pikepdf.Array) else [k]):
                if isinstance(kid, pikepdf.Dictionary):
                    walk(kid)

        st = pdf.Root.get("/StructTreeRoot")
        kk = st.get("/K") if st is not None else None
        if kk is not None:
            for x in (kk if isinstance(kk, pikepdf.Array) else [kk]):
                walk(x)
        return out

    def test_sony_has_no_template_frame_figures(self):
        # Match all four coordinates: the real p17 figure happens to share the
        # frame's x-extent (both span the margin box), so an x-only test would
        # flag genuine content.
        bad = [f for f in self._figures(self.out)
               if f[1] and all(abs(a - b) < 2
                               for a, b in zip(f[1], SONY_FRAME_BBOX))]
        self.assertEqual(bad, [], "template frame must not be a /Figure")

    def test_sony_keeps_exactly_the_four_authored_figures(self):
        figs = self._figures(self.out)
        self.assertEqual(len(figs), 4, f"expected the 4 source figures, got {figs}")
        described = [f for f in figs if f[2] and f[2].strip().lower() != "figure"]
        self.assertEqual(len(described), 4, "every authored description must survive")

    def test_sony_landscape_pages_have_at_most_one_figure_each(self):
        figs = self._figures(self.out)
        for pg in SONY_LANDSCAPE_PAGES:
            self.assertLessEqual(
                len([f for f in figs if f[0] == pg]), 1,
                f"page {pg} is double-tagged")

    def test_sony_page18_font_gains_a_tounicode(self):
        self.assertIsNotNone(
            _page18_font(self.out).get("/ToUnicode"),
            "the bare-CFF Type0 font must end up with a ToUnicode")

    def test_ontela_emits_no_figures(self):
        self.assertEqual(
            self._figures(self.ontela_out), [],
            "Ontela's source has no figures; the tool must not invent any")


if __name__ == "__main__":
    unittest.main()
