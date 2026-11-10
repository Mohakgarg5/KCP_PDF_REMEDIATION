# Exhibit Alt Text (Audio-First) — reviewer's house style

Source: the skill Katharine has locked in with Claude, sent 2026-11-10.
This is the authority for how `alt_text_drafter` writes. Keep it in sync.

Alt text here is written to be **heard, not read**. A listener using a screen
reader takes it in as a spoken audio clip, so the craft is making it easy to
process by ear: concept first, plain language, short sentences, no data dumps.
Guiding principle: **less is more**. If a shorter version conveys the idea just
as well, use the shorter version, and lean short by default.

The one exception is mathematical formulas — full fidelity. See below.

## Core principle: alt text orients, it doesn't replace

For graphs, charts, diagrams and timelines the job is only to flag that a
visual is present and orient the listener to its general nature. Surrounding
text or a data table carries the specifics.

- Two tight sentences at most — often one is enough. This is the target, not
  just a ceiling.
- State what kind of visual it is and the general idea or shape it conveys
  (a rising trend, a comparison across groups, a step-by-step process).
- **Lead with the concept. Do NOT front-load specific numbers, data labels,
  axis values or category names.**
- Prioritise meaning over literal contents.

Photographs and ads may carry more concrete visual description — there is no
underlying data table to defer to — but keep it tight.

## Colour and non-functional visual detail

Do NOT include colour unless the colour itself carries functional meaning.
Many screen-reader users have been blind since birth and have no experiential
reference for colour.

- Cut purely decorative colour ("a red bandana", "the green bars").
- Keep colour only when it IS the information — a red-versus-green status
  indicator, or a legend where colour is the only way categories are told
  apart (and then name what the colour means, not the hue).

Named-spec test for any concrete detail:
- **Keep** named specifications and data ("colour temperature 3500",
  "60-watt equivalent", "rated to last nine years") — facts, not visuals.
- **Cut** appearance-only descriptors (hue, sheen, exact packaging styling).

Exception — when the appearance IS the subject: a design-patent drawing, an
architectural rendering, a logo-design exhibit. There the form is the payload.
Still skip incidental colour.

## "graph" vs "chart"

- **graph** for data plots — line, bar, scatter; anything plotting
  quantitative data on axes. Default for data plots.
- **chart** for other visual formats — flowcharts, org charts, comparison
  tables presented visually.
- Test: plotting numeric data on axes? → graph. Different structure? → chart.
- Don't trust the caption's label. A Bloomberg screenshot labelled "Bar Chart"
  is usually an OHLC chart (open-high-low-close) — each vertical bar is one
  trading period. Its cousin is the candlestick chart. For these, **"price
  chart"** is the honest plain-language term. Common in finance material.

## Length targets

| Visual type | Target |
|---|---|
| Photographs, ads | closer to ~25 words, less if it reads clean |
| Graphs, timelines, charts, diagrams | up to ~20 words, aim much lower — one tight line is the goal, two sentences the ceiling |
| Flagged tables | ~15 words, one clause |

Targets, not quotas — lean short. One crisp <=15-word line usually does the
whole job. Reach for a second sentence only when the visual genuinely carries
two distinct ideas.

## Exhibit titles

- Do NOT copy the exhibit title word-for-word.
- DO reference the relevant subject matter, phrased differently, so the alt
  text isn't a redundant echo of a caption that may already be read aloud.
- **Dates:** cut dates that merely restate the title. If the title does NOT
  state the span, pull it from the axis or plotted data and include it — a
  graph labelled only "sales history" reads very differently stopping in 2004
  versus running to 2025. Test: is the date in the title? Yes → cut. No, and
  it's part of what the visual shows → keep.

## Multiple images in one exhibit

Give each genuinely separate image its own alt text. But a single **composed
graphic is ONE figure** — a designed unit (category boxes plus product photos
arranged as one graphic) reads as one thing and gets one alt text. Don't
shatter a composed layout into a description per sub-element.

**Logo walls / client collages:** do NOT enumerate every logo. One brief line:
"Selection of logos for [company]'s major clients gathered together."

## Repeated exhibits in a series

When a run of exhibits is built from one template differing only by a single
variable, describe the structure once and lean on "same format" for the rest,
naming only what changes ("same format, for the bars line"). Each line must
still name the distinguishing variable.

## Tables

**Do NOT write alt text for tables by default.** Most are genuine tables a
screen reader navigates cell by cell. Only write alt text for a table when the
user explicitly says that table needs it.

When a table IS flagged: describe it, never transcribe it. ONE short orienting
line naming what kind of table it is and what it's **about** — its subject,
not its mechanics. ~15 words, one clause. Naming columns or computations
re-summarises what the screen reader is about to walk.

- Too wordy (mechanics): "Table comparing the two firms on depreciation,
  laying out capacity, output, and utilization to work out how their per-unit
  depreciation would differ if each ran at its designed volume."
- Tight (subject): "Table comparing the two firms' per-unit depreciation if
  each ran at its designed volume."

Never read out cells — even when the table is trapped in an image. Flag it as
data-locked-in-an-image instead; flagging is a one-line heads-up, not licence
to transcribe.

## Structural diagrams count as figures — even when built from live text

