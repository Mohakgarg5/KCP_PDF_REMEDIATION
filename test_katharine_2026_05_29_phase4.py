"""
test_katharine_2026_05_29_phase4.py — Phase 4 regression tests.

Phase 4 closes the gap Katharine flagged in her 2026-05-29 email
(``reversionfixes (3)/`` batch): single-page Kellogg logos with
aspect ~3.0–4.0 that slip through the Phase 3 banner gate.

Root cause was twofold:
  1. ``_is_banner_artifact`` required ``aspect >= 4.0`` in the margin
     band; the Asahi page-3 logo measures 122.93 × 34.73 pt (aspect
     3.54) and was kept as ``/Figure`` with generic alt ``"Figure"``.
  2. The cross-page repeating-banner pre-pass only fires when the
     same bbox appears on ≥ 2 pages; a logo that renders on a single
     page (title-page, "Continued" page) is invisible to it.

Fix widens the margin-band sub-rules to cover short banners
(``aspect >= 3.0 and height <= 50pt``) and tightens the icon-chrome
rule (``width <= 150 and height <= 60``).  Both still require the
``y_center`` to fall in the top/bottom 20% Y-band so body charts are
unaffected.

Tests check the shape gate directly AND run the end-to-end pipeline
on the three actual PDFs Katharine flagged.

Run:
    ./venv/bin/python -m unittest test_katharine_2026_05_29_phase4 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf

from pdf_tagger import _is_banner_artifact


PAGE_BOX_LETTER = [0.0, 0.0, 612.0, 792.0]


class TestPhase4BannerGate(unittest.TestCase):
    """Direct shape-gate tests for the Phase 4 widened thresholds."""

    def test_asahi_page3_logo_shape_matches(self):
        """The exact bbox observed for the Kellogg logo on KEL003 Asahi
        page 3: 122.93 × 34.73, aspect 3.54, sits in the bottom Y-margin
        band (y_center ≈ 0.177).  Pre-Phase-4 this returned False because
        aspect < 4.0 and width > 120."""
        bbox = [82.8, 123.12, 205.732, 157.847]
        self.assertTrue(
            _is_banner_artifact(bbox, PAGE_BOX_LETTER),
            "Kellogg-shaped logo (122.9x34.7, aspect 3.54) in bottom band "
            "must now be classified as banner.",
        )

    def test_keurig_page11_logo_shape_matches(self):
        """KEL137 Keurig page-11 BDC range covers a 143 × 45 region in the
        bottom band, aspect ≈ 3.18.  Same family as Asahi."""
        bbox = [83.0, 113.0, 226.0, 158.0]
        self.assertTrue(
            _is_banner_artifact(bbox, PAGE_BOX_LETTER),
            "143x45 bottom-band region (aspect 3.18) must be banner.",
        )

    def test_widened_aspect_floor_top_band(self):
        """Mirror of the new rule in the top margin band."""
        bbox = [82.8, 634.15, 205.732, 668.88]  # y_center ≈ 651 > 0.8*792
        self.assertTrue(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    def test_body_short_aspect3_does_not_match(self):
        """Critical guard: a 122.9 × 34.7 region in the BODY (y_center
        ≈ 0.5) must NOT be classified as banner — only margin-band shapes
        are demoted.  Real (rare) body annotations stay as Figure."""
        bbox = [82.8, 378.0, 205.732, 412.7]   # y_center ≈ 395 / 792 = 0.50
        self.assertFalse(
            _is_banner_artifact(bbox, PAGE_BOX_LETTER),
            "Same shape in the body must NOT be demoted — the gate is "
            "position-sensitive on purpose.",
        )

    def test_tall_chart_in_margin_band_still_passes(self):
        """A real chart that happens to dip into the margin band — but is
        tall (height > 80) — must NOT be demoted.  Guards the existing
        aspect>=4/h<=80 sub-rule which still has a height ceiling."""
        # 400-wide × 120-tall chart whose y_center sits in bottom band.
        bbox = [100.0, 60.0, 500.0, 180.0]   # height 120, y_center 0.15
        self.assertFalse(
            _is_banner_artifact(bbox, PAGE_BOX_LETTER),
            "Tall chart (h=120) in margin band must remain Figure — the "
            "Phase-4 widening only covers banners h ≤ 50pt.",
        )

    def test_tall_short_aspect_does_not_match(self):
        """The widened rule still requires height ≤ 50pt; a taller
        short-aspect region in margin band must NOT match (so a body
        chart that briefly dips into the margin band isn't demoted)."""
        bbox = [80.0, 100.0, 200.0, 200.0]   # 120 × 100, aspect 1.2
        self.assertFalse(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    def test_aspect_below_3_and_wider_than_icon_does_not_match(self):
        """Edge case: a 200×80 region in margin band, aspect 2.5, height
        80. Aspect < 3 fails the new rule. Width > 150 fails the widened
        icon rule. Aspect < 4 fails the original rule. Must remain Figure.
        """
        bbox = [80.0, 80.0, 280.0, 160.0]   # 200 × 80, aspect 2.5
        self.assertFalse(
            _is_banner_artifact(bbox, PAGE_BOX_LETTER),
            "Aspect 2.5 and w=200 wider than icon rule — must remain Figure.",
        )

    def test_aspect_3_height_just_above_50_does_not_match(self):
        """Height ceiling at 50pt for the aspect≥3 sub-rule.  A 200×55
        region in margin band: aspect 3.6, height 55.  Fails new
        height≤50 rule AND fails widened icon (w>150). Old aspect≥4 rule
        with h<=80 still applies — but aspect 3.6 < 4.  Stays Figure."""
        bbox = [80.0, 100.0, 280.0, 155.0]   # 200 × 55, aspect 3.6
        self.assertFalse(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    # ---- Tests preserved from Phase 3 to ensure no regression ----

    def test_phase3_kel201_footer_still_matches(self):
        """The Phase-3 KEL201 footer-logo shape (aspect ~8) still hits
        the extreme-banner sub-rule."""
        bbox = [82.8, 116.74, 521.78, 169.25]  # 439 × 52.5, aspect 8.36
        self.assertTrue(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    def test_phase3_body_chart_still_passes(self):
        """A typical 311×229 line chart in the middle of the page must
        still be classified as a real Figure (not a banner)."""
        bbox = [150.0, 280.0, 461.0, 509.0]
        self.assertFalse(_is_banner_artifact(bbox, PAGE_BOX_LETTER))


@unittest.skipUnless(
    os.path.exists("/Users/mohakgarg/Desktop/reversionfixes (3)"),
    "Katharine 2026-05-29 input batch not present",
)
class TestPhase4EndToEnd(unittest.TestCase):
    """Run the three new PDFs through the current pipeline and verify
    the Kellogg logos Katharine flagged are now demoted to /Artifact
    while real Figures (charts, photos) survive intact."""

    BATCH_DIR = "/Users/mohakgarg/Desktop/reversionfixes (3)"
    FIXTURES = (
        "KEL003 Asahi.pdf",
        "KEL137 Keurig at Home TN.pdf",
        "KEL348 Cotton.pdf",
    )

    @classmethod
    def setUpClass(cls):
        from main import process_single_pdf
        cls.tmpdir = tempfile.mkdtemp(prefix="phase4_e2e_")
        cls.outputs: dict = {}
        for name in cls.FIXTURES:
            src = os.path.join(cls.BATCH_DIR, name)
            if not os.path.exists(src):
                continue
            res = process_single_pdf(src, cls.tmpdir, skip_validation=True)
            if res.success:
                cls.outputs[name] = res.output_path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @staticmethod
    def _collect_figures(pdf):
        """Walk the structure tree and return every /Figure with its
        /BBox, /Alt, and the page index of the first MCID it references.

        Uses ``objgen`` for dedup so the walker survives repeated reads
        (id() is not stable across pikepdf object reloads)."""
        page_to_idx = {pg.objgen: i for i, pg in enumerate(pdf.pages)}
        figs: list = []
        visited: set = set()

        def find_pg(n):
            try:
                pg = n.get("/Pg")
            except Exception:
                pg = None
            if pg is not None:
                return pg
            try:
                k = n.get("/K")
            except Exception:
                return None
            if k is None:
                return None
            items = k if isinstance(k, pikepdf.Array) else [k]
            for kid in items:
                if isinstance(kid, pikepdf.Dictionary):
                    try:
                        p = kid.get("/Pg")
                    except Exception:
                        p = None
                    if p is not None:
                        return p
            return None

        def walk(n):
            try:
                og = n.objgen
                key = ("og", og)
            except Exception:
                key = ("id", id(n))
            if key in visited:
                return
            visited.add(key)
            if not isinstance(n, pikepdf.Dictionary):
                return
            s = n.get("/S")
            if s is not None and str(s) == "/Figure":
                alt = n.get("/Alt")
                a = n.get("/A")
                bbox = None
                if a is not None and hasattr(a, "get"):
                    bb = a.get("/BBox")
                    if bb is not None:
                        bbox = [float(v) for v in bb]
                pg = find_pg(n)
                pi = page_to_idx.get(pg.objgen) if pg is not None else None
                figs.append({
                    "page": pi,
                    "alt": str(alt) if alt else "",
                    "bbox": bbox,
                })
            k = n.get("/K")
            if k is None:
                return
            items = k if isinstance(k, pikepdf.Array) else [k]
            for kid in items:
                if isinstance(kid, pikepdf.Dictionary):
                    walk(kid)

        root = pdf.Root.get("/StructTreeRoot")
        if root is not None:
            walk(root)
        return figs

    def test_kel003_asahi_no_logo_figure_remains(self):
        """KEL003 Asahi previously emitted 3 /Figure StructElems: a
        page-3 logo (alt='Figure'), a line chart, and a tax table.
        Phase 4 must drop the logo (2 Figures left), and both
        remaining Figures must have descriptive author alt."""
        path = self.outputs.get("KEL003 Asahi.pdf")
        if not path:
            self.skipTest("KEL003 fixture not processed")
        with pikepdf.Pdf.open(path) as pdf:
            figs = self._collect_figures(pdf)
        generic_alts = [f for f in figs if f["alt"].strip().lower()
                        in ("", "figure", "image")]
        self.assertEqual(
            len(generic_alts), 0,
            f"KEL003 must have 0 Figures with generic alt; got "
            f"{[(f['page'], f['alt'][:30], f['bbox']) for f in generic_alts]}",
        )
        # We expect the 2 real Figures (chart + table) to survive.
        self.assertGreaterEqual(
            len(figs), 2,
            f"KEL003 expected ≥2 real Figures (chart + table); got {len(figs)}",
        )

    def test_kel137_keurig_no_generic_alt_figures(self):
        """KEL137 Keurig had a leaked logo on page 11 (143×45 aspect 3.18).
        Phase 4 must demote it.  Real diagrams on pages 3 and 11 stay."""
        path = self.outputs.get("KEL137 Keurig at Home TN.pdf")
        if not path:
            self.skipTest("KEL137 fixture not processed")
        with pikepdf.Pdf.open(path) as pdf:
            figs = self._collect_figures(pdf)
        generic_alts = [f for f in figs if f["alt"].strip().lower()
                        in ("", "figure", "image")]
        self.assertEqual(
            len(generic_alts), 0,
            f"KEL137 must have 0 Figures with generic alt; got "
            f"{[(f['page'], f['alt'][:30], f['bbox']) for f in generic_alts]}",
        )

    def test_kel348_cotton_real_figures_preserved(self):
        """KEL348 Cotton has 6 author-tagged photo / diagram Figures with
        descriptive alt — the Phase 4 widening must NOT demote any of
        them (they all have descriptive author alt, so the source-intent
        gate protects them anyway, but we verify here as belt-and-suspenders)."""
        path = self.outputs.get("KEL348 Cotton.pdf")
        if not path:
            self.skipTest("KEL348 fixture not processed")
        with pikepdf.Pdf.open(path) as pdf:
            figs = self._collect_figures(pdf)
        # Six real photos / diagrams in the source — must all survive.
        self.assertGreaterEqual(
            len(figs), 6,
            f"KEL348 expected ≥6 real Figures (cotton photos), got {len(figs)}: "
            f"{[(f['page'], f['alt'][:30]) for f in figs]}",
        )
        for f in figs:
            alt_lower = f["alt"].strip().lower()
            self.assertNotIn(
                alt_lower, ("", "figure", "image"),
                f"KEL348 Figure on page {f['page']} has generic alt {f['alt']!r}",
            )


if __name__ == "__main__":
    unittest.main()
