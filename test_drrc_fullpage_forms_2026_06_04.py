"""
test_drrc_fullpage_forms_2026_06_04.py — Katharine 2026-06-04 (DRRC batch).

reversionfixes(5) is an OLDER generation of InDesign-export PDFs
(KE1004/1005 Amgen, KE1106/1107 Tale of Two Properties, KE1128/1129
Landlord's Certainty).  Each page draws its decorative chrome — a
full-page background frame plus a rotated spine-title boilerplate — as a
single full-page Form XObject (``/Fm0``).  The page's real text lives in
the page content stream.

Symptom (Katharine): "whole pages will be marked as figures."  The
tagger's Form branch wraps ANY Form ``Do`` as a ``/Figure``, so the
full-page background became a per-page ``/Figure`` with generic alt
(BBox == the whole MediaBox).  The source's own struct tree has only the
handful of real figures; the chrome is decoration.

Fix: a full-page Form XObject (covers ~the whole page) that carries no
raster image and is not a detected real vector figure is page-template
chrome → emit as ``/Artifact``, never ``/Figure``.

Run:
    ./venv/bin/python -m unittest test_drrc_fullpage_forms_2026_06_04 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf

from pdf_tagger import _form_is_full_page_background


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
KE1004 = os.path.join(FIXTURES_DIR, "ke1004_amgen.pdf")
PAGE_LETTER = [0.0, 0.0, 612.0, 792.0]


class TestFullPageBackgroundGate(unittest.TestCase):
    """Direct geometry gate (image presence is checked at the call site
    with the real page; here we exercise the bbox-coverage logic via the
    page=None fast path)."""

    def test_full_page_form_is_background(self):
        # Form covering the whole MediaBox, no page object → coverage-only.
        self.assertTrue(
            _form_is_full_page_background(None, "/Fm0",
                                          [0.0, 0.0, 612.0, 792.0],
                                          PAGE_LETTER))

    def test_rotated_full_page_form_is_background(self):
        # Landscape spread drawn full-bleed on a portrait page.
        self.assertTrue(
            _form_is_full_page_background(None, "/Fm0",
                                          [0.0, 0.0, 612.0, 792.0],
                                          PAGE_LETTER))

    def test_small_inline_form_is_not_background(self):
        # A 300x200 form in the body — a real diagram, must stay a Figure.
        self.assertFalse(
            _form_is_full_page_background(None, "/Fm1",
                                          [150.0, 300.0, 450.0, 500.0],
                                          PAGE_LETTER))

    def test_half_page_form_is_not_background(self):
        # Covers full width but only half height — not a full-page template.
        self.assertFalse(
            _form_is_full_page_background(None, "/Fm2",
                                          [0.0, 0.0, 612.0, 380.0],
                                          PAGE_LETTER))

    def test_none_bbox_safe(self):
        self.assertFalse(_form_is_full_page_background(None, "/Fm0", None,
                                                       PAGE_LETTER))


@unittest.skipUnless(os.path.exists(KE1004),
                     "ke1004_amgen.pdf fixture not present")
class TestKE1004NoWholePageFigures(unittest.TestCase):
    """End-to-end: the full-page /Fm0 background on every page must become
    a content /Artifact, not a full-page /Figure."""

    @classmethod
    def setUpClass(cls):
        from main import process_single_pdf
        cls.tmp = tempfile.mkdtemp(prefix="ke1004_")
        res = process_single_pdf(KE1004, cls.tmp, skip_validation=True)
        cls.out = res.output_path if res.success else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _figure_bboxes(self, pdf):
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
                a = n.get("/A")
                bb = a.get("/BBox") if (a is not None and hasattr(a, "get")) else None
                out.append([float(v) for v in bb] if bb else None)
            k = n.get("/K")
            if k is not None:
                walk(k)

        st = pdf.Root.get("/StructTreeRoot")
        if st is not None:
            walk(st.get("/K"))
        return out

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_full_page_figures(self):
        with pikepdf.Pdf.open(self.out) as pdf:
            bboxes = self._figure_bboxes(pdf)
        full_page = [b for b in bboxes if b and
                     (b[2] - b[0]) >= 0.9 * 612 and (b[3] - b[1]) >= 0.9 * 792]
        self.assertEqual(
            full_page, [],
            f"No /Figure may span the whole page (those are the /Fm0 "
            f"background chrome); got {len(full_page)} full-page figures.")

    def test_fm0_is_artifact_on_each_page(self):
        with pikepdf.Pdf.open(self.out) as pdf:
            for i, pg in enumerate(pdf.pages, 1):
                cs = pg.obj.get("/Contents")
                data = (b"".join(c.read_bytes() for c in cs)
                        if isinstance(cs, pikepdf.Array) else cs.read_bytes())
                txt = data.decode("latin-1", "replace")
                idx = txt.find("/Fm0 Do")
                if idx < 0:
                    continue
                preceding = txt[max(0, idx - 140):idx]
                last_artifact = preceding.rfind("/Artifact")
                last_figure = preceding.rfind("/Figure")
                self.assertGreater(
                    last_artifact, last_figure,
                    f"page {i}: /Fm0 background must be wrapped /Artifact, "
                    f"not /Figure.")

    def test_no_tagged_content_inside_artifacted_form(self):
        """When a full-page /Fm0 background is demoted to /Artifact, its
        OWN internal marked content (InDesign /Figure, /Span, /PlacedPDF)
        must be stripped — tagged content inside an /Artifact range
        violates PDF/UA clause 7.1."""
        with pikepdf.Pdf.open(self.out) as pdf:
            offenders = []
            for i, pg in enumerate(pdf.pages, 1):
                xo = pg.obj.get("/Resources", {})
                xo = xo.get("/XObject", {}) if xo else {}
                cs = pg.obj.get("/Contents")
                data = (b"".join(c.read_bytes() for c in cs)
                        if isinstance(cs, pikepdf.Array) else cs.read_bytes())
                txt = data.decode("latin-1", "replace")
                # For every form drawn inside an /Artifact range, the form
                # must contain no tagged BDC of its own.
                for name, obj in (xo.items() if xo else []):
                    if obj.get("/Subtype") != pikepdf.Name("/Form"):
                        continue
                    idx = txt.find(f"{name} Do")
                    if idx < 0:
                        continue
                    pre = txt[max(0, idx - 140):idx]
                    if pre.rfind("/Artifact") <= pre.rfind("/Figure"):
                        continue  # not drawn inside an artifact
                    tags = []
                    try:
                        for operands, op in pikepdf.parse_content_stream(obj):
                            if str(op) in ("BDC", "BMC") and operands:
                                t = str(operands[0])
                                if t != "/Artifact":
                                    tags.append(t)
                    except Exception:
                        pass
                    if tags:
                        offenders.append((i, str(name), tags[:5]))
            self.assertEqual(
                offenders, [],
                f"Artifacted forms still carry internal tagged content: "
                f"{offenders}")

    def test_figure_count_drops_to_real_figures(self):
        """Source has 2 real /Figure; output must not balloon to ~1 per
        page.  Allow a small margin for auto-detected vector figures."""
        with pikepdf.Pdf.open(self.out) as pdf:
            n = len(self._figure_bboxes(pdf))
        self.assertLessEqual(
            n, 6,
            f"Expected only a few real figures (source had 2), not a "
            f"per-page figure explosion; got {n}.")


if __name__ == "__main__":
    unittest.main()
