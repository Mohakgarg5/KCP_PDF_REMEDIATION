# Katharine Follow-up — Phase 0 Diagnosis

**Date:** 2026-05-18
**Author:** Mohak Garg (with Claude)
**Branch:** `fix/charlotte-followup`
**Status:** Diagnosis complete — awaiting approval for Phase 1 fix design

---

## Why this document

Katharine forwarded a folder (`~/Desktop/reversionfixesnew`) of 5 case PDFs with PAC/UA reports showing failures persist after our latest update. This document captures the Phase 0 diagnosis: what failures remain, the exact root cause of each, and which code paths produce them. **No code changes have been made.** Phase 1 (the generalized fix design) follows after Mohak signs off on this diagnosis.

## Phase 0 method

For each PDF in the folder I:

1. Read the PAC report cover page to get the failure counts per checkpoint category.
2. Opened the PDF with pikepdf and walked the structure tree to enumerate every struct-type used and the contents of `StructTreeRoot/RoleMap`.
3. For Figures specifically: dumped each `/Figure` StructElem's attributes (`/Alt`, `/A`, `/BBox`, `/K`).
4. For untagged-content suspicion: built the set of MCIDs reachable from the structure tree and diffed against MCIDs present in each page's content stream.

## PAC failure counts (cover-page summary)

| File | Result | Failures reported by PAC |
|---|---|---|
| AutoRetailing(A) | ❌ | 1 structure-element failure, 1 structure-tree warning |
| KEL002 Steel Wars — original ("NO CHANGES") | ❌ | 3 structure-element failures, 3 structure-tree warnings |
| KEL002 Steel Wars — our retag ("RETAGGED") | ❌ | **8 role-mapping failures** (new failure type) |
| KEL201 AutoRetailing(B) — original ("NO TAGGING") | ❌ | 8 structure-element failures, 8 structure-tree warnings |
| KEL201 AutoRetailing(B) — our retag ("WITH TAGGING") | ❌ | **51 role-mapping failures** (new failure type) |
| KEL203 SatelliteRadio — original ("NO CHANGES") | ✅ **passed UA-1** | none |
| KEL203 SatelliteRadio — our retag ("RETAGGED") | ❌ | **2 role-mapping failures** (new failure type — regression on a passing file) |
| KEL712 Micawber Capital | ❌ | 3 Figure bounding-box errors, 3 "possibly inappropriate use of Figure" warnings |

## Findings

### Finding 1 — Struct sanitizer is writing `/S = /Artifact` on StructElems

**Affects:** Steel Wars retag, KEL201 WITH TAGGING, SatRadio retag.
**Likely source:** the struct-type sanitizer added in commit `25f6534` ("feat(postprocess): struct-type sanitizer + pipeline invariants").
**Evidence:** struct-tree inventory across the three retagged PDFs found exactly one type that is neither standard nor RoleMap-registered:

| File | `/Artifact` struct-elements | PAC role-mapping failures |
|---|---:|---:|
| Steel Wars retag | 8 | 8 |
| KEL201 WITH TAGGING | 51 | 51 |
| SatRadio retag | 2 | 2 |

The match is one-to-one — every `/Artifact` StructElem produces exactly one PAC role-mapping failure.

Comparing originals to retags by Figure count makes the mechanism unambiguous:

| File | Figures in original | Figures in retag | Artifacts in retag |
|---|---:|---:|---:|
| Steel Wars | 12 | 4 | 8 |
| KEL201 | 54 | 3 | 51 |
| SatRadio | 5 | 3 | 2 |

The pipeline is taking existing `/Figure` StructElems from the source and reclassifying some of them as decorative, but the reclassification writes `/S = /Artifact` directly into the StructElem. That is malformed PDF: `/Artifact` is a *marked-content* role used inside content streams (in the `BDC` operator), not a valid `/S` value for a structure element. PDF/UA-1 either requires every non-standard `/S` to be mapped to a standard role via `StructTreeRoot/RoleMap` (and `/Artifact` is not a structure role at all), or that decorative content be excluded from the structure tree entirely with its content stream marked `/Artifact BDC … EMC`.

`StructTreeRoot/RoleMap` is empty in every retagged file — so there is no role mapping to rescue this even if `/Artifact` were a legitimate mapping target.

**Impact:** Three of the five retagged PDFs in this folder fail UA-1 for this single bug. One of them (SatRadio) was UA-1 compliant before our pipeline ran on it — we strictly regressed it.

### Finding 2 — Figure `/BBox` is emitted inconsistently

