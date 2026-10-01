"""
test_verapdf_7217_simple_fonts.py — clause 7.21.7 for simple (1-byte) fonts.

Found while sweeping the real-world corpus for reversionfixes(12): AUMC TN was
the only document still failing veraPDF (103 passed / 1 failed).

TWO defects, both pre-existing.

A. pdf_postprocess — the clause 7.21.7 "complete ToUnicode for used glyphs"
   pass is gated on ``multibyte`` (Type0) in BOTH places: fonts are only
   registered in ``used_by_font`` when ``multibyte`` is true, and used codes
   are only recorded when ``multibyte`` is true.  A simple single-byte
   /TrueType subset whose ToUnicode has gaps is therefore never completed.

   AUMC's AAAAAT+Calibri covers codes 33..92 except 40 and 59 — the "tt" and
   "ti" ligatures Word emitted but never mapped ("A{40}ached" = "Attached",
   "nego{59}a{59}ons" = "negotiations").  The source PDF has the same gap, so
   this is inherited, not introduced: our pipeline already fixes 9 of the
   source's 10 clause failures and leaves this one.

   Second-order detail: placeholders must be written at the CMap's own code
   width.  ``_append_tounicode_placeholders`` hard-coded ``<%04X>``, which is
   right for a 2-byte Identity CMap but malformed inside a 1-byte
   ``<00><FF>`` codespace.

B. validator — ``_parse_verapdf_json`` reads ``details["rules"]`` with
   ``status == "failed"``, but veraPDF emits ``details["ruleSummaries"]`` with
   ``ruleStatus``.  So ``total_failed`` was reported while ``failed_rules``
   stayed empty: the report said something failed but never which clause.

Run:
    ./venv/bin/python -m unittest test_verapdf_7217_simple_fonts -v
"""
import json
import os
import re
import unittest

import pikepdf

import pdf_postprocess as PP
import validator as V


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
AUMC = os.path.join(FIXTURES_DIR, "aumc_tn_final.pdf")

ONE_BYTE_CMAP = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00><FF>
endcodespacerange
1 beginbfchar
<21> <0031>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""

TWO_BYTE_CMAP = ONE_BYTE_CMAP.replace("<00><FF>", "<0000><FFFF>")


def _mk_font(pdf, cmap_text, subtype="/TrueType"):
    f = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        Subtype=pikepdf.Name(subtype),
        BaseFont=pikepdf.Name("/AAAAAT+Calibri"),
        FirstChar=33,
        LastChar=92,
    ))
    f[pikepdf.Name("/ToUnicode")] = pdf.make_stream(cmap_text.encode("latin-1"))
    return f


# ---------------------------------------------------------------------------
# B. validator surfaces the failing clause
# ---------------------------------------------------------------------------

class TestValidatorReportsFailedRules(unittest.TestCase):

    PAYLOAD = {
        "report": {"jobs": [{"validationResult": {
            "compliant": False,
            "details": {
                "passedRules": 103,
                "failedRules": 1,
                "ruleSummaries": [
                    {"clause": "7.1", "testNumber": 3, "ruleStatus": "PASSED",
                     "description": "fine", "checks": []},
                    {"clause": "7.21.7", "testNumber": 1, "ruleStatus": "FAILED",
                     "failedChecks": 4,
                     "description": "The Font dictionary of all fonts shall "
                                    "define the map of all used character codes",
                     "checks": [{"context": "root/document[0]/pages[5]",
                                 "errorMessage": "The glyph can not be mapped "
                                                 "to Unicode"}]},
                ],
            },
        }}]}
    }

    def test_failed_rule_is_surfaced(self):
        r = V._parse_verapdf_json("x.pdf", json.dumps(self.PAYLOAD), "")
        self.assertFalse(r.is_compliant)
        self.assertEqual(r.total_passed, 103)
        self.assertEqual(r.total_failed, 1)
        self.assertEqual(len(r.failed_rules), 1,
                         "failed clause must be reported, not just counted")
        self.assertEqual(r.failed_rules[0].clause, "7.21.7")
        self.assertEqual(r.failed_rules[0].test_number, 1)
        self.assertIn("glyph", r.failed_rules[0].context.lower() + " "
                      + r.failed_rules[0].description.lower())

    def test_report_text_names_the_clause(self):
        r = V._parse_verapdf_json("x.pdf", json.dumps(self.PAYLOAD), "")
        text = V.format_validation_report(r)
        self.assertIn("7.21.7", text)

    def test_legacy_rules_key_still_parsed(self):
        # Older veraPDF builds emit details["rules"] with status="failed".
        legacy = {"report": {"jobs": [{"validationResult": {
            "compliant": False,
            "details": {"passedRules": 1, "failedRules": 1, "rules": [
                {"clause": "7.2", "testNumber": 2, "status": "failed",
                 "description": "legacy", "checks": [{"context": "ctx"}]}]},
        }}]}}
        r = V._parse_verapdf_json("x.pdf", json.dumps(legacy), "")
        self.assertEqual(len(r.failed_rules), 1)
        self.assertEqual(r.failed_rules[0].clause, "7.2")


