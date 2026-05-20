"""
test_katharine_2026_05_20.py — Regression tests for Katharine's 2026-05-20 follow-up.

Two bugs:

  Bug B  "Excessive figure tagging" on KEL201 (54 /Figure StructElems for 3
         actual diagrams).  Root cause: a source /Figure StructElem with N
         BDC ranges in the content stream produced N independent output
         /Figure StructElems instead of one StructElem with N /MCRs.
         Result: screen readers announce the same alt text 18× per chart.

  Bug A  /S=/Artifact StructElems shipped in older pipeline outputs.  PAC
         flags every one ("non-standard structure type 'Artifact'") under
         role-mapping checks.  Defensive sanitizer aligns /S to the actual
         content-stream BDC tag so legacy outputs heal when re-processed.
"""
import os
import shutil
import tempfile
import unittest
from collections import Counter

import pikepdf

from main import process_single_pdf
import pdf_postprocess


DRRC_DIR = "/Users/mohakgarg/Desktop/DRRC Documents/reversionfixes (1)"
KEL201_SRC = os.path.join(DRRC_DIR, "KEL201 AutoRetailing (B).pdf")
ZOLTEK_OUTPUT = os.path.join(DRRC_DIR, "KEL007 Zoltek_accessible.pdf")
STEEL_WARS_OUTPUT = os.path.join(DRRC_DIR, "KEL002 Steel Wars_accessible (1).pdf")
KEL201_RETAGGED = os.path.join(
    DRRC_DIR,
    "KEL201 AutoRetailing (B)_accessible RETAGGED AFTER LATEST TOOL UPDATE.pdf",
)


def _walk_counter(stroot):
    c, seen = Counter(), set()

    def w(n):
        if not isinstance(n, pikepdf.Dictionary):
            return
        og = n.objgen if hasattr(n, "objgen") else None
        if og in seen:
            return
        if og is not None:
            seen.add(og)
        s = n.get("/S")
        c[str(s) if s else None] += 1
        k = n.get("/K")
        if isinstance(k, pikepdf.Array):
            for x in k:
                if isinstance(x, pikepdf.Dictionary):
                    w(x)
        elif isinstance(k, pikepdf.Dictionary):
            w(k)

    w(stroot)
    return c


class TestBugBFigureGrouping(unittest.TestCase):
    """KEL201 source has 3 /Figure StructElems (one per actual chart) with
    multiple BDC ranges each.  Output must have a small number (≤5), not 54."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(KEL201_SRC):
            cls.skip = f"Fixture missing: {KEL201_SRC}"
            return
        cls.skip = None
        cls.tmpdir = tempfile.mkdtemp(prefix="kath_05_20_")
        local = os.path.join(cls.tmpdir, "KEL201.pdf")
        shutil.copy(KEL201_SRC, local)
        cls.result = process_single_pdf(local, cls.tmpdir, skip_validation=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "tmpdir", None):
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        if self.skip:
            self.skipTest(self.skip)
        self.assertTrue(self.result.success)

    def test_figure_count_grouped(self):
        pdf = pikepdf.open(self.result.output_path)
        try:
            counts = _walk_counter(pdf.Root["/StructTreeRoot"])
            # Pre-fix: 54.  Post-fix: 4 (3 source-grouped + 1 auto on page 0).
            self.assertLessEqual(
                counts.get("/Figure", 0), 5,
                f"KEL201 produced {counts.get('/Figure', 0)} /Figure "
                f"StructElems — source figures must be grouped, not "
                f"emitted per BDC range."
            )
        finally:
            pdf.close()

    def test_no_artifact_struct_types(self):
        pdf = pikepdf.open(self.result.output_path)
        try:
            counts = _walk_counter(pdf.Root["/StructTreeRoot"])
            self.assertEqual(counts.get("/Artifact", 0), 0)
        finally:
            pdf.close()


class TestBugASanitizerHealsLegacyArtifacts(unittest.TestCase):
    """Pre-Phase-1 pipeline shipped files with /S=/Artifact StructElems whose
    content streams said /Figure BDC.  The healer aligns /S to the stream
    tag so PAC role-mapping passes."""

    def _heal_in_place(self, src_path):
        if not os.path.exists(src_path):
            self.skipTest(f"Fixture missing: {src_path}")
        tmp = tempfile.mkdtemp(prefix="heal_test_")
        try:
            local = os.path.join(tmp, os.path.basename(src_path))
            shutil.copy(src_path, local)
            pdf = pikepdf.open(local, allow_overwriting_input=True)
            before = _walk_counter(pdf.Root["/StructTreeRoot"])
            pdf_postprocess._heal_artifact_struct_elems(pdf)
            pdf_postprocess._sanitize_non_standard_struct_types(pdf)
            pdf_postprocess._check_pipeline_invariants(pdf)
            after = _walk_counter(pdf.Root["/StructTreeRoot"])
            pdf.close()
            return before, after
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zoltek_legacy_output_heals(self):
        before, after = self._heal_in_place(ZOLTEK_OUTPUT)
        self.assertGreater(before.get("/Artifact", 0), 0)
        self.assertEqual(after.get("/Artifact", 0), 0)

    def test_steel_wars_legacy_output_heals(self):
        before, after = self._heal_in_place(STEEL_WARS_OUTPUT)
        self.assertGreater(before.get("/Artifact", 0), 0)
        self.assertEqual(after.get("/Artifact", 0), 0)

    def test_kel201_retagged_legacy_output_heals(self):
        before, after = self._heal_in_place(KEL201_RETAGGED)
        self.assertEqual(before.get("/Artifact", 0), 50)
        self.assertEqual(after.get("/Artifact", 0), 0)


if __name__ == "__main__":
    unittest.main()
