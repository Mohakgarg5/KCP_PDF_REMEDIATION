"""
validator.py - Validate PDF/UA-1 compliance using veraPDF CLI.

veraPDF must be installed separately (Java application) and available on PATH.
Download from: https://verapdf.org/software/
"""
import json
import logging
import os
import subprocess
import shutil
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Common install locations for veraPDF (checked if not on PATH)
_VERAPDF_KNOWN_PATHS = [
    os.path.expanduser("~/verapdf/verapdf"),
    "/usr/local/bin/verapdf",
    "/opt/verapdf/verapdf",
]


@dataclass
class ValidationRule:
    """A single validation rule result."""
    clause: str
    test_number: int
    description: str
    status: str
    context: str = ""


@dataclass
class ValidationResult:
    """Complete validation result for one PDF."""
    pdf_path: str
    is_compliant: bool
    total_passed: int = 0
    total_failed: int = 0
    failed_rules: list = field(default_factory=list)
    error_message: Optional[str] = None


def validate_pdf(pdf_path: str) -> ValidationResult:
    """Validate a PDF against PDF/UA-1 using veraPDF.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        ValidationResult with compliance status and details.
    """
    verapdf_cmd = shutil.which("verapdf")
    if not verapdf_cmd:
        # Check known install locations
        for path in _VERAPDF_KNOWN_PATHS:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                verapdf_cmd = path
                break
    if not verapdf_cmd:
        return ValidationResult(
            pdf_path=pdf_path,
            is_compliant=False,
            error_message=(
                "veraPDF not found on PATH or in ~/verapdf/. Install from "
                "https://verapdf.org/software/ and ensure 'verapdf' "
                "is available in your system PATH."
            ),
        )

    try:
        # Ensure JAVA_HOME is set for veraPDF (Java app)
        env = os.environ.copy()
        if "JAVA_HOME" not in env:
            # Try common Java locations
            for java_home in [
                "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home",
                "/usr/lib/jvm/java-11-openjdk-amd64",
                "/usr/lib/jvm/default-java",
            ]:
                if os.path.isdir(java_home):
                    env["JAVA_HOME"] = java_home
                    break

        result = subprocess.run(
            [verapdf_cmd, "-f", config.VERAPDF_PROFILE, "--format", "json", pdf_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ValidationResult(
            pdf_path=pdf_path,
            is_compliant=False,
            error_message="veraPDF validation timed out after 120 seconds.",
        )
    except FileNotFoundError:
        return ValidationResult(
            pdf_path=pdf_path,
            is_compliant=False,
            error_message="veraPDF executable not found.",
        )

    return _parse_verapdf_json(pdf_path, result.stdout, result.stderr)


def _parse_verapdf_json(
    pdf_path: str, stdout: str, stderr: str
) -> ValidationResult:
    """Parse veraPDF JSON output into a ValidationResult."""
    if not stdout.strip():
        return ValidationResult(
            pdf_path=pdf_path,
            is_compliant=False,
            error_message=f"veraPDF produced no output. stderr: {stderr[:500]}",
        )

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return ValidationResult(
            pdf_path=pdf_path,
            is_compliant=False,
            error_message=f"Failed to parse veraPDF JSON: {e}. Raw: {stdout[:500]}",
        )

    try:
        jobs = data.get("report", {}).get("jobs", [])
        if not jobs:
            return ValidationResult(
                pdf_path=pdf_path,
                is_compliant=False,
                error_message="No validation jobs in veraPDF output.",
            )

        job = jobs[0]
        val_result = job.get("validationResult", {})

        is_compliant = val_result.get("compliant", False)
        details = val_result.get("details", {})

        total_passed = details.get("passedRules", 0)
        total_failed = details.get("failedRules", 0)

        failed_rules = []
        for rule in details.get("rules", []):
            if rule.get("status") == "failed":
                checks = rule.get("checks", [])
                context = checks[0].get("context", "") if checks else ""
                failed_rules.append(ValidationRule(
                    clause=rule.get("clause", ""),
                    test_number=rule.get("testNumber", 0),
                    description=rule.get("description", ""),
                    status="failed",
                    context=context[:200],
                ))

        return ValidationResult(
            pdf_path=pdf_path,
            is_compliant=is_compliant,
            total_passed=total_passed,
            total_failed=total_failed,
            failed_rules=failed_rules,
        )
    except (KeyError, IndexError, TypeError) as e:
        logger.warning("Error parsing veraPDF results: %s", e)
        return ValidationResult(
            pdf_path=pdf_path,
            is_compliant=False,
            error_message=f"Error parsing veraPDF results: {e}",
        )


def format_validation_report(result: ValidationResult) -> str:
    """Format a ValidationResult as a human-readable report."""
    lines = [
        f"PDF: {result.pdf_path}",
        f"Compliant: {'YES' if result.is_compliant else 'NO'}",
        f"Rules passed: {result.total_passed}",
        f"Rules failed: {result.total_failed}",
    ]

    if result.error_message:
        lines.append(f"Error: {result.error_message}")

    if result.failed_rules:
        lines.append("\nFailed rules:")
        for rule in result.failed_rules:
            lines.append(f"  - Clause {rule.clause} (Test {rule.test_number})")
            lines.append(f"    {rule.description}")
            if rule.context:
                lines.append(f"    Context: {rule.context}")

    return "\n".join(lines)
