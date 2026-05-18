# Katharine Follow-up — Phase 1 Fix Design

**Date:** 2026-05-18
**Author:** Mohak Garg (with Claude)
**Branch:** `fix/charlotte-followup` (continues current branch)
**Status:** Awaiting Mohak's approval before implementation
**Prereq:** Phase 0 diagnosis (`2026-05-18-katharine-followup-diagnosis.md`) must be read first.

---

## Goal

Resolve every hard PAC/UA-1 failure in our retag output for the `reversionfixesnew` folder by fixing the two root causes (not the symptoms), and install a gate that prevents the same defect classes from ever shipping again. Both fixes are general-case — they apply to any input PDF, not just these five.

**Acceptance bar:**
1. The 5 retags in `reversionfixesnew` go to zero PDF/UA-1 hard failures.
2. No existing fixture or test PDF regresses (sugar_daddy, May-11 set, May-12 image-reconciliation corpus, the existing test_*.py suites).
3. The pre-save invariant gate (`_check_pipeline_invariants`) refuses to save a PDF that violates the two new invariants.

## Non-goals

- Fixing pre-existing source defects in the originals (`NO CHANGES` / `NO TAGGING` variants). Those are publisher-side problems and require a separate remediation pass.
- The cosmetic "Possibly inappropriate use of Figure" PAC warning — that's a warning, not a hard failure, and it falls out as a side-effect of adding `/BBox` (Fix 2).
- Restructuring the tagger's classification logic. WATERMARK and HEADER_FOOTER detection stays as it is; only the *emission* of those elements changes.

---

## Fix 1 — Stop creating `/S = /Artifact` StructElems

### Root cause

`pdf_tagger.py:1779-1795` — `_element_to_struct_type()`:

```python
def _element_to_struct_type(tb: TextBlock) -> str:
    ...
    elif tb.element_type == ElementType.WATERMARK:
        return "/Artifact"           # <-- Bug
    elif tb.element_type == ElementType.HEADER_FOOTER:
        return "/Artifact"           # <-- Bug
    else:
        return "/P"
```

Returned string flows downstream to the StructElem builder at `pdf_tagger.py:1491-1494`, which writes it verbatim as `/S = /Artifact`. PDF/UA-1 forbids `/Artifact` as a structure type because `/Artifact` is a *marked-content* role used inside content streams (the `BDC` operator), not a valid structure tag.

### Design

WATERMARK and HEADER_FOOTER content must be wrapped as `/Artifact BDC ... EMC` in the **content stream**, with **no StructElem** in the structure tree at all. The infrastructure for this already exists in `pdf_tagger.py` — three call sites already emit content-stream artifact markers:

- `pdf_tagger.py:694` — `[pikepdf.Name("/Artifact")]` BDC
- `pdf_tagger.py:735` — `[pikepdf.Name("/Artifact"), ...]` BDC with properties
- `pdf_tagger.py:1070` — same pattern in the image-emission path

### Changes

| # | File | Site | Change |
|---|------|------|--------|
| 1.1 | `pdf_tagger.py` | `_element_to_struct_type` (1779) | Tighten contract: function returns `Optional[str]`. Return `None` for `WATERMARK` and `HEADER_FOOTER` (signals "skip StructElem, emit content-stream artifact"). |
| 1.2 | `pdf_tagger.py` | Wherever `_element_to_struct_type` is called and the result drives StructElem creation | If return value is `None`, route the underlying TextBlock through the existing content-stream artifact path instead of appending to `struct_elems`. No StructElem, no MCID, no struct-tree entry. |
| 1.3 | `pdf_tagger.py:1491-1494` | StructElem builder | Add a defensive assert: `assert struct_type != "/Artifact"`. If anyone ever re-introduces this, the assertion fires immediately at the emission site, not deep in PAC validation. |

Watermark/header-footer content stays visible to AT? **No** — that's the whole point. Marked-content artifacts are skipped by screen readers, which is correct behavior for repetitive page furniture.

---

## Fix 2 — Every `/Figure` StructElem must carry `/BBox`

### Root cause

`pdf_tagger.py:1517-1525`:

```python
if struct_type == "/Figure" and fig_bbox:
    elem_dict["/A"] = pikepdf.Dictionary({
        "/O": pikepdf.Name("/Layout"),
        "/BBox": pikepdf.Array([...]),
        "/Placement": pikepdf.Name("/Block"),
    })
```

`/A` is only emitted when `fig_bbox` is truthy. When upstream callers append a 4-tuple like `(mcid, "/Figure", alt, None)` — or a 3-tuple where index 3 isn't supplied at all — the resulting StructElem has no `/A` and no `/BBox`. PAC then flags the Figure as failing PDF/UA-1 (every page-local Figure must have a `/BBox` in user space).

Confirmed in Micawber: 2 of 5 Figures carry `/A /BBox` (the source-preservation path is fine), 3 of 5 do not.

### Design

Centralize Figure StructElem construction in a helper that **requires** a bbox. Every caller computes the bbox at append-time or the helper computes it from the marked-content's draw operators.

### Changes

