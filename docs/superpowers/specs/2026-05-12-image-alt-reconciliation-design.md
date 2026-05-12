# Image / Alt-text Reconciliation — Design Spec

**Date:** 2026-05-12
**Author:** Mohak Garg (with Claude)
**Target branch:** `fix/image-alt-reconciliation` (off `develop`)
**Status:** Awaiting user approval before implementation

---

## Problem

Three correlated bugs surface in horizontally-arranged image regions, watermarked DNC documents, and decoratively-marked images:

1. **Horizontally adjacent images get scrambled alt text.** Brownfield case: four photos in two rows display as visual order `1 2 / 3 4` but receive alts in order `4 1 / 3 2`.
2. **DNC (watermarked) cases lose alt text after the first image in a row.** Subsequent images show the placeholder `"Figure N on page M"` in place of the authored alt.
3. **Decorative images become labelled figures.** The Kellogg logo, marked Decorative in Word, becomes an announced `/Figure` in the output with uninformative alt text.

All three trace to **`pdf_extractor._extract_images`** matching source-PDF alt texts to image XObjects **by index**. That assumes three independent orderings agree — pdfminer's layout order, PDF resource-dict order, and structure-tree walk order — which they only do when the source is a tidy vertical flow of images. Watermark image XObjects (which have no alt entries in the struct tree) further shift indices on DNC documents.

## Goal

Replace the index-based matching with a **source-aware, position-based reconciliation pass** that maps every image XObject occurrence to its source-PDF intent (alt text, decorative status, watermark status). Generalized across producers (Word, InDesign, Acrobat) and arrangements (vertical, horizontal, grids, nested in Form XObjects).

Acceptance: zero veraPDF regressions on the existing corpus, all custom invariant assertions pass, and the bug-trigger PDFs (Brownfield, Unilever, Perils Watermarked) produce correct output as judged by manual PAC 2024 review.

## Non-goals

- Improving alt-text *quality* when the source had none (out of scope — handled by upstream Word authoring).
- Adding language detection or other accessibility fixes (unrelated).
- Refactoring `pdf_tagger.py` matching logic — its position-based tag-time matching is correct; the bug is upstream in extraction.

---

## Architecture

A single new module `image_reconciliation.py` (~250 lines) with one public function and a few dataclasses. Smallest possible blast radius.

```
input PDF
  ↓
extract_document()                                     # pdf_extractor.py
  ├─ _extract_text_blocks()                            # unchanged
  └─ _extract_images()                                 # rewritten to delegate
       └─ reconcile_page_images() ──── NEW MODULE ───┐
                                                     │
            ┌──── struct-tree walk ──────────────────┤
            │     produces SourceStructElement[]     │
            │     (Figure + Artifact, with MCIDs,    │
            │      /Alt, optional /BBox)             │
            │                                        │
            ├──── content-stream pass ───────────────┤
            │     produces ImageOccurrence[]         │
            │     (xobj_name, page_bbox, MCID,       │
            │      in_watermark)                     │
            │                                        │
            └──── matching engine ───────────────────┘
                  produces ResolvedImage[] per page,
                  ordered top-to-bottom, left-to-right
  ↓
tag_pdf()                                              # pdf_tagger.py — unchanged
postprocess + validate                                 # unchanged
```

### Public API

```python
@dataclass
class ResolvedImage:
    xobject_name: str            # e.g. "/Im0" — for tagger to match Do operators
    bbox: BBox                   # page coordinates (never image-space)
    alt_text: str                # "" if decorative or no source alt
    is_decorative: bool          # True → tag as /Artifact
    is_watermark: bool           # True → wrap as /Artifact /Subtype /Watermark

def reconcile_page_images(pdf_path: str) -> dict[int, list[ResolvedImage]]:
    """Return per-page list of ResolvedImage in visual reading order."""
```

### Integration touchpoints (two)

1. **`pdf_extractor._extract_images`** is rewritten to call `reconcile_page_images` and build `ImageBlock` objects from the result. External contract preserved — still populates `page_content.images` with bbox, alt_text, is_decorative, image_bytes. `_read_existing_alt_texts` and `_collect_images_recursive` are removed (their roles are subsumed by reconciliation).
2. **`pdf_tagger._detect_watermark_forms`** is moved into `image_reconciliation.py` so extractor and tagger share one implementation. Tagger imports from the new location. No behavioral change to the tagger.

Everything else (`pdf_postprocess.py`, `validator.py`, `main.py`, `app.py`, `models.py`) is untouched.

---

## Components

### 1. Struct tree reader

Walks `/StructTreeRoot` recursively, dedupes by `objgen`, emits one `SourceStructElement` per `/Figure` and per `/Artifact`.

