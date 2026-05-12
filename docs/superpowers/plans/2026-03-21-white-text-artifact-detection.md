# White Text Artifact Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect near-white/invisible text in PDFs and mark it as an artifact so it is excluded from WCAG 1.4.3 contrast checks, while safely preserving white text that sits on a legitimately dark background.

**Architecture:** Two new functions are added to `pdf_extractor.py`: `_extract_filled_rects()` does a single pre-pass over each page's content stream (via pikepdf) to collect all filled rectangles and their RGB fill colors; `_has_dark_background()` checks whether a text block's center point is covered by one of those dark rectangles. `_classify_elements()` gains a new guard: if a text block's font color is near-white AND no dark rectangle covers it, it is classified as `ElementType.WATERMARK` (artifact). Two configuration constants control the thresholds.

**Tech Stack:** Python 3, pikepdf (content stream parsing), pdfminer.six (text/color extraction already in place)

---

## File Map

| File | Change |
|------|--------|
| `config.py` | Add `INVISIBLE_TEXT_COLOR_THRESHOLD` and `DARK_BACKGROUND_LUMINANCE` constants |
| `pdf_extractor.py` | Add `_extract_filled_rects()`, add `_has_dark_background()`, update `_classify_elements()` signature + logic, update `extract_document()` call site |

No other files need to change.

---

### Task 1: Add configuration constants

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add the two constants at the bottom of the watermark section**

Open `config.py` and add after the existing `WATERMARK_LIGHT_COLOR_THRESHOLD = 0.7` line:

```python
# Invisible/white text detection
INVISIBLE_TEXT_COLOR_THRESHOLD = 0.95  # all RGB channels above this → near-white text
DARK_BACKGROUND_LUMINANCE = 0.5        # luminance below this → background counts as dark
```

- [ ] **Step 2: Verify syntax**

```bash
cd /Users/mohakgarg/Desktop/KTR_REMIDIATION && source venv/bin/activate && python -c "import config; print(config.INVISIBLE_TEXT_COLOR_THRESHOLD, config.DARK_BACKGROUND_LUMINANCE)"
```

Expected output: `0.95 0.5`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add thresholds for invisible-text artifact detection"
```

---

### Task 2: Add `_extract_filled_rects()`

**Files:**
- Modify: `pdf_extractor.py` (add new function after `_extract_images`)

This function does one pass over each page's raw content stream to find every `re` (rectangle) + fill operator pair and records its bounding box and RGB fill color, respecting `q`/`Q` graphics-state save/restore for color.

- [ ] **Step 1: Add the function**

Insert this function in `pdf_extractor.py` immediately after the `_extract_images()` function (around line 765):

```python
def _extract_filled_rects(pdf_path: str, page_count: int) -> list:
    """Parse each page's content stream to collect filled rectangles and their RGB fill colors.

    Returns a list of length page_count. Each entry is a list of tuples:
        (x0, y0, x1, y1, r, g, b)  — all floats, r/g/b in [0, 1].

    Only simple rectangles defined by the 're' operator are tracked.
    Complex paths (m/l/c) are ignored because they are almost never used
    for solid background fills in business documents.

    Colour state is tracked through q/Q save-restore pairs.
    """
    result = [[] for _ in range(page_count)]

    try:
        pdf = pikepdf.Pdf.open(pdf_path)
    except Exception as e:
        logger.warning("Could not open PDF for background rect extraction: %s", e)
        return result

    for page_idx, page in enumerate(pdf.pages):
        if page_idx >= page_count:
            break
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception as e:
            logger.debug("Could not parse content stream on page %d: %s", page_idx, e)
            continue

        fill_color = (0.0, 0.0, 0.0)   # default fill: black
        color_stack = []                 # for q/Q save-restore
        current_rect = None              # last 're' rectangle seen
        complex_path = False             # True if non-rect path ops seen after 're'

        for operands, operator in ops:
            op = str(operator)

            # --- Graphics state save / restore ---
            if op == "q":
                color_stack.append(fill_color)
                continue
            if op == "Q":
                if color_stack:
                    fill_color = color_stack.pop()
                continue

            # --- Non-stroking (fill) colour operators ---
            if op == "rg" and len(operands) >= 3:
                try:
                    fill_color = (
                        float(operands[0]),
                        float(operands[1]),
                        float(operands[2]),
                    )
                except (ValueError, TypeError):
                    pass
                continue

            if op == "g" and len(operands) >= 1:
                try:
                    g = float(operands[0])
                    fill_color = (g, g, g)
                except (ValueError, TypeError):
                    pass
                continue

            if op == "k" and len(operands) >= 4:
                try:
                    c, m, y, k = [float(v) for v in operands[:4]]
                    fill_color = (
                        (1 - c) * (1 - k),
                        (1 - m) * (1 - k),
                        (1 - y) * (1 - k),
                    )
                except (ValueError, TypeError):
                    pass
                continue

            # --- Rectangle path definition ---
            if op == "re" and len(operands) >= 4:
                try:
                    x = float(operands[0])
                    y = float(operands[1])
                    w = float(operands[2])
                    h = float(operands[3])
                    current_rect = (
                        min(x, x + w), min(y, y + h),
                        max(x, x + w), max(y, y + h),
                    )
                    complex_path = False
                except (ValueError, TypeError):
                    current_rect = None
                continue

            # --- Complex path ops (not a simple rect) ---
            if op in ("m", "l", "c", "v", "y", "h"):
                complex_path = True
                continue

            # --- Fill operations ---
            if op in ("f", "F", "f*", "B", "B*", "b", "b*"):
                if current_rect and not complex_path:
                    result[page_idx].append((*current_rect, *fill_color))
                current_rect = None
                complex_path = False
                continue

            # --- Path end without fill ---
            if op in ("n", "S", "s", "W", "W*"):
                current_rect = None
                complex_path = False
                continue

    try:
        pdf.close()
    except Exception:
        pass

    return result
