"""
main.py - PDF Accessibility Remediation Pipeline orchestrator.

Usage:
    python main.py                              # Process all PDFs in input/
    python main.py --input file.pdf             # Process a single file
    python main.py --input-dir my_pdfs/         # Process a directory
    python main.py --output-dir results/        # Specify output directory
    python main.py --skip-validation            # Skip veraPDF validation
"""
import argparse
import logging
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field

from pdf_extractor import extract_document
from models import ElementType
from pdf_tagger import tag_pdf
from pdf_postprocess import postprocess_pdf
from validator import validate_pdf, format_validation_report
from alt_text_drafter import draft_alt_text, format_report as format_alt_text_report
import config


@dataclass
class PipelineResult:
    input_path: str
    output_path: str = ""
    success: bool = False
    validation_compliant: bool = False
    validation_report: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    alt_text_report: str = ""
    alt_text_summary: dict = field(default_factory=dict)


def process_single_pdf(input_path: str, output_dir: str, skip_validation: bool = False) -> PipelineResult:
    """Process a single PDF through the full accessibility pipeline."""
    result = PipelineResult(input_path=input_path)
    start_time = time.time()

    input_file = Path(input_path)
    output_path = Path(output_dir) / f"{input_file.stem}_accessible.pdf"
    result.output_path = str(output_path)

    try:
        # Pre-check: ensure file is a valid PDF
        with open(input_path, 'rb') as f:
            header = f.read(8)
            if not header.startswith(b'%PDF'):
                raise ValueError(f"Not a valid PDF file (header: {header[:8]})")

        # Stage 1: Extract & classify content
        print(f"  [1/4] Extracting content from {input_file.name}...")
        doc_content = extract_document(str(input_path))
        text_count = sum(len(p.text_blocks) for p in doc_content.pages)
        img_count = sum(len(p.images) for p in doc_content.pages)
        print(f"        Found {text_count} text blocks, {img_count} images "
              f"across {len(doc_content.pages)} pages")
        print(f"        Language: {doc_content.language}, Title: {doc_content.title[:60]}")
        heading_count = sum(
            1 for p in doc_content.pages for tb in p.text_blocks
            if tb.element_type == ElementType.HEADING
        )
        table_count = sum(len(p.tables) for p in doc_content.pages)
        list_count = sum(
            1 for p in doc_content.pages for tb in p.text_blocks
            if tb.element_type == ElementType.LIST_ITEM
        )
        artifact_count = sum(
            1 for p in doc_content.pages for tb in p.text_blocks
            if tb.element_type in (ElementType.WATERMARK, ElementType.HEADER_FOOTER)
        )
        print(f"        Structure: {heading_count} headings, {table_count} tables, "
              f"{list_count} list items, {artifact_count} artifacts")

        # Stage 2: Tag the original PDF with structure markers
        print(f"  [2/4] Adding structure tags to original PDF...")
        tag_pdf(str(input_path), str(output_path), doc_content)
        print(f"        Tagged: headings, paragraphs, images, artifacts")

        # Stage 3: Post-process metadata
        print(f"  [3/4] Post-processing metadata with pikepdf...")
        postprocess_pdf(str(output_path), doc_content.title, doc_content.language)
        print(f"        Metadata fixed: MarkInfo, Lang, ViewerPreferences, TabOrder, XMP")

        # Stage 3b: Draft alt text for figures that have none.  Runs after the
        # structure tree is final so it only reads /Figure elements and writes
        # /Alt — no tagging logic is involved.  Figures that already carry an
        # authored description are never touched.
        if config.ALT_TEXT_DRAFTING:
            print(f"  [3b ] Drafting alt text for undescribed figures...")
            try:
                report_path = str(output_path).rsplit(".pdf", 1)[0] + "_alt_text_report.json"
                summary = draft_alt_text(
                    str(output_path), input_path,
                    doc_content=doc_content, report_path=report_path,
                )
                result.alt_text_summary = summary
                result.alt_text_report = format_alt_text_report(summary)
                print(f"        {summary['drafted']}/{summary['figures_needing_alt']} "
                      f"drafted ({summary['deterministic']} from text layer, "
                      f"{summary['vision']} from image), "
                      f"{summary['left_blank']} left blank")
            except Exception as e:
                # Drafting must never break remediation — the PDF is already
                # structurally correct and compliant at this point.
                result.alt_text_report = f"Alt-text drafting failed: {type(e).__name__}: {e}"
                print(f"        WARNING: {result.alt_text_report}")

        # Stage 4: Validate
        if skip_validation:
            print(f"  [4/4] Validation skipped (--skip-validation)")
            result.validation_report = "Validation skipped by user."
        else:
            print(f"  [4/4] Validating with veraPDF...")
            val_result = validate_pdf(str(output_path))
            result.validation_compliant = val_result.is_compliant
            result.validation_report = format_validation_report(val_result)

            if val_result.error_message:
                print(f"        Validation warning: {val_result.error_message}")
            else:
                status = "PASS" if val_result.is_compliant else "WARN (see report)"
                print(f"        Validation: {status} "
                      f"({val_result.total_passed} passed, {val_result.total_failed} failed)")

        result.success = True

    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        print(f"        ERROR: {result.error}")

    result.duration_seconds = time.time() - start_time
    return result


