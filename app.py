"""
app.py - Streamlit UI for the PDF Accessibility Remediation Pipeline.

Run with:
    streamlit run app.py
"""
import io
import logging
import tempfile
import time
import zipfile
from pathlib import Path

import streamlit as st

from pdf_extractor import extract_document
from pdf_tagger import tag_pdf
from pdf_postprocess import postprocess_pdf

logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s] %(message)s")

MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PDF Accessibility Fixer",
    page_icon="♿",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ---- global ---- */
html, body, [data-testid="stApp"] {
    background: #0f1117;
    color: #e8eaf0;
}
[data-testid="stSidebar"] {
    background: #161b27;
    border-right: 1px solid #2a2f3e;
}

/* ---- hero banner ---- */
.hero {
    background: linear-gradient(135deg, #1e3a5f 0%, #0d2137 60%, #101624 100%);
    border: 1px solid #2d4a6b;
    border-radius: 16px;
    padding: 2.4rem 2.8rem 2rem;
    margin-bottom: 2rem;
}
.hero h1 { font-size: 2.2rem; font-weight: 800; margin: 0 0 .4rem; color: #e8eaf0; }
.hero p  { font-size: 1.05rem; color: #9aa8be; margin: 0; }
.badge {
    display: inline-block;
    background: #1a3a5c;
    border: 1px solid #2d5a8e;
    border-radius: 6px;
    padding: 3px 10px;
    font-size: .78rem;
    color: #7eb8f7;
    margin-right: 6px;
    margin-top: 10px;
}

/* ---- file card ---- */
.file-card {
    background: #161b27;
    border: 1px solid #2a2f3e;
    border-radius: 12px;
    padding: 1.1rem 1.4rem;
    margin-bottom: .9rem;
    display: flex;
    align-items: center;
    gap: 1rem;
}
.file-icon { font-size: 1.6rem; flex-shrink: 0; }
.file-info { flex: 1; min-width: 0; }
.file-name { font-weight: 600; font-size: .95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-size { font-size: .78rem; color: #6c7a94; margin-top: 2px; }
.status-badge {
    font-size: .75rem;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 20px;
    white-space: nowrap;
    flex-shrink: 0;
}
.status-waiting  { background: #1e2535; color: #6c7a94; border: 1px solid #2a3040; }
.status-running  { background: #1a3050; color: #7eb8f7; border: 1px solid #2d5a8e; }
.status-done     { background: #0d3320; color: #4ade80; border: 1px solid #166534; }
.status-error    { background: #3a1010; color: #f87171; border: 1px solid #7f1d1d; }
.status-skipped  { background: #2a2000; color: #facc15; border: 1px solid #713f12; }

/* ---- metric card ---- */
.metric-row { display: flex; gap: .8rem; margin: .8rem 0 1rem; flex-wrap: wrap; }
.metric-card {
    background: #1a2035;
    border: 1px solid #2a3550;
    border-radius: 10px;
    padding: .7rem 1.1rem;
    flex: 1;
    min-width: 90px;
    text-align: center;
}
.metric-card .val { font-size: 1.5rem; font-weight: 700; color: #7eb8f7; }
.metric-card .lbl { font-size: .72rem; color: #6c7a94; margin-top: 2px; text-transform: uppercase; letter-spacing: .05em; }

/* ---- summary banner ---- */
.summary-ok   { background:#0d3320; border:1px solid #166534; border-radius:10px; padding:1rem 1.4rem; color:#4ade80; font-weight:600; }
.summary-warn { background:#2a2000; border:1px solid #713f12; border-radius:10px; padding:1rem 1.4rem; color:#facc15; font-weight:600; }

/* ---- buttons ---- */
div[data-testid="stButton"] > button,
div[data-testid="stDownloadButton"] > button {
    border-radius: 8px !important;
    font-weight: 600 !important;
}

/* ---- upload zone ---- */
[data-testid="stFileUploader"] {
    background: #161b27;
    border-radius: 12px;
    border: 2px dashed #2a3550;
    padding: .5rem;
}

/* ---- divider ---- */
hr { border-color: #2a2f3e !important; }

/* ---- expander ---- */
details summary { color: #9aa8be !important; }
</style>
""", unsafe_allow_html=True)


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ♿ PDF Accessibility Fixer")
    st.markdown("---")
    st.markdown("**What this tool fixes:**")
    fixes = [
        ("🏷️", "Structure tags (H1–H6, P, Table…)"),
        ("🖼️", "Image alt-text & BBox layout"),
        ("📋", "XMP metadata & pdfuaid"),
        ("🔤", "Font embedding"),
        ("🔗", "Link annotation wiring"),
        ("👻", "Watermarks → Artifact"),
        ("📑", "MarkInfo, TabOrder, ViewerPrefs"),
    ]
    for icon, label in fixes:
        st.markdown(f"{icon} &nbsp; <span style='font-size:.88rem'>{label}</span>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("**Standards**")
    st.markdown("""
<span class='badge'>PDF/UA-1</span>
<span class='badge'>ISO 14289-1</span>
<span class='badge'>Matterhorn</span>
""", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(f"**Limits**")
    st.caption(f"Max {MAX_FILE_SIZE_MB} MB per file · PDF only")
    st.markdown("---")
    st.caption("Powered by pikepdf · pdfminer · Pillow")


# ── Hero ──────────────────────────────────────────────────────────────────────
st.markdown("""
<div class='hero'>
  <h1>♿ PDF Accessibility Fixer</h1>
  <p>Transform any PDF into a <strong>PDF/UA-1 compliant</strong>, fully accessible document.
     Upload one or more files — we handle everything automatically.</p>
  <span class='badge'>PDF/UA-1</span>
  <span class='badge'>PAC 2024</span>
  <span class='badge'>veraPDF ready</span>
  <span class='badge'>Batch processing</span>
</div>
""", unsafe_allow_html=True)


# ── Upload ────────────────────────────────────────────────────────────────────
uploaded_files = st.file_uploader(
    "Drop your PDF files here, or click to browse",
    type=["pdf"],
    accept_multiple_files=True,
    help=f"Maximum {MAX_FILE_SIZE_MB} MB per file. Multiple files are processed in batch.",
)

if not uploaded_files:
    st.markdown("""
<div style='text-align:center; padding: 2.5rem; color:#4a5568;'>
  <div style='font-size:3.5rem;'>📄</div>
  <div style='font-size:1rem; margin-top:.5rem;'>No files uploaded yet</div>
  <div style='font-size:.82rem; margin-top:.3rem;'>Drag & drop PDFs above to get started</div>
</div>
""", unsafe_allow_html=True)
    st.stop()

# ── Validate sizes ────────────────────────────────────────────────────────────
oversized = [f for f in uploaded_files if f.size > MAX_FILE_SIZE_BYTES]
valid_files = [f for f in uploaded_files if f.size <= MAX_FILE_SIZE_BYTES]

if oversized:
    for f in oversized:
        st.warning(f"⚠️  **{f.name}** is too large ({f.size/1024/1024:.1f} MB > {MAX_FILE_SIZE_MB} MB) — skipped.")

if not valid_files:
    st.error("No valid files to process.")
    st.stop()

# ── File list preview ─────────────────────────────────────────────────────────
st.markdown(f"### {len(valid_files)} file{'s' if len(valid_files) != 1 else ''} ready")

def _size_label(n: int) -> str:
    if n >= 1_048_576:
        return f"{n/1_048_576:.1f} MB"
    return f"{n/1024:.0f} KB"

for f in valid_files:
    st.markdown(f"""
<div class='file-card'>
  <div class='file-icon'>📄</div>
  <div class='file-info'>
    <div class='file-name'>{f.name}</div>
    <div class='file-size'>{_size_label(f.size)}</div>
  </div>
  <div class='status-badge status-waiting'>Waiting</div>
</div>
""", unsafe_allow_html=True)

st.markdown("")

col_run, col_clear = st.columns([3, 1])
with col_run:
    run = st.button("▶  Make Accessible", type="primary", use_container_width=True)
with col_clear:
    if st.button("✕  Clear", use_container_width=True):
        st.rerun()

if not run:
    st.stop()

# ── Processing ────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown("### Processing")

results = []  # list of dicts

for idx, uploaded in enumerate(valid_files):
    st.markdown(f"#### {idx+1}. {uploaded.name}")
    progress_bar = st.progress(0, text="Queued…")
    status_box   = st.empty()

    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path  = Path(tmp_dir) / uploaded.name
        output_path = Path(tmp_dir) / f"{input_path.stem}_accessible.pdf"
        input_path.write_bytes(uploaded.getvalue())

        try:
            t0 = time.time()

            # Stage 1 – Extract
            progress_bar.progress(10, text="Stage 1/3 — Extracting content…")
            doc = extract_document(str(input_path))
            text_count = sum(len(p.text_blocks) for p in doc.pages)
            img_count  = sum(len(p.images)      for p in doc.pages)
            tbl_count  = sum(len(p.tables)       for p in doc.pages)

            # Stage 2 – Tag
            progress_bar.progress(45, text="Stage 2/3 — Injecting structure tags…")
            tag_pdf(str(input_path), str(output_path), doc)

            # Stage 3 – Post-process
            progress_bar.progress(80, text="Stage 3/3 — Fixing metadata & fonts…")
            postprocess_pdf(str(output_path), doc.title, doc.language)

            elapsed = time.time() - t0
            progress_bar.progress(100, text="Done ✓")

            # Metrics
            status_box.markdown(f"""
<div class='metric-row'>
  <div class='metric-card'><div class='val'>{len(doc.pages)}</div><div class='lbl'>Pages</div></div>
  <div class='metric-card'><div class='val'>{text_count}</div><div class='lbl'>Text Blocks</div></div>
  <div class='metric-card'><div class='val'>{img_count}</div><div class='lbl'>Images</div></div>
  <div class='metric-card'><div class='val'>{tbl_count}</div><div class='lbl'>Tables</div></div>
  <div class='metric-card'><div class='val'>{elapsed:.1f}s</div><div class='lbl'>Time</div></div>
</div>
<div style='font-size:.82rem; color:#6c7a94; margin-bottom:.5rem;'>
  📖 &nbsp;<strong>Title:</strong> {doc.title[:80]}{'…' if len(doc.title)>80 else ''} &nbsp;|&nbsp;
  🌐 &nbsp;<strong>Lang:</strong> {doc.language}
</div>
""", unsafe_allow_html=True)

            pdf_bytes = output_path.read_bytes()
            st.download_button(
                label=f"⬇  Download  {output_path.name}",
                data=pdf_bytes,
                file_name=output_path.name,
                mime="application/pdf",
                key=f"dl_{idx}",
                use_container_width=True,
            )
            results.append({
                "name": uploaded.name,
                "out_name": output_path.name,
                "ok": True,
                "bytes": pdf_bytes,
                "pages": len(doc.pages),
                "elapsed": elapsed,
            })

        except Exception as exc:
            progress_bar.progress(100, text="Failed ✗")
            status_box.error(f"**Error:** `{type(exc).__name__}: {exc}`")
            results.append({"name": uploaded.name, "ok": False, "error": str(exc)})

    st.markdown("")  # spacing

# ── Summary & batch download ──────────────────────────────────────────────────
st.markdown("---")
ok_results  = [r for r in results if r["ok"]]
bad_results = [r for r in results if not r["ok"]]

if len(results) > 1:
    st.markdown("### Summary")
    col1, col2, col3 = st.columns(3)
    col1.metric("Total files",     len(results))
    col2.metric("Succeeded",       len(ok_results),  delta=None)
    col3.metric("Failed",          len(bad_results),  delta=None)

    for r in results:
        icon   = "✅" if r["ok"] else "❌"
        detail = f"{r['pages']} pages · {r['elapsed']:.1f}s" if r["ok"] else r.get("error","")
        st.markdown(f"{icon} &nbsp; **{r['name']}** &emsp; <span style='color:#6c7a94;font-size:.82rem;'>{detail}</span>",
                    unsafe_allow_html=True)

    if len(ok_results) == len(results):
        st.markdown("<div class='summary-ok'>🎉 All files processed successfully — fully PDF/UA-1 compliant!</div>",
                    unsafe_allow_html=True)
    elif ok_results:
        st.markdown(f"<div class='summary-warn'>⚠️  {len(ok_results)} of {len(results)} files succeeded.</div>",
                    unsafe_allow_html=True)

    # Batch ZIP download
    if ok_results:
        st.markdown("")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in ok_results:
                zf.writestr(r["out_name"], r["bytes"])
        buf.seek(0)
        st.download_button(
            label=f"⬇  Download All ({len(ok_results)} files) as ZIP",
            data=buf,
            file_name="accessible_pdfs.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
            key="dl_all_zip",
        )
