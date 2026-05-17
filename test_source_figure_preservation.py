"""
test_source_figure_preservation.py — Regression tests for source /Figure preservation.

Guards the bug Mohak reported on Micawber Capital: when a source PDF wraps a
flowchart/map as a single /Figure BDC...EMC range whose interior alternates
between vector path operators and text labels (typical of InDesign-exported
diagrams), the tagger discarded the source range, the vector-figure detector
fragmented the diagram into N pieces (one per text-separated path block), and
the source's /Alt was lost.  The downstream side-effect: users would mark the
fragments as artifacts in Adobe, which inserts /S /Artifact struct elements
that PAC then flags as non-standard structure types (the "role mapping" error
with nothing visible to fix).

Run:
    ./venv/bin/python -m unittest test_source_figure_preservation -v
"""
import os
import re
import shutil
import tempfile
import unittest

import pikepdf

from main import process_single_pdf
from test_pipeline_integration import (
    FIXTURES_DIR,
    _collect_figures_and_artifacts,
)

MICAWBER = "micawber_capital.pdf"


class TestSourceFigurePreservation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="source_figure_preservation_")
        src = os.path.join(FIXTURES_DIR, MICAWBER)
        if not os.path.exists(src):
            cls.skip_reason = f"Fixture missing: {src}"
            cls.result = None
            return
        cls.skip_reason = None
        cls.result = process_single_pdf(src, cls.tmpdir, skip_validation=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        if self.skip_reason:
            self.skipTest(self.skip_reason)
        self.assertTrue(self.result.success, f"Pipeline failed: {self.result.error}")

    def _open_output(self):
        return pikepdf.Pdf.open(self.result.output_path)

    def _open_source(self):
        return pikepdf.Pdf.open(os.path.join(FIXTURES_DIR, MICAWBER))

    def test_flowchart_not_fragmented(self):
        """The NGO/NBFC funding flowchart (page idx 10) must stay one figure,
        not the 7 fragments produced by the old vector-figure detector."""
        out = self._open_output()
        try:
            page = out.pages[10]
            cs = page.Contents
            if isinstance(cs, pikepdf.Array):
                data = b"".join(s.read_bytes() for s in cs)
            else:
                data = cs.read_bytes()
            txt = data.decode("latin-1", errors="ignore")
            figure_bdc_count = len(re.findall(r"/Figure\s*<<[^>]*MCID", txt))
            # Source page 11 (idx 10) has 2 /Figure BDCs (the funding diagrams).
            # Pre-fix output had 8.  Allow up to 3 to absorb minor detector noise.
            self.assertLessEqual(
                figure_bdc_count, 3,
                f"Page 11 emitted {figure_bdc_count} /Figure BDCs — flowchart "
                f"is being fragmented again."
            )
        finally:
            out.close()

    def test_no_artifact_struct_types(self):
        """The output must contain ZERO struct elements with /S /Artifact.
        Artifact is a marked-content operator, not a struct type — emitting it
        in the structure tree triggers PAC's 'non-standard structure type'
        error with no fixable target in the role map."""
        out = self._open_output()
        try:
            artifact_struct_count = 0
            stack = [out.Root["/StructTreeRoot"]]
            seen = set()
            while stack:
                node = stack.pop()
                if not isinstance(node, pikepdf.Dictionary):
                    continue
                try:
                    key = node.objgen
                except Exception:
                    key = id(node)
                if key in seen:
                    continue
                seen.add(key)
                if str(node.get("/S", "")) == "/Artifact":
                    artifact_struct_count += 1
                kids = node.get("/K")
                if isinstance(kids, pikepdf.Array):
                    for k in kids:
                        if isinstance(k, pikepdf.Dictionary):
                            stack.append(k)
                elif isinstance(kids, pikepdf.Dictionary):
                    stack.append(kids)
            self.assertEqual(
                artifact_struct_count, 0,
                f"Found {artifact_struct_count} /S /Artifact struct elements "
                f"— Artifact is not a valid struct type."
            )
        finally:
            out.close()

    def test_source_figure_count_preserved(self):
        """Output /Figure count must not exceed source /Figure count by more
        than a small margin (one diagram → one figure, not N fragments)."""
        out = self._open_output()
        src = self._open_source()
        try:
            src_figs, _ = _collect_figures_and_artifacts(src)
            out_figs, _ = _collect_figures_and_artifacts(out)
            # Source has 5 /Figure elements; allow at most +2 for incidental
            # raster images the source missed.  Pre-fix output had 12.
            self.assertLessEqual(
                len(out_figs), len(src_figs) + 2,
                f"Output has {len(out_figs)} figures vs source {len(src_figs)} "
                f"— vector figures are being split."
            )
        finally:
            out.close()
            src.close()

    def test_source_alt_texts_survive(self):
        """All five source /Figure alts must appear verbatim in the output."""
        out = self._open_output()
        src = self._open_source()
        try:
            src_figs, _ = _collect_figures_and_artifacts(src)
            out_figs, _ = _collect_figures_and_artifacts(out)
            src_alts = {
                " ".join(str(f.get("/Alt", "")).replace("\x00", "").split())
                for f in src_figs
                if f.get("/Alt") and str(f.get("/Alt")).strip()
            }
            out_alts = {
                " ".join(str(f.get("/Alt", "")).replace("\x00", "").split())
                for f in out_figs
                if f.get("/Alt") and str(f.get("/Alt")).strip()
            }
            missing = src_alts - out_alts
            self.assertEqual(
                missing, set(),
                f"Source alts dropped from output: {sorted(missing)[:3]}"
            )
        finally:
            out.close()
            src.close()


if __name__ == "__main__":
    unittest.main()