# ---------------------------------------------------------------------------
# A1. placeholders honour the CMap's code width
# ---------------------------------------------------------------------------

class TestPlaceholderCodeWidth(unittest.TestCase):

    def test_detects_single_byte_codespace(self):
        self.assertEqual(PP._cmap_code_hex_digits(ONE_BYTE_CMAP), 2)

    def test_detects_two_byte_codespace(self):
        self.assertEqual(PP._cmap_code_hex_digits(TWO_BYTE_CMAP), 4)

    def test_defaults_to_two_bytes_when_absent(self):
        self.assertEqual(PP._cmap_code_hex_digits("begincmap endcmap"), 4)

    def test_single_byte_font_gets_two_digit_codes(self):
        pdf = pikepdf.new()
        f = _mk_font(pdf, ONE_BYTE_CMAP)
        n = PP._append_tounicode_placeholders(pdf, f, {40, 59})
        self.assertEqual(n, 2)
        out = bytes(f.get("/ToUnicode").read_bytes()).decode("latin-1")
        self.assertIn("<28>", out)
        self.assertIn("<3B>", out)
        self.assertNotIn("<0028>", out,
                         "4-digit code is malformed in a 1-byte codespace")
        self.assertIn("<21> <0031>", out, "existing entries must survive")

    def test_two_byte_font_keeps_four_digit_codes(self):
        pdf = pikepdf.new()
        f = _mk_font(pdf, TWO_BYTE_CMAP, subtype="/Type0")
        PP._append_tounicode_placeholders(pdf, f, {40})
        out = bytes(f.get("/ToUnicode").read_bytes()).decode("latin-1")
        self.assertIn("<0028>", out)


# ---------------------------------------------------------------------------
# A1b. real Unicode is recovered before any placeholder is used
# ---------------------------------------------------------------------------

