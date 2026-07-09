"""
test_reversionfixes_2026_07_09.py — Katharine 2026-07-09 (reversionfixes 9).

Three failing PACs in PAC 2026 (26.1.0.0), three independent root causes.

#1 — CAREER CENTRAL ("role mapping" fail + PAC report-writer crash):
    A single struct element with /S=/Artifact and no /RoleMap.  /Artifact is a
    marked-content role, never a valid /S.  PAC 2026 not only flags it
    ("Non-standard structure type 'Artifact' ...") but crashes its parallel
    report writer on it.  The offending node is a demoted /Figure copied
    verbatim from the source struct tree, bypassing the tagger's assert.
    GUARD: current code already heals this end-to-end; this test locks it in.

#2 — STEINWAY DNC ("Link annotation is not nested inside a Link structure
    element"):  the DNC build adds internal cross-reference links (jump to
    Exhibit / figure / table / equation).  6 of 76 link annotations end up
    with no /Link struct element referencing them via OBJR.  _fix_annotations
    skipped them because _is_already_tagged only checked whether the
    annotation's /StructParent mapped to *something containing* a /Link —
    a false positive when the key is a shared page MCID array, or a /Link SE
    that references a *different* annotation.  Fix: skip only annotations that
    are genuinely the /Obj of an OBJR under a /Link SE.

#3 — CRM (both versions) ("Characters in a text object cannot be mapped to
    Unicode", 12 errors):  the equations render as real text in an embedded
    Cambria Math subset (Type0/Identity-H) whose glyphs (glyph00144 …) have no
    cmap and no recoverable Unicode.  Katharine marked the equations /Figure
    with the rewritten equation in /Alt.  Fix: propagate that /Alt to
    /ActualText on the /Figure marked-content sequence so the unmappable
    glyphs are covered per PDF/UA (Matterhorn 09-004).

Run:
    ./venv/bin/python -m pytest test_reversionfixes_2026_07_09.py -v
"""
import os
import re
import tempfile
import unittest

import pikepdf

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
CAREER = os.path.join(FIXTURES_DIR, "kel093_career_central.pdf")
STEINWAY_DNC = os.path.join(FIXTURES_DIR, "ke1243_steinway_dnc.pdf")
CRM = os.path.join(FIXTURES_DIR, "kel695_crm.pdf")
CRM_DNC = os.path.join(FIXTURES_DIR, "kel695_crm_dnc.pdf")


def _process(path):
    from main import process_single_pdf
    tmp = tempfile.mkdtemp(prefix="rev9_")
    res = process_single_pdf(path, tmp, skip_validation=True)
    return tmp, (res.output_path if res.success else None)


# --------------------------------------------------------------------------- #
# Shared struct-tree helpers
# --------------------------------------------------------------------------- #
def _walk(node, seen=None):
    if seen is None:
        seen = set()
    if isinstance(node, pikepdf.Array):
        for k in node:
            yield from _walk(k, seen)
        return
    if not isinstance(node, pikepdf.Dictionary):
        return
    try:
        if node.objgen in seen:
            return
        seen.add(node.objgen)
    except Exception:
        pass
    yield node
    yield from _walk(node.get("/K"), seen)


def _struct_types(pdf):
    st = pdf.Root.get("/StructTreeRoot")
    return [str(n.get("/S")) for n in _walk(st) if n.get("/S") is not None]


def _annots_wrapped_by_link_objr(pdf):
    """objgen set of annotations referenced by an OBJR under a /Link StructElem."""
    st = pdf.Root.get("/StructTreeRoot")
    wrapped = set()
    for n in _walk(st):
        if str(n.get("/S")) != "/Link":
            continue
        k = n.get("/K")
        items = k if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
        for it in items:
            if isinstance(it, pikepdf.Dictionary) and str(it.get("/Type")) == "/OBJR":
                obj = it.get("/Obj")
                if obj is not None:
                    try:
                        wrapped.add(obj.objgen)
                    except Exception:
                        pass
    return wrapped


def _link_annots(pdf):
    out = []
    for pi, pg in enumerate(pdf.pages):
        annots = pg.obj.get("/Annots")
        if not annots:
            continue
        for a in annots:
            if str(a.get("/Subtype")) == "/Link":
                out.append((pi, a.objgen))
    return out


# --------------------------------------------------------------------------- #
# ToUnicode / ActualText mapping analysis (mirrors PAC's char-mapping check)
# --------------------------------------------------------------------------- #
def _parse_tounicode(data):
    """Return set of source codes that map to a *valid* (non-zero) Unicode."""
    good = set()
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
        for s, d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            if not (len(set(d.lower())) == 1 and d.lower()[0] == "0") and d.lower() not in ("fffe", "ffff"):
                good.add(int(s, 16))
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, _d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            for c in range(int(lo, 16), int(hi, 16) + 1):
                good.add(c)
    return good


