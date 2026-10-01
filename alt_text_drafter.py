"""
alt_text_drafter.py — draft alt text for figures that don't have any.

Backend only. Runs as a stage AFTER the structure tree is built and
post-processed, so it never touches tagging logic: it walks the finished
/StructTreeRoot, finds /Figure elements whose /Alt is missing or generic
("", "Figure", "Image"), writes a description into /Alt, and emits a sidecar
review report.

**A figure that already carries a description is never touched.** That is the
whole contract with the reviewer — their authored alt text is authoritative.

Two drafting paths, cheapest-and-most-accurate first:

1. ``deterministic`` — when the chart's data sits in the page's TEXT layer
   (Nissan's pie charts: "Nano 16%", "Alto 69%" are real text objects), the
   numbers are read directly and composed into a description. Nothing leaves
   the machine, and the values are exact rather than read off pixels.
2. ``vision`` — otherwise the figure is rendered to PNG and sent to Claude.
   This is the only path for raster exhibits (KEL189's pie, HP's charts),
   where the labels are baked into the image.

Everything that is neither — photographs, logos, anything the classifier is
unsure about — is left blank and listed in the report for manual authoring,
per the original proposal to Charlotte.

Drafts are written as clean text with NO "[DRAFT]" prefix: /Alt is read aloud
verbatim by a screen reader, so a marker there would be spoken. Which figures
were drafted (and by which path) is recorded in the sidecar report and in the
PDF's document info instead.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Optional

import pikepdf

import config

logger = logging.getLogger(__name__)

_GENERIC_ALTS = {"", "figure", "image", "graphic", "picture"}

# A caption is the strongest context signal we have for a figure: it is the
# author's own one-line statement of what the reader should take from it.
_CAPTION_RE = re.compile(
    r"^\s*(exhibit|figure|table|chart|appendix)\s+([0-9]+[A-Za-z]?)\s*[:.—-]\s*(.+)",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")

# Minimum rendered crop edge, in pixels, worth sending to a vision model.
_MIN_CROP_PX = 40

# Consecutive API failures after which the vision path is abandoned for the
# rest of the document.
_API_FAILURE_LIMIT = 2


@dataclass
class FigureContext:
    """Everything known about one undescribed figure before drafting."""
    page: int                      # 1-based
    bbox: list                     # [x0, y0, x1, y1] in PDF points
    caption: Optional[str] = None
    caption_label: Optional[str] = None   # e.g. "Exhibit 4A"
    source_note: Optional[str] = None
    nearby_text: list = field(default_factory=list)
    data_labels: list = field(default_factory=list)  # [(label, percent), ...]
    width_pt: float = 0.0
    height_pt: float = 0.0


@dataclass
class DraftResult:
    page: int
    bbox: list
    caption: Optional[str]
    method: str            # "deterministic" | "vision" | "skipped"
    alt_text: Optional[str]
    reason: Optional[str] = None
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# Finding figures that need a description
# ---------------------------------------------------------------------------

def _is_generic(alt) -> bool:
    if alt is None:
        return True
    return str(alt).replace("\x00", "").strip().lower() in _GENERIC_ALTS


def _elem_bbox(node) -> Optional[list]:
    a = node.get("/A")
    if a is None:
        return None
    try:
        entry = a[0] if isinstance(a, pikepdf.Array) else a
        bb = entry.get("/BBox")
        if bb is None:
            return None
        return [float(v) for v in bb]
    except Exception:
        return None


def find_undescribed_figures(pdf: pikepdf.Pdf) -> list:
    """Return [(struct_elem, page_number_1based, bbox)] needing a description."""
    page_of = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}

    def resolve_page(node):
        pg = node.get("/Pg")
        if pg is not None:
            return page_of.get(pg.objgen)
        kids = node.get("/K")
        if kids is None:
            return None
        for kid in (kids if isinstance(kids, pikepdf.Array) else [kids]):
            if isinstance(kid, pikepdf.Dictionary):
                g = kid.get("/Pg")
                if g is not None:
                    return page_of.get(g.objgen)
                r = resolve_page(kid)
                if r is not None:
                    return r
        return None

    out = []

    def walk(node):
        if not isinstance(node, pikepdf.Dictionary):
            return
        if str(node.get("/S")) == "/Figure" and _is_generic(node.get("/Alt")):
            out.append((node, resolve_page(node), _elem_bbox(node)))
        kids = node.get("/K")
        if kids is None:
            return
        for kid in (kids if isinstance(kids, pikepdf.Array) else [kids]):
            if isinstance(kid, pikepdf.Dictionary):
                walk(kid)

    st = pdf.Root.get("/StructTreeRoot")
    kids = st.get("/K") if st is not None else None
    if kids is not None:
        for k in (kids if isinstance(kids, pikepdf.Array) else [kids]):
            walk(k)
    return out


# ---------------------------------------------------------------------------
# Context harvesting — the free, deterministic part
# ---------------------------------------------------------------------------

def _overlaps(a, b, pad=0.0) -> bool:
    return not (a[2] < b[0] - pad or a[0] > b[2] + pad
                or a[3] < b[1] - pad or a[1] > b[3] + pad)


def _rect_distance(point, rect) -> float:
    """Distance from a point to a rect (0 when inside)."""
    px, py = point
    dx = max(rect[0] - px, 0.0, px - rect[2])
    dy = max(rect[1] - py, 0.0, py - rect[3])
    return (dx * dx + dy * dy) ** 0.5


def build_context(doc_content, page_no: int, bbox: list,
                  caption_search_pt: float = 90.0,
                  sibling_bboxes: Optional[list] = None,
                  label_pad: float = 45.0) -> FigureContext:
    """Collect caption, source note and in-figure data labels for one figure.

    Captions in this corpus sit ABOVE the figure ("Exhibit 4: …", "Figure 5: …")
    and the source note below it, so the search is a vertical band either side
    of the figure's bbox rather than a radius.

    Data labels need a *generous* radius, because a pie chart's slice labels sit
    OUTSIDE the circle on leader lines — Nissan p8's "Maruti 800 5%" is 16pt
    clear of the pie's bbox.  But two pies share that page, so a label is only
    attributed to this figure when this figure is the nearest one: otherwise
    each pie would absorb the other's slices and the sum-to-100 check would
    pass on a scrambled series.
    """
    ctx = FigureContext(page=page_no, bbox=list(bbox))
    ctx.width_pt = bbox[2] - bbox[0]
    ctx.height_pt = bbox[3] - bbox[1]
    try:
        page = doc_content.pages[page_no - 1]
    except Exception:
        return ctx

    for tb in page.text_blocks:
        b = tb.bbox
        text = " ".join(tb.text.split())
        if not text:
            continue
        et = getattr(tb.element_type, "name", "")
        if et == "HEADER_FOOTER":
            continue

        # Caption: just above the figure, left-ish, matching the label pattern.
        if bbox[3] <= b.y0 <= bbox[3] + caption_search_pt:
            m = _CAPTION_RE.match(text)
            if m and ctx.caption is None:
                ctx.caption_label = f"{m.group(1).title()} {m.group(2)}"
                ctx.caption = m.group(3).strip()
                continue
        # Source note: just below.
        if bbox[1] - caption_search_pt <= b.y1 <= bbox[1]:
            if text.lower().startswith("source") and ctx.source_note is None:
                ctx.source_note = text
                continue
        # Data labels drawn inside or just outside the figure.
        if _overlaps([b.x0, b.y0, b.x1, b.y1], bbox, pad=label_pad):
            centre = ((b.x0 + b.x1) / 2.0, (b.y0 + b.y1) / 2.0)
            if sibling_bboxes:
                mine = _rect_distance(centre, bbox)
                if any(_rect_distance(centre, sib) < mine
                       for sib in sibling_bboxes if sib is not bbox):
                    continue  # belongs to a neighbouring figure
            ctx.nearby_text.append(text)
            m = _PERCENT_RE.search(text)
            if m:
                label = _PERCENT_RE.sub("", text).strip(" :–-")
                if label:
                    ctx.data_labels.append((label, float(m.group(1))))
    return ctx


# ---------------------------------------------------------------------------
# Path 1 — deterministic, from the text layer. No data leaves the machine.
# ---------------------------------------------------------------------------

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december",
           "q1", "q2", "q3", "q4", "fy")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _find_period(nearby_text: list) -> Optional[str]:
    """Pick out a period label ("June 2011", "2012") from in-figure text.

    Two pie charts under one caption are told apart only by their own title,
    so without this both panels of Nissan's "Market Share of A Hatchbacks"
    would get word-for-word identical alt text.
    """
    for text in nearby_text:
        t = text.strip()
        if len(t) > 40 or _PERCENT_RE.search(t):
            continue
        low = t.lower()
        if _YEAR_RE.search(t) and (any(m in low for m in _MONTHS)
                                   or len(t.split()) <= 3):
            return t
    return None


def draft_deterministic(ctx: FigureContext,
                        tolerance: float = 2.0) -> Optional[str]:
    """Compose a description from text-layer data labels, or None.

    Only fires when the labels look like a complete part-to-whole series —
    at least three slices summing to ~100%. That check is what makes the
    result trustworthy: a partial scrape would describe the chart wrongly,
    and silently wrong alt text is worse than none.
    """
    labels = ctx.data_labels
    if len(labels) < 3:
        return None
    total = sum(v for _l, v in labels)
    if abs(total - 100.0) > tolerance:
        return None

    ordered = sorted(labels, key=lambda t: -t[1])
    parts = ", ".join(f"{name} {value:g}%" for name, value in ordered)

    # Caption text is title-cased by the author — reproduce it verbatim rather
    # than case-folding it ("market Share of A Hatchbacks" reads as a typo).
    subject = ctx.caption.rstrip(".") if ctx.caption else None
    period = _find_period(ctx.nearby_text)
    if subject and period:
        lead = f"Pie chart showing {subject}, {period}"
    elif subject:
        lead = f"Pie chart showing {subject}"
    elif period:
        lead = f"Pie chart, {period}"
    else:
        lead = "Pie chart"

    biggest, biggest_v = ordered[0]
    return (
        f"{lead}. Shares, largest to smallest: {parts}. "
        f"{biggest} holds the largest share at {biggest_v:g}%."
    )


# ---------------------------------------------------------------------------
# Rendering a figure to PNG for the vision path
# ---------------------------------------------------------------------------

def render_figure_png(src_pdf: str, page_no: int, bbox: list,
                      dpi: int = 150, pad_pt: float = 6.0) -> Optional[bytes]:
    """Render the page and crop to the figure's bbox.

    Rendering (rather than pulling the image XObject out) is deliberate: it
    captures the figure AS IT APPEARS, including any callouts, leader lines or
    labels drawn on top of it, and it works identically for vector figures
    that have no XObject at all.
    """
    if shutil.which("pdftoppm") is None:
        logger.warning("pdftoppm not found — cannot render figures for vision drafting")
        return None
    try:
        from PIL import Image
    except Exception:
        logger.warning("Pillow not available — cannot crop rendered figures")
        return None

    with tempfile.TemporaryDirectory() as td:
        stem = os.path.join(td, "pg")
        try:
            subprocess.run(
                ["pdftoppm", "-f", str(page_no), "-l", str(page_no),
                 "-r", str(dpi), "-png", src_pdf, stem],
                check=True, capture_output=True, timeout=120,
            )
        except Exception as e:
            logger.warning("pdftoppm failed on page %s: %s", page_no, e)
            return None
        rendered = [f for f in os.listdir(td) if f.endswith(".png")]
        if not rendered:
            return None
        img = Image.open(os.path.join(td, rendered[0])).convert("RGB")

        scale = dpi / 72.0
        page_h_px = img.height
        x0 = max(0, int((bbox[0] - pad_pt) * scale))
        x1 = min(img.width, int((bbox[2] + pad_pt) * scale))
        # PDF y grows upward, image y grows downward.
        y0 = max(0, int(page_h_px - (bbox[3] + pad_pt) * scale))
        y1 = min(page_h_px, int(page_h_px - (bbox[1] - pad_pt) * scale))
        # Anything this small is an icon or a stray rule, not an exhibit.
        # Rejecting it here saves a pointless API call on a crop no model
        # could describe.
        if x1 - x0 < _MIN_CROP_PX or y1 - y0 < _MIN_CROP_PX:
            return None
        crop = img.crop((x0, y0, x1, y1))

        # Keep well inside the API's per-image limits without losing detail.
        max_edge = 1568
        if max(crop.size) > max_edge:
            ratio = max_edge / max(crop.size)
            crop = crop.resize(
                (max(1, int(crop.width * ratio)), max(1, int(crop.height * ratio)))
            )
        buf = io.BytesIO()
        crop.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Path 2 — vision drafting via Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write alt text for figures in Kellogg School of Management business case \
studies, for students using screen readers.

Rules:
- Lead with the figure type ("Bar chart showing…", "Flowchart of…", \
"Photograph of…"), then the content.
- Report the actual data. Give real axis labels, categories, series names and \
values you can read. A reader who cannot see the figure should be able to \
discuss it in class.
- State the trend or comparison the figure exists to show.
- Do not begin with "Image of", "Graphic showing", or the figure's number — \
the caption already supplies the number.
- Do not repeat the caption verbatim, and do not restate the "Source:" note. \
Both are real text on the page and are already read aloud; repeating them \
makes the reader hear the same sentence twice.
- No interpretation beyond what is visibly supported. Never invent a value.
- Plain prose, no markdown, no bullet points.
- 1-3 sentences for simple figures. Up to 6 for dense exhibits (tables, \
multi-panel charts, org charts); include the key rows or nodes.
- If the figure is purely decorative (a logo, a border, a stock photograph \
with no informational content), reply with exactly: DECORATIVE
- If you cannot read it well enough to describe it accurately, reply with \
exactly: UNCLEAR
"""


