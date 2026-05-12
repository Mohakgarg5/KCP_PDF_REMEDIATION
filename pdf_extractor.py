"""
pdf_extractor.py - Extract structured content from a PDF.

Uses pdfminer.six for text extraction with full font/position/color metadata,
and pikepdf for reliable image extraction.
"""
import logging
import math
from pathlib import Path
from collections import Counter
from io import BytesIO

from pdfminer.high_level import extract_pages
from pdfminer.layout import (
    LAParams, LTTextBox, LTTextLine, LTChar, LTAnno,
    LTFigure, LTImage, LTPage,
)
import pikepdf
from langdetect import detect as detect_language

from models import (
    DocumentContent, PageContent, TextBlock, ImageBlock,
    TableBlock, FontInfo, BBox, ElementType,
)
import config

logger = logging.getLogger(__name__)


def extract_document(pdf_path: str) -> DocumentContent:
    """Main entry point: extract all content from a PDF file.

    Raises pikepdf.PasswordError for encrypted PDFs.
    """
    # Pre-check: verify PDF is readable and not encrypted
    try:
        _pdf = pikepdf.Pdf.open(pdf_path)
        _pdf.close()
    except pikepdf.PasswordError:
        logger.error("PDF is encrypted/password-protected: %s", pdf_path)
        raise
    except Exception as e:
        logger.warning("PDF pre-check warning: %s", e)

    # Phase 1: Extract text blocks with full metadata via pdfminer
    raw_pages = _extract_text_blocks(pdf_path)

    # Phase 2: Merge split text blocks (drop-cap / first-char splits)
    for page in raw_pages:
        page.text_blocks = _merge_split_blocks(page.text_blocks)

    # Phase 3: Extract images via pikepdf
    existing_alt_texts = _read_existing_alt_texts(pdf_path)
    _extract_images(pdf_path, raw_pages, existing_alt_texts)

    # Phase 4: Detect the body font size (most common size)
    body_font_size = _detect_body_font_size(raw_pages)

    # Phase 5: Detect repeated header/footer text across pages
    hf_signatures = _detect_header_footer_signatures(raw_pages)

    # Phase 5b: Extract filled rectangles per page for white-text background detection
    bg_rects_per_page = _extract_filled_rects(pdf_path, len(raw_pages))

    # Phase 6: Classify elements
    for page in raw_pages:
        page_bg = (bg_rects_per_page[page.page_number]
                   if page.page_number < len(bg_rects_per_page) else [])
        _classify_elements(page, body_font_size, hf_signatures, page_bg)

    # Phase 7: Normalize heading hierarchy (start at H1, no skipped levels)
    _normalize_heading_hierarchy(raw_pages)

    # Phase 8: Detect tables
    for page in raw_pages:
        _detect_tables(page, body_font_size)

    # Phase 9: Detect document language (use all text, not just paragraphs)
    all_text = " ".join(
        tb.text for p in raw_pages for tb in p.text_blocks
        if tb.element_type not in (ElementType.WATERMARK, ElementType.HEADER_FOOTER)
        and tb.text.strip()
    )
    try:
        language = detect_language(all_text[:5000]) if all_text.strip() else "en"
    except Exception as e:
        logger.warning("Language detection failed, defaulting to 'en': %s", e)
        language = "en"

    # Phase 10: Extract title (try PDF metadata first, then largest font)
    title = _detect_title(raw_pages, pdf_path)

    return DocumentContent(
        title=title,
        language=language,
        pages=raw_pages,
        source_path=pdf_path,
    )


def _extract_text_blocks(pdf_path: str) -> list:
    """Use pdfminer.six extract_pages to get text with full metadata."""
    laparams = LAParams(
        line_margin=0.5,
        word_margin=0.1,
        char_margin=2.0,
        boxes_flow=0.5,
        detect_vertical=True,
    )

    pages = []
    for page_num, page_layout in enumerate(extract_pages(pdf_path, laparams=laparams)):
        page_content = PageContent(
            page_number=page_num,
            width=page_layout.width,
            height=page_layout.height,
        )
        try:
            for element in page_layout:
                _process_layout_element(element, page_content, page_num)
        except Exception as e:
            logger.warning("Partial extraction on page %d: %s", page_num, e)
        pages.append(page_content)

    return pages