```

- [ ] **Step 2: Verify it imports and runs without error on the existing PDF**

```bash
cd /Users/mohakgarg/Desktop/KTR_REMIDIATION && source venv/bin/activate && python -c "
from pdf_extractor import _extract_filled_rects
rects = _extract_filled_rects('input/KE1294_DNC_20240820_accessible.pdf', 12)
print('Pages with rects:', sum(1 for p in rects if p))
print('Page 9 rects:', len(rects[8]))
for r in rects[8]:
    print(' ', r)
"
```

Expected: Prints rect data for page 9 (0-indexed = index 8). Should show at least some coloured rectangles from the table.

- [ ] **Step 3: Commit**

```bash
git add pdf_extractor.py
git commit -m "feat: add _extract_filled_rects() for background colour tracking"
```

---

### Task 3: Add `_has_dark_background()` helper

**Files:**
- Modify: `pdf_extractor.py` (add immediately after `_extract_filled_rects`)

- [ ] **Step 1: Add the helper function**

Insert immediately after `_extract_filled_rects()`:

```python
def _has_dark_background(bbox: "BBox", bg_rects: list) -> bool:
    """Return True if the text block's centre point falls inside a dark filled rectangle.

    'Dark' means the rectangle's fill colour has a luminance (ITU-R BT.601)
    below config.DARK_BACKGROUND_LUMINANCE.

    Uses the centre point of the text block for matching to avoid false
    positives from adjacent dark elements on the same page.
    """
    cx = (bbox.x0 + bbox.x1) / 2
    cy = (bbox.y0 + bbox.y1) / 2

    for rx0, ry0, rx1, ry1, r, g, b in bg_rects:
        if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            if luminance < config.DARK_BACKGROUND_LUMINANCE:
                return True
    return False
```

- [ ] **Step 2: Quick smoke test**

```bash
cd /Users/mohakgarg/Desktop/KTR_REMIDIATION && source venv/bin/activate && python -c "
from pdf_extractor import _has_dark_background
from models import BBox
# Simulate a white text block at (100,100)-(200,120)
bbox = BBox(100, 100, 200, 120)
# Dark rect that covers it (navy blue)
dark_rects = [(50, 80, 300, 140, 0.0, 0.05, 0.2)]
print('Has dark bg (should be True):', _has_dark_background(bbox, dark_rects))
# No rect covers it
print('Has dark bg (should be False):', _has_dark_background(bbox, []))
# White rect covers it
white_rects = [(50, 80, 300, 140, 1.0, 1.0, 1.0)]
print('Has dark bg with white rect (should be False):', _has_dark_background(bbox, white_rects))
"
```

Expected output:
```
Has dark bg (should be True): True
Has dark bg (should be False): False
Has dark bg with white rect (should be False): False
```

- [ ] **Step 3: Commit**

```bash
git add pdf_extractor.py
git commit -m "feat: add _has_dark_background() helper for contrast-safe text detection"
```

---

### Task 4: Update `_classify_elements()` to detect invisible text

**Files:**
- Modify: `pdf_extractor.py` — `_classify_elements()` signature and watermark block

- [ ] **Step 1: Update function signature**

Change:
```python
def _classify_elements(page: PageContent, body_font_size: float, hf_signatures: set):
```
To:
```python
def _classify_elements(page: PageContent, body_font_size: float, hf_signatures: set,
                       bg_rects: list = None):
