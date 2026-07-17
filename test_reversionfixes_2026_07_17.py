"""
test_reversionfixes_2026_07_17.py — Katharine 2026-07-17 (reversionfixes 10).

New InDesign "formatting method" — whitespace-only /Contents on annotations.

PAC 2026 raises a soft alert ("It's possible that some PDF/UA requirements
aren't met") on a dozen files coming out of the new authoring method.  All of
them share ONE warning, repeated per-annotation:

    Logical Structure ▸ Alternative Descriptions ▸
      Alternative description for annotations:
        "Contents" entry on an annotation exists, but is only comprised of
        whitespace.   (33× on KE1411 standard/DNC, 12× on KE1412 TN/TN_DNC)

Root cause — the new InDesign export writes /Contents = " " (a single space
placeholder) on the endnote back-reference /GoTo link annotations on the
Endnotes page.  Our pipeline preserves annotation /Contents verbatim, so the
whitespace flows straight through to the accessible output.

_fix_annotations already means to give every link a meaningful /Contents, but
its guard —

    if "/Contents" not in annot or not str(annot.get("/Contents", "")):

treated a single space as truthy ("already has contents") and skipped it.
`not " "` is False, so the whitespace-only value was never replaced.

Fix: make the guard whitespace-aware (``.strip()``) so a Contents comprised
only of whitespace is treated as empty and re-derived via
_derive_annot_contents (→ "Internal link" for these /GoTo back-links).

Run:
    ./venv/bin/python -m pytest test_reversionfixes_2026_07_17.py -v
"""
import os
import tempfile
import unittest

import pikepdf

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
KE1411 = os.path.join(FIXTURES_DIR, "ke1411_mcc_sweat.pdf")


def _whitespace_contents_links(pdf):
    """Return list of (page, repr) for Link annots whose /Contents is
    present but comprised only of whitespace."""
    out = []
    for pi, page in enumerate(pdf.pages):
        annots = page.get("/Annots")
        if annots is None:
            continue
        for a in annots:
            if str(a.get("/Subtype", "")) != "/Link":
                continue
            c = a.get("/Contents")
            if c is not None and str(c).strip() == "":
                out.append((pi + 1, repr(str(c))))
    return out


class TestWhitespaceAnnotContents(unittest.TestCase):
    def test_fixture_reproduces_source_whitespace(self):
        """Sanity: the raw source really does carry whitespace /Contents."""
        with pikepdf.open(KE1411) as pdf:
            ws = _whitespace_contents_links(pdf)
        self.assertTrue(
            ws, "fixture should contain whitespace-only /Contents link annots"
        )

    def test_pipeline_clears_whitespace_contents(self):
        """After remediation no Link annotation may keep a whitespace-only
        /Contents — PAC's "only comprised of whitespace" warning."""
        from main import process_single_pdf

        tmp = tempfile.mkdtemp(prefix="rev10_")
        res = process_single_pdf(KE1411, tmp, skip_validation=True)
        self.assertTrue(res.success, "pipeline should succeed")
        with pikepdf.open(res.output_path) as pdf:
            ws = _whitespace_contents_links(pdf)
        self.assertEqual(
            ws, [], f"whitespace-only /Contents links survived: {ws[:5]}"
        )


if __name__ == "__main__":
    unittest.main()
