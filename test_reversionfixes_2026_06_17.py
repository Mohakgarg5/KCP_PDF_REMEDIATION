"""
test_reversionfixes_2026_06_17.py — Katharine 2026-06-17 (reversionfixes 8).

ONE root cause behind four reported defects: solid vector shapes that sit
*inside* tables get promoted to /Figure by the auto vector-figure detector.

    _detect_vector_figure_regions() clusters runs of path operators into
    /Figure regions so InDesign/Word charts drawn as raw paths become
    visible to screen readers.  It already drops regions the author marked
    /Artifact and regions matching repeating banners — but nothing checks
    whether a region lands inside a table.  Cell-background fills therefore
    become bogus /Figure elements:

      * KEL334 PurplePill   — blank cell fills            (2 generic figures)
      * KEL225 Ariba Med-X  — a red circle in a cell      (1 generic figure)
      * KEL149 B&K          — blue "fill-in" boxes p18-21  (8 generic figures)
      * KEL150 B&K_TN       — color-coded cell fills       (8 generic figures)

    Every one of these carries no descriptive /Alt (the placeholder "Figure")
    because it is decoration, not an image.  None are source-tagged figures.

    Fix: in _insert_markers, before auto regions become figures, drop any
    auto region whose bbox lies substantially within the union of table-cell
    (/TD,/TH) bboxes on the page.  The vector ops then fall through to normal
    /Artifact handling — Katharine's "they've gotta go".  Authoritative
    source figures (charts, diagrams, screenshots) are untouched because the
    guard only filters auto-detected regions.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_06_17 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
PURPLEPILL = os.path.join(FIXTURES_DIR, "kel334_purplepill.pdf")
ARIBA_TN = os.path.join(FIXTURES_DIR, "kel225_ariba_medx_tn.pdf")
BK = os.path.join(FIXTURES_DIR, "kel149_bk.pdf")
BK_TN = os.path.join(FIXTURES_DIR, "kel150_bk_tn.pdf")


def _figure_alts(pdf):
    """Return [alt_text] for every /Figure struct element (NUL-stripped)."""
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
            alt = n.get("/Alt")
            out.append(str(alt).replace("\x00", "").strip() if alt is not None else "")
        k = n.get("/K")
        if k is not None:
            walk(k)

    st = pdf.Root.get("/StructTreeRoot")
    if st is not None:
        walk(st.get("/K"))
    return out


def _process(path):
    from main import process_single_pdf
    tmp = tempfile.mkdtemp(prefix="rev8_")
    res = process_single_pdf(path, tmp, skip_validation=True)
    return tmp, (res.output_path if res.success else None)


def _generic(alt):
    return alt.strip().lower() in ("", "figure", "image")


# --------------------------------------------------------------------------- #
# Unit: the table-overlap guard (deterministic, no PDF)
# --------------------------------------------------------------------------- #
class TestRegionInsideTable(unittest.TestCase):
    def setUp(self):
        from pdf_tagger import _region_inside_table
        self.f = _region_inside_table

    def test_region_fully_inside_one_cell_is_inside(self):
        # blue box drawn inside a single table cell
        cells = [(100.0, 400.0, 300.0, 500.0)]
        region = (120.0, 420.0, 280.0, 480.0)
        self.assertTrue(self.f(region, cells))

    def test_region_spanning_whole_table_is_inside(self):
        # color-coded fill covering a 2x2 grid of cells (whole table)
        cells = [
            (100.0, 400.0, 200.0, 500.0), (200.0, 400.0, 300.0, 500.0),
            (100.0, 300.0, 200.0, 400.0), (200.0, 300.0, 300.0, 400.0),
        ]
        region = (100.0, 300.0, 300.0, 500.0)
        self.assertTrue(self.f(region, cells))

    def test_standalone_chart_outside_cells_is_not_inside(self):
        # a real chart sitting in the body, no table cells overlapping it
        cells = [(100.0, 400.0, 300.0, 500.0)]
        region = (100.0, 60.0, 500.0, 350.0)
        self.assertFalse(self.f(region, cells))

    def test_region_barely_clipping_a_cell_is_not_inside(self):
        # a chart whose corner grazes a cell — below the coverage threshold
        cells = [(100.0, 400.0, 300.0, 500.0)]
        region = (290.0, 490.0, 600.0, 700.0)  # tiny 10x10 overlap of a big region
        self.assertFalse(self.f(region, cells))

    def test_no_cells_means_not_inside(self):
        self.assertFalse(self.f((10.0, 10.0, 50.0, 50.0), []))

    def test_degenerate_region_is_not_inside(self):
        cells = [(100.0, 400.0, 300.0, 500.0)]
        self.assertFalse(self.f((150.0, 450.0, 150.0, 450.0), cells))
        self.assertFalse(self.f(None, cells))


# --------------------------------------------------------------------------- #
# Integration: bogus in-table figures gone, real figures preserved
# --------------------------------------------------------------------------- #
class _InTableFigureMixin:
    PATH = None
    MIN_DESCRIBED = 0          # source figures that must survive
    DESCRIBED_NEEDLES = ()     # substrings that must still be present

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(cls.PATH)
        cls.alts = _figure_alts(pikepdf.open(cls.out)) if cls.out else []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_generic_figures_remain(self):
        # the in-table vector shapes (blank fills / red circle / blue boxes /
        # color fills) carried placeholder alt and must now be artifacts.
        generic = [a for a in self.alts if _generic(a)]
        self.assertEqual(
            generic, [],
            f"in-table vector shapes still tagged /Figure: {generic} "
            f"(all figures: {self.alts})")

    def test_source_figures_preserved(self):
        described = [a for a in self.alts if not _generic(a)]
        self.assertGreaterEqual(
            len(described), self.MIN_DESCRIBED,
            f"lost authoritative figures; described={described}")

    def test_named_figures_present(self):
        joined = " || ".join(self.alts).lower()
        for needle in self.DESCRIBED_NEEDLES:
            self.assertIn(needle, joined,
                          f"figure {needle!r} missing; figures={self.alts}")


@unittest.skipUnless(os.path.exists(PURPLEPILL), "kel334_purplepill.pdf missing")
class TestPurplePillBlanks(_InTableFigureMixin, unittest.TestCase):
    PATH = PURPLEPILL
    MIN_DESCRIBED = 9
    DESCRIBED_NEEDLES = ("funnel-like attrition", "esomeprazole")


@unittest.skipUnless(os.path.exists(ARIBA_TN), "kel225_ariba_medx_tn.pdf missing")
class TestAribaRedCircle(_InTableFigureMixin, unittest.TestCase):
    PATH = ARIBA_TN
    MIN_DESCRIBED = 5
    DESCRIBED_NEEDLES = ("critical ratio",)


@unittest.skipUnless(os.path.exists(BK), "kel149_bk.pdf missing")
class TestBKBlueBoxes(_InTableFigureMixin, unittest.TestCase):
    PATH = BK
    MIN_DESCRIBED = 8
    DESCRIBED_NEEDLES = ("network infrastructure",)


@unittest.skipUnless(os.path.exists(BK_TN), "kel150_bk_tn.pdf missing")
class TestBKTNTablesMarked(_InTableFigureMixin, unittest.TestCase):
    PATH = BK_TN
    MIN_DESCRIBED = 10
    DESCRIBED_NEEDLES = ("screenshot of excel",)


if __name__ == "__main__":
    unittest.main(verbosity=2)