def _process_layout_element(element, page_content: PageContent, page_num: int):
    """Recursively process layout elements.

    When a text box contains lines with very different font sizes,
    splits it into separate blocks per line group so headings aren't
    averaged with body text.
    """
    if isinstance(element, LTTextBox):
        # Collect per-line info to detect mixed-size boxes
        line_infos = []  # (text, chars, line_obj)
        for line in element:
            if isinstance(line, LTTextLine):
                line_chars = [item for item in line if isinstance(item, LTChar)]
                line_text = line.get_text().strip()
                if line_chars and line_text:
                    line_infos.append((line_text, line_chars, line))

        if not line_infos:
            return

        # Check if lines have significantly different font sizes
        line_sizes = []
        for text, chars, line_obj in line_infos:
            avg_size = sum(c.size for c in chars) / len(chars)
            line_sizes.append(avg_size)

        # If max/min ratio > 1.3, split by line groups of similar size
        min_size = min(line_sizes) if line_sizes else 1.0
        if min_size < 1.0:
            min_size = 1.0
        if line_sizes and max(line_sizes) / min_size > 1.3:
            _split_text_box_by_lines(line_infos, line_sizes, page_content, page_num)
        else:
            # All lines similar size — create one block
            all_chars = [c for _, chars, _ in line_infos for c in chars]
            full_text = "\n".join(t for t, _, _ in line_infos)
            font_info = _dominant_font(all_chars)
            rotation = _calc_rotation(all_chars[0].matrix) if all_chars else 0.0

            text_block = TextBlock(
                text=full_text,
                bbox=BBox(element.x0, element.y0, element.x1, element.y1),
                font=font_info,
                rotation_degrees=rotation,
                page_number=page_num,
            )
            page_content.text_blocks.append(text_block)

    elif isinstance(element, LTFigure):
        for child in element:
            _process_layout_element(child, page_content, page_num)

    elif isinstance(element, LTImage):
        page_content.images.append(ImageBlock(
            image_bytes=b"",
            format="unknown",
            bbox=BBox(element.x0, element.y0, element.x1, element.y1),
            page_number=page_num,
        ))


def _split_text_box_by_lines(line_infos, line_sizes, page_content, page_num):
    """Split a text box into separate blocks when lines have different font sizes.

    Groups consecutive lines with similar sizes together.
    """
    SIZE_TOLERANCE = 1.5  # lines within this pt difference are grouped

    groups = []  # list of (texts, chars, line_objs)
    current_group_texts = []
    current_group_chars = []
    current_group_lines = []
    current_group_size = line_sizes[0]

    for i, (text, chars, line_obj) in enumerate(line_infos):
        size = line_sizes[i]
        if abs(size - current_group_size) <= SIZE_TOLERANCE:
            current_group_texts.append(text)
            current_group_chars.extend(chars)
            current_group_lines.append(line_obj)
        else:
            if current_group_texts:
                groups.append((current_group_texts, current_group_chars, current_group_lines))
            current_group_texts = [text]
            current_group_chars = list(chars)
            current_group_lines = [line_obj]
            current_group_size = size

    if current_group_texts:
        groups.append((current_group_texts, current_group_chars, current_group_lines))

    for texts, chars, lines in groups:
        full_text = "\n".join(texts)
        font_info = _dominant_font(chars)
        rotation = _calc_rotation(chars[0].matrix) if chars else 0.0

        # Calculate bbox from the lines
        x0 = min(l.x0 for l in lines)
        y0 = min(l.y0 for l in lines)
        x1 = max(l.x1 for l in lines)
        y1 = max(l.y1 for l in lines)

        text_block = TextBlock(
            text=full_text,
            bbox=BBox(x0, y0, x1, y1),
            font=font_info,
            rotation_degrees=rotation,
            page_number=page_num,
        )
        page_content.text_blocks.append(text_block)


