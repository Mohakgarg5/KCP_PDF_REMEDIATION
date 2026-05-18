"""
test_struct_sanitizer.py — Unit tests for the postprocess struct-type
sanitizer and pipeline invariant check.

Both are safety nets: on a healthy pipeline output they are no-ops.  These
tests drive each branch directly with synthetic struct trees so the safety
nets are themselves protected against regression.

Run:
    ./venv/bin/python -m unittest test_struct_sanitizer -v
"""
import unittest

import pikepdf

from pdf_postprocess import (
    PipelineInvariantError,
    _check_pipeline_invariants,
    _sanitize_non_standard_struct_types,
    _STANDARD_STRUCT_TYPES,
    _STRUCT_TYPE_ALIASES,
)


def _make_pdf_with_struct_types(types: list) -> pikepdf.Pdf:
    """Build a minimal in-memory PDF with a struct tree containing the given
    /S values, each as a direct child of /Document."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    doc_kids = pikepdf.Array()
    doc_elem = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/StructElem"),
        "/S": pikepdf.Name("/Document"),
        "/K": doc_kids,
    }))
    for t in types:
        elem_dict = {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name(t),
            "/P": doc_elem,
        }
        # Figures must carry /BBox to satisfy INV-3; give synthetic figures
        # a stand-in bbox so the helper produces healthy trees by default.
        if t == "/Figure":
            elem_dict["/A"] = pikepdf.Dictionary({
                "/O": pikepdf.Name("/Layout"),
                "/BBox": pikepdf.Array([0, 0, 100, 100]),
                "/Placement": pikepdf.Name("/Block"),
            })
        elem = pdf.make_indirect(pikepdf.Dictionary(elem_dict))
        doc_kids.append(elem)
    stroot = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/StructTreeRoot"),
        "/K": doc_elem,
    }))
    doc_elem["/P"] = stroot
    pdf.Root[pikepdf.Name("/StructTreeRoot")] = stroot
    return pdf


class TestSanitizer(unittest.TestCase):
    """Sanitizer adds RoleMap entries for any non-standard /S."""

    def test_clean_tree_is_noop(self):
        pdf = _make_pdf_with_struct_types(["/P", "/H1", "/Figure", "/Table"])
        try:
            _sanitize_non_standard_struct_types(pdf)
            stroot = pdf.Root["/StructTreeRoot"]
            self.assertNotIn("/RoleMap", stroot,
                             "Clean tree must not gain a RoleMap")
        finally:
            pdf.close()

    def test_known_alias_gets_role_map_entry(self):
        pdf = _make_pdf_with_struct_types(["/Diagram", "/InlineShape"])
        try:
            _sanitize_non_standard_struct_types(pdf)
            rm = pdf.Root["/StructTreeRoot"].get("/RoleMap")
            self.assertIsNotNone(rm, "Sanitizer must create /RoleMap")
            self.assertEqual(str(rm["/Diagram"]), "/Figure")
            self.assertEqual(str(rm["/InlineShape"]), "/Figure")
        finally:
            pdf.close()

    def test_unknown_type_gets_defensive_span_mapping(self):
        pdf = _make_pdf_with_struct_types(["/MysteryTag"])
        try:
            _sanitize_non_standard_struct_types(pdf)
            rm = pdf.Root["/StructTreeRoot"].get("/RoleMap")
            self.assertIsNotNone(rm)
            # Defensive mapping prevents PAC failure even on tags we've
            # never seen — readers still get something resolvable.
            self.assertEqual(str(rm["/MysteryTag"]), "/Span")
        finally:
            pdf.close()

    def test_existing_role_map_entry_preserved(self):
        pdf = _make_pdf_with_struct_types(["/Diagram"])
        try:
            stroot = pdf.Root["/StructTreeRoot"]
            stroot[pikepdf.Name("/RoleMap")] = pikepdf.Dictionary({
                "/Diagram": pikepdf.Name("/Note"),
            })
            _sanitize_non_standard_struct_types(pdf)
            rm = stroot["/RoleMap"]
            # Sanitizer must not clobber the source's existing decision.
            self.assertEqual(str(rm["/Diagram"]), "/Note")
        finally:
            pdf.close()

    def test_artifact_in_tree_not_auto_remapped(self):
        # /Artifact is never a valid struct type. Sanitizer must NOT add a
        # RoleMap entry that would suppress the issue.  The companion
        # invariant check is what fails the build.
        pdf = _make_pdf_with_struct_types(["/Artifact"])
        try:
            _sanitize_non_standard_struct_types(pdf)
            stroot = pdf.Root["/StructTreeRoot"]
            rm = stroot.get("/RoleMap")
            if rm is not None:
                self.assertNotIn("/Artifact", [str(k) for k in rm.keys()])
        finally:
            pdf.close()

    def test_no_struct_tree_is_noop(self):
        pdf = pikepdf.new()
        pdf.add_blank_page(page_size=(612, 792))
        try:
            _sanitize_non_standard_struct_types(pdf)  # must not raise
        finally:
            pdf.close()


class TestInvariants(unittest.TestCase):
    """Invariant check raises on known-bad output, no-ops on healthy."""

    def test_healthy_tree_passes(self):
        pdf = _make_pdf_with_struct_types(["/P", "/H1", "/Figure"])
        try:
            _check_pipeline_invariants(pdf)  # must not raise
        finally:
            pdf.close()

    def test_artifact_struct_raises(self):
        pdf = _make_pdf_with_struct_types(["/Artifact"])
        try:
            with self.assertRaises(PipelineInvariantError) as ctx:
                _check_pipeline_invariants(pdf)
            self.assertIn("INV-1", str(ctx.exception))
        finally:
            pdf.close()

    def test_unresolved_non_standard_raises(self):
        # Sanitizer not run — invariant must catch the gap.
        pdf = _make_pdf_with_struct_types(["/Diagram"])
        try:
            with self.assertRaises(PipelineInvariantError) as ctx:
                _check_pipeline_invariants(pdf)
            self.assertIn("INV-2", str(ctx.exception))
            self.assertIn("/Diagram", str(ctx.exception))
        finally:
            pdf.close()

    def test_figure_missing_bbox_raises(self):
        # /Figure without /BBox: must trip INV-3.
        pdf = _make_pdf_with_struct_types(["/P"])
        try:
            # Append a bbox-less Figure directly to the document
            stroot = pdf.Root["/StructTreeRoot"]
            doc_elem = stroot["/K"]
            doc_kids = doc_elem["/K"]
            bad_fig = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Figure"),
                "/P": doc_elem,
            }))
            doc_kids.append(bad_fig)
            with self.assertRaises(PipelineInvariantError) as ctx:
                _check_pipeline_invariants(pdf)
            self.assertIn("INV-3", str(ctx.exception))
        finally:
            pdf.close()

    def test_non_standard_with_role_map_passes(self):
        pdf = _make_pdf_with_struct_types(["/Diagram"])
        try:
            pdf.Root["/StructTreeRoot"][pikepdf.Name("/RoleMap")] = (
                pikepdf.Dictionary({"/Diagram": pikepdf.Name("/Figure")})
            )
            _check_pipeline_invariants(pdf)  # passes — alias resolved
        finally:
            pdf.close()

    def test_sanitizer_then_invariants_chain_passes(self):
        # End-to-end: dirty tree → sanitizer fixes alias → invariants ok.
        pdf = _make_pdf_with_struct_types(["/Diagram", "/Picture"])
        try:
            _sanitize_non_standard_struct_types(pdf)
            _check_pipeline_invariants(pdf)
        finally:
            pdf.close()

    def test_no_struct_tree_is_noop(self):
        pdf = pikepdf.new()
        pdf.add_blank_page(page_size=(612, 792))
        try:
            _check_pipeline_invariants(pdf)  # must not raise
        finally:
            pdf.close()


class TestAliasTableShape(unittest.TestCase):
    """Guard the lookup table against silent regression."""

    def test_aliases_map_to_standard_types(self):
        for alias, target in _STRUCT_TYPE_ALIASES.items():
            self.assertIn(target, _STANDARD_STRUCT_TYPES,
                          f"Alias {alias} -> {target} but target is "
                          f"not in standard struct types")
            self.assertNotIn(alias, _STANDARD_STRUCT_TYPES,
                             f"Alias {alias} is itself listed as standard")

    def test_artifact_not_in_alias_table(self):
        # The sanitizer relies on this absence to avoid auto-hiding the bug.
        self.assertNotIn("/Artifact", _STRUCT_TYPE_ALIASES)
        self.assertNotIn("/Artifact", _STANDARD_STRUCT_TYPES)


class TestElementToStructTypeContract(unittest.TestCase):
    """Guard the tagger's element→struct-type mapping.

    WATERMARK and HEADER_FOOTER must NEVER produce a struct-type string —
    they belong in the content stream as /Artifact BDC, not in the struct
    tree.  Returning "/Artifact" from this function would be a contract bug
    even though downstream code currently intercepts it via is_artifact.
    """

    @staticmethod
    def _make_tb(element_type):
        from models import TextBlock, BBox, FontInfo
        return TextBlock(
            text="x",
            bbox=BBox(0, 0, 100, 20),
            font=FontInfo(name="Helv", size=10.0, is_bold=False, is_italic=False),
            element_type=element_type,
        )

    def test_watermark_returns_none(self):
        from pdf_tagger import _element_to_struct_type
        from models import ElementType
        self.assertIsNone(_element_to_struct_type(self._make_tb(ElementType.WATERMARK)))

    def test_header_footer_returns_none(self):
        from pdf_tagger import _element_to_struct_type
        from models import ElementType
        self.assertIsNone(_element_to_struct_type(self._make_tb(ElementType.HEADER_FOOTER)))

    def test_paragraph_returns_p(self):
        from pdf_tagger import _element_to_struct_type
        from models import ElementType
        self.assertEqual(
            _element_to_struct_type(self._make_tb(ElementType.PARAGRAPH)), "/P",
        )


if __name__ == "__main__":
    unittest.main()
