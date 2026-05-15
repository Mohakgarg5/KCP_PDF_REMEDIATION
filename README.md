# PDF Accessibility Remediation Pipeline

> Automatically transform Kellogg case PDFs into **PDF/UA-1 compliant**, screen-reader-ready documents — validated with veraPDF, zero failures.

---

## Why This Exists

Kellogg School of Management publishes case study PDFs that students need to read with screen readers and assistive technology. These PDFs, typically exported from Adobe InDesign, lack the structure tags, metadata, and accessibility markup required by the **PDF/UA-1 (ISO 14289-1)** standard. This pipeline fixes that automatically.

---

## What It Does

Drop a PDF in, get a fully accessible PDF out. The pipeline:

| Problem | Solution |
|---|---|
| No structure tags | Injects `/Document`, `/Art`, `/P`, `/H1`-`/H6`, `/Figure`, `/Table`, `/TR`, `/TD`, `/L`, `/LI`, `/Link` |
| Untagged images | Adds `/Figure` with `/Alt` text, `/BBox` layout attributes, preserves existing alt text |
| Vector diagrams broken into paths | Wraps Form XObjects as single `/Figure` elements |
| Missing XMP metadata | Writes `dc:title`, `dc:language`, `pdfuaid:part`, `pdf:Producer` |
| Missing MarkInfo | Sets `/Marked true`, `/Suspects false` |
| No tab order / viewer prefs | Sets `/Tabs /S` on all pages, `DisplayDocTitle true` |
| Broken link annotations | Wires `/Link` structure elements with MCR (text) + OBJR (annotation ref) |
| Watermarks / running headers | Tagged as `/Artifact` so screen readers skip them |
| Nested images in containers | Recursively discovers images inside Form XObject hierarchies |
| InDesign hierarchy lost | Preserves `Document -> Art -> content` structure tree |

---

## Quick Start

### Prerequisites