def _merge_split_blocks(text_blocks: list) -> list:
    """Merge text blocks split by pdfminer (e.g. 'P' + 'ublicly Available').

    pdfminer sometimes splits the first character into a separate text box
    when it has a slightly different position or formatting (drop caps, etc.).
    This merges them back together.
    """
    if len(text_blocks) < 2:
        return text_blocks

    X_GAP_MAX = 30.0   # max gap between blocks to consider them adjacent
    SHORT_THRESHOLD = 3  # blocks with <= this many chars are merge candidates

    merged = []
    used = set()

    indexed = list(enumerate(text_blocks))

    for i, block_a in indexed:
        if i in used:
            continue

        # Look for a short fragment to merge with this block, or vice versa
        best_merge = None
        for j, block_b in indexed:
            if j in used or j == i:
                continue

            # Check if blocks are on the same line using Y overlap or proximity
            y_overlap = (min(block_a.bbox.y1, block_b.bbox.y1) -
                         max(block_a.bbox.y0, block_b.bbox.y0))
            y_close = abs(block_a.bbox.y0 - block_b.bbox.y0) < 5.0
            if y_overlap <= 0 and not y_close:
                continue

            # One must be short (1-3 chars)
            a_short = len(block_a.text.strip()) <= SHORT_THRESHOLD
            b_short = len(block_b.text.strip()) <= SHORT_THRESHOLD
            if not a_short and not b_short:
                continue

            # Determine which is the short (fragment) and long block
            if a_short:
                short_b, long_b = block_a, block_b
                short_idx, long_idx = i, j
            else:
                short_b, long_b = block_b, block_a
                short_idx, long_idx = j, i

            # Check X adjacency using multiple strategies:
            # 1. Short block is left-adjacent to long block (normal split)
            x_gap = long_b.bbox.x0 - short_b.bbox.x1
            left_adjacent = -10.0 <= x_gap <= X_GAP_MAX

            # 2. Blocks share same x0 (drop-cap style: tall first letter)
            same_column = abs(short_b.bbox.x0 - long_b.bbox.x0) < 5.0

            # 3. Short block's x range is within long block's x range
            contained = (short_b.bbox.x0 >= long_b.bbox.x0 - 5.0 and
                         short_b.bbox.x1 <= long_b.bbox.x1 + 5.0)

            if left_adjacent or same_column or contained:
                best_merge = (short_b, long_b, short_idx, long_idx)
                break

        if best_merge:
            short_b, long_b, short_idx, long_idx = best_merge
            # Prepend short fragment to long block text
            merged_text = short_b.text.strip() + long_b.text.strip()
            merged_bbox = BBox(
                x0=min(short_b.bbox.x0, long_b.bbox.x0),
                y0=min(short_b.bbox.y0, long_b.bbox.y0),
                x1=max(short_b.bbox.x1, long_b.bbox.x1),
                y1=max(short_b.bbox.y1, long_b.bbox.y1),
            )
            # Use the longer block's font info (more representative)
            merged_block = TextBlock(
                text=merged_text,
                bbox=merged_bbox,
                font=long_b.font,
                rotation_degrees=long_b.rotation_degrees,
                page_number=long_b.page_number,
            )
            merged.append(merged_block)
            used.add(short_idx)
            used.add(long_idx)
        else:
            merged.append(block_a)
            used.add(i)

    return merged


def _dominant_font(chars: list) -> FontInfo:
    """Determine the dominant font in a list of LTChar objects."""
    if not chars:
        return FontInfo(name="unknown", size=12.0, is_bold=False,
                        is_italic=False, color=(0.0, 0.0, 0.0))

    font_counter = Counter()
    sizes = []

    for ch in chars:
        try:
            font_counter[ch.fontname] += 1
            sizes.append(ch.size)
        except (AttributeError, TypeError):
            continue

    if not font_counter:
        return FontInfo(name="unknown", size=12.0, is_bold=False,
                        is_italic=False, color=(0.0, 0.0, 0.0))

    dominant_name = font_counter.most_common(1)[0][0]
    avg_size = sum(sizes) / len(sizes) if sizes else 12.0

    name_lower = dominant_name.lower() if dominant_name else ""
    is_bold = any(kw in name_lower for kw in ["bold", "black", "heavy", "demi"])
    is_italic = any(kw in name_lower for kw in ["italic", "oblique", "slant"])

    # Extract color safely from graphicstate
    color = (0.0, 0.0, 0.0)
    try:
        gs = getattr(chars[0], 'graphicstate', None)
        ncolor = getattr(gs, 'ncolor', None) if gs else None
        if ncolor is not None:
            if isinstance(ncolor, (list, tuple)):
                if len(ncolor) == 3:
                    color = tuple(float(c) for c in ncolor)
                elif len(ncolor) == 4:
                    c, m, y, k = [float(v) for v in ncolor]
                    color = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
                elif len(ncolor) >= 1:
                    g = float(ncolor[0])
                    color = (g, g, g)
            elif isinstance(ncolor, (int, float)):
                g = float(ncolor)
                color = (g, g, g)
    except (ValueError, TypeError, IndexError, AttributeError):
        color = (0.0, 0.0, 0.0)

    return FontInfo(
        name=dominant_name or "unknown",
        size=avg_size,
        is_bold=is_bold,
        is_italic=is_italic,
        color=color,
    )


def _calc_rotation(matrix: tuple) -> float:
    """Extract rotation angle in degrees from a PDF transformation matrix."""
    if not matrix or len(matrix) < 2:
        return 0.0
    a, b = matrix[0], matrix[1]
    angle_rad = math.atan2(b, a)
    return math.degrees(angle_rad)


_LIST_ITEM_RE = None


def _is_list_item(text: str) -> bool:
    """Check if text starts with a bullet or numbered list marker.

    Handles: bullets (-, *, bullet chars), numbers (1., 2), letters (a., b)),
    and roman numerals (i., ii.) as list prefixes.
    """
    global _LIST_ITEM_RE
    import re
    if _LIST_ITEM_RE is None:
        _LIST_ITEM_RE = re.compile(
            r'^\s*(?:'
            r'[\u2022\u2023\u2043\u25aa\u25ab\u25b8\u25b9\u25cb\u25cf\u25e6\u2013\u2014\u2219\u00b7]'  # bullet chars
            r'|[-*\u2010\u2011]'       # dash/asterisk bullets
            r'|\d{1,3}[.)]\s'          # numbered: 1. 2) 10.
            r'|[a-zA-Z][.)]\s'         # lettered: a. b) A.
            r'|[ivxIVX]{1,4}[.)]\s'    # roman: i. ii) IV.
            r'|\(\d{1,3}\)\s'          # parenthesized: (1) (2)
            r'|\([a-zA-Z]\)\s'         # parenthesized: (a) (b)
            r')',
            re.UNICODE,
        )
    text = text.strip()
    if not text or len(text) < 2:
        return False
    return bool(_LIST_ITEM_RE.match(text))