class TestSimpleFontRecovery(unittest.TestCase):
    """A space placeholder rewrites text, so it must be the last resort."""

    def _font_with_differences(self, pdf, diffs):
        f = _mk_font(pdf, ONE_BYTE_CMAP)
        f[pikepdf.Name("/Encoding")] = pikepdf.Dictionary(
            Type=pikepdf.Name("/Encoding"),
            Differences=pikepdf.Array(diffs),
        )
        return f

    def test_recovers_from_differences_glyph_names(self):
        pdf = pikepdf.new()
        f = self._font_with_differences(
            pdf, [40, pikepdf.Name("/A"), pikepdf.Name("/bullet")])
        got = PP._recover_simple_font_codes(f, {40, 41})
        self.assertEqual(got.get(40), "A")
        self.assertEqual(got.get(41), "•")

    def test_recovers_ligature_glyph_names(self):
        # AGL resolves ligature names, which is exactly the AUMC glyph class
        # when the subset bothers to name them.
        pdf = pikepdf.new()
        f = self._font_with_differences(pdf, [40, pikepdf.Name("/f_i")])
        self.assertEqual(PP._recover_simple_font_codes(f, {40}).get(40), "fi")

    def test_recovers_uniXXXX_glyph_names(self):
        pdf = pikepdf.new()
        f = self._font_with_differences(pdf, [65, pikepdf.Name("/uni20AC")])
        self.assertEqual(PP._recover_simple_font_codes(f, {65}).get(65), "€")

    def test_recovers_from_base_encoding(self):
        pdf = pikepdf.new()
        f = _mk_font(pdf, ONE_BYTE_CMAP)
        f[pikepdf.Name("/Encoding")] = pikepdf.Name("/WinAnsiEncoding")
        self.assertEqual(PP._recover_simple_font_codes(f, {65}).get(65), "A")

    def test_returns_nothing_when_unrecoverable(self):
        # No /Encoding, no font program — AUMC's situation.
        pdf = pikepdf.new()
        f = _mk_font(pdf, ONE_BYTE_CMAP)
        self.assertEqual(PP._recover_simple_font_codes(f, {40, 59}), {})

    def test_recovered_mapping_is_written_not_a_space(self):
        pdf = pikepdf.new()
        f = self._font_with_differences(pdf, [40, pikepdf.Name("/A")])
        PP._append_tounicode_recovered(pdf, f, {40: "A"})
        out = bytes(f.get("/ToUnicode").read_bytes()).decode("latin-1")
        self.assertIn("<28> <0041>", out)

    def test_multichar_recovery_is_encoded_as_a_sequence(self):
        pdf = pikepdf.new()
        f = _mk_font(pdf, ONE_BYTE_CMAP)
        PP._append_tounicode_recovered(pdf, f, {40: "tt"})
        out = bytes(f.get("/ToUnicode").read_bytes()).decode("latin-1")
        self.assertIn("<28> <00740074>", out)


# ---------------------------------------------------------------------------
# A2. simple fonts are completed end-to-end
# ---------------------------------------------------------------------------

def _tounicode_missing(path, basefont_tag):
    """Codes in FirstChar..LastChar absent from the font's ToUnicode."""
    pdf = pikepdf.open(path)
    for page in pdf.pages:
        res = page.get("/Resources") or {}
        for _k, f in (res.get("/Font") or {}).items():
            if basefont_tag not in str(f.get("/BaseFont")):
                continue
            tu = f.get("/ToUnicode")
            if tu is None:
                return None
            data = bytes(tu.read_bytes()).decode("latin-1")
            cov = set()
            for blk in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
                for lo, hi, _d in re.findall(
                    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
                ):
                    cov.update(range(int(lo, 16), int(hi, 16) + 1))
            for blk in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
                for s, _d in re.findall(
                    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
                ):
                    cov.add(int(s, 16))
            fc = int(f.get("/FirstChar")); lc = int(f.get("/LastChar"))
            return [c for c in range(fc, lc + 1) if c not in cov]
    return None


class TestAumcSimpleFontCompleted(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tempfile
        from main import process_single_pdf
        cls._tmp = tempfile.mkdtemp(prefix="ua7217_")
        r = process_single_pdf(AUMC, cls._tmp, skip_validation=True)
        assert r.success, r.error
        cls.out = r.output_path

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_source_has_the_gap(self):
        self.assertEqual(_tounicode_missing(AUMC, "AAAAAT+Calibri"), [40, 59],
                         "fixture precondition: source is missing 40 and 59")

    def test_output_has_no_unmapped_used_codes(self):
        self.assertEqual(
            _tounicode_missing(self.out, "AAAAAT+Calibri"), [],
            "every used code must map to Unicode (clause 7.21.7)",
        )

    def test_output_passes_verapdf(self):
        r = V.validate_pdf(self.out)
        self.assertTrue(
            r.is_compliant,
            f"failed {r.total_failed} rule(s): "
            f"{[(x.clause, x.context[:60]) for x in r.failed_rules]}",
        )


if __name__ == "__main__":
    unittest.main()