| # | File | Site | Change |
|---|------|------|--------|
| 2.1 | `pdf_tagger.py` | New helper (near line 1485) | Add `_build_figure_struct_elem(pdf, page_ref, mcid, alt_text, bbox)` returning the StructElem dict. **Raises `ValueError` if `bbox` is None.** Single source of truth for Figure shape. |
| 2.2 | `pdf_tagger.py` | StructElem builder loop (1491-1525) | When `struct_type == "/Figure"`, call the helper instead of inlining. Remove the conditional `if fig_bbox:` — the helper enforces presence. |
| 2.3 | `pdf_tagger.py` | All `struct_elems.append((..., "/Figure", ...))` sites | Audit and ensure every site passes a non-None bbox. Sites known so far: line 1114 (source-figure preservation, already passes `fig_bbox`); line 1152 (Form XObject as Figure, already passes `form_bbox`); image-based Figures originating from the image extractor (need to check `image_reconciliation.py` and the image-to-StructElem path around `pdf_tagger.py:246`). |
| 2.4 | `pdf_tagger.py` | Image-Figure path | If the image extractor doesn't already produce a bbox, derive it from the image's CTM-transformed `/BBox` in the resource dict. The math already lives at `pdf_tagger.py:1330` (Form XObject bbox composition) and can be reused. |

The bbox is in default user-space coordinates: `[llx, lly, urx, ury]` — the visible page-space rectangle the figure occupies.

---

## Fix 3 — Extend the pre-save invariant gate

### Current state

`pdf_postprocess.py:_check_pipeline_invariants` (line 295) already enforces:
- **INV-1**: No StructElem has `/S = /Artifact`.
- **INV-2**: Every `/S` is standard PDF/UA *or* appears in `/RoleMap`.

Why these PDFs still shipped: they were produced *before* commit `25f6534` (May 17). With Fix 1 the producer itself is correct, and the gate ensures regressions can never sneak back.

### Changes

| # | File | Site | Change |
|---|------|------|--------|
| 3.1 | `pdf_postprocess.py:_check_pipeline_invariants` | Body (313-356) | Add **INV-3**: every StructElem with `/S = /Figure` has a `/BBox` reachable either at `/BBox` directly or inside `/A` (when `/A` is a dict or array of dicts). Count violations and append to `problems`. |
| 3.2 | `pdf_postprocess.py:_check_pipeline_invariants` | Same | Add **INV-4** (defensive): every MCID present in any page's content stream is reachable from the struct tree. Computed once with the same walker used in Phase 0 diagnosis. |
| 3.3 | `pdf_postprocess.py` | New docstring lines on the function | Document INV-3 and INV-4 alongside INV-1 and INV-2. |

INV-3 is the protection against a future code change that re-introduces bbox-less Figures. INV-4 is the safety net against tagger regressions that strand content unreachable from the structure tree.

---

## Fix 4 — Regression corpus

### Set

- `reversionfixesnew/` originals (`NO CHANGES` / `NO TAGGING` variants of Steel Wars, KEL201, SatRadio) plus Micawber Capital and AutoRetailing(A)
- The 5 retags in `reversionfixesnew/` re-run through the fixed pipeline
- Existing test fixtures: `sugar_daddy`, May-11 set, May-12 image-reconciliation corpus
- The existing pytest suites: `test_struct_sanitizer.py`, `test_source_figure_preservation.py`, `test_vector_figures.py`, `test_fixes.py`, `test_image_reconciliation.py`, `test_pipeline_integration.py`

### Acceptance criteria

1. **Zero regressions.** No PDF in any of the corpora has *more* PAC hard failures after the fix than before.
2. **The 5 retags in `reversionfixesnew/` reach zero hard failures** (after re-running through the fixed pipeline).
3. **All existing pytest suites pass.**
4. **veraPDF** (via `validator.py`) reports `is_compliant = True` for all 5 retags after the fix.

### New test cases

- `test_struct_sanitizer.py`: add a case asserting `_check_pipeline_invariants` raises when a Figure lacks `/BBox` (INV-3 enforcement).
- `test_struct_sanitizer.py`: add a case asserting that `_element_to_struct_type` returns `None` for `WATERMARK` and `HEADER_FOOTER` (Fix 1.1 contract).
- New `test_watermark_header_footer_artifact.py`: end-to-end — input PDF with explicit watermark/header/footer content → output PDF has those content ranges wrapped in `/Artifact BDC ... EMC` and **zero** StructElems with `/S = /Artifact`.

---

## Sequence of work

1. Implement **Fix 3** (extend gate) first. Tests for INV-3 / INV-4 will fail until Fix 1 and Fix 2 land — that's intentional: the gate proves the regression exists.
2. Implement **Fix 1** (tagger watermark/header-footer routing). Re-run gate — INV-1 violations drop to zero.
3. Implement **Fix 2** (Figure /BBox unification). Re-run gate — INV-3 violations drop to zero.
4. **Fix 4** — run the full corpus regression sweep.
5. Commit each fix as a separate atomic commit with its own test.

---

## Risk register

| Risk | Mitigation |
|------|------------|
| Routing WATERMARK/HF to content-stream artifact changes MCID numbering in pages where those elements occur, breaking ParentTree consistency. | The content-stream artifact path already exists (3 call sites) and doesn't allocate MCIDs — artifacts are not parented. Existing tests cover this; new test verifies. |
| Some image-Figure code path can't compute a bbox because the image's draw region is unknown. | Fall back to the page's `/MediaBox` as a coarse bbox rather than emit a Figure without one. Coarse bbox passes UA-1; absence does not. |
| INV-4 (orphan MCID) fires on legacy fixtures that have known orphans. | Run INV-4 in warning-only mode for one release cycle to catch real regressions without breaking the build, then promote to hard failure. |
| Fix 1's `Optional[str]` return type changes the function signature; callers that don't handle `None` will TypeError. | Audit all call sites in the same change. Mypy/grep sweep ensures completeness. |

---

## Out-of-scope follow-up

After this fix lands:
- Run the same diagnosis on the May-15 batch and the rest of the corpus to surface any other latent defect classes.
- Consider adding the PAC CLI (or veraPDF) to CI so every PR runs the gate on the test corpus automatically.