```

- [ ] **Step 2: Add the invisible-text guard at the TOP of the per-block loop**

Inside `_classify_elements`, the `for tb in page.text_blocks:` loop currently starts with watermark detection. Add a new guard **before** the existing watermark block:

```python
        # --- Invisible (near-white) text detection ---
        # Text whose colour is essentially white and has no dark background
        # is invisible to sighted users — mark it as an artifact so it is
        # excluded from WCAG 1.4.3 contrast checks.
        if bg_rects is not None:
            is_near_white = all(
                c >= config.INVISIBLE_TEXT_COLOR_THRESHOLD
                for c in tb.font.color
            )
            if is_near_white and not _has_dark_background(tb.bbox, bg_rects):
                tb.element_type = ElementType.WATERMARK
                continue
```

- [ ] **Step 3: Verify syntax**

```bash
cd /Users/mohakgarg/Desktop/KTR_REMIDIATION && source venv/bin/activate && python -c "import pdf_extractor; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add pdf_extractor.py
git commit -m "feat: classify near-white text with no dark background as artifact"
```

---

### Task 5: Wire everything together in `extract_document()`

**Files:**
- Modify: `pdf_extractor.py` — `extract_document()` Phase 6 call site

- [ ] **Step 1: Add background rect extraction before Phase 6**

In `extract_document()`, between Phase 5 (detect header/footer signatures) and Phase 6 (classify elements), add:

```python
    # Phase 5b: Extract filled rectangles for white-text background detection
    bg_rects_per_page = _extract_filled_rects(pdf_path, len(raw_pages))
```

- [ ] **Step 2: Pass bg_rects into `_classify_elements()`**

Change the existing Phase 6 loop from:
```python
    for page in raw_pages:
        _classify_elements(page, body_font_size, hf_signatures)
```
To:
```python
    for page in raw_pages:
        page_bg = (bg_rects_per_page[page.page_number]
                   if page.page_number < len(bg_rects_per_page) else [])
        _classify_elements(page, body_font_size, hf_signatures, page_bg)
```

- [ ] **Step 3: Full end-to-end run**

```bash
cd /Users/mohakgarg/Desktop/KTR_REMIDIATION && source venv/bin/activate && python main.py 2>&1
```

Expected output ends with:
```
[PASS ] KE1294_DNC_20240820_accessible.pdf  ...
Total: 1 | Processed: 1 | Compliant: 1 | Failed: 0
```

Also verify the artifact count increased compared to before (was 46, should now be higher since the hidden white-text blocks are now marked as artifacts):

```bash
cd /Users/mohakgarg/Desktop/KTR_REMIDIATION && source venv/bin/activate && python -c "
from pdf_extractor import extract_document
from models import ElementType
doc = extract_document('input/KE1294_DNC_20240820_accessible.pdf')
artifacts = sum(
    1 for p in doc.pages for tb in p.text_blocks
    if tb.element_type in (ElementType.WATERMARK, ElementType.HEADER_FOOTER)
)
print('Total artifacts now:', artifacts)
"
```

Expected: number higher than the previous 46.

- [ ] **Step 4: Commit**

```bash
git add pdf_extractor.py
git commit -m "feat: wire background rect extraction into classify pipeline"
```

---

## Safety Summary

| Scenario | What happens | Correct? |
|---|---|---|
| Hidden white text on white page | Near-white → no dark bg → artifact | ✅ |
| White text in dark navy table header | Near-white → dark rect found → kept as real content | ✅ |
| Normal dark text (99% of all text) | Not near-white → check skipped entirely | ✅ |
| Light-gray text (< 0.95 threshold) | Not near-white → check skipped | ✅ |
| White text on image/gradient background | Near-white → no rect found → artifact | ⚠️ Rare edge case |
| White text inside a form XObject | Near-white → no rect found → artifact | ⚠️ Very rare |

The two edge cases at the bottom are extremely uncommon in business/case-study PDFs. If they arise, the `INVISIBLE_TEXT_COLOR_THRESHOLD` constant in `config.py` can be raised to `1.0` to narrow the detection to pure-white only.
