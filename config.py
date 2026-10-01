"""
config.py - Configuration constants for the PDF accessibility pipeline.
"""
import os
from pathlib import Path

# Directories
DEFAULT_INPUT_DIR = Path("input")
DEFAULT_OUTPUT_DIR = Path("output")

# Heading detection thresholds (ratio of font size to body text size)
HEADING_SIZE_RATIO_H1 = 1.8
HEADING_SIZE_RATIO_H2 = 1.5
HEADING_SIZE_RATIO_H3 = 1.25
HEADING_SIZE_RATIO_H4 = 1.1

# Watermark detection
WATERMARK_MIN_ROTATION = 15.0
WATERMARK_MAX_ROTATION = 75.0
WATERMARK_MIN_FONT_SIZE = 36.0
WATERMARK_LIGHT_COLOR_THRESHOLD = 0.7

# Invisible/white text detection
INVISIBLE_TEXT_COLOR_THRESHOLD = 0.95  # all RGB channels above this → near-white text
DARK_BACKGROUND_LUMINANCE = 0.5        # BT.601 luminance below this → background is dark

# Header/footer detection (fraction of page height)
HEADER_ZONE_FRACTION = 0.08
FOOTER_ZONE_FRACTION = 0.08

# veraPDF
VERAPDF_PROFILE = "ua1"

# Image alt text placeholder
DEFAULT_IMAGE_ALT = "Figure"

# ---------------------------------------------------------------------------
# Alt-text drafting (backend only — nothing in the Streamlit UI depends on it)
# ---------------------------------------------------------------------------
# Master switch. When off, the pipeline behaves exactly as it did before.
ALT_TEXT_DRAFTING = os.getenv("ALT_TEXT_DRAFTING", "1") not in ("0", "false", "False")

# Vision drafting sends a rendered image of each undescribed figure to the
# Claude API.  Figures from unpublished cases therefore leave the machine.
# Approved 2026-10-01.  Set ALT_TEXT_USE_VISION=0 to fall back to the
# text-layer-only path, which sends nothing anywhere.
ALT_TEXT_USE_VISION = os.getenv("ALT_TEXT_USE_VISION", "1") not in ("0", "false", "False")

ALT_TEXT_MODEL = os.getenv("ALT_TEXT_MODEL", "claude-sonnet-5")
# low | medium | high | xhigh | max.  Ignored on models that do not accept an
# effort setting (Haiku 4.5 rejects it with a 400) — see _supports_effort.
ALT_TEXT_EFFORT = os.getenv("ALT_TEXT_EFFORT", "medium")
ALT_TEXT_RENDER_DPI = int(os.getenv("ALT_TEXT_RENDER_DPI", "150"))
# Spend guard: a runaway document (KEL189 had 46 image tiles before grouping)
# should not fan out into hundreds of API calls.
ALT_TEXT_MAX_FIGURES_PER_DOC = int(os.getenv("ALT_TEXT_MAX_FIGURES_PER_DOC", "60"))