def _unmappable_char_shows(pdf):
    """Count text-shown characters that neither map to Unicode nor sit inside an
    /ActualText marked-content sequence.  This is exactly what PAC flags as
    'Characters in a text object cannot be mapped to Unicode'."""
    total_bad = 0
    detail = []
    for pi, pg in enumerate(pdf.pages):
        res = pg.obj.get("/Resources")
        fonts = res.get("/Font") if res else None
        if not fonts:
            continue
        finfo = {}
        for fn in fonts.keys():
            fo = fonts[fn]
            tu = fo.get("/ToUnicode")
            good = set()
            if tu is not None:
                try:
                    good = _parse_tounicode(tu.read_bytes().decode("latin-1"))
                except Exception:
                    pass
            finfo[str(fn)] = {
                "good": good,
                "multibyte": str(fo.get("/Subtype")) == "/Type0",
                "bf": str(fo.get("/BaseFont")),
            }
        try:
            ops = list(pikepdf.parse_content_stream(pg))
        except Exception:
            continue
        cur = None
        actualtext_depth = 0
        bdc_has_at = []
        for operands, op in ops:
            o = str(op)
            if o in ("BDC", "BMC"):
                has_at = False
                if o == "BDC" and len(operands) >= 2 and hasattr(operands[1], "get"):
                    has_at = operands[1].get("/ActualText") is not None
                bdc_has_at.append(has_at)
                if has_at:
                    actualtext_depth += 1
            elif o == "EMC" and bdc_has_at:
                if bdc_has_at.pop():
                    actualtext_depth -= 1
            elif o == "Tf" and len(operands) >= 2:
                cur = str(operands[0])
            elif o in ("Tj", "TJ", "'", '"') and cur in finfo:
                if actualtext_depth > 0:
                    continue  # covered by ActualText
                info = finfo[cur]
                if not info["good"]:
                    continue  # font has no ToUnicode at all — handled elsewhere
                strs = []
                for x in operands:
                    if isinstance(x, pikepdf.String):
                        strs.append(bytes(x))
                    elif isinstance(x, pikepdf.Array):
                        for y in x:
                            if isinstance(y, pikepdf.String):
                                strs.append(bytes(y))
                for bs in strs:
                    if info["multibyte"]:
                        codes = [(bs[i] << 8) | bs[i + 1] for i in range(0, len(bs) - 1, 2)]
                    else:
                        codes = list(bs)
                    for c in codes:
                        if c not in info["good"]:
                            total_bad += 1
                            detail.append((pi, info["bf"], hex(c)))
    return total_bad, detail


# --------------------------------------------------------------------------- #
# #1 — Career Central: no /S=/Artifact survives (regression guard)
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(CAREER), "kel093_career_central.pdf missing")
class TestCareerCentralNoArtifactStructType(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(CAREER)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_artifact_struct_type(self):
        with pikepdf.open(self.out) as pdf:
            types = _struct_types(pdf)
            self.assertNotIn(
                "/Artifact", types,
                "/S=/Artifact struct element survived — PAC 2026 role-mapping "
                "fail + report-writer crash",
            )

    def test_rolemap_has_no_unmapped_nonstandard(self):
        with pikepdf.open(self.out) as pdf:
            st = pdf.Root.get("/StructTreeRoot")
            rm = st.get("/RoleMap")
            mapped = {str(k) for k in rm.keys()} if rm else set()
            std = {
                "/Document", "/Part", "/Art", "/Sect", "/Div", "/BlockQuote",
                "/Caption", "/TOC", "/TOCI", "/Index", "/NonStruct", "/Private",
                "/P", "/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6", "/L", "/LI",
                "/Lbl", "/LBody", "/Table", "/TR", "/TH", "/TD", "/THead", "/TBody",
                "/TFoot", "/Span", "/Quote", "/Note", "/Reference", "/BibEntry",
                "/Code", "/Link", "/Annot", "/Figure", "/Formula", "/Form",
                "/Ruby", "/RB", "/RT", "/RP", "/Warichu", "/WT", "/WP",
            }
            for t in set(_struct_types(pdf)):
                self.assertTrue(
                    t in std or t in mapped,
                    f"non-standard struct type {t} is neither standard nor "
                    f"mapped in /RoleMap",
                )


# --------------------------------------------------------------------------- #
# #2 — Steinway DNC: every Link annotation nested in a /Link struct element
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(STEINWAY_DNC), "ke1243_steinway_dnc.pdf missing")
class TestSteinwayDNCLinkNesting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(STEINWAY_DNC)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_every_link_annotation_wrapped_in_link_se(self):
        with pikepdf.open(self.out) as pdf:
            wrapped = _annots_wrapped_by_link_objr(pdf)
            annots = _link_annots(pdf)
            orphans = [(pi, og) for pi, og in annots if og not in wrapped]
            self.assertEqual(
                orphans, [],
                f"{len(orphans)} link annotation(s) not nested inside a /Link "
                f"struct element (OBJR): {orphans}",
            )


# --------------------------------------------------------------------------- #
# #3 — CRM: equation glyphs are Unicode-mappable or ActualText-covered
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.path.exists(CRM), "kel695_crm.pdf missing")
class TestCRMEquationCharsMappable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(CRM)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_unmappable_text_characters(self):
        with pikepdf.open(self.out) as pdf:
            bad, detail = _unmappable_char_shows(pdf)
            self.assertEqual(
                bad, 0,
                f"{bad} character(s) in text objects cannot be mapped to "
                f"Unicode and are not ActualText-covered: {detail[:12]}",
            )


@unittest.skipUnless(os.path.exists(CRM_DNC), "kel695_crm_dnc.pdf missing")
class TestCRMDNCEquationCharsMappable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.out = _process(CRM_DNC)

    def test_processed_ok(self):
        self.assertIsNotNone(self.out)

    def test_no_unmappable_text_characters(self):
        with pikepdf.open(self.out) as pdf:
            bad, detail = _unmappable_char_shows(pdf)
            self.assertEqual(
                bad, 0,
                f"{bad} character(s) in text objects cannot be mapped to "
                f"Unicode and are not ActualText-covered: {detail[:12]}",
            )


if __name__ == "__main__":
    unittest.main()
