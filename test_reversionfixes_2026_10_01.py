"""
test_reversionfixes_2026_10_01.py — Katharine 2026-10-01 (reversionfixes 12).

Two reported symptom classes, ONE root cause plus one grouping gap.

CLUSTER A — PHANTOM FIGURE OVER A REAL FIGURE
    Reported as "Acrobat's watermarking tool is now confusing the tool …
    connected to images that have extra labels added to them (like an
    in-image comment)" (Michaels, Microsoft) and as "pie graphs" the tool
    cannot label (Nissan, HP).

    The watermark itself is a red herring: Acrobat's watermark is an OCG
    Form XObject per page and image_reconciliation.detect_watermark_forms
    already handles it.  What is actually new is Acrobat *callout /
    text-box comment* overlays drawn on top of an image.

    Root cause: _merge_figure_regions drops an auto-detected vector region
    only when its *op-index range* overlaps a *source /Figure* range.  There
    is no geometric test, and no test against image figures at all.  Because
    _detect_vector_figure_regions requires do_count == 0 it never sees the
    image — only the decoration's strokes, which live in their own q…Q
    block.  So vector chrome that DECORATES a figure (an Acrobat callout
    box, a pie chart's leader lines, an exhibit's slide frame) is emitted as
    an extra sibling /Figure carrying the placeholder alt "Figure",
    overlapping the real one.

    Fix: drop an auto vector region when the union of its intersections
    with already-known figure bboxes (source figures + non-decorative image
    figures) covers >= 50% of the region's own area.

CLUSTER B — CAPTIONED EXHIBIT FRAGMENTS PER IMAGE TILE
    "Figure 3: Millennium Development Goals" (HP p6) is drawn as 8 separate
    90x90 icon images and became 8 /Figure elements; "Exhibit 4A: Marketing
    Organization Chart" (KEL189 p14) is drawn as 46 tiny tiles.  Katharine
    writes one description per caption, not one per tile.

    Fix: cluster adjacent generic-alt image figures on a page and merge each
    cluster into ONE /Figure — but ONLY when no live text block intersects
    the merged bbox.  That guard is what keeps Nissan p8's slice labels
    ("Nano 16%", "Alto 69%") and any org-chart body text out of a figure,
    where alt text would replace them.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_10_01 -v
"""
import os
import unittest

import pikepdf

import pdf_tagger as T
from pdf_extractor import extract_document


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
MICHAELS = os.path.join(FIXTURES_DIR, "kel036_michaels_watermarked.pdf")
MICROSOFT = os.path.join(FIXTURES_DIR, "kel097_microsoft_watermarked.pdf")
NISSAN = os.path.join(FIXTURES_DIR, "kel774_nissan_micra.pdf")
HP = os.path.join(FIXTURES_DIR, "kel896_hp_shared_value.pdf")
MARKETING_MSFT = os.path.join(FIXTURES_DIR, "kel189_marketing_microsoft.pdf")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _page_regions(path, pageno):
    """Return (ops, source_regions, auto_regions, image_bboxes) for one page.

    Mirrors the pre-merge state inside _insert_markers so the suppression
    decision can be exercised without running the whole pipeline.
    """
    pdf = pikepdf.open(path)
    page = pdf.pages[pageno - 1]
    raw = list(pikepdf.parse_content_stream(page))
    sfa = T._build_source_figure_alt_map(pdf).get(pageno - 1, {})
    ops, _mask, src_regions = T._strip_markers_with_source_intent(raw, sfa)
    auto = T._detect_vector_figure_regions(ops)
    return ops, src_regions, auto


def _image_bboxes(path, pageno):
    doc = extract_document(path)
    return [
        [im.bbox.x0, im.bbox.y0, im.bbox.x1, im.bbox.y1]
        for im in doc.pages[pageno - 1].images
    ]


def _overlap_fraction(region_bbox, figure_bboxes):
    """Delegate to the implementation under test."""
    return T._auto_region_figure_overlap(region_bbox, figure_bboxes)


# ---------------------------------------------------------------------------
# CLUSTER A — phantom figure suppression
# ---------------------------------------------------------------------------

