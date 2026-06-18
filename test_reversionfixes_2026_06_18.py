"""
test_reversionfixes_2026_06_18.py — Katharine 2026-06-18 (reversionfixes 8, part 2).

The two documents that were blocked last round on missing source PDFs.

1. KEL958 ATF_TN — "stubborn Kellogg header logo".
   The title-page Kellogg logo is image /Im0 at bbox ~[90,676,221.7,746.3]
   (top margin band, 131.7 x 70.3pt).  It appears on ONE page only, so the
   cross-page repeating-banner pre-pass cannot catch it, and _is_banner_artifact
   missed it: its icon-chrome rule capped height at 60pt but this logo is 70pt
   tall.  Result: a decorative header logo tagged /Figure with generic alt.
   Fix: raise the margin-band icon-chrome height cap so a small, top/bottom-band
   image is recognised as page chrome.

2. KEL335 PurplePill Case Supplement — PAC "font not embedded".
   The chemical-structure drawing on page 10 is a Form XObject (/Meta45,
   /Meta48) whose own /Resources/Font references /Arial, not embedded.
   _fix_fonts only embedded page-level fonts, never descending into Form
   XObject resources, so veraPDF clause 7.21.4.1 fired.  Fix: descend into
   Form XObject resources and embed their non-embedded fonts too.

Run:
    ./venv/bin/python -m unittest test_reversionfixes_2026_06_18 -v
"""
import os
import shutil
import tempfile
import unittest

import pikepdf


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
ATF_TN = os.path.join(FIXTURES_DIR, "kel958_atf_tn.pdf")
SUPPLEMENT = os.path.join(FIXTURES_DIR, "kel335_purplepill_supplement.pdf")

_STD14 = {
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Symbol", "ZapfDingbats",
}


def _process(path):
    from main import process_single_pdf
    tmp = tempfile.mkdtemp(prefix="rev8b_")
    res = process_single_pdf(path, tmp, skip_validation=True)
    return tmp, (res.output_path if res.success else None)


def _generic(alt):
    return (alt or "").replace("\x00", "").strip().lower() in ("", "figure", "image")


def _figures(pdf):
    """[(alt, bbox)] for every /Figure SE."""
    out, seen = [], set()

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
            bb = None
            A = n.get("/A")
            if A is not None:
                for x in (A if isinstance(A, pikepdf.Array) else [A]):
                    try:
                        if "/BBox" in x:
                            bb = [float(v) for v in x["/BBox"]]
                    except Exception:
                        pass
            out.append((str(alt).replace("\x00", "").strip() if alt is not None else "", bb))
        walk(n.get("/K"))

    st = pdf.Root.get("/StructTreeRoot")
    if st is not None:
        walk(st.get("/K"))
    return out


def _unembedded_form_fonts(pdf):
    """Names of fonts declared INSIDE Form XObjects that lack an embedded font
    program — the exact shape of the veraPDF 7.21.4.1 failure on KEL335 (Arial
    used inside the chemical-structure form on page 10).

    Scoped to Form XObject resources: page-level declared-but-unused fonts are
    not flagged by veraPDF (it only checks fonts used for rendering), and our
    fix embeds the form fonts specifically.  Forms are deduped by their real
    indirect objgen; the page /Resources dicts here are direct objects whose
    objgen is (0,0), so they must NOT be used as dedup keys.
    """
    bad = []
    seen_forms = set()

    def font_unembedded(fo):
        try:
            subtype = str(fo.get("/Subtype", ""))
        except Exception:
            return None
        bf = str(fo.get("/BaseFont", "")).lstrip("/").split("+")[-1]
        if subtype == "/Type0":
            desc_fonts = fo.get("/DescendantFonts")
            cid = desc_fonts[0] if desc_fonts else None
            desc = (cid.get("/FontDescriptor", {}) if cid else {}) or {}
        else:
            desc = fo.get("/FontDescriptor", {}) or {}
        embedded = any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
        return bf if (not embedded and bf not in _STD14) else None

    def walk_forms(res):
        if res is None:
            return
        xobjs = res.get("/XObject")
        for _, xo in (xobjs.items() if xobjs else []):
            try:
                if str(xo.get("/Subtype", "")) != "/Form":
                    continue
                gen = xo.objgen
            except Exception:
                continue
            if gen in seen_forms:
                continue
            seen_forms.add(gen)
            fres = xo.get("/Resources")
            if fres is not None:
                for _, fo in (fres.get("/Font", {}).items()
                              if fres.get("/Font") else []):
                    name = font_unembedded(fo)
                    if name:
                        bad.append(name)
                walk_forms(fres)  # nested forms

    for page in pdf.pages:
        walk_forms(page.get("/Resources"))
    return bad


# --------------------------------------------------------------------------- #
# KEL958 ATF_TN — Kellogg header logo must be an artifact, not a /Figure
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(ATF_TN), "kel958_atf_tn.pdf missing")
class TestATFKelloggLogo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(ATF_TN)
        cls.pdf = pikepdf.open(cls.out) if cls.out else None
        cls.page_h = float(cls.pdf.pages[0]["/MediaBox"][3]) if cls.pdf else 792.0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_generic_figure_in_top_margin_band(self):
        # The Kellogg header logo sits in the top 20% margin band with generic
        # alt — it must now be an artifact, not a /Figure.
        offenders = []
        for alt, bb in _figures(self.pdf):
            if bb and _generic(alt):
                y_center = (bb[1] + bb[3]) / 2 / self.page_h
                if y_center >= 0.80:
                    offenders.append((alt, [round(x, 1) for x in bb]))
        self.assertEqual(
            offenders, [],
            f"header-band logo still tagged /Figure: {offenders}")

    def test_described_company_logos_survive(self):
        # The body company logos that carry real alt must NOT be touched.
        joined = " || ".join(a for a, _ in _figures(self.pdf)).lower()
        for needle in ("asyst", "rifast", "mikrotech"):
            self.assertIn(needle, joined,
                          f"described logo {needle!r} lost; figures={_figures(self.pdf)}")


# --------------------------------------------------------------------------- #
# KEL335 Case Supplement — fonts inside Form XObjects must be embedded
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(SUPPLEMENT), "kel335_purplepill_supplement.pdf missing")
class TestSupplementFormFontsEmbedded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(SUPPLEMENT)
        cls.pdf = pikepdf.open(cls.out) if cls.out else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_unembedded_fonts_in_form_xobjects(self):
        bad = _unembedded_form_fonts(self.pdf)
        self.assertEqual(
            bad, [],
            f"non-embedded font(s) inside Form XObjects (veraPDF 7.21.4.1): {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
