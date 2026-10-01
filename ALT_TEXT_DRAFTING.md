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

## Deploying on Streamlit Community Cloud

1. Point the app at branch **`deploy/alt-text-trial`**, main file `app.py`.
2. In **Settings → Secrets**, add:

   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```

   Streamlit exposes secrets via `st.secrets`, not the process environment, so
   `app.py` bridges them into `os.environ` before importing `config` — without
   that bridge the SDK never sees the key.

3. `packages.txt` already includes **`poppler-utils`**. That provides
   `pdftoppm`, which renders each figure for the vision path. Without it every
   raster figure is skipped with `render_failed`.

Any of the switches below can also go in Secrets, e.g. `ALT_TEXT_MODEL`.

Note: veraPDF needs Java and is not available on Streamlit Cloud, so the app
does not validate there — same as before this change.

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
| `ALT_TEXT_MODEL` | `claude-sonnet-5` | Model id. See the cost/accuracy table below. |
| `ALT_TEXT_EFFORT` | `medium` | `low`/`medium`/`high`/`xhigh`/`max`. Raise for quality, lower for cost. |
| `ALT_TEXT_RENDER_DPI` | `150` | Render resolution for the crop sent to the model. |
| `ALT_TEXT_MAX_FIGURES_PER_DOC` | `60` | Spend guard. |

## Model choice and cost

Measured on KEL189 Exhibit 3 (a pie chart whose percentages are year-over-year
GROWTH rates, not shares — a figure that is easy to misread):

| Model | Facts correct | $/figure | $/20-figure case | ~6,000 figures |
|---|---|---|---|---|
| `claude-haiku-4-5` | **No** — named the wrong largest unit, read growth as share | $0.0020 | $0.04 | ~$12 |
| `claude-sonnet-5` (default) | Yes | $0.0047 | $0.09 | ~$28 |
| `claude-opus-5` | Yes, richest prose | $0.0137 | $0.27 | ~$82 |

Haiku saves about five cents per case and in exchange states facts that are
wrong, which the reviewer then has to catch. Sonnet is the default for that
reason. Haiku remains one environment variable away if throughput matters more
than accuracy on a particular batch.

Note: Haiku 4.5 rejects `output_config.effort`, so the parameter is dropped
automatically for models that do not accept it (`_supports_effort`).

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
