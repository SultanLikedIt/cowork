# Document generation (PDF / DOCX / PPTX)

How a requested report becomes a downloadable file. XLSX/CSV/JSON/HTML bundles
have their own native writers in `backend/artifacts.py` and are not covered here.

## Pipeline

```
Research notes
   |
   v
Document LLM  (execution/langgraph_runtime/workforce.py: _run_document_worker)
   |  returns JSON: {filename, title, summary, blocks: [...]}
   |  JSON mode on; unescaped quotes / trailing commas repaired
   v
document_model.attach_blocks_to_payload      validate + normalize blocks,
   |                                         mirror them into `content` as markdown
   v
artifacts.ArtifactTools.create_pdf_artifact / create_docx_artifact
   |
   |-- PDF:  document_html.build_pdf    HTML/CSS printed by Playwright Chromium
   |          -> fallback document_model.build_pdf (reportlab)
   |-- DOCX: document_docx.build_docx   styled python-docx, same design as the PDF
   |          -> fallback document_model.build_docx (plain)
   |-- HTML deck (when also requested): document_html.render_deck from the same blocks
   v
document_verify.verify_document    reopen the file and check it
   |  fail -> redraw with the fallback writer, keep whichever passes
   v
<workspace>/artifacts/<file>
```

## Typed blocks

The Document LLM describes the report as an ordered list of blocks instead of
free markdown, so structure is never guessed from text:

| type | fields |
|---|---|
| `heading` | `text`, `level` 1-3 |
| `paragraph` | `text` (`**bold**` allowed) |
| `list` | `ordered`, `items` (strings, or `{text, items}` for nesting) |
| `table` | `columns`, `rows` (max 8 columns) |
| `quote` | `text` — highlighted callout |
| `image` | `caption` — where the fetched topic image goes |

Parsing is lenient (`ul`, `h2`, `bullets`, row objects, bare strings are all
accepted); a block that cannot be understood is dropped on its own.

Used for narrative PDF/DOCX only. Table/ranking requests, uploaded-template
edits (`edit_docx_template`, `create_docx_from_template`) and interactive
HTML keep their own payload shapes; PPTX has typed slides (next section).

If the reply has no usable blocks, or the worker replaces the write-up with
the research notes (write-up < 60% of the notes), the markdown writers in
`document_export.py` are used instead.

## PDF rendering

`document_html.render_html` builds one self-contained HTML page (title
block, table of contents for 4+ sections, headings, lists, tables with
repeating headers, callouts, embedded images, wrapped URLs) and
`html_to_pdf` prints it with the Chromium that Playwright already installs
for crawl4ai. Rendering runs in its own thread and event loop because the
artifact writers are synchronous and called from inside the worker's loop.
Footer: report title and `Sayfa X / Y`.

Chromium missing, crashing or timing out falls back to the reportlab
renderer in `document_model.build_pdf`; the job never fails for it.

## Word rendering

`document_docx.build_docx` draws the same blocks with the PDF's palette and
structure: accent-barred title block with date, a table of contents whose
entries link to bookmarks on the section headings (4+ sections), accent
headings with a rule, tables with a filled header row, zebra rows and a
repeating header, a shaded callout for quotes, clickable URLs, real bold
runs, and a footer with the report title and `Sayfa X / Y` fields.

The look lives on Word's own styles (Normal, Title, Heading 1-3, Quote,
List*) with the theme fonts removed, so the navigation pane and Word's own
TOC keep working. If it raises, or its file fails verification, the plain
`document_model.build_docx` redraws the same blocks.

## PowerPoint (typed slides)

A .pptx ask gets its own Document reply: `{filename, title, subtitle,
slides: [...]}` (`workforce._SLIDES_SHAPE`). The model designs the deck; the
server draws it. `backend/slide_model.py` validates it, `backend/document_pptx.py`
renders it.

| layout | keys | drawn as |
|---|---|---|
| `bullets` | `bullets` (strings, or `{text, items}` for one sub-level) | real bullets with a hanging indent |
| `stats` | `stats`: 2-4 `{value, label, detail}` | big-number cards |
| `table` | `columns`, `rows` (max 8 columns, every row) | native table, header repeated on continuation slides |
| `chart` | `chart`: `{type: line/column/bar/pie, categories, series, unit}`, up to 3 side bullets | native, editable chart |
| `two_column` | `left` / `right`: `{heading, bullets}` | two cards |
| `quote` | `text`, `attribution` | one large statement |
| `section` | `title`, `subtitle` | divider (numbered when there are several) |
| `takeaways` | `bullets` | numbered cards, the last slide |