def _context_prompt(ctx: FigureContext) -> str:
    bits = []
    if ctx.caption_label or ctx.caption:
        bits.append(f"Caption: {ctx.caption_label or ''} {ctx.caption or ''}".strip())
    if ctx.source_note:
        bits.append(f"Source note: {ctx.source_note}")
    if ctx.data_labels:
        labels = ", ".join(f"{n} {v:g}%" for n, v in ctx.data_labels)
        bits.append(
            "Values recovered from the PDF text layer (these are exact — "
            f"prefer them over reading the pixels): {labels}"
        )
    elif ctx.nearby_text:
        snippet = " | ".join(ctx.nearby_text[:25])[:1200]
        bits.append(f"Text found inside the figure region: {snippet}")
    bits.append(f"Figure is on page {ctx.page}.")
    return "\n".join(bits) + "\n\nWrite the alt text."


def _supports_effort(model: str) -> bool:
    """Haiku 4.5 and the other pre-4.6 models reject output_config.effort.

    Sending it anyway turns every figure into a 400, so the parameter is
    dropped rather than letting the whole run fail on an unrelated knob.
    """
    m = (model or "").lower()
    return not ("haiku" in m or "sonnet-4-5" in m or "sonnet-3" in m)


def draft_with_vision(png: bytes, ctx: FigureContext, client,
                      model: str, effort: str) -> tuple:
    """Return (alt_text_or_None, reason). Never raises."""
    try:
        kwargs = dict(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.standard_b64encode(png).decode("ascii"),
                    }},
                    {"type": "text", "text": _context_prompt(ctx)},
                ],
            }],
        )
        if effort and _supports_effort(model):
            kwargs["output_config"] = {"effort": effort}
        resp = client.messages.create(**kwargs)
    except Exception as e:
        return None, f"api_error: {type(e).__name__}: {str(e)[:160]}"

    if getattr(resp, "stop_reason", None) == "refusal":
        return None, "model_refusal"
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        return None, "empty_response"
    if text.upper().startswith("DECORATIVE"):
        return None, "model_says_decorative"
    if text.upper().startswith("UNCLEAR"):
        return None, "model_says_unclear"
    return " ".join(text.split()), None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def draft_alt_text(pdf_path: str, source_pdf: str, doc_content=None,
                   report_path: Optional[str] = None) -> dict:
    """Fill in missing /Alt on ``pdf_path`` in place. Returns a summary dict.

    ``source_pdf`` is rendered for the vision path — the tagged output renders
    identically, but using the untouched source keeps rendering independent of
    anything the pipeline did to the content streams.
    """
    pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    targets = find_undescribed_figures(pdf)
    if not targets:
        pdf.close()
        return {"pdf": os.path.basename(pdf_path), "figures_needing_alt": 0,
                "drafted": 0, "deterministic": 0, "vision": 0, "left_blank": 0,
                "model": None, "figures": []}

    results: list = []
    client = None
    client_error = None
    if config.ALT_TEXT_USE_VISION:
        try:
            import anthropic
            client = anthropic.Anthropic()
        except Exception as e:
            client_error = f"{type(e).__name__}: {str(e)[:160]}"
            logger.warning("Vision drafting unavailable: %s", client_error)

    budget = config.ALT_TEXT_MAX_FIGURES_PER_DOC
    vision_calls = 0
    # A bad key, a wrong model id or a dead network fails identically on every
    # figure.  Without this, a 40-figure document renders and calls 40 times to
    # collect 40 copies of the same error.
    api_failures = 0
    api_circuit_open = False

    for elem, page_no, bbox in targets:
        if page_no is None or not bbox:
            results.append(DraftResult(
                page=page_no or -1, bbox=bbox or [], caption=None,
                method="skipped", alt_text=None, reason="no_page_or_bbox"))
            continue

        siblings = [bb for el, pg, bb in targets
                    if pg == page_no and bb and bb is not bbox]
        ctx = (build_context(doc_content, page_no, bbox,
                             sibling_bboxes=siblings)
               if doc_content is not None
               else FigureContext(page=page_no, bbox=list(bbox)))

        # 1. Deterministic, exact, free.
        alt = draft_deterministic(ctx)
        if alt:
            elem[pikepdf.Name("/Alt")] = pikepdf.String(alt)
            results.append(DraftResult(
                page=page_no, bbox=bbox, caption=ctx.caption,
                method="deterministic", alt_text=alt))
            continue

        # 2. Vision.
        if client is None or api_circuit_open:
            results.append(DraftResult(
                page=page_no, bbox=bbox, caption=ctx.caption,
                method="skipped", alt_text=None,
                reason=(client_error or "vision_disabled") if client is None
                else "vision_unavailable (earlier calls failed)"))
            continue
        if vision_calls >= budget:
            results.append(DraftResult(
                page=page_no, bbox=bbox, caption=ctx.caption,
                method="skipped", alt_text=None, reason="per_document_cap"))
            continue

        png = render_figure_png(source_pdf, page_no, bbox,
                                dpi=config.ALT_TEXT_RENDER_DPI)
        if png is None:
            results.append(DraftResult(
                page=page_no, bbox=bbox, caption=ctx.caption,
                method="skipped", alt_text=None, reason="render_failed"))
            continue

        vision_calls += 1
        alt, reason = draft_with_vision(
            png, ctx, client, config.ALT_TEXT_MODEL, config.ALT_TEXT_EFFORT)
        if alt:
            elem[pikepdf.Name("/Alt")] = pikepdf.String(alt)
            results.append(DraftResult(
                page=page_no, bbox=bbox, caption=ctx.caption,
                method="vision", alt_text=alt, model=config.ALT_TEXT_MODEL))
        else:
            if reason and reason.startswith("api_error"):
                api_failures += 1
                if api_failures >= _API_FAILURE_LIMIT:
                    api_circuit_open = True
                    logger.warning(
                        "Disabling vision drafting for this document after %d "
                        "consecutive API failures: %s", api_failures, reason)
            results.append(DraftResult(
                page=page_no, bbox=bbox, caption=ctx.caption,
                method="skipped", alt_text=None, reason=reason))

    drafted = [r for r in results if r.alt_text]
    # Re-saving a multi-megabyte PDF to write nothing is pure cost, and this
    # stage runs on every document — only save when a description was added.
    if drafted:
        try:
            with pdf.open_metadata() as meta:
                meta["dc:description"] = (
                    f"{len(drafted)} figure description(s) drafted automatically "
                    f"and pending reviewer approval."
                )
        except Exception:
            pass
        pdf.save(pdf_path)
    pdf.close()

    summary = {
        "pdf": os.path.basename(pdf_path),
        "figures_needing_alt": len(targets),
        "drafted": len(drafted),
        "deterministic": sum(1 for r in results if r.method == "deterministic"),
        "vision": sum(1 for r in results if r.method == "vision"),
        "left_blank": sum(1 for r in results if r.method == "skipped"),
        "model": config.ALT_TEXT_MODEL if config.ALT_TEXT_USE_VISION else None,
        "figures": [asdict(r) for r in results],
    }
    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Could not write alt-text report: %s", e)
    return summary


def format_report(summary: dict) -> str:
    """Human-readable review report — this is how draft QUALITY gets judged."""
    lines = [
        f"Alt-text drafting — {summary['pdf']}",
        f"  figures needing a description : {summary['figures_needing_alt']}",
        f"  drafted                       : {summary['drafted']} "
        f"({summary['deterministic']} from text layer, {summary['vision']} from image)",
        f"  left blank for manual input   : {summary['left_blank']}",
    ]
    if summary.get("model"):
        lines.append(f"  model                         : {summary['model']}")
    lines.append("")
    for f in summary["figures"]:
        head = f"  p{f['page']}"
        if f.get("caption"):
            head += f"  [{f['caption'][:60]}]"
        lines.append(head)
        if f["alt_text"]:
            lines.append(f"      ({f['method']}) {f['alt_text']}")
        else:
            lines.append(f"      LEFT BLANK — {f.get('reason')}")
    return "\n".join(lines)
