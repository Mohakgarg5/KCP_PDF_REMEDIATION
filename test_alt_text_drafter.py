"""
test_alt_text_drafter.py — alt-text drafting (feat/alt-text-drafting).

Covers everything up to the API boundary for real, and mocks only the Claude
client itself, since live calls need a key and cost money.

The contract that matters most and is tested hardest: **a figure that already
carries an authored description is never modified.** Katharine's and
Charlotte's alt text is authoritative; a drafting bug that overwrites it would
be far worse than one that drafts nothing.

The second-most important behaviour is the deterministic path REFUSING to
draft when it isn't certain. KEL189's Exhibit 3 is the motivating case: it is
a pie chart whose labels are percentages, but they are year-over-year GROWTH
rates (16+18+19+33+4+16+17 = 123%), not shares. Scraping them into a
"shares, largest to smallest" sentence would produce confident nonsense, so
the sum-to-100 check must reject it.

Run:
    ./venv/bin/python -m unittest test_alt_text_drafter -v
"""
import io
import json
import os
import shutil
import tempfile
import unittest

import pikepdf

import alt_text_drafter as A
from alt_text_drafter import FigureContext


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
NISSAN = os.path.join(FIXTURES_DIR, "kel774_nissan_micra.pdf")
KEL189 = os.path.join(FIXTURES_DIR, "kel189_marketing_microsoft.pdf")
HP = os.path.join(FIXTURES_DIR, "kel896_hp_shared_value.pdf")
MICHAELS = os.path.join(FIXTURES_DIR, "kel036_michaels_watermarked.pdf")


# ---------------------------------------------------------------------------
# A fake Claude client — the only thing mocked in this module
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)] if text is not None else []
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeClient:
    def __init__(self, outcome):
        self.messages = FakeMessages(outcome)


# ---------------------------------------------------------------------------
# Deterministic drafting — and, crucially, when it must refuse
# ---------------------------------------------------------------------------

class TestDeterministicDrafting(unittest.TestCase):

    def _ctx(self, labels, caption=None, nearby=None):
        return FigureContext(page=1, bbox=[0, 0, 100, 100], caption=caption,
                             data_labels=labels, nearby_text=nearby or [])

    def test_drafts_a_complete_share_series(self):
        ctx = self._ctx(
            [("Alto", 69.0), ("Nano", 16.0), ("Spark", 10.0),
             ("Maruti 800", 5.0), ("Eon", 0.0)],
            caption="Market Share of A Hatchbacks",
            nearby=["June 2011"],
        )
        alt = A.draft_deterministic(ctx)
        self.assertIsNotNone(alt)
        self.assertIn("Market Share of A Hatchbacks", alt)
        self.assertIn("June 2011", alt)
        self.assertIn("Alto 69%", alt)
        self.assertIn("Maruti 800 5%", alt)
        self.assertTrue(alt.startswith("Pie chart showing"))

    def test_orders_slices_largest_first(self):
        ctx = self._ctx([("C", 10.0), ("A", 70.0), ("B", 20.0)])
        alt = A.draft_deterministic(ctx)
        self.assertLess(alt.index("A 70%"), alt.index("B 20%"))
        self.assertLess(alt.index("B 20%"), alt.index("C 10%"))

    def test_refuses_growth_rates_that_do_not_sum_to_100(self):
        # KEL189 Exhibit 3: percentages are YoY growth, not shares.
        ctx = self._ctx([
            ("Information Worker", 18.0), ("Server & Tools", 19.0),
            ("Client", 16.0), ("Mobile and Embedded Devices", 33.0),
            ("Microsoft Business Solutions", 4.0), ("MSN", 16.0),
            ("Home & Entertainment", 17.0),
        ], caption="Revenue by the Seven P&Ls")
        self.assertIsNone(
            A.draft_deterministic(ctx),
            "growth rates must not be described as shares",
        )

    def test_refuses_an_incomplete_scrape(self):
        # One slice missed -> 95% -> cannot be trusted.
        ctx = self._ctx([("Alto", 69.0), ("Nano", 16.0), ("Spark", 10.0)])
        self.assertIsNone(A.draft_deterministic(ctx))

    def test_refuses_fewer_than_three_labels(self):
        ctx = self._ctx([("A", 50.0), ("B", 50.0)])
        self.assertIsNone(A.draft_deterministic(ctx))

    def test_tolerates_rounding(self):
        ctx = self._ctx([("A", 33.3), ("B", 33.3), ("C", 33.3)])
        self.assertIsNotNone(A.draft_deterministic(ctx))

    def test_caption_case_is_preserved(self):
        ctx = self._ctx([("A", 50.0), ("B", 30.0), ("C", 20.0)],
                        caption="Market Share of A Hatchbacks")
        self.assertIn("Market Share of A Hatchbacks",
                      A.draft_deterministic(ctx))


