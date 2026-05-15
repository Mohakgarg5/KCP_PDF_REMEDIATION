"""
test_vector_figures.py — Regression tests for graph/vector-figure preservation.

These tests guard against the regression Charlotte reported: charts, line
graphs, bar graphs, scatter plots, and pie charts drawn as raw vector path
operators in the page content stream were being tagged as /Artifact
(decorative) instead of /Figure, making them invisible to screen readers.

Fixtures:
  sugar_daddy_tagged_graphs.pdf — InDesign-tagged Kellogg case with a line
    graph (page 3 ≈ 9170 path ops) and a pie chart-style page (page 5/6).
    Source PDF already contains /Figure tags around the vector content.

Run:
    ./venv/bin/python -m unittest test_vector_figures -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf

from main import process_single_pdf
from test_pipeline_integration import (
    FIXTURES_DIR,
    _collect_figures_and_artifacts,
)


def _page_struct_type_counts(pdf):
    """Per-page counter of BDC/BMC tag types in the content stream.

    Returns {page_idx: {struct_type: count}} where struct_type is the
    /Tag from BDC operands or "BMC:/Tag" for BMC.  Used to assert that
    vector path content on a given page is wrapped as /Figure rather
    than /Artifact.
    """
    result: dict[int, dict[str, int]] = {}
    for i, page in enumerate(pdf.pages):
        counts: dict[str, int] = {}
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception:
            result[i] = counts
            continue
        for operands, op in ops:
            op_b = bytes(op)
            if op_b == b"BDC" and operands:
                key = str(operands[0])
                counts[key] = counts.get(key, 0) + 1
            elif op_b == b"BMC" and operands:
                key = "BMC:" + str(operands[0])
                counts[key] = counts.get(key, 0) + 1
        result[i] = counts
    return result


class TestVectorFigurePreservation(unittest.TestCase):
    """Sugar Daddy regression: graphs drawn with raw path operators must be
    wrapped in /Figure structure elements, not /Artifact."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="vector_figures_")
        fixture = os.path.join(FIXTURES_DIR, "sugar_daddy_tagged_graphs.pdf")
        if not os.path.exists(fixture):
            raise unittest.SkipTest(f"Fixture missing: {fixture}")
        result = process_single_pdf(fixture, cls.tmpdir, skip_validation=True)
        if not result.success:
            raise RuntimeError(f"Pipeline failed: {result.error}")
        cls.output_path = result.output_path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_output_struct_tree_contains_figures(self):
        """Output must contain at least one /Figure struct element.

        The Sugar Daddy fixture has 68 /Figure elements in its source
        StructTreeRoot (55 on page 3, 1 on page 5, 12 on page 6).  Prior
        to the fix, the output had ZERO Figures across 8 pages.
        """
        with pikepdf.Pdf.open(self.output_path) as pdf:
            figures, _ = _collect_figures_and_artifacts(pdf)
            self.assertGreater(
                len(figures), 0,
                "Output struct tree contains 0 /Figure elements — every "
                "graph was lost.  Source PDF has 68 Figures.",
            )

    def test_graph_page_has_figure_bdc(self):
        """Page 3 (large line graph) must have at least one /Figure BDC
        wrapper in its content stream.

        Pre-fix: page 3 had 599 BMC /Artifact wrappers and 0 /Figure
        wrappers — the entire graph was tagged as decorative.
        """
        with pikepdf.Pdf.open(self.output_path) as pdf:
            counts = _page_struct_type_counts(pdf)
        page3 = counts.get(3, {})
        figure_count = page3.get("/Figure", 0)
        self.assertGreater(
            figure_count, 0,
            f"Page 3 (line graph, ~9170 path ops) has 0 /Figure BDC "
            f"wrappers in content stream. Tags found: {page3}",
        )

    def test_graph_pages_have_fewer_artifact_wrappers_than_source(self):
        """Vector-heavy pages should not be exploded into hundreds of
        BMC /Artifact wrappers.

        Pre-fix: page 3 had 599 BMC /Artifact wrappers, page 5 had 85,
        page 6 had 68 — one wrapper per graphics-state save (q) around
        path operators.  Post-fix: vector clusters consolidated into
        /Figure wrappers, so artifact count should drop significantly.
        """
        with pikepdf.Pdf.open(self.output_path) as pdf:
            counts = _page_struct_type_counts(pdf)
        # Page 3 had 599 BMC /Artifact wrappers before the fix.  Allow a
        # generous ceiling: anything < 100 indicates vector content was
        # consolidated into Figure(s) instead of fragmented as Artifact.
        page3_artifacts = counts.get(3, {}).get("BMC:/Artifact", 0)
        self.assertLess(
            page3_artifacts, 100,
            f"Page 3 still has {page3_artifacts} BMC /Artifact wrappers "
            f"(was 599 pre-fix). Vector cluster consolidation failed.",
        )


if __name__ == "__main__":
    unittest.main()