_ROMAN_NUMERAL_RE = None


def _is_page_number(text: str) -> bool:
    """Check if text looks like a page number in any common format.

    Handles: "1", "- 1 -", "Page 1", "page 1 of 10", "i", "ii", "iv", etc.
    """
    global _ROMAN_NUMERAL_RE
    import re
    if _ROMAN_NUMERAL_RE is None:
        _ROMAN_NUMERAL_RE = re.compile(
            r'^[- ]*(?:page\s+)?\d+(?:\s+of\s+\d+)?[- ]*$|'
            r'^[- ]*(?:page\s+)?[ivxlcdm]+[- ]*$',
            re.IGNORECASE,
        )
    text = text.strip()
    if not text:
        return False
    return bool(_ROMAN_NUMERAL_RE.match(text))


def _detect_body_font_size(pages: list) -> float:
    """Find the most common font size across all text blocks."""
    size_counter = Counter()
    for page in pages:
        for tb in page.text_blocks:
            rounded = round(tb.font.size * 2) / 2
            size_counter[rounded] += len(tb.text)

    if not size_counter:
        return 12.0
    return size_counter.most_common(1)[0][0]


def _detect_single_page_hf(pages: list) -> set:
    """Detect header/footer elements on single-page documents.

    For single-page docs, we cannot use cross-page repetition. Instead,
    detect page numbers and very small footer-zone text as header/footer.
    """
    if not pages:
        return set()

    page = pages[0]
    page_height = page.height
    header_threshold = page_height * (1 - config.HEADER_ZONE_FRACTION)
    footer_threshold = page_height * config.FOOTER_ZONE_FRACTION

    signatures = set()
    for tb in page.text_blocks:
        in_header = tb.bbox.y1 > header_threshold
        in_footer = tb.bbox.y0 < footer_threshold
        if not (in_header or in_footer):
            continue

        zone = "header" if in_header else "footer"
        normalized = tb.text.strip().lower().replace("\n", " ").strip()

        # Page numbers in header/footer zones
        if _is_page_number(normalized):
            signatures.add(("__page_number__", zone))
            continue

        # Very small text in footer zone (copyright lines, disclaimers)
        if in_footer and len(normalized) < 120 and tb.font.size < 9.0:
            signatures.add((normalized, zone))

    return signatures


def _detect_header_footer_signatures(pages: list) -> set:
    """Detect text that repeats across pages in header/footer zones.

    Only text that appears at similar positions on 2+ pages is considered
    a running header/footer. This prevents falsely excluding title text
    or body content that happens to be near the top/bottom of a page.

    Returns a set of (normalized_text, zone) signatures.
    """
    if len(pages) < 2:
        return _detect_single_page_hf(pages)

    # Collect candidate texts from header/footer zones on each page
    zone_texts = {}  # (normalized_text, zone) -> set of page numbers

    for page in pages:
        page_height = page.height
        header_threshold = page_height * (1 - config.HEADER_ZONE_FRACTION)
        footer_threshold = page_height * config.FOOTER_ZONE_FRACTION

        for tb in page.text_blocks:
            in_header = tb.bbox.y1 > header_threshold
            in_footer = tb.bbox.y0 < footer_threshold

            if not (in_header or in_footer):
                continue

            zone = "header" if in_header else "footer"
            # Normalize: strip, lowercase, remove page numbers
            normalized = tb.text.strip().lower()
            # Remove standalone page numbers in various formats:
            # "1", "- 1 -", "Page 1", "page 1 of 10", "i", "ii", "iii", etc.
            if _is_page_number(normalized):
                normalized = "__page_number__"
            # Remove common variations
            normalized = normalized.replace("\n", " ").strip()

            key = (normalized, zone)
            zone_texts.setdefault(key, set()).add(page.page_number)

    # Only keep signatures that appear on 2+ pages
    return {key for key, page_nums in zone_texts.items() if len(page_nums) >= 2}