class TestPeriodDetection(unittest.TestCase):

    def test_finds_month_and_year(self):
        self.assertEqual(A._find_period(["June 2011", "Alto"]), "June 2011")

    def test_finds_bare_year(self):
        self.assertEqual(A._find_period(["2012"]), "2012")

    def test_ignores_percentage_labels(self):
        self.assertIsNone(A._find_period(["Alto 69%", "Nano 16%"]))

    def test_ignores_long_prose(self):
        self.assertIsNone(A._find_period([
            "Source: Raw numbers were compiled from Team-BHP in 2011 and "
            "then manipulated to calculate market share"
        ]))


# ---------------------------------------------------------------------------
# Context harvesting against real documents
# ---------------------------------------------------------------------------

class TestContextHarvesting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from pdf_extractor import extract_document
        cls.nissan = extract_document(NISSAN)

    def test_finds_the_caption_above_the_figure(self):
        ctx = A.build_context(self.nissan, 8, [129.2, 532.8, 226.4, 630.0])
        self.assertEqual(ctx.caption, "Market Share of A Hatchbacks")
        self.assertEqual(ctx.caption_label, "Figure 5")

    def test_finds_the_source_note_below(self):
        ctx = A.build_context(self.nissan, 8, [129.2, 532.8, 226.4, 630.0])
        self.assertIsNotNone(ctx.source_note)
        self.assertTrue(ctx.source_note.lower().startswith("source"))

    def test_sibling_figures_do_not_steal_each_others_labels(self):
        # Two pies on p8.  Each must get its own five slices.
        pie1 = [129.2, 532.8, 226.4, 630.0]
        pie2 = [364.4, 530.0, 462.0, 627.2]
        c1 = A.build_context(self.nissan, 8, pie1, sibling_bboxes=[pie2])
        c2 = A.build_context(self.nissan, 8, pie2, sibling_bboxes=[pie1])
        self.assertEqual(len(c1.data_labels), 5, c1.data_labels)
        self.assertEqual(len(c2.data_labels), 5, c2.data_labels)
        self.assertAlmostEqual(sum(v for _n, v in c1.data_labels), 100.0, places=1)
        self.assertAlmostEqual(sum(v for _n, v in c2.data_labels), 100.0, places=1)
        self.assertEqual(dict(c1.data_labels)["Alto"], 69.0)
        self.assertEqual(dict(c2.data_labels)["Alto"], 60.0)

    def test_without_sibling_awareness_the_series_is_wrong(self):
        # Guards the guard: prove the sibling check is load-bearing.
        both = A.build_context(self.nissan, 8, [129.2, 532.8, 462.0, 630.0])
        self.assertGreater(len(both.data_labels), 5)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

