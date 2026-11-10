"""
fixture_paths.py — locate the reviewer's batch folders wherever they now live.

Three test modules loaded their inputs from hardcoded absolute paths:

    /Users/mohakgarg/Desktop/DRRC Documents/reversionfixes (1)
    /Users/mohakgarg/Desktop/reversionfixes (2)
    /Users/mohakgarg/Desktop/reversionfixes (3)

The October 2026 desktop reorganisation moved all three under
"DRRC Documents/04 Kellogg DRRC Work/Accessibility Fixes (PAC-WCAG)/".  The
tests did not fail — they SKIPPED, silently, and kept reporting OK.  Eleven
tests guarding figure grouping and the /S=/Artifact healer stopped running for
roughly three weeks without anyone noticing.

Searching a list of candidate roots means the next move costs one edit here
instead of three silent regressions.  ``DRRC_BATCH_ROOT`` overrides everything
for anyone whose copy lives elsewhere.
"""
import os

_HOME = os.path.expanduser("~")

# Newest layout first.
_CANDIDATE_ROOTS = [
    os.path.join(_HOME, "Desktop", "DRRC Documents", "04 Kellogg DRRC Work",
                 "Accessibility Fixes (PAC-WCAG)"),
    os.path.join(_HOME, "Desktop", "DRRC Documents"),
    os.path.join(_HOME, "Desktop"),
]


def batch_dir(name: str) -> str:
    """Absolute path to a reviewer batch folder, e.g. "reversionfixes (2)".

    Returns the first candidate that exists.  When none do, returns the
    preferred path so the caller's own skip message names somewhere sensible.
    """
    env = os.environ.get("DRRC_BATCH_ROOT")
    roots = ([env] if env else []) + _CANDIDATE_ROOTS
    for root in roots:
        path = os.path.join(root, name)
        if os.path.isdir(path):
            return path
    return os.path.join(roots[0], name)


def batch_file(name: str, filename: str) -> str:
    """Absolute path to one file inside a reviewer batch folder."""
    return os.path.join(batch_dir(name), filename)