- **Python 3.10+** (developed on 3.12)
- **Java** (for veraPDF validation)
- **veraPDF** ([download](https://verapdf.org/software/))

### Setup

```bash
# Clone the repository
git clone https://github.com/Mohakgarg5/KCP_PDF_REMEDIATION.git
cd KCP_PDF_REMEDIATION

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set JAVA_HOME (macOS Homebrew example)
export JAVA_HOME=/opt/homebrew/Cellar/openjdk/25.0.2/libexec/openjdk.jdk/Contents/Home
```

### Run

```bash
# Process all PDFs in input/ directory
python main.py

# Process a single file
python main.py --input "input/my_case.pdf"

# Custom directories
python main.py --input-dir docs/ --output-dir accessible_docs/

# Skip veraPDF validation (if Java not available)
python main.py --skip-validation

# Verbose logging
python main.py --verbose
```

Output files are saved as `<original_name>_accessible.pdf` in the output directory.

### Web UI

```bash
streamlit run app.py
# Open http://localhost:8501, drag and drop PDFs
```

### Convenience Script

```bash
./run.sh                           # Process all PDFs in input/
./run.sh --input file.pdf          # Single file
./run.sh --skip-validation         # Skip veraPDF
```

---

## Architecture

```
input PDF
    |
    v
+--------------------+
|  pdf_extractor.py  |  Stage 1 - Content extraction & classification
|                    |  - Parses text blocks, fonts, bounding boxes (pdfminer.six)
|                    |  - Classifies headings, lists, tables, artifacts
|                    |  - Extracts images (including nested Form XObjects)
|                    |  - Reads existing alt text from structure tree
+--------+-----------+
         | DocumentContent (dataclass)
         v
+--------------------+
|  pdf_tagger.py     |  Stage 2 - Structure tag injection
|                    |  - Writes BDC/EMC markers into content streams
|                    |  - Builds structure tree (Document > Art > content)
|                    |  - Assigns MCIDs, builds ParentTree
|                    |  - Wraps Form XObjects as single Figure elements
+--------+-----------+
         | tagged PDF
         v
+--------------------+
|  pdf_postprocess.py|  Stage 3 - Metadata & compliance fixes
|                    |  - XMP metadata (pdfuaid, dc, pdf namespaces)
|                    |  - MarkInfo, ViewerPreferences, TabOrder
|                    |  - Font embedding, CIDToGIDMap, CIDSet fixes
|                    |  - Annotation accessibility, RoleMap
+--------+-----------+
         | remediated PDF
         v
+--------------------+
|  validator.py      |  Stage 4 - veraPDF validation
|                    |  - Runs veraPDF CLI with PDF/UA-1 profile
|                    |  - Parses JSON report, surfaces failed rules
+--------------------+
         |
         v
    output PDF  (PDF/UA-1 compliant)
```

---

## Project Structure

```
KTR_REMIDIATION/
|-- main.py              # CLI entry point & pipeline orchestrator
|-- app.py               # Streamlit web UI
|-- pdf_extractor.py     # Stage 1: content extraction & classification
|-- pdf_tagger.py        # Stage 2: structure tag injection
|-- pdf_postprocess.py   # Stage 3: metadata, fonts, annotations
|-- validator.py         # Stage 4: veraPDF validation
|-- models.py            # Shared dataclasses (DocumentContent, TextBlock, etc.)
|-- config.py            # Tunable thresholds and constants
|-- test_fixes.py        # Unit tests for critical accessibility fixes
|-- requirements.txt     # Python dependencies
|-- packages.txt         # System packages (for Streamlit Cloud deployment)
|-- run.sh               # Convenience runner script
|-- input/               # Place source PDFs here
|-- output/              # Remediated PDFs written here
`-- venv/                # Python virtual environment (not committed)
```

---

## Configuration

Edit `config.py` to tune detection thresholds:

```python
# Heading detection (ratio of font size to body text)
HEADING_SIZE_RATIO_H1 = 1.8    # >= 1.8x body = H1
HEADING_SIZE_RATIO_H2 = 1.5    # >= 1.5x body = H2
HEADING_SIZE_RATIO_H3 = 1.25   # >= 1.25x body = H3
HEADING_SIZE_RATIO_H4 = 1.1    # >= 1.1x body = H4

# Header/footer zone (fraction of page height)
HEADER_ZONE_FRACTION = 0.08
FOOTER_ZONE_FRACTION = 0.08

# Watermark detection
WATERMARK_MIN_ROTATION   = 15.0
WATERMARK_MAX_ROTATION   = 75.0
WATERMARK_MIN_FONT_SIZE  = 36.0

# veraPDF profile
VERAPDF_PROFILE = "ua1"
```

---

## Testing

```bash
source venv/bin/activate

# Unit tests (8 tests covering critical reviewer fixes)
python test_fixes.py

# Full end-to-end pipeline with validation
python main.py --input-dir input/ --output-dir output/
```

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| pikepdf | 10.3.0 | Low-level PDF structure manipulation |
| pdfminer.six | 20260107 | Text extraction with font/position metadata |
| Pillow | >= 10.0.0 | Image handling |
| streamlit | 1.54.0 | Web UI |
| langdetect | >= 1.0.9 | Document language detection |
| fonttools | >= 4.0.0 | Font analysis and embedding |
| veraPDF | >= 1.24 | PDF/UA-1 validation (external, Java) |

---

## Compliance Standards

Targets **PDF/UA-1 (ISO 14289-1)** as validated by [veraPDF](https://verapdf.org/).

Key Matterhorn Protocol checkpoints addressed:

| Checkpoint | Requirement |
|---|---|
| 01-004 | Tagged PDF flag (`MarkInfo /Marked true`) |
| 01-006 | Link elements contain both MCR and OBJR |
| 06-001 | Document language specified (`/Lang`) |
| 07-001 | No nested tagged/artifact content |
| 09-004 | Figure elements have `/Alt` text |
| 14-002 | Artifacts correctly marked |
| 28-002 | XMP metadata includes `pdfuaid:part = 1` |

---

## Limitations

- **Scanned PDFs** (image-only) are not supported -- the pipeline requires selectable text
- **Complex multi-column layouts** may produce suboptimal reading order; manual review recommended
- **Right-to-left scripts** are language-tagged but reading-order reversal is not applied
- veraPDF must be installed separately (requires Java runtime)

---

## License

MIT