```python
@dataclass
class SourceStructElement:
    struct_type: str             # "/Figure" or "/Artifact"
    alt_text: str                # "" if /Alt is missing
    page_index: int
    mcids: list[int]             # leaf integer kids in /K subtree
    bbox: Optional[BBox]         # from element's /BBox if present, else None
```

Key differences from the current `_read_existing_alt_texts`:

- Captures `/Artifact` elements too (current code skips them — that's how decorative status gets lost).
- Captures MCID lists (current code captures nothing about position).
- Captures `/BBox` if the producer emitted one (Acrobat sometimes does).

### 2. Content-stream pass

Per page, parses operators while tracking:

- **Marked-content stack** (BDC/BMC push, EMC pop; innermost MCID property is the active MCID at any operator)
- **Graphics-state stack** (q/Q saves the full CTM)
- **CTM** (concat-matrix operator `cm` multiplies the current CTM)
- **Form XObject descent**: when `Do` references a Form XObject, save CTM stack, parse Form's content stream with the concatenated CTM, recurse. This is how InDesign-nested figures get reached.

For each `Do` operator referencing an Image XObject (or an Image inside a recursed Form), emit:

```python
@dataclass
class ImageOccurrence:
    xobject_name: str            # e.g. "/Im0"
    page_bbox: BBox              # CTM applied to unit square; covers rotation/skew
    mcid: Optional[int]          # innermost active MCID at this Do, or None
    in_watermark_ancestor: bool  # True if any ancestor Form is a watermark
```

Bbox is computed from all four corners of the unit-square image space transformed by the CTM, then min/max across corners — handles rotation and skew, not just axis-aligned scale.

Watermark Form detection runs once per page (`_detect_watermark_forms`, moved from tagger). Image XObjects inside a watermark Form inherit `in_watermark_ancestor=True`.

### 3. Matching engine

For each `ImageOccurrence` on a page, decide its `ResolvedImage` fields by priority:

| Priority | Condition | Outcome |
|---|---|---|
| 1 | `in_watermark_ancestor` | `is_decorative=True, is_watermark=True, alt_text=""` |
| 2 | `mcid` exists and matches a `SourceStructElement.mcids` | `is_decorative = (struct_type == "/Artifact")`; `alt_text = source.alt_text` |
| 3 | Bbox overlap (≥50% IOU) with a `SourceStructElement` (greedy, each source claimed at most once) | Same as priority 2 |
| 4 | No match, in header band (top 10% of page) or footer band (bottom 10%) | `is_decorative=True, alt_text=""` |
| 5 | No match, body region | `is_decorative=False, alt_text=""` (tagger handles fallback as today) |

Per-page result sorted by `(page_height - y_center, x_center)` so output is in visual reading order (top→bottom, left→right). PDF y-axis is bottom-up, hence the subtraction.

---

## Data flow (worked example: 2×2 grid of photos)

Source PDF: four photos arranged 2-rows × 2-cols on page 1. Source Word doc gave alts `"Photo 1"`, `"Photo 2"`, `"Photo 3"`, `"Photo 4"` reading left-to-right, top-to-bottom. Word's struct tree walks Figures in that logical order. But Word emits the page's `/XObject` resource dict in insertion order: `/Im0=Photo3, /Im1=Photo1, /Im2=Photo4, /Im3=Photo2` (real-world example of producer arbitrariness).

### Current (broken) behavior

- `_read_existing_alt_texts` returns `["Photo 1", "Photo 2", "Photo 3", "Photo 4"]`.
- `_collect_images_recursive` yields `/Im0, /Im1, /Im2, /Im3` in dict order.
- Index match: `/Im0 (Photo3)` gets alt `"Photo 1"`, `/Im1 (Photo1)` gets `"Photo 2"`, etc.
- Result on screen: photos appear with scrambled alts.

### New behavior

- Struct tree reader emits four `SourceStructElement(struct_type="/Figure")` with MCIDs `[10], [11], [12], [13]` and alts `"Photo 1"..."Photo 4"`.
- Content stream pass finds four `ImageOccurrence` records — one per `Do` operator — each with its own MCID and CTM-derived bbox.
- Matching engine pairs MCID 10 ↔ "Photo 1", 11 ↔ "Photo 2", etc. **Resource-dict order is irrelevant.**
- ResolvedImage list sorted visually: Photo1 (top-left), Photo2 (top-right), Photo3 (bottom-left), Photo4 (bottom-right).

If the source PDF had no MCIDs (rare; some Acrobat-exported PDFs strip them), Priority 3 kicks in: each occurrence finds the SourceStructElement with the largest bbox overlap. Bboxes for source elements come from `/BBox` if present, else are derived by re-resolving each element's MCID list through the content-stream pass (a second mini-walk just for source elements).

---

## Error handling

Per-page try/except wrapping the entire reconciliation. If anything throws inside `reconcile_page_images` for page N, log warning at DEBUG and fall back to the existing index-based logic for page N only. Other pages still receive the new robust path.

If `/StructTreeRoot` is absent entirely → reconciliation returns empty; existing fallback (placeholder alts) kicks in unchanged.

If content-stream parsing fails (malformed PDF) → fall back to resource-dict iteration without alt text on that page (no worse than today).

All struct-walk and content-stream cycle detection uses `objgen` dedupe, same pattern as existing code.

**Invariant:** the new path never produces worse output than the old path. Worst case is "no improvement" on a problem page, never regression.

---

## Testing

### Layer 1: unit tests (`test_image_reconciliation.py`, new)

Synthetic in-memory struct trees + parsed content streams (no real PDF files needed; uses the same `pikepdf` mocking pattern as `test_fixes.py`). Cases:

- Horizontal row, MCIDs present → correct alt assignment.
- Horizontal row, no MCIDs, /BBox absent → bbox derived via content-stream re-walk, correct assignment.
- Vertical stack → same output as old code would produce (regression check).
- `/Artifact`-wrapped image → `is_decorative=True`.
- Watermark Form containing an image → `is_watermark=True`.
- Image XObject drawn twice on a page → two `ResolvedImage` records, each correctly tagged.
- Image with no source struct entry, in header band → `is_decorative=True` via heuristic.
- Mixed `/Figure` + `/Artifact` on same page → each gets correct treatment.

### Layer 2: pipeline integration tests (`test_pipeline_integration.py`, new)

For each PDF in `tests/fixtures/`, run the full pipeline end-to-end. Assertions:

- **veraPDF returns PASS** (no new failures, no regressions).
- **Image-tagging invariant** (the core "PAC-equivalent" custom check):
  every Image XObject in the output is inside a `/Figure` element with non-empty `/Alt`, OR inside an `/Artifact` wrapper. No third option.
- **No placeholder leak**: no `/Alt` in the output matches the regex `^Figure \d+ on page \d+$`. (Placeholder strings are an internal fallback; they should never reach production output when source intent is available.)
- **Preservation check**: every non-empty `/Alt` string present in the source struct tree appears in the output struct tree. (Catches both reordering and dropout.)

### Layer 3: fixture corpus (`tests/fixtures/`)

Permanent regression corpus, added to git LFS or stored as small representative samples:

- `Perils_regular.pdf` (existing input/)
- `Perils_watermarked.pdf` (existing input/)
- `KE1335_raw.pdf` (existing input/)
- `KE1335_already_accessible.pdf` (existing input/)
- `Brownfield.pdf` (canonical horizontal-shuffle case)
- `Unilever_Vitality_regular.pdf` (Kellogg logo case)
- `Unilever_Vitality_DNC.pdf` (watermarked + grouped images)

Every future PR runs against this full corpus automatically.

### Layer 4: existing test_fixes.py

The three reviewer-fix unit tests must continue to pass. Gates against regressing prior fixes.

---

## What does NOT change

- `models.py` data classes (signatures preserved).
- `pdf_tagger.py` matching logic (already correct — only the data it consumes changes).
- `pdf_postprocess.py`, `validator.py`, `main.py`, `app.py`.
- `_extract_text_blocks` and all text classification in `pdf_extractor.py`.
- Existing CLI / Streamlit UX.

---

## Rollout

1. Create branch `fix/image-alt-reconciliation` off `develop`.
2. Implement and unit-test `image_reconciliation.py`.
3. Rewire `pdf_extractor._extract_images`; remove `_read_existing_alt_texts` and `_collect_images_recursive`.
4. Move `_detect_watermark_forms` into the new module; update tagger import.
5. Add `tests/fixtures/` corpus; add `test_pipeline_integration.py`.
6. Run full test suite (`test_fixes.py` + new unit + new integration) → all green.
7. Manual PAC 2024 spot-check on Brownfield, Unilever DNC, Perils Watermarked.
8. PR to `develop` for Codex + Gemini review.
9. After review approval + manual PAC pass: merge to `develop`, then `main`.

## Risks

- **Source PDFs without MCIDs and without /BBox** force Priority 3 bbox-derived matching. Mitigation: bbox derivation reuses the same content-stream parser, so it's consistent with the source's actual draw positions.
- **Producer emits a /Figure with no MCIDs and no /BBox** (rare). Mitigation: falls through to Priority 4 — header/footer heuristic — which classifies it as decorative. Conservative but never incorrect.
- **Performance**: extra content-stream pass per page. Mitigation: content stream is already parsed by `pdf_tagger`; we'll measure and, if needed, cache parsed operators between extractor and tagger. Not expected to be a bottleneck (current pipeline is bound by image extraction and veraPDF, not parsing).

---

## Open questions

None. Design is complete and ready for implementation planning.