def main():
    parser = argparse.ArgumentParser(
        description="PDF Accessibility Remediation Pipeline - Makes PDFs pass PAC/PDF/UA-1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                              # Process input/ -> output/
  python main.py --input report.pdf           # Single file
  python main.py --input-dir docs/ --output-dir accessible_docs/
  python main.py --skip-validation            # Skip veraPDF step
        """,
    )
    parser.add_argument("--input", "-i", help="Single PDF file to process")
    parser.add_argument("--input-dir", "-d", default="input",
                        help="Directory of PDFs to process (default: input/)")
    parser.add_argument("--output-dir", "-o", default="output",
                        help="Output directory (default: output/)")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Skip veraPDF validation step")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose/debug logging")
    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s [%(name)s] %(message)s",
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect input files
    pdf_files = []
    if args.input:
        p = Path(args.input)
        if not p.exists():
            print(f"Error: {p} does not exist")
            sys.exit(1)
        pdf_files.append(p)
    else:
        input_dir = Path(args.input_dir)
        if not input_dir.exists():
            print(f"Error: Input directory '{input_dir}' does not exist.")
            print(f"Create it and place your PDF files inside:")
            print(f"  mkdir {input_dir}")
            sys.exit(1)
        pdf_files = sorted(input_dir.glob("*.pdf"))
        if not pdf_files:
            print(f"No PDF files found in '{input_dir}/'")
            print(f"Place your PDF files in the '{input_dir}/' directory and run again.")
            sys.exit(0)

    print(f"PDF Accessibility Remediation Pipeline")
    print(f"=" * 50)
    print(f"Processing {len(pdf_files)} file(s)")
    print()

    results = []
    for idx, pdf_file in enumerate(pdf_files, 1):
        print(f"[{idx}/{len(pdf_files)}] Processing: {pdf_file.name}")
        result = process_single_pdf(str(pdf_file), str(out_dir), args.skip_validation)
        results.append(result)
        print()

    _print_summary(results)


def _print_summary(results: list):
    """Print a summary table of all results."""
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    success_count = sum(1 for r in results if r.success)
    compliant_count = sum(1 for r in results if r.validation_compliant)

    for r in results:
        if r.validation_compliant:
            status = "PASS"
        elif r.success:
            status = "WARN"
        else:
            status = "ERROR"
        name = Path(r.input_path).name
        print(f"  [{status:5s}] {name:40s} ({r.duration_seconds:.1f}s)")
        if r.error:
            print(f"          Error: {r.error}")

    print()
    print(f"Total: {len(results)} | "
          f"Processed: {success_count} | "
          f"Compliant: {compliant_count} | "
          f"Failed: {len(results) - success_count}")

    if results and results[0].output_path:
        print(f"\nAccessible PDFs saved to: {Path(results[0].output_path).parent}/")

    # Detailed reports for non-compliant files
    for r in results:
        if r.validation_report and not r.validation_compliant and r.success:
            print(f"\n--- Detailed report for {Path(r.input_path).name} ---")
            print(r.validation_report)


if __name__ == "__main__":
    main()
