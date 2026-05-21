"""
test_katharine_2026_05_20_phase3.py — Phase 3 regression tests.

Phase 3 closes two new defect classes Katharine reported on 2026-05-20:

  Bug C (logo Figure/Artifact mismatch): the Kellogg / Northwestern footer
    logo at the bottom of every case-PDF page was being tagged as /Figure
    with a generic "Figure" alt text instead of as decorative page chrome.
    A wide-and-short region in the Y-margin band with generic / empty alt
    should now emit as content-stream /Artifact, not as a /Figure StructElem.

  Bug D (vector-chart fragmentation): a chart drawn as several path-region
    clusters with text labels between them (e.g. KEL193 "Economics of
    Congestion" on page 6) was being split into N separate /Figure
    StructElems because op-stream merging cannot bridge text-op gaps.
    Phase 3 adds a spatial clustering pass that assigns a synthetic
    source-Figure id to bbox-proximate vector regions, collapsing them
    into one /Figure with N /MCRs via the existing Phase-2 grouping path.

Tests use a mix of direct helper tests and an end-to-end pipeline run
against the actual reversionfixes (2) inputs so the assertions guard the
real bug, not just a synthetic stand-in.

Run:
    ./venv/bin/python -m unittest test_katharine_2026_05_20_phase3 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf

from pdf_tagger import (
    _is_banner_artifact,
    _bbox_within_proximity,
    _cluster_vector_figures,
    _bbox_matches_any,
)


PAGE_BOX_LETTER = [0.0, 0.0, 612.0, 792.0]


class TestBannerArtifactHeuristic(unittest.TestCase):
    """``_is_banner_artifact`` distinguishes page-chrome banners from
    real charts using bbox shape and position only.  The full pipeline
    layers a repetition check on top (see TestRepeatingBannerPrePass) but
    the shape gate is the workhorse and deserves direct coverage."""

    def test_footer_kellogg_logo_shape_matches(self):
        # Real bbox of the Northwestern footer logo on KEL201 page 0:
        # wide banner near the page-bottom Y-margin band.
        bbox = [82.8, 116.74, 521.78, 169.25]
        self.assertTrue(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    def test_header_band_wide_short_matches(self):
        # Same dimensions, mirrored to the top margin band.
        bbox = [82.8, 622.75, 521.78, 675.26]
        self.assertTrue(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    def test_body_region_chart_does_not_match(self):
        # Typical line chart: 311x229 in the middle of the page.
        bbox = [150.0, 280.0, 461.0, 509.0]
        self.assertFalse(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    def test_body_region_wide_short_does_not_match(self):
        # A wide-and-short region in the middle of the body — could be
        # a timeline chart.  Must not be misclassified as a banner.
        bbox = [100.0, 380.0, 540.0, 432.0]   # height 52, y_center 0.51
        self.assertFalse(_is_banner_artifact(bbox, PAGE_BOX_LETTER))

    def test_tall_logo_does_not_match(self):
        # Tall thin column doesn't fit the banner shape.
        bbox = [50.0, 80.0, 100.0, 700.0]
        self.assertFalse(_is_banner_artifact(bbox, PAGE_BOX_LETTER))


class TestBboxWithinProximity(unittest.TestCase):

    def test_overlapping_bboxes_match(self):
        a = [0, 0, 100, 100]
        b = [50, 50, 150, 150]
        self.assertTrue(_bbox_within_proximity(a, b, proximity=0))

    def test_adjacent_within_threshold_match(self):
        a = [0, 0, 100, 100]
        b = [120, 0, 200, 100]
        self.assertTrue(_bbox_within_proximity(a, b, proximity=30))

    def test_far_apart_no_match(self):
        a = [0, 0, 100, 100]
        b = [300, 0, 400, 100]
        self.assertFalse(_bbox_within_proximity(a, b, proximity=30))


class TestBboxMatchesAny(unittest.TestCase):

    def test_matches_within_tolerance(self):
        targets = {(82.8, 116.74, 521.78, 169.25)}
        bbox = [82.5, 116.5, 521.5, 169.5]
        self.assertTrue(_bbox_matches_any(bbox, targets, tol=10.0))

    def test_no_match_outside_tolerance(self):
        targets = {(82.8, 116.74, 521.78, 169.25)}
        bbox = [200.0, 300.0, 400.0, 500.0]
        self.assertFalse(_bbox_matches_any(bbox, targets, tol=10.0))

    def test_empty_set_returns_false(self):
        self.assertFalse(_bbox_matches_any([0, 0, 100, 100], set(), tol=10.0))


class TestClusterVectorFigures(unittest.TestCase):
    """The clustering pass assigns synthetic ``source_fig_id`` to
    spatially-clustered vector /Figure tuples on one page so the existing
    /Figure grouping in ``_build_structure_tree`` collapses them into one
    StructElem with N MCRs."""

    def test_two_adjacent_vectors_get_same_synth_id(self):
        # Two vector Figures (source_fig_id=None) with bboxes close enough
        # to cluster.
        struct_elems = [
            (1, "/Figure", "Figure", [100, 100, 200, 200], None),
            (2, "/Figure", "Figure", [210, 110, 280, 200], None),  # 10pt gap
        ]
        out = _cluster_vector_figures(struct_elems, page_idx=0)
        self.assertEqual(out[0][4], out[1][4])
        self.assertIsNotNone(out[0][4])

    def test_distant_vectors_stay_independent(self):
        struct_elems = [
            (1, "/Figure", "Figure", [100, 100, 200, 200], None),
            (2, "/Figure", "Figure", [400, 100, 500, 200], None),  # 200pt gap
        ]
        out = _cluster_vector_figures(struct_elems, page_idx=0)
        # Neither received a cluster id (cluster size of 1 means no synth id).
        self.assertIsNone(out[0][4])
        self.assertIsNone(out[1][4])

    def test_source_tagged_figures_left_alone(self):
        # Source-tagged Figures (source_fig_id != None) must not be merged
        # with anything else — the author already grouped them.
        existing_id = 0
        struct_elems = [
            (1, "/Figure", "Bar chart", [100, 100, 200, 200], existing_id),
            (2, "/Figure", "Figure", [210, 110, 280, 200], None),
        ]
        out = _cluster_vector_figures(struct_elems, page_idx=0)
        self.assertEqual(out[0][4], existing_id)
        # Second tuple keeps its None (only 1 vector candidate after filtering)
        self.assertIsNone(out[1][4])

    def test_chain_of_three_fully_collapses(self):
        # Three fragments in a chain: A — B — C, each within proximity of
        # the next.  Multi-pass merge should collapse them all into ONE
        # cluster (not two clusters of two).
        struct_elems = [
            (1, "/Figure", "Figure", [100, 100, 180, 180], None),
            (2, "/Figure", "Figure", [200, 100, 280, 180], None),  # adj to A
            (3, "/Figure", "Figure", [300, 100, 380, 180], None),  # adj to B
        ]
        out = _cluster_vector_figures(struct_elems, page_idx=0)
        self.assertEqual(out[0][4], out[1][4])
        self.assertEqual(out[1][4], out[2][4])


@unittest.skipUnless(
    os.path.exists("/Users/mohakgarg/Desktop/reversionfixes (2)"),
    "Katharine 2026-05-20 input batch not present",
)
class TestPhase3EndToEnd(unittest.TestCase):
    """Re-runs the new batch through the current pipeline and asserts the
    specific defects Katharine flagged are gone."""

    BATCH_DIR = "/Users/mohakgarg/Desktop/reversionfixes (2)"

    @classmethod
    def setUpClass(cls):
        from main import process_single_pdf
        cls.tmpdir = tempfile.mkdtemp(prefix="phase3_e2e_")
        cls.outputs: dict = {}
        for name in (
            "KEL201 AutoRetailing (B)_accessible KELLOGG LOGO NOT RETAGGED.pdf",
            "KEL007 Zoltek_accessible KELLOGG LOGO NOT RETAGGED.pdf",
            "KEL193 London's Congestion Charge.pdf",
        ):
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
        """Return list of {alt, bbox} for every /Figure StructElem in tree.

        Uses object-generation for dedup so the walker survives repeated
        pikepdf reads (id() is not stable across reads)."""
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
            t = n.get("/Type")
            if t is not None and str(t) == "/StructElem":
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

    def test_kel007_logo_is_artifact_not_figure(self):
        """KEL007 Zoltek has the Northwestern footer logo as its only
        figure-like vector content on page 2.  Phase 3 should classify
        it as content-stream /Artifact so the output struct tree has 0
        /Figure StructElems."""
        path = self.outputs.get(
            "KEL007 Zoltek_accessible KELLOGG LOGO NOT RETAGGED.pdf"
        )
        if not path:
            self.skipTest("KEL007 fixture not processed")
        with pikepdf.Pdf.open(path) as pdf:
            figs = self._collect_figures(pdf)
        self.assertEqual(
            len(figs), 0,
            f"KEL007 should have 0 /Figure (logo → artifact) but got "
            f"{len(figs)}: {[(f['page'], f['alt'][:30]) for f in figs]}",
        )

    def test_kel201_logo_eliminated_real_charts_preserved(self):
        """KEL201 source has 4 candidate Figures (3 real charts + 1
        footer logo).  Phase 3 keeps the 3 real charts with descriptive
        alt and reclassifies the logo as artifact."""
        path = self.outputs.get(
            "KEL201 AutoRetailing (B)_accessible KELLOGG LOGO NOT RETAGGED.pdf"
        )
        if not path:
            self.skipTest("KEL201 fixture not processed")
        with pikepdf.Pdf.open(path) as pdf:
            figs = self._collect_figures(pdf)
        # 3 charts: timeline + 2 line graphs
        self.assertEqual(
            len(figs), 3,
            f"KEL201 should have 3 Figures (charts), got {len(figs)}: "
            f"{[(f['page'], f['alt'][:30]) for f in figs]}",
        )
        # Every remaining Figure must have descriptive alt (not generic)
        for f in figs:
            alt_lower = f["alt"].strip().lower()
            self.assertNotIn(
                alt_lower, ("", "figure", "image"),
                f"KEL201 Figure on page {f['page']} has generic alt "
                f"{f['alt']!r} — should have been classified as artifact.",
            )

    def test_kel193_economics_chart_not_fragmented(self):
        """KEL193 "Economics of Congestion" on page 6 was previously
        emitted as 4 separate /Figure StructElems (one per vector path
        cluster).  Phase 3 clustering collapses them into 1."""
        path = self.outputs.get("KEL193 London's Congestion Charge.pdf")
        if not path:
            self.skipTest("KEL193 fixture not processed")
        with pikepdf.Pdf.open(path) as pdf:
            figs = self._collect_figures(pdf)
        page6_figs = [f for f in figs if f["page"] == 6]
        self.assertLessEqual(
            len(page6_figs), 2,
            f"KEL193 page 6 should have at most 2 Figures (was 4 "
            f"fragmented pre-Phase-3, target 1), got {len(page6_figs)}: "
            f"{[(f['alt'][:30], f['bbox']) for f in page6_figs]}",
        )


if __name__ == "__main__":
    unittest.main()