Every slide may carry `subtitle` (a one-line key message) and `notes`
(speaker notes). The server adds the title slide (with the topic photo as a
right-hand panel when one was fetched), an agenda when there are 3+ distinct
slide titles, the page footer and a clickable sources slide.

**Fitting, not clipping.** Every text box is measured before it is drawn
(Calibri advance widths, `document_pptx.wrap_count`) and gets the largest
font size from a ladder that fits (bullets 24→16 pt, tables 16→11 pt). When
even the smallest does not fit, the slide is split into parts of balanced
height: tables by rows with the header repeated, bullets by whole groups (a
sub-bullet never leaves its parent), so no continuation slide carries one
leftover line. Text is never cut with "…". A layout that cannot fit
(an overlong two-column or quote) is redrawn as bullets.

**Lenient parsing.** Layout aliases (`kpi`, `comparison`, `graph`, …),
numbers written `6.842,40` or `1,200`, table rows as objects. A slide that
lost its content degrades on its own: a chart without usable numbers
becomes bullets, a single stat becomes a bullet, a half two-column becomes a
list. Fewer than two content slides means the reply is not a deck.

**Grounding.** Stat cards and charts turn a number into a headline, so their
figures are checked against the research notes (plus uploaded material):
a stat whose value is not in the notes is dropped, and a chart with more than
20% of its values missing from the notes is dropped (`slide_model.ground_deck`,
logged as `document slides: ...`). Bullets and tables carry the notes' own
wording and are not checked. With no notes at all nothing is checked.

**Fallbacks.** No usable slides (empty reply, timeout, no `slides` key,
unparseable JSON) → the notes-driven deck, `document_export.build_pptx`, as
before. A slide renderer crash, or a file that fails verification → the same
legacy deck, kept if it passes. `DOCUMENT_SLIDES=false` turns the whole
path off.

A .pptx written beside a typed-block PDF/DOCX ("Word ve PowerPoint") is
drawn from those blocks through the same renderer, one slide per section
(`slide_model.deck_from_document`), with no extra model call.

## HTML deck

"Word + HTML sunum" jobs also write an HTML deck. When the reply carried
typed blocks (and `content` was not replaced afterwards),
`document_html.render_deck` builds it from them: an opening slide with the
title, a lead sentence and an agenda, then one numbered slide per main
section with real lists, tables and callouts, and a sources slide. Printed,
each slide is one landscape page. Without blocks the previous
`workforce._html_deck_from_payload` deck is used.

## Company brand

`brand.py` reads `BRAND_DIR/brand.json` (compose mounts `proje/brand` at
`/app/brand`; setup in [`../brand/README.md`](../brand/README.md)) and every
writer wears it:

| Output | What changes |
|---|---|
| PDF (Chromium) + typed-block HTML deck | CSS overrides: primary colour on headings, table headers, links and the TOC; secondary colour on the cover bar and eyebrow; logo above the cover; company name in the footer |
| PDF (reportlab fallback) | heading accent |
| DOCX | style colours from the brand, secondary colour on the cover, logo above the cover, company name in the footer, brand font |
| PPTX (typed slides and the notes fallback) | light corporate theme instead of the dark one: white slides, primary/secondary accents, chart series from the brand colours, logo on the title slide. The font stays Calibri: text fitting is measured with Calibri widths |
| Markdown HTML deck | the same light palette as a style override, logo in the hero |
| XLSX | header row filled with the primary colour, white bold text, frozen header; values untouched |

The brand is read once at import, so a changed brand.json or logo needs a
backend restart. With no brand (BRAND_DIR empty or no brand.json) every
writer keeps its built-in look; a broken brand.json is logged and ignored.

## Verification

`document_verify.verify_document` reopens every PDF/DOCX/PPTX (pypdfium2 /
python-docx / python-pptx) and fails it when:

- the file does not open or has no pages (a deck: fewer than 2 slides)
- there is almost no text, or only the "no final answer" placeholder
- markup leaked into the text (`**`, `## `, `|---|`, raw JSON, `\n`)
- a heading that should be there is missing (a deck: a slide title)
- an expected sources section is missing
- PPTX only: a text box's text runs past its frame (more than 15%), or a
  shape extends past the slide edge (`inspect_pptx`)

A failing file is redrawn with the fallback writer (PDF: Chromium ->
reportlab blocks -> markdown; DOCX: styled -> plain blocks -> markdown). If nothing passes, the first file is delivered and the
problems are logged and returned in the tool result's `verification` field.

## Settings

| variable | default | meaning |
|---|---|---|
| `DOCUMENT_BLOCKS` | `true` (compose) | request typed blocks for narrative PDF/DOCX; `false` = markdown path |
| `DOCUMENT_SLIDES` | `true` | request typed slides for PPTX; `false` = the notes-driven deck |
| `DOCUMENT_PDF_ENGINE` | `chromium` | `reportlab` skips Chromium |
| `DOCUMENT_PDF_TIMEOUT_SECONDS` | `45` | Chromium print timeout |
| `BRAND_DIR` | `/app/brand` (compose) | company brand folder; empty = built-in look |
| `DOCUMENT_LLM_REASONING_EFFORT` | empty | OpenRouter `reasoning.effort` for the Document call: `low` / `medium` / `high` |
| `DOCUMENT_LLM_PROVIDER_SORT` | empty | OpenRouter provider routing for the Document call: `throughput` / `latency` / `price` |

## Logs

```
docker logs -f proje-backend-1 2>&1 | grep -E "document |llm stage=|unparseable|no content"
```

A healthy run:

```
document path: calling Document LLM for typed blocks (kind=pdf, ...)
llm stage=document_agent model=... provider=... seconds=... output_tokens=... reasoning_tokens=... tok_per_s=... finish=stop
document path: received N typed blocks (...)
document path=blocks kind=pdf file=...
document pdf engine=chromium (0.4s, ... bytes)
document verify ok=True kind=pdf pages=2 ... path=blocks
document html deck from N typed blocks        (only when an HTML deck was also asked for)
```

A healthy PowerPoint run:

```
document path: calling Document LLM for typed slides (kind=pptx, ...)
document path: received N typed slides (bullets, chart, stats, table, ...)
document path=slides kind=pptx file=...
document pptx path=slides slides_in=N (0.1s, ... bytes)
document verify ok=True kind=pptx pages=M ... path=slides
```

`document slides: ... dropped` lines name stat cards, charts or quotes the
notes did not back; `slides were unusable -> notes-driven deck` and
`path=legacy_pptx` mean the old deck was used.

Every non-streaming model call logs one `llm stage=` line, so a slow job
shows which stage, provider and token split cost the time.

## Document LLM latency (measured)

Same Document prompt, `deepseek/deepseek-v4-flash` via OpenRouter, 8 calls:

| variant | seconds | provider | tok/s | reasoning tokens |
|---|---|---|---|---|
| default | 26, 52 | DigitalOcean, OpenInference | 47, 21 | 0, 0 |
| `reasoning.effort=low` | 145, 137 | OpenInference | 26, 22 | 2533, 1988 |
| `provider.sort=throughput` | 65, 60 | OpenInference | 25, 21 | 0, 0 |
| `provider.order=[DigitalOcean]` | 69, 46 | DigitalOcean | 24, 36 | 0, 0 |

The time is set by the serving provider's throughput at that moment
(21-47 tok/s) and by how much the model writes (1.1k-1.7k tokens). By
default this model does no reasoning on this prompt; `effort=low` switches
reasoning on and makes the call 2-3x slower. Sorting or pinning providers
gave no consistent gain, so both `DOCUMENT_LLM_*` settings default to empty.
Re-measure with the `llm stage=document_agent` log line when the model changes.

Warnings that mean the fallback was used: `EMPTY reply`, `had no blocks key`,
`unparseable JSON (... saved: <path>)`, `write-up too short`,
`engine=chromium FAILED`, `document verify FAILED`.

## History

DOCX/PPTX were once rendered through a Carbone (Node.js) subprocess with
maintained templates. That path had been unused since the python-docx /
python-pptx writers replaced it, and was removed together with Node.js from
the backend image.