def _classify_elements(page: PageContent, body_font_size: float, hf_signatures: set,
                       bg_rects: list = None):
    """Classify text blocks as headings, watermarks, headers/footers, etc."""
    page_height = page.height
    header_threshold = page_height * (1 - config.HEADER_ZONE_FRACTION)
    footer_threshold = page_height * config.FOOTER_ZONE_FRACTION

    for tb in page.text_blocks:
        # Invisible (near-white) text detection — runs before watermark check.
        # Text whose colour is essentially white and has no dark background rectangle
        # is invisible to sighted users; mark it as an artifact so WCAG 1.4.3
        # contrast checks are not applied to it.
        if bg_rects is not None:
            is_near_white = all(
                c >= config.INVISIBLE_TEXT_COLOR_THRESHOLD
                for c in tb.font.color
            )
            if is_near_white and not _has_dark_background(tb.bbox, bg_rects):
                tb.element_type = ElementType.WATERMARK
                continue

        # Watermark detection
        abs_rotation = abs(tb.rotation_degrees)
        is_rotated = (config.WATERMARK_MIN_ROTATION <= abs_rotation
                      <= config.WATERMARK_MAX_ROTATION)
        is_large = tb.font.size >= config.WATERMARK_MIN_FONT_SIZE
        is_light = all(
            c > config.WATERMARK_LIGHT_COLOR_THRESHOLD
            for c in tb.font.color
        )
        watermark_signals = sum([is_rotated, is_large, is_light])
        if watermark_signals >= 2:
            tb.element_type = ElementType.WATERMARK
            continue

        # Header/footer detection — only if text repeats across pages
        in_header = tb.bbox.y1 > header_threshold
        in_footer = tb.bbox.y0 < footer_threshold
        if in_header or in_footer:
            zone = "header" if in_header else "footer"
            normalized = tb.text.strip().lower()
            if _is_page_number(normalized):
                normalized = "__page_number__"
            normalized = normalized.replace("\n", " ").strip()

            # Only artifact repeating headers/footers from page 2 onwards.
            # On the first page they are genuine document metadata (case #,
            # byline, date) and must remain readable by screen readers.
            if page.page_number > 0 and (normalized, zone) in hf_signatures:
                tb.element_type = ElementType.HEADER_FOOTER
                continue
            # Also catch small-font text in footer zone (copyright notices etc.)
            if in_footer and tb.font.size < body_font_size * 0.85:
                tb.element_type = ElementType.HEADER_FOOTER
                continue

        # List item detection (before heading detection)
        if _is_list_item(tb.text):
            tb.element_type = ElementType.LIST_ITEM
            continue

        # Heading detection by font size ratio
        ratio = tb.font.size / body_font_size if body_font_size > 0 else 1.0

        if ratio >= config.HEADING_SIZE_RATIO_H1:
            tb.element_type = ElementType.HEADING
            tb.heading_level = 1
        elif ratio >= config.HEADING_SIZE_RATIO_H2:
            tb.element_type = ElementType.HEADING
            tb.heading_level = 2
        elif ratio >= config.HEADING_SIZE_RATIO_H3:
            tb.element_type = ElementType.HEADING
            tb.heading_level = 3
        elif ratio >= config.HEADING_SIZE_RATIO_H4 and tb.font.is_bold:
            tb.element_type = ElementType.HEADING
            tb.heading_level = 4
        elif tb.font.is_bold and ratio >= 1.0 and len(tb.text) < 200:
            tb.element_type = ElementType.HEADING
            tb.heading_level = 5
        elif (ratio >= 0.95 and len(tb.text) < 200
              and tb.text.strip() == tb.text.strip().upper()
              and any(c.isalpha() for c in tb.text)):
            tb.element_type = ElementType.HEADING
            tb.heading_level = 6
        else:
            tb.element_type = ElementType.PARAGRAPH


def _normalize_heading_hierarchy(pages: list):
    """Normalize heading levels so they start at H1 and don't skip levels.

    E.g. if all headings are H3-H5, remap to H1-H3.
    If headings jump from H1 to H4, compress to H1-H2.
    """
    # Collect all heading levels used across all pages
    used_levels = sorted(set(
        tb.heading_level
        for page in pages for tb in page.text_blocks
        if tb.element_type == ElementType.HEADING and tb.heading_level
    ))

    if not used_levels:
        return

    # Create mapping: old_level -> new_level (sequential starting at 1)
    level_map = {}
    for new_level, old_level in enumerate(used_levels, start=1):
        level_map[old_level] = min(new_level, 6)

    # Apply mapping
    for page in pages:
        for tb in page.text_blocks:
            if tb.element_type == ElementType.HEADING and tb.heading_level:
                tb.heading_level = level_map.get(tb.heading_level, tb.heading_level)


