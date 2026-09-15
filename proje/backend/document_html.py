"""HTML/CSS rendering of a DocumentModel, printed to PDF by headless Chromium.

Why this module exists
----------------------
reportlab draws a page one flowable at a time and every piece of layout is
code: column widths, list indents, keep-with-next, a table header repeated
by hand. A browser already does all of that, and does it better -- real
typography, hyphen-free URL wrapping, tables whose header repeats on every
page, internal links and PDF bookmarks. The document is described once as
HTML + CSS and Chromium prints it.

Chromium is already in the backend image (Playwright, installed for
crawl4ai), so this adds no dependency. It is launched per document in its
own thread with its own event loop: the artifact writers are synchronous
and are called from inside the Document worker's running loop, where
Playwright's API cannot be used directly.

The reportlab renderer in document_model stays as the fallback whenever
Chromium is missing, fails, times out, or prints a file that does not pass
verification -- see artifacts.ArtifactTools._render_blocks_pdf.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import html
import mimetypes
import os
import re
from datetime import datetime
from typing import Any, Optional, Sequence

import document_export
from document_model import (
    DocumentModel,
    HeadingBlock,
    ImageBlock,
    ListBlock,
    ListItem,
    ParagraphBlock,
    QuoteBlock,
    TableBlock,
    _SOURCES_LABEL,
)

PDF_TIMEOUT_SECONDS = float(os.environ.get("DOCUMENT_PDF_TIMEOUT_SECONDS", "45"))
# A table of contents earns its space only once there is something to navigate.
TOC_MIN_SECTIONS = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_TR_MONTHS = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)
_URL = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:!?)\]]")


def pdf_engine() -> str:
    """'chromium' (default) or 'reportlab', from DOCUMENT_PDF_ENGINE."""
    value = os.environ.get("DOCUMENT_PDF_ENGINE", "chromium").strip().lower()
    return "reportlab" if value in {"reportlab", "legacy", "off", "false", "0"} else "chromium"


# --- HTML -------------------------------------------------------------------

CSS = """
@page { size: A4; }
:root {
  --ink: #1f2933; --muted: #616e7c; --accent: #2f5d8a;
  --accent-soft: #eaf1f7; --rule: #d9e2ec; --zebra: #f5f7fa;
}
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; color: var(--ink);
  font-family: 'Liberation Sans', 'FreeSans', 'Segoe UI', Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.55;
}
.cover { border-left: 5px solid var(--accent); padding: 1mm 0 1mm 6mm; margin: 0 0 8mm; }
.cover .eyebrow {
  font-size: 8pt; font-weight: 700; letter-spacing: .14em;
  text-transform: uppercase; color: var(--accent);
}
.cover h1 { font-size: 23pt; line-height: 1.18; margin: 2mm 0 2.5mm; letter-spacing: -.01em; }
.cover .meta { font-size: 9pt; color: var(--muted); }
nav.toc {
  background: var(--accent-soft); border-radius: 4px;
  padding: 4mm 6mm 3mm; margin: 0 0 9mm; break-inside: avoid;
}
nav.toc .toc-title {
  font-size: 8pt; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; color: var(--accent); margin: 0 0 2mm;
}
nav.toc ol { margin: 0; padding-left: 5mm; columns: 2; column-gap: 10mm; }
nav.toc li { margin: 0 0 1.2mm; break-inside: avoid; }
nav.toc a { color: var(--ink); }
h2 {
  font-size: 15pt; line-height: 1.25; color: var(--accent);
  margin: 8mm 0 3mm; padding-bottom: 1.6mm; border-bottom: 1px solid var(--rule);
  break-after: avoid;
}
h3 { font-size: 12pt; line-height: 1.3; margin: 6mm 0 2mm; break-after: avoid; }
h4 { font-size: 10.5pt; margin: 5mm 0 1.5mm; color: var(--muted); break-after: avoid; }
h2 + p, h3 + p, h4 + p { margin-top: 0; }
p { margin: 0 0 3mm; orphans: 3; widows: 3; }
strong { font-weight: 700; color: #102a43; }
ul, ol { margin: 0 0 3.5mm; padding-left: 6mm; }
li { margin: 0 0 1.3mm; }
li > ul, li > ol { margin: 1.3mm 0 0; }
ol ol { list-style-type: lower-alpha; }
ol ol ol { list-style-type: lower-roman; }
table { width: 100%; border-collapse: collapse; margin: 2mm 0 5mm; font-size: 9pt; }
thead { display: table-header-group; }
th {
  background: var(--accent); color: #fff; text-align: left; font-weight: 700;
  padding: 2mm 2.6mm; vertical-align: bottom;
}
td { padding: 1.8mm 2.6mm; border-bottom: 1px solid var(--rule); vertical-align: top; }
tbody tr:nth-child(even) td { background: var(--zebra); }
tr { break-inside: avoid; }
aside.callout {
  border-left: 3px solid var(--accent); background: var(--accent-soft);
  padding: 3mm 4.5mm; margin: 3mm 0 5mm; break-inside: avoid;
}
figure { margin: 4mm 0 5mm; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; max-height: 95mm; }
figcaption { font-size: 8pt; color: var(--muted); margin-top: 1.5mm; }
a { color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }
section.sources { font-size: 9pt; }
section.sources h2 { font-size: 13pt; }
section.sources li { overflow-wrap: anywhere; }
"""


def _inline(text: str) -> str:
    """Escaped text with URLs linked and **spans** in <strong>."""
    escaped = html.escape(text or "", quote=False)

    def link(match: re.Match[str]) -> str:
        url = match.group(0)
        return f'<a href="{html.escape(html.unescape(url), quote=True)}">{url}</a>'

    linked = _URL.sub(link, escaped)
    return document_export.INLINE_MARKS.sub(r"<strong>\2</strong>", linked)


def _turkish_date(moment: Optional[datetime] = None) -> str:
    moment = moment or datetime.now()
    return f"{moment.day} {_TR_MONTHS[moment.month - 1]} {moment.year}"


def _list_html(items: Sequence[ListItem], ordered: bool) -> str:
    tag = "ol" if ordered else "ul"
    parts: list[str] = [f"<{tag}>"]
    depth = 0
    open_item = False
    for item in items:
        level = max(0, item.level)
        if level > depth and open_item:
            # Open sublists inside the item that is still open.
            while depth < level:
                parts.append(f"<{tag}>")
                depth += 1
                open_item = False
        else:
            if open_item:
                parts.append("</li>")
            while depth > level:
                parts.append(f"</{tag}></li>")
                depth -= 1
        parts.append(f"<li>{_inline(item.text)}")
        open_item = True
    if open_item:
        parts.append("</li>")
    while depth > 0:
        parts.append(f"</{tag}></li>")
        depth -= 1
    parts.append(f"</{tag}>")
    return "".join(parts)


def _table_html(block: TableBlock) -> str:
    head = "".join(f"<th>{_inline(c)}</th>" for c in block.columns)
    rows = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
        for row in block.rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"


def _image_html(image: Any, caption: str = "") -> str:
    path = getattr(image, "path", None)
    try:
        if not path or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            return ""
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    note = " — ".join(p for p in (caption, getattr(image, "attribution", "")) if p)
    figcaption = f"<figcaption>{_inline(note)}</figcaption>" if note else ""
    return f'<figure><img src="data:{mime};base64,{data}" alt="">{figcaption}</figure>'


def render_html(
    document_model: DocumentModel,
    *,
    images: Optional[Sequence[Any]] = None,
    generated: Optional[datetime] = None,
) -> str:
    """A complete, self-contained HTML document for the report."""
    title = document_export.short_report_title(document_model.title, fallback="Rapor")
    blocks = list(document_model.blocks)
    pending_images = list(images or [])
    explicit_images = any(isinstance(b, ImageBlock) for b in blocks)

    main_sections = [
        b for b in blocks
        if isinstance(b, HeadingBlock) and b.level == 1 and not _SOURCES_LABEL.match(b.text)
    ]
    anchors: dict[int, str] = {}
    toc_entries: list[str] = []
    for index, block in enumerate(main_sections, start=1):
        anchors[id(block)] = f"bolum-{index}"
        toc_entries.append(f'<li><a href="#bolum-{index}">{_inline(block.text)}</a></li>')

    body: list[str] = []
    in_sources = False
    for block in blocks:
        if isinstance(block, HeadingBlock):
            is_sources = bool(_SOURCES_LABEL.match(block.text))
            if is_sources and pending_images and not explicit_images:
                body.extend(_image_html(image) for image in pending_images)
                pending_images = []
            if is_sources and not in_sources:
                body.append('<section class="sources">')
                in_sources = True
            tag = {1: "h2", 2: "h3"}.get(block.level, "h4")
            anchor = anchors.get(id(block))
            id_attr = f' id="{anchor}"' if anchor else ""
            body.append(f"<{tag}{id_attr}>{_inline(block.text)}</{tag}>")
        elif isinstance(block, ParagraphBlock):
            body.append(f"<p>{_inline(block.text)}</p>")
        elif isinstance(block, ListBlock):
            body.append(_list_html(block.items, block.ordered))
        elif isinstance(block, TableBlock):
            body.append(_table_html(block))
        elif isinstance(block, QuoteBlock):
            body.append(f'<aside class="callout">{_inline(block.text)}</aside>')
        elif isinstance(block, ImageBlock) and pending_images:
            body.append(_image_html(pending_images.pop(0), block.caption))
    body.extend(_image_html(image) for image in pending_images)
    if in_sources:
        body.append("</section>")
    if not document_model.has_content() and not images:
        body.append("<p>This job did not produce a final synthesized answer.</p>")

    toc = ""
    if len(main_sections) >= TOC_MIN_SECTIONS:
        toc = (
            '<nav class="toc"><div class="toc-title">İçindekiler</div><ol>'
            + "".join(toc_entries)
            + "</ol></nav>"
        )
    cover = (
        '<header class="cover"><div class="eyebrow">Rapor</div>'
        f"<h1>{html.escape(title, quote=False)}</h1>"
        f'<div class="meta">Hazırlanma tarihi: {_turkish_date(generated)}</div></header>'
    )
    return (
        '<!doctype html><html lang="tr"><head><meta charset="utf-8">'
        f"<title>{html.escape(title, quote=False)}</title><style>{CSS}</style></head>"
        f"<body>{cover}{toc}{''.join(body)}</body></html>"
    )


def footer_template(title: str) -> str:
    """Chromium footer: report title left, page X / Y right."""
    safe = html.escape(title[:90], quote=False)
    return (
        "<div style=\"width:100%;padding:0 18mm;font-size:7.5px;color:#7b8794;"
        "font-family:'Liberation Sans','FreeSans',Arial,sans-serif;"
        "display:flex;justify-content:space-between;-webkit-print-color-adjust:exact;\">"
        f"<span>{safe}</span>"
        "<span>Sayfa <span class=\"pageNumber\"></span> / <span class=\"totalPages\"></span></span>"
        "</div>"
    )


# --- Chromium ---------------------------------------------------------------


async def _print_to_pdf(page_html: str, footer: str, timeout_ms: float) -> bytes:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )
        try:
            page = await browser.new_page()
            page.set_default_timeout(timeout_ms)
            await page.set_content(page_html, wait_until="load")
            await page.emulate_media(media="print")
            return await page.pdf(
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=footer,
                margin={"top": "18mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
                outline=True,
                tagged=True,
            )
        finally:
            await browser.close()


def html_to_pdf(page_html: str, *, footer: str, timeout: float = PDF_TIMEOUT_SECONDS) -> bytes:
    """Print HTML with Chromium from synchronous code, even inside a running loop."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-chromium")
    future = pool.submit(
        lambda: asyncio.run(
            asyncio.wait_for(_print_to_pdf(page_html, footer, timeout * 1000), timeout)
        )
    )
    try:
        return future.result(timeout=timeout + 10)
    finally:
        # Never block the caller on a wedged browser; the thread ends with it.
        pool.shutdown(wait=False)


def build_pdf(document_model: DocumentModel, *, images: Optional[Sequence[Any]] = None) -> bytes:
    """Render a DocumentModel to PDF through HTML/CSS and Chromium."""
    title = document_export.short_report_title(document_model.title, fallback="Rapor")
    return html_to_pdf(
        render_html(document_model, images=images),
        footer=footer_template(title),
    )


__all__ = ["build_pdf", "footer_template", "html_to_pdf", "pdf_engine", "render_html"]