class TestPhantomFigureOverlap(unittest.TestCase):
    """The geometric predicate that decides 'this region decorates a figure'."""

    def test_region_fully_inside_a_figure_is_fully_covered(self):
        # Michaels p11: the Acrobat callout box sits wholly inside the photo.
        frac = _overlap_fraction(
            [342.3, 512.2, 490.0, 614.8],
            [[94.1, 354.1, 500.1, 659.8]],
        )
        self.assertAlmostEqual(frac, 1.0, places=3)

    def test_pie_leader_lines_mostly_cover_the_pie_image(self):
        # Nissan p8, first pie: leader lines spill outside the circle toward
        # the slice labels, so coverage is partial but still a majority.
        frac = _overlap_fraction(
            [137.9, 599.0, 241.1, 644.9],
            [[129.2, 532.8, 226.4, 630.0]],
        )
        self.assertGreater(frac, 0.5)

    def test_coverage_is_the_union_across_several_figures(self):
        # HP p14: one frame region spans TWO stacked slide images.  Measured
        # against either image alone it is under half; the union is not.
        region = [90.8, 472.7, 524.2, 660.8]
        a = [91.6, 566.7, 523.5, 660.1]
        b = [91.6, 473.4, 523.5, 566.7]
        self.assertLess(_overlap_fraction(region, [a]), 0.5)
        self.assertLess(_overlap_fraction(region, [b]), 0.5)
        self.assertGreater(_overlap_fraction(region, [a, b]), 0.9)

    def test_disjoint_region_is_not_suppressed(self):
        # A standalone vector chart on a page that merely happens to carry an
        # unrelated logo must survive.
        frac = _overlap_fraction(
            [100.0, 100.0, 300.0, 300.0],
            [[400.0, 400.0, 500.0, 500.0]],
        )
        self.assertEqual(frac, 0.0)

    def test_no_figures_means_no_coverage(self):
        self.assertEqual(_overlap_fraction([1, 1, 10, 10], []), 0.0)


class TestPhantomFigureSuppressedEndToEnd(unittest.TestCase):
    """Each affected page must stop emitting the extra generic-alt figure."""

    def test_michaels_callout_region_overlaps_the_source_photo(self):
        ops, src, auto = _page_regions(MICHAELS, 11)
        self.assertEqual(len(auto), 1, "expected the callout to be detected")
        region = T._compute_region_bbox(ops, *auto[0])
        src_bboxes = [T._compute_region_bbox(ops, s, e) for s, e, _, _ in src]
        self.assertGreater(_overlap_fraction(region, src_bboxes), 0.5)

    def test_nissan_pie_leader_regions_overlap_the_pie_images(self):
        ops, _src, auto = _page_regions(NISSAN, 8)
        self.assertEqual(len(auto), 2, "expected both pies' leader lines")
        imgs = _image_bboxes(NISSAN, 8)
        for a in auto:
            region = T._compute_region_bbox(ops, *a)
            self.assertGreater(_overlap_fraction(region, imgs), 0.5)


# ---------------------------------------------------------------------------
# CLUSTER B — caption-level grouping of tiled image figures
# ---------------------------------------------------------------------------

class TestTiledFigureGrouping(unittest.TestCase):
    """Adjacent generic-alt image tiles collapse to one figure, text permitting."""

    def test_hp_mdg_icon_grid_merges_to_one_figure(self):
        # HP p6 "Figure 3: Millennium Development Goals" = 8 icons, 4x2.
        imgs = _image_bboxes(HP, 6)
        self.assertEqual(len(imgs), 8)
        clusters = T._cluster_adjacent_image_figures(imgs)
        self.assertEqual(len(clusters), 1)

    def test_marketing_microsoft_org_chart_tiles_merge_to_one_figure(self):
        # KEL189 p14 "Exhibit 4A: Marketing Organization Chart" = 46 tiles.
        imgs = _image_bboxes(MARKETING_MSFT, 14)
        self.assertGreater(len(imgs), 40)
        clusters = T._cluster_adjacent_image_figures(imgs)
        self.assertEqual(len(clusters), 1)

    def test_distant_images_do_not_merge(self):
        # Two exhibits far apart on one page stay separate.
        clusters = T._cluster_adjacent_image_figures(
            [[90.0, 600.0, 300.0, 700.0], [90.0, 100.0, 300.0, 200.0]]
        )
        self.assertEqual(len(clusters), 2)

    def test_merge_is_blocked_when_live_text_sits_inside(self):
        # Directly exercise the guard: two adjacent tiles that would merge on
        # proximity alone, with a live text block between them.  Absorbing it
        # into a /Figure would replace real text with alt text.
        tiles = [[100.0, 100.0, 200.0, 200.0], [220.0, 100.0, 320.0, 200.0]]
        self.assertEqual(len(T._cluster_adjacent_image_figures(tiles)), 1)
        self.assertEqual(len(T._mergeable_image_clusters(tiles, [])), 1)
        self.assertEqual(
            T._mergeable_image_clusters(tiles, [[205.0, 140.0, 215.0, 150.0]]),
            [],
            "a text block inside the merged bbox must block the merge",
        )

    def test_nissan_pies_are_never_merged_together(self):
        # Nissan p8: the two pies are 138pt apart and carry live slice labels
        # ("Alto 69%", "Maruti 800 5%").  They must stay two figures, so the
        # labels are never swallowed.
        doc = extract_document(NISSAN)
        page = doc.pages[7]
        imgs = [
            [im.bbox.x0, im.bbox.y0, im.bbox.x1, im.bbox.y1]
            for im in page.images
        ]
        text_bboxes = [
            [tb.bbox.x0, tb.bbox.y0, tb.bbox.x1, tb.bbox.y1]
            for tb in page.text_blocks
        ]
        self.assertEqual(T._mergeable_image_clusters(imgs, text_bboxes), [])

    def test_text_free_cluster_is_mergeable(self):
        # HP p6's icon grid has no text between the icons.
        doc = extract_document(HP)
        page = doc.pages[5]
        imgs = [
            [im.bbox.x0, im.bbox.y0, im.bbox.x1, im.bbox.y1]
            for im in page.images
        ]
        text_bboxes = [
            [tb.bbox.x0, tb.bbox.y0, tb.bbox.x1, tb.bbox.y1]
            for tb in page.text_blocks
        ]
        merged = T._mergeable_image_clusters(imgs, text_bboxes)
        self.assertEqual(len(merged), 1)