def _detect_tables(page: PageContent, body_font_size: float = 12.0):
    """Detect table structures by finding grid-aligned text blocks."""
    if len(page.text_blocks) < 4:
        return

    ROW_TOLERANCE = max(3.0, min(8.0, body_font_size * 0.4))
    rows_by_y = {}
    for tb in page.text_blocks:
        if tb.element_type != ElementType.PARAGRAPH:
            continue
        y_key = round(tb.bbox.y0 / ROW_TOLERANCE) * ROW_TOLERANCE
        rows_by_y.setdefault(y_key, []).append(tb)

    multi_col_rows = {y: blocks for y, blocks in rows_by_y.items()
                      if len(blocks) >= 2}

    if len(multi_col_rows) < 2:
        return

    sorted_ys = sorted(multi_col_rows.keys(), reverse=True)
    first_row = sorted(multi_col_rows[sorted_ys[0]], key=lambda b: b.bbox.x0)
    col_count = len(first_row)

    table_rows = []
    table_blocks = []  # store actual block references for bbox calculation

    for y in sorted_ys:
        row_blocks = sorted(multi_col_rows[y], key=lambda b: b.bbox.x0)
        if len(row_blocks) >= col_count - 1:
            table_rows.append(list(row_blocks))  # keep TextBlock objects (bbox needed for tagging)
            table_blocks.extend(row_blocks)

    if len(table_rows) >= 2:
        # Use direct bbox comparison instead of id()
        table_block_bboxes = [
            (b.bbox.x0, b.bbox.y0, b.bbox.x1, b.bbox.y1) for b in table_blocks
        ]
        if table_blocks:
            table_bbox = BBox(
                x0=min(b.bbox.x0 for b in table_blocks),
                y0=min(b.bbox.y0 for b in table_blocks),
                x1=max(b.bbox.x1 for b in table_blocks),
                y1=max(b.bbox.y1 for b in table_blocks),
            )
        else:
            table_bbox = None

        table = TableBlock(
            rows=table_rows,
            header_rows=1,
            bbox=table_bbox,
            page_number=page.page_number,
        )
        page.tables.append(table)
        # Remove table blocks using bbox equality instead of id()
        page.text_blocks = [
            tb for tb in page.text_blocks
            if (tb.bbox.x0, tb.bbox.y0, tb.bbox.x1, tb.bbox.y1) not in table_block_bboxes
        ]


def _read_existing_alt_texts(pdf_path: str) -> dict:
    """Read existing /Alt text from /Figure elements in the PDF structure tree.

    Returns a dict mapping page_index (int) -> list of alt-text strings in
    document order.  Only non-empty /Alt values are stored.  If the PDF has
    no structure tree (or it cannot be read), returns an empty dict.
    """
    result: dict = {}
    try:
        pdf = pikepdf.Pdf.open(pdf_path)
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            pdf.close()
            return result

        # Build a page-index lookup: (objgen) -> page index
        page_id_to_idx = {page.objgen: idx for idx, page in enumerate(pdf.pages)}
        visited: set = set()

        def _walk(node):
            """Recursively walk the structure tree and collect Figure alt texts."""
            try:
                # Use objgen for cycle detection (stable across references)
                try:
                    oid = node.objgen
                except Exception:
                    oid = id(node)
                if oid in visited:
                    return
                visited.add(oid)

                struct_type = node.get("/S")
                if struct_type == pikepdf.Name("/Figure"):
                    # Always record every Figure element — including those with
                    # no /Alt — so positional indices stay aligned with XObject
                    # iteration order.  Empty string means "no existing alt text;
                    # use the generated fallback instead."
                    raw_alt = node.get("/Alt")
                    alt_str = str(raw_alt).strip() if raw_alt else ""

                    # Determine which page this element belongs to by
                    # following /Pg on the element or its first /K child.
                    pg = node.get("/Pg")
                    if pg is None:
                        kids = node.get("/K")
                        if kids is not None:
                            if isinstance(kids, pikepdf.Array) and len(kids):
                                first = kids[0]
                            elif hasattr(kids, "get"):
                                first = kids
                            else:
                                first = None
                            try:
                                pg = first.get("/Pg") if (first is not None and hasattr(first, "get")) else None
                            except Exception:
                                pg = None
                    if pg is not None:
                        try:
                            page_idx = page_id_to_idx.get(pg.objgen)
                            if page_idx is not None:
                                result.setdefault(page_idx, []).append(alt_str)
                        except Exception:
                            pass

                # Recurse into children
                kids = node.get("/K")
                if kids is None:
                    return
                if isinstance(kids, pikepdf.Array):
                    for child in kids:
                        try:
                            if hasattr(child, "get"):
                                _walk(child)
                        except Exception:
                            continue
                elif isinstance(kids, pikepdf.Dictionary):
                    _walk(kids)
                # Integer kids are MCID leaf references — nothing to recurse into
            except Exception:
                pass

        _walk(struct_root)
        pdf.close()
    except Exception as e:
        logger.debug("Could not read existing alt texts: %s", e)

    return result