@unittest.skipIf(shutil.which("pdftoppm") is None, "poppler not installed")
class TestRendering(unittest.TestCase):

    def test_crop_is_right_way_up_and_not_blank(self):
        from PIL import Image
        png = A.render_figure_png(KEL189, 13, [67.1, 74.8, 467.2, 375.7], dpi=150)
        self.assertIsNotNone(png)
        img = Image.open(io.BytesIO(png))
        # Aspect ratio must follow the requested bbox, which is the thing a
        # flipped or un-scaled crop gets wrong.
        want = (467.2 - 67.1) / (375.7 - 74.8)
        self.assertAlmostEqual(img.width / img.height, want, delta=0.12)
        # A blank crop (wrong y origin) would have near-zero colour variance.
        colours = img.convert("RGB").getcolors(maxcolors=1 << 24) or []
        self.assertGreater(len(colours), 500, "crop looks blank")

    def test_oversize_figure_is_downscaled(self):
        from PIL import Image
        png = A.render_figure_png(HP, 14, [91.6, 189.0, 523.5, 660.1], dpi=400)
        img = Image.open(io.BytesIO(png))
        self.assertLessEqual(max(img.size), 1568)

    def test_degenerate_bbox_returns_none(self):
        self.assertIsNone(A.render_figure_png(KEL189, 13, [10, 10, 11, 11]))


# ---------------------------------------------------------------------------
# Vision path, with the client mocked
# ---------------------------------------------------------------------------

class TestVisionPath(unittest.TestCase):

    CTX = FigureContext(page=5, bbox=[0, 0, 10, 10],
                        caption="Revenue by the Seven P&Ls",
                        caption_label="Exhibit 3")

    def _call(self, outcome):
        client = FakeClient(outcome)
        alt, reason = A.draft_with_vision(b"\x89PNG fake", self.CTX, client,
                                          "claude-opus-5", "medium")
        return alt, reason, client

    def test_returns_the_models_text(self):
        alt, reason, _ = self._call(_Resp("Pie chart of revenue by business unit."))
        self.assertEqual(alt, "Pie chart of revenue by business unit.")
        self.assertIsNone(reason)

    def test_sends_image_and_context_with_the_configured_model(self):
        _alt, _r, client = self._call(_Resp("ok"))
        kw = client.messages.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5")
        self.assertEqual(kw["output_config"], {"effort": "medium"})
        blocks = kw["messages"][0]["content"]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")
        self.assertIn("Exhibit 3", blocks[1]["text"])

    def test_exact_text_layer_values_are_offered_to_the_model(self):
        ctx = FigureContext(page=1, bbox=[0, 0, 1, 1],
                            data_labels=[("Alto", 69.0), ("Nano", 16.0)])
        client = FakeClient(_Resp("ok"))
        A.draft_with_vision(b"x", ctx, client, "claude-opus-5", "medium")
        prompt = client.messages.calls[0]["messages"][0]["content"][1]["text"]
        self.assertIn("Alto 69%", prompt)
        self.assertIn("exact", prompt.lower())

    def test_decorative_is_left_blank(self):
        alt, reason, _ = self._call(_Resp("DECORATIVE"))
        self.assertIsNone(alt)
        self.assertEqual(reason, "model_says_decorative")

    def test_unclear_is_left_blank(self):
        alt, reason, _ = self._call(_Resp("UNCLEAR"))
        self.assertIsNone(alt)
        self.assertEqual(reason, "model_says_unclear")

    def test_refusal_is_left_blank(self):
        alt, reason, _ = self._call(_Resp("no", stop_reason="refusal"))
        self.assertIsNone(alt)
        self.assertEqual(reason, "model_refusal")

    def test_empty_response_is_left_blank(self):
        alt, reason, _ = self._call(_Resp(None))
        self.assertIsNone(alt)
        self.assertEqual(reason, "empty_response")

    def test_api_error_never_raises(self):
        alt, reason, _ = self._call(RuntimeError("connection reset"))
        self.assertIsNone(alt)
        self.assertTrue(reason.startswith("api_error"))