**Affects:** Micawber Capital, AutoRetailing(A). Almost certainly affects other PDFs not in this folder.
**Likely source:** divergence between the source-Figure preservation path (commit `38932f0`) and other code paths that emit Figures.
**Evidence:** Micawber has 5 `/Figure` StructElems. Two carry a complete `/A` dict with `/BBox`, `/O = /Layout`, `/Placement = /Block`. Three carry only `/S /Alt /K /Type` — no `/A`, no `/BBox`.

| Figure | Subject | Has `/A` /BBox? |
|---|---|---|
| 1 (obj 245) | Map of India | ✓ |
| 2 (obj 249) | Exhibit 3 — NGO Funding Structure flowchart (Katharine's screenshot) | ✗ |
| 3 (obj 252) | Exhibit 4 — Funding flow diagram | ✓ |
| 4 (obj 255) | Pie graph before IPO | ✗ |
| 5 (obj 257) | Pie graph after IPO | ✗ |

Three missing BBoxes match PAC's three Figure-BBox errors exactly. Same arithmetic for AutoRetailing(A): 1 of 6 Figures missing BBox, PAC reports 1 structure-element failure.

PDF/UA-1 requires a `/BBox` (placed in `/A`, with `/O = /Layout`) on every `/Figure` StructElem that lives on a single page, so that assistive technology can crop/locate the figure. Pages that have Figures without BBox also produce PAC's "possibly inappropriate use of Figure" warning — that's the same defect, classified separately.

### Finding 3 — Pre-existing source defects (originals)

The original files Katharine sent us (`NO CHANGES` / `NO TAGGING`) have their own failures from the source publisher's tagger. These are not regressions; they're the inputs we have not yet remediated. Steel Wars original has 3 structure-element failures; KEL201 NO TAGGING has 8. These are likely missing Alt on existing source Figures or untagged decorative content. KEL203 SatelliteRadio was already clean.

### Non-findings worth recording

- **Orphan MCIDs.** I built the reachable-MCID set and diffed against page-stream MCIDs for AutoRetailing(A) — there are zero orphans. So AutoRetA(A)'s 1 failure is not an untagged-content issue; it's the missing-BBox issue from Finding 2.
- **Heading-level skips.** No heading sequence in the inspected files skips a level (e.g., H1 → H3).
- **Empty `/TD` cells.** AutoRet(A) has 14 empty `/TD` and 11 `/Link` elements without `/Alt`. PAC does not flag these as hard failures in any of the reports we received, so they are not part of this remediation pass.

## Mapping of remaining defects to fix paths

| Defect class | PDFs affected (in this folder) | Failure count | Code area |
|---|---|---:|---|
| `/S = /Artifact` on StructElems | Steel Wars retag, KEL201, SatRadio | 61 | `pdf_postprocess.py` — struct-type sanitizer |
| Missing `/Figure` /BBox | Micawber, AutoRetA(A) | 4 | `pdf_tagger.py` and/or `pdf_postprocess.py` — Figure emission |
| Pre-existing original defects | Steel Wars orig, KEL201 NO TAGGING | 11 | Inputs not yet run through our pipeline |

The total remaining hard failures attributable to our pipeline output (after Stage 3 post-process) across these 8 files: **65**. All 65 fall into the two defect classes above. Both classes are general-case code bugs, not per-document content issues.

## Proposed Phase 1 scope

1. **Fix the sanitizer's decorative-Figure handling.** Stop writing `/S = /Artifact`. Replace with one of: keep `/Figure` with empty `/Alt = ""`, or drop the StructElem and mark the content as artifact in the content stream. Generalized — applies to any input PDF whose Figures the sanitizer classifies as decorative.
2. **Unify Figure emission to always write `/BBox`.** Every code path that produces a `/Figure` StructElem must compute the page-space bounding box from the underlying marked-content operators and write `/A << /O /Layout /BBox [...] /Placement /Block >>`. Generalized — applies to all Figures, regardless of how they entered the structure tree.
3. **Add a UA-1 output gate to the pipeline.** A validator pass that fails the run if any StructElem has a non-standard `/S` missing from RoleMap, or any `/Figure` lacks `/BBox`, or any MCID in a page stream is unreachable from the structure tree. This is the durable protection: a build cannot ship to Katharine with these classes of defect.
4. **Regression set.** Run the fixed pipeline against every PDF in this folder plus the existing test fixtures (`sugar_daddy`, the May-11 set, the May-12 image-reconciliation corpus). Acceptance: no file's failure count regresses; every retag in this folder reaches zero hard failures.

Detailed design for the fixes lives in the follow-up document `2026-05-18-katharine-followup-design.md` (Phase 1, to be written after this diagnosis is approved).