def _collect_images_recursive(resources, visited=None):
    """Recursively yield (name, obj) for every Image XObject reachable from resources.

    Descends into Form XObjects so images nested inside InDesign containers
    (e.g. grouped figures, chart forms) are found regardless of nesting depth.
    Cycle detection via visited set prevents infinite recursion on circular refs.
    """
    if visited is None:
        visited = set()
    if resources is None:
        return
    try:
        xobjects = resources.get("/XObject")
    except Exception:
        return
    if xobjects is None:
        return

    for name, obj in xobjects.items():
        try:
            if not hasattr(obj, "keys"):
                continue
            try:
                obj_id = obj.objgen
                if obj_id in visited:
                    continue
                visited.add(obj_id)
            except Exception:
                pass

            subtype = obj.get("/Subtype")
            if subtype == pikepdf.Name.Image:
                yield (name, obj)
            elif subtype == pikepdf.Name.Form:
                form_resources = obj.get("/Resources")
                if form_resources:
                    yield from _collect_images_recursive(form_resources, visited)
        except Exception:
            continue


def _extract_images(pdf_path: str, pages: list, existing_alt_texts: dict = None):
    """Extract images and reconcile their alt text + decorative status.

    Delegates classification (alt text, decorative, watermark) to
    image_reconciliation.reconcile_page_images, which uses source-tree intent
    + position-based matching.  Image bytes are then rendered per Resolved
    occurrence so each ImageBlock carries: image_bytes, page bbox, alt_text,
    is_decorative.

    The `existing_alt_texts` parameter is retained for backward compatibility
    but ignored — reconciliation reads the struct tree directly.
    """
    from image_reconciliation import reconcile_page_images

    try:
        pdf = pikepdf.Pdf.open(pdf_path)
    except Exception as e:
        logger.warning("Could not open PDF for image extraction: %s", e)
        return

    resolved_by_page = reconcile_page_images(pdf_path)

    def _collect_image_xobjects(resources, lookup, seen):
        """Recursive: gather every Image XObject reachable from `resources`."""
        if resources is None:
            return
        try:
            xobjects = resources.get("/XObject")
        except Exception:
            return
        if xobjects is None:
            return
        for n, o in xobjects.items():
            try:
                ogen = getattr(o, "objgen", None)
                if ogen and ogen in seen:
                    continue
                if ogen:
                    seen.add(ogen)
                sub = o.get("/Subtype")
                if sub == pikepdf.Name("/Image"):
                    lookup.setdefault(str(n), o)
                elif sub == pikepdf.Name("/Form"):
                    inner = o.get("/Resources")
                    if inner is not None:
                        _collect_image_xobjects(inner, lookup, seen)
            except Exception:
                continue

    try:
        for page_idx, pdf_page in enumerate(pdf.pages):
            if page_idx >= len(pages):
                break
            page_content = pages[page_idx]

            page_resolved = resolved_by_page.get(page_idx)
            if page_resolved is None:
                # Reconciliation skipped this page (failure or empty);
                # leave page_content.images as pdfminer populated it.
                continue

            # Look up rendered image bytes by xobject name on this page,
            # descending into Form XObjects to reach nested Image data.
            xobj_lookup: dict = {}
            try:
                resources = pdf_page.get("/Resources")
                if resources is None:
                    parent = pdf_page.get("/Parent")
                    while parent is not None and resources is None:
                        resources = parent.get("/Resources")
                        parent = parent.get("/Parent")
                _collect_image_xobjects(resources, xobj_lookup, set())
            except Exception:
                pass

            new_blocks: list = []
            for resolved in page_resolved:
                img_obj = xobj_lookup.get(resolved.xobject_name)
                image_bytes = b""
                fmt = ""
                if img_obj is not None:
                    try:
                        pdfimage = pikepdf.PdfImage(img_obj)
                        pil_image = pdfimage.as_pil_image()
                        buf = BytesIO()
                        pil_image.save(buf, format="PNG")
                        image_bytes = buf.getvalue()
                        fmt = "png"
                    except Exception as e:
                        logger.debug(
                            "Could not render image '%s' on page %d: %s",
                            resolved.xobject_name, page_idx, e,
                        )
                new_blocks.append(ImageBlock(
                    image_bytes=image_bytes,
                    format=fmt,
                    bbox=resolved.bbox,
                    page_number=page_idx,
                    alt_text=resolved.alt_text,
                    is_decorative=resolved.is_decorative,
                ))

            page_content.images = new_blocks
    finally:
        pdf.close()