# ---------------------------------------------------------------------------
# End to end on a real document
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="altdraft_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _run(self, src, use_vision=False):
        import config
        from main import process_single_pdf
        old = config.ALT_TEXT_USE_VISION
        config.ALT_TEXT_USE_VISION = use_vision
        try:
            r = process_single_pdf(src, self._tmp, skip_validation=True)
            self.assertTrue(r.success, r.error)
            return r
        finally:
            config.ALT_TEXT_USE_VISION = old

    def test_nissan_pies_are_drafted_from_the_text_layer(self):
        r = self._run(NISSAN)
        s = r.alt_text_summary
        self.assertEqual(s["deterministic"], 2)
        drafted = [f["alt_text"] for f in s["figures"] if f["alt_text"]]
        self.assertTrue(any("June 2011" in a for a in drafted))
        self.assertTrue(any("June 2012" in a for a in drafted))
        self.assertTrue(any("Alto 69%" in a for a in drafted))

    def test_the_two_pies_get_different_descriptions(self):
        s = self._run(NISSAN).alt_text_summary
        drafted = [f["alt_text"] for f in s["figures"] if f["alt_text"]]
        self.assertEqual(len(drafted), len(set(drafted)))

    def test_drafts_land_in_the_pdf_itself(self):
        r = self._run(NISSAN)
        pdf = pikepdf.open(r.output_path)
        alts = []

        def walk(n):
            if not isinstance(n, pikepdf.Dictionary):
                return
            if n.get("/Alt") is not None:
                alts.append(str(n.get("/Alt")))
            k = n.get("/K")
            if k is None:
                return
            for kid in (k if isinstance(k, pikepdf.Array) else [k]):
                if isinstance(kid, pikepdf.Dictionary):
                    walk(kid)
        st = pdf.Root.get("/StructTreeRoot")
        for x in (st.get("/K") if isinstance(st.get("/K"), pikepdf.Array)
                  else [st.get("/K")]):
            walk(x)
        self.assertTrue(any("Alto 69%" in a for a in alts))

    def test_a_review_report_is_written(self):
        r = self._run(NISSAN)
        path = r.output_path.rsplit(".pdf", 1)[0] + "_alt_text_report.json"
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["figures_needing_alt"], len(data["figures"]))
        self.assertIn("Alt-text drafting", A.format_report(data))

    def test_authored_alt_text_is_never_overwritten(self):
        # Michaels carries 12 descriptions written by the reviewer.
        before = self._figure_alts(MICHAELS, build=True)
        authored = [a for a in before
                    if a.strip().lower() not in ("", "figure", "image")]
        self.assertGreaterEqual(len(authored), 12)
        after = self._last_alts
        for a in authored:
            self.assertIn(a, after, "an authored description was modified")

    def _figure_alts(self, src, build=False):
        r = self._run(src)
        pdf = pikepdf.open(r.output_path)
        out = []

        def walk(n):
            if not isinstance(n, pikepdf.Dictionary):
                return
            if str(n.get("/S")) == "/Figure" and n.get("/Alt") is not None:
                out.append(str(n.get("/Alt")))
            k = n.get("/K")
            if k is None:
                return
            for kid in (k if isinstance(k, pikepdf.Array) else [k]):
                if isinstance(kid, pikepdf.Dictionary):
                    walk(kid)
        st = pdf.Root.get("/StructTreeRoot")
        for x in (st.get("/K") if isinstance(st.get("/K"), pikepdf.Array)
                  else [st.get("/K")]):
            walk(x)
        self._last_alts = out
        return out

    def test_drafting_failure_does_not_break_remediation(self):
        import config
        from main import process_single_pdf
        old = config.ALT_TEXT_MODEL
        config.ALT_TEXT_USE_VISION = True
        config.ALT_TEXT_MODEL = "definitely-not-a-model"
        try:
            r = process_single_pdf(NISSAN, self._tmp, skip_validation=True)
            self.assertTrue(r.success, "a drafting failure must not fail the run")
            self.assertTrue(os.path.exists(r.output_path))
        finally:
            config.ALT_TEXT_MODEL = old
            config.ALT_TEXT_USE_VISION = False


if __name__ == "__main__":
    unittest.main()