# ---------------------------------------------------------------------------
# End-to-end: the figures a reviewer actually opens the file and sees
# ---------------------------------------------------------------------------

def _run_pipeline(src, tmpdir):
    from main import process_single_pdf
    res = process_single_pdf(src, tmpdir, skip_validation=True)
    assert res.success, res.error
    return res.output_path


def _figures(path):
    """[(page_number_1based, bbox, alt)] for every /Figure in the output."""
    pdf = pikepdf.open(path)
    pm = {pg.obj.objgen: i + 1 for i, pg in enumerate(pdf.pages)}

    def page_of(node):
        pg = node.get("/Pg")
        if pg is not None:
            return pm.get(pg.objgen)
        kids = node.get("/K")
        if kids is None:
            return None
        for kid in (kids if isinstance(kids, pikepdf.Array) else [kids]):
            if isinstance(kid, pikepdf.Dictionary):
                g = kid.get("/Pg")
                if g is not None:
                    return pm.get(g.objgen)
                r = page_of(kid)
                if r is not None:
                    return r
        return None

    out = []

    def walk(node):
        if not isinstance(node, pikepdf.Dictionary):
            return
        if str(node.get("/S")) == "/Figure":
            a = node.get("/A")
            bbox = None
            if a is not None:
                try:
                    aa = a[0] if isinstance(a, pikepdf.Array) else a
                    bbox = [float(v) for v in aa.get("/BBox")]
                except Exception:
                    bbox = None
            alt = node.get("/Alt")
            out.append((page_of(node), bbox,
                        str(alt) if alt is not None else None))
        kids = node.get("/K")
        if kids is None:
            return
        for kid in (kids if isinstance(kids, pikepdf.Array) else [kids]):
            if isinstance(kid, pikepdf.Dictionary):
                walk(kid)

    st = pdf.Root.get("/StructTreeRoot")
    kids = st.get("/K") if st is not None else None
    if kids is not None:
        for k in (kids if isinstance(kids, pikepdf.Array) else [kids]):
            walk(k)
    return out


class TestEndToEndFigureCounts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.mkdtemp(prefix="revfix1001_")
        cls.michaels = _figures(_run_pipeline(MICHAELS, cls._tmp))
        cls.nissan = _figures(_run_pipeline(NISSAN, cls._tmp))
        cls.hp = _figures(_run_pipeline(HP, cls._tmp))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_michaels_has_no_phantom_callout_figure(self):
        p11 = [f for f in self.michaels if f[0] == 11]
        self.assertEqual(
            len(p11), 1,
            f"page 11 should hold only the photograph, got {p11}",
        )
        self.assertIn("shopworn", (p11[0][2] or "").lower())

    def test_michaels_keeps_every_authored_description(self):
        alts = [a for _, _, a in self.michaels]
        self.assertEqual(len(alts), 12)
        self.assertNotIn("Figure", alts, "no placeholder alt should remain")

    def test_michaels_callout_text_stays_readable(self):
        # The callout's words are live text on the page; suppressing the
        # phantom figure must not artifact them away.
        doc = extract_document(
            os.path.join(self._tmp,
                         "kel036_michaels_watermarked_accessible.pdf"))
        hits = [tb.text for p in doc.pages for tb in p.text_blocks
                if "shopworn and dirty." in tb.text]
        self.assertTrue(hits, "callout text must survive as readable text")

    def test_nissan_two_pies_give_two_figures(self):
        p8 = [f for f in self.nissan if f[0] == 8]
        self.assertEqual(len(p8), 2, f"one figure per pie, got {p8}")

    def test_hp_mdg_grid_is_one_figure(self):
        p6 = [f for f in self.hp if f[0] == 6]
        self.assertEqual(len(p6), 1, f"Figure 3 is one picture, got {p6}")

    def test_hp_slide_exhibit_is_one_figure(self):
        p14 = [f for f in self.hp if f[0] == 14]
        self.assertEqual(len(p14), 1, f"Exhibit 1 is one picture, got {p14}")


if __name__ == "__main__":
    unittest.main()