def _extract_filled_rects(pdf_path: str, page_count: int) -> list:
    """Parse each page's content stream to collect filled rectangles and their RGB fill colors.

    Returns a list of length page_count. Each entry is a list of tuples:
        (x0, y0, x1, y1, r, g, b)  — all floats, r/g/b in [0, 1].

    Only simple rectangles defined by the 're' operator are tracked.
    Multiple consecutive 're' operators before a single fill are all recorded.
    Complex paths (m/l/c) are ignored.
    Fill colour state is tracked through q/Q graphics-state save/restore pairs.
    Handles DeviceRGB ('rg'), DeviceGray ('g'), and DeviceCMYK ('k') colour operators.
    'sc'/'scn' (ICC/pattern colour spaces) are not handled; if encountered, the
    previously-known fill colour is kept, which may over-preserve white text (safe).
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
        pending_rects = []               # accumulated 're' rectangles before fill
        complex_path = False             # True if non-rect path ops seen after last 're'

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
            # Multiple 're' ops can precede a single fill ('re re re f' is valid PDF).
            if op == "re" and len(operands) >= 4:
                try:
                    x = float(operands[0])
                    y = float(operands[1])
                    w = float(operands[2])
                    h = float(operands[3])
                    pending_rects.append((
                        min(x, x + w), min(y, y + h),
                        max(x, x + w), max(y, y + h),
                    ))
                    complex_path = False
                except (ValueError, TypeError):
                    pass
                continue

            # --- Complex path construction (not a simple rect) ---
            if op in ("m", "l", "c", "v", "y", "h"):
                complex_path = True
                continue

            # --- W / W* are clipping modifiers, NOT path-ending operators ---
            # They must precede the next path op; do not disturb pending_rects.
            if op in ("W", "W*"):
                continue

            # --- Fill operations: record all pending simple rects ---
            if op in ("f", "F", "f*", "B", "B*", "b", "b*"):
                if not complex_path:
                    for rect in pending_rects:
                        result[page_idx].append((*rect, *fill_color))
                pending_rects = []
                complex_path = False
                continue

            # --- Path end without fill: discard pending rects ---
            if op in ("n", "S", "s"):
                pending_rects = []
                complex_path = False
                continue

    try:
        pdf.close()
    except Exception:
        pass

    return result


def _has_dark_background(bbox: BBox, bg_rects: list) -> bool:
    """Return True if the text block's centre point falls inside a dark filled rectangle.

    Uses BT.601 luminance (0.299R + 0.587G + 0.114B) to classify background brightness.
    Note: this is used for background classification only, not for computing WCAG
    contrast ratios (which use BT.709 coefficients).
    A luminance below config.DARK_BACKGROUND_LUMINANCE is considered dark.
    """
    cx = (bbox.x0 + bbox.x1) / 2
    cy = (bbox.y0 + bbox.y1) / 2

    for rx0, ry0, rx1, ry1, r, g, b in bg_rects:
        if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            if luminance < config.DARK_BACKGROUND_LUMINANCE:
                return True
    return False


def _detect_title(pages: list, pdf_path: str) -> str:
    """Detect document title using multiple strategies.

    1. Try original PDF metadata (skip generic titles like 'Title', 'Untitled')
    2. Find the largest-font text on page 1
    3. Find the first heading on page 1
    4. Fallback to filename
    """
    # Strategy 1: PDF metadata
    try:
        pdf = pikepdf.Pdf.open(pdf_path)
        # Try XMP metadata first
        with pdf.open_metadata() as meta:
            xmp_title = meta.get("dc:title", "")
        # Try DocInfo
        doc_title = str(pdf.docinfo.get("/Title", ""))
        pdf.close()

        for candidate in [xmp_title, doc_title]:
            if candidate and _is_meaningful_title(candidate):
                return candidate[:200]
    except Exception as e:
        logger.debug("Could not read PDF metadata for title: %s", e)

    # Strategy 2: Largest font text on page 1
    if pages:
        largest_size = 0
        largest_text = ""
        for tb in pages[0].text_blocks:
            if tb.font.size > largest_size and len(tb.text.strip()) > 3:
                largest_size = tb.font.size
                largest_text = tb.text.strip()

        # Only use if it's notably larger than body text
        body_size = _detect_body_font_size(pages)
        if largest_text and largest_size > body_size * 1.2:
            # Take just the first line if multi-line
            first_line = largest_text.split("\n")[0].strip()
            if first_line:
                return first_line[:200]

    # Strategy 3: First heading on page 1
    if pages:
        for tb in pages[0].text_blocks:
            if tb.element_type == ElementType.HEADING:
                return tb.text.split("\n")[0].strip()[:200]

    # Strategy 4: First paragraph on page 1
    if pages:
        for tb in pages[0].text_blocks:
            if tb.element_type == ElementType.PARAGRAPH:
                return tb.text[:100]

    # Strategy 5: Filename
    return Path(pdf_path).stem.replace("_", " ").replace("-", " ").title()


def _is_meaningful_title(title: str) -> bool:
    """Check if a title from PDF metadata is meaningful (not a generic placeholder)."""
    if not title or not title.strip():
        return False
    normalized = title.strip().lower()
    generic_titles = {
        "title", "untitled", "document", "doc", "pdf", "file",
        "microsoft word", "powerpoint presentation", "slide 1",
        "new document", "unnamed",
    }
    if normalized in generic_titles:
        return False
    if len(normalized) < 3:
        return False
    return True
