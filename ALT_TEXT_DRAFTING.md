# Alt-text drafting — deployment notes

Backend only. Nothing in the Streamlit UI changes; drafts are written into the
output PDF's `/Alt` and into a sidecar `*_alt_text_report.json` next to it.

## What it does

For every `/Figure` in the finished PDF:

| Figure already has a description | → **left completely untouched** |
|---|---|
| Chart whose values are in the PDF text layer | → described deterministically, **nothing leaves the machine** |
| Anything else | → rendered to PNG and described by Claude |
| Photograph / logo / unreadable | → **left blank and flagged** in the report |

## Required at deploy time

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # required for the vision path
```

Without a key the pipeline still runs and still produces a compliant PDF — the
vision path just reports `api_error` and leaves those figures blank.

## Switches (all optional, read from the environment)

| Variable | Default | Meaning |
|---|---|---|
| `ALT_TEXT_DRAFTING` | `1` | Master switch. `0` restores the previous behaviour exactly. |
| `ALT_TEXT_USE_VISION` | `1` | `0` keeps drafting but **sends nothing to any API** — text-layer charts only. |
| `ALT_TEXT_MODEL` | `claude-opus-5` | Model id. |
| `ALT_TEXT_EFFORT` | `medium` | `low`/`medium`/`high`/`xhigh`/`max`. Raise for quality, lower for cost. |
| `ALT_TEXT_RENDER_DPI` | `150` | Render resolution for the crop sent to the model. |
| `ALT_TEXT_MAX_FIGURES_PER_DOC` | `60` | Spend guard. |

## Reviewing quality

```bash
python main.py --input case.pdf --output-dir out/
python -c "import json,alt_text_drafter as A; \
print(A.format_report(json.load(open('out/case_accessible_alt_text_report.json'))))"
```

The report lists every figure, its caption, which path drafted it, the text
produced, and the reason anything was left blank.

## Deliberate choices worth knowing

- **No `[DRAFT]` prefix in `/Alt`.** A screen reader reads `/Alt` aloud verbatim,
  so a marker there would be spoken to the student. Draft status lives in the
  sidecar report and in the PDF's `dc:description` instead.
- **The deterministic path refuses when unsure.** It only fires on a complete
  part-to-whole series (3+ slices summing to ~100%). KEL189's Exhibit 3 is a pie
  chart whose labels are *growth rates* summing to 123% — scraping those into a
  "shares" sentence would be confident nonsense, so it is sent to the vision path
  instead.
- **Drafting can never fail the run.** It is wrapped so that any error leaves the
  already-compliant PDF intact.
