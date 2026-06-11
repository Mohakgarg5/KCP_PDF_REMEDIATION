"""
test_reversionfixes_2026_06_11.py — Charlotte 2026-06-11 (reversionfixes 7).

Two independent root causes behind five reported defects.

CLUSTER A — figures DROPPED ("missing flow chart / curve / slide recognition"):
    _strip_markers_with_source_intent only recognised a source figure when
    the *content-stream* BDC tag was literally "/Figure".  Word / InDesign
    exports routinely mark figure content with /Shape, /InlineShape or even
    /P while the *struct tree* element is /Figure.  The struct-tree-derived
    alt map (_build_source_figure_alt_map) already knows those MCIDs belong
    to a figure, but strip ignored them because it keyed off the tag name.
    Result: whole figures (Ariba p9 HRMS/ERP flow chart, A&D p11 performance
    curve, every pure-vector iTunes slide) were never tagged.
    Fix: detect source-figure membership by MCID-in-alt-map, tag-agnostic.

    Affected fixtures: kel224_ariba, kel159_ad_hightech_b_tn, kel142_itunes_tn.

CLUSTER B — figures DOUBLED ("images doubling up with captions"):
    A logical figure whose image/form XObject is placed more than once on a
    page — with at least one placement OUTSIDE the authoritative source
    /Figure BDC range (a stray Do, or a /Figure BDC whose MCID is absent
    from the struct tree) — got tagged a SECOND time by the image/form-Do
    block path, carrying the SAME alt propagated from the source figure.
    Screen readers then announce the identical description twice.
    Fix: per page, figures sharing identical non-generic alt are merged into
    ONE /Figure StructElem (union bbox, multiple MCRs) via a shared group id.

    Affected fixtures: kel319_airfrance_case (Airline Revenue graph),
    reawakening (Retrofit Project Timeline diagram).

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_06_11 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
ARIBA = os.path.join(FIXTURES_DIR, "kel224_ariba.pdf")
AD_TN = os.path.join(FIXTURES_DIR, "kel159_ad_hightech_b_tn.pdf")
ITUNES_TN = os.path.join(FIXTURES_DIR, "kel142_itunes_tn.pdf")
AIRFRANCE = os.path.join(FIXTURES_DIR, "kel319_airfrance_case.pdf")
REAWAKENING = os.path.join(FIXTURES_DIR, "reawakening.pdf")


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
    tmp = tempfile.mkdtemp(prefix="rev7_")
    res = process_single_pdf(path, tmp, skip_validation=True)
    return tmp, (res.output_path if res.success else None)


def _generic(alt):
    return alt.strip().lower() in ("", "figure", "image")


# --------------------------------------------------------------------------- #
# CLUSTER A — dropped figures must be preserved
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(ARIBA), "kel224_ariba.pdf missing")
class TestAribaFlowChartPreserved(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(ARIBA)
        cls.alts = _figure_alts(pikepdf.open(cls.out)) if cls.out else []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_main_hrms_erp_flowchart_present(self):
        # Source p9 /Figure (mcids 0-12) marked with /Shape + /P, not /Figure.
        hits = [a for a in self.alts if "hrms" in a.lower() or "erp system" in a.lower()]
        self.assertTrue(
            hits, f"HRMS/ERP flow chart figure missing; figures={self.alts}")

    def test_all_three_source_figures_present(self):
        # p9 flowchart + p10 pipeline flowchart + p10 Gantt chart
        self.assertGreaterEqual(
            len([a for a in self.alts if not _generic(a)]), 3,
            f"expected >=3 described figures; got {self.alts}")


@unittest.skipUnless(os.path.exists(AD_TN), "kel159_ad_hightech_b_tn.pdf missing")
class TestADPerformanceCurvePreserved(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(AD_TN)
        cls.alts = _figure_alts(pikepdf.open(cls.out)) if cls.out else []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_performance_curve_present(self):
        # Source p11 /Figure (mcids 2-8) marked with /InlineShape + /P.
        hits = [a for a in self.alts if "performance curve" in a.lower()]
        self.assertTrue(
            hits, f"performance-curve figure missing; figures={self.alts}")

    def test_both_source_figures_present(self):
        self.assertGreaterEqual(
            len([a for a in self.alts if not _generic(a)]), 2,
            f"expected >=2 described figures; got {self.alts}")


@unittest.skipUnless(os.path.exists(ITUNES_TN), "kel142_itunes_tn.pdf missing")
class TestITunesSlideRecognition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(ITUNES_TN)
        cls.alts = _figure_alts(pikepdf.open(cls.out)) if cls.out else []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_slides_recognized(self):
        # Source carries 9 slide /Figure SEs (1 "Title slide" + 8 "Slide
        # titled ...") all marked /Shape + /P in the content stream.
        slideish = [a for a in self.alts if "slide" in a.lower()]
        self.assertGreaterEqual(
            len(slideish), 8,
            f"expected >=8 slide figures; got {len(slideish)}: {self.alts}")

    def test_specific_slides_present(self):
        joined = " || ".join(self.alts).lower()
        for needle in ("what can go wrong", "recent execution",
                       "highlights of apple history"):
            self.assertIn(needle, joined,
                          f"slide {needle!r} missing; figures={self.alts}")


# --------------------------------------------------------------------------- #
# CLUSTER B — doubled figures must collapse to one
# --------------------------------------------------------------------------- #
class _NoDuplicateAltMixin:
    PATH = None
    UNIQUE_SUBSTR = None

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(cls.PATH)
        cls.alts = _figure_alts(pikepdf.open(cls.out)) if cls.out else []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_duplicate_descriptive_alts(self):
        described = [a.lower() for a in self.alts if not _generic(a)]
        dups = {a for a in described if described.count(a) > 1}
        self.assertFalse(
            dups, f"duplicate /Figure alts (doubling): {dups}")

    def test_target_figure_appears_once(self):
        n = sum(1 for a in self.alts if self.UNIQUE_SUBSTR in a.lower())
        self.assertEqual(
            n, 1,
            f"{self.UNIQUE_SUBSTR!r} should appear exactly once; got {n}: {self.alts}")


@unittest.skipUnless(os.path.exists(AIRFRANCE), "kel319_airfrance_case.pdf missing")
class TestAirFranceNoDoubling(_NoDuplicateAltMixin, unittest.TestCase):
    PATH = AIRFRANCE
    UNIQUE_SUBSTR = "airline revenue and global economic growth"


@unittest.skipUnless(os.path.exists(REAWAKENING), "reawakening.pdf missing")
class TestReawakeningNoDoubling(_NoDuplicateAltMixin, unittest.TestCase):
    PATH = REAWAKENING
    UNIQUE_SUBSTR = "timeline diagram showing key activities"


if __name__ == "__main__":
    unittest.main(verbosity=2)