A diagram whose meaning lives in its structure — org chart, nested-box claim
parse, flow or process chart, value chain, chasm/two-sides diagram, arrow
diagram of converging forces — is a describable figure and gets a normal
one-line orienting description, even when drawn as vector boxes with live,
selectable text inside.

The test is not "is the text live?" but **"does the arrangement carry meaning
the linear text doesn't?"**

Genuinely plain live-text content still gets skipped: real data tables,
equations that are live text, ordinary bulleted lists, simple bordered callout
boxes that are just a sentence in a frame.

## Images of text (reproduced forms, statements, rate sheets)

Here the image IS the content. Two paired parts:

1. A short identifying alt naming what the image is.
2. A full text alternative transcribing the content.

When transcribing for the ear: spell out numbered lists ("One... Two..."),
expand abbreviations ("F2S" -> "Farm to School"), transcribe faithfully —
don't silently fix the source; flag oddities rather than correcting invisibly.

**Flag data-locked-in-an-image** so the user can decide.

Boundary — not everything with words on it is an image-of-text. Transcription
is for cases where the exact wording is the payload. A designed values /
mission / policy slide is treated like any ordinary figure: concept-first
line, no quoting. Ask whether losing the exact words loses the point.

## Genograms and family trees

Describe high-level: call it a family-tree diagram, note roughly how many
generations, convey the general shape. Leave symbol conventions out by default
— assume a key explains them. Name a convention only when it carries meaning
the text doesn't cover.

## Opening words

Open on the noun naming the visual type, **with no leading article**:
"Line graph tracking...", "Screenshot of...", "Photograph of...",
"Flowchart of..." — never "A line graph...", "An org chart...".
Never begin with "image of" or "picture of".

## Mathematical formulas (full fidelity — the one exception)

First check whether it needs alt text at all: an equation that is already
live, selectable text does not. Only write formula alt text for equations that
appear as images.

Watch for equation objects that only LOOK live — MathType/Office Equation
Editor embeds often carry a text layer that linearises into scrambled tokens
(`X t M e q p e p q t ( ) ( ) ( ) = - +`). Scrambled extraction is the tell:
treat as an image. Clean in-order inline math is genuinely live — skip.

Don't scope the formula pass to exhibits — equations live in running
narrative, appendices, footnotes and worked examples.

**Less-is-more does NOT apply.** Rules:

- Read every term in full. No "the same form as above" — a listener cannot
  re-scan.
- Explicit grouping words. Fractions: numerator, then "all over", then
  denominator. Grouped terms: "the quantity ...".
- Voice true exponents trimmed to "to the i" (not "to the power of i").
- Grouped expression raised to a power: fence the group first, power at the
  end — "the quantity X of t minus one, squared", NOT "the square of X of t
  minus one". Same for roots: "the square root of the quantity b squared
  minus four a c".
- Distinguish superscript/subscript LABELS from powers. `R^RF` is
  "R superscript RF", not "R to the RF". Subscripts: "R sub t".
- **Read what is literally rendered** — never simplify to an algebraic
  equivalent. Transcribe internal inconsistencies and flag the mismatch.
- Numbers as full number-words ("two hundred sixty thousand").
- Set up structure before details: "the sum, from i equals one to thirty-five,
  of ...".
- Accuracy is paramount. State back how you're reading the equation so a
  misparse can be caught. Re-examine at high magnification when unsure.

Example — present value: "V equals the sum, from i equals one to infinity, of
the following term: rent in year i minus expenses in year i, all over the
quantity one plus r, to the i."

Example — Sharpe ratio: "SR equals: one over T, times the sum, from t equals
one to T, of the quantity R sub t minus R sub t superscript RF; all over sigma
of R sub t."

## Worked examples

Bar graph titled "Q3 Regional Revenue Growth (%)"
- Too detailed: "Bar graph titled Q3 Regional Revenue Growth showing North at
  12%, South at 8%, East at 15%, West at 4%."
- Leading article: "A bar graph comparing revenue growth..."
- Good: "Bar graph comparing revenue growth across four regions in the third
  quarter, with growth varying noticeably by region."

Timeline titled "History of the Company: 1990-2020"
- Good: "Timeline tracing the company's major milestones from its founding
  through its growth into an international business." (span omitted — the
  title carries it)

Timeline with no span in the title
- Good: "Timeline comparing the two programs from 1970 to 1990, aligning their
  milestones against contemporary conflicts." (span pulled off the axis)

Line graph of an exchange rate
- Good: "Line graph of the yen-to-dollar exchange rate over roughly nine
  years. The rate dips through the middle before rebounding sharply near the
  end."

Genogram
- Good: "Family-tree diagram spanning three generations, running from a
  founding couple at the top down through their children and grandchildren."

Product photo — colour stripped, named specs kept
- Padded: "A compact fluorescent bulb with a white spiral tube in red-and-
  white retail blister packaging."
- Good: "Fluorescent light bulb with a spiral tube, in packaging that
  advertises it as a 60-watt-equivalent bright white bulb rated to last nine
  years."

Specifications screenshot
- Good: "Screenshot of the bulb's online product page listing specifications
  such as dimensions, lumens, wattage, colour temperature, rated life, and
  voltage."
