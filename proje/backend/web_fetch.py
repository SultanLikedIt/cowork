"""Bounded web-content post-processing shared by web_tools.web_read.

The active network and crawl engine is Crawl4AI (see crawl4ai_engine.py) --
this module does not talk to the network at all. What lives here is the
infrastructure web_tools.WebTools.web_read reuses once Crawl4AI has already
rendered a page: table/record shaping and materialization to disk
(_table_payloads, _shape_tables, _save_tables, _save_records), and the
per-run text-size budget (_spend_text). Deciding which table matters, which
result is relevant, or navigating to another page is the agent's job, not
this module's -- see web_tools.py's module docstring for that split.

TableTransformTools (filter/select/sort a saved dataset without reading its
rows into model context) is unrelated infrastructure that happens to live in
this file; it never touches the network either.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse, urlunparse

# artifacts does not import this module, so there is no cycle.
from artifacts import ARTIFACTS_DIRNAME, INTERNAL_DIRNAME

# --- Bounds ---------------------------------------------------------------
MAX_TABLE_ROWS = 2_000
MAX_TABLE_COLUMNS = 64

# Rows of a table returned INLINE to the model. Everything beyond this stays
# in the file _save_tables writes.
#
# This number is small on purpose. A tool result is not read once -- CAMEL
# keeps it in the agent's memory and resends it on every subsequent turn, so a
# large result is paid for once per remaining turn.
#
# 25 rows is enough to see the shape, the header, the value formats and the
# obvious data-quality problems -- everything a model needs in order to write
# code against the data or decide which columns matter. It is nowhere near
# enough to be a way of READING the dataset, which is the point: the dataset
# goes file -> file, never through a completion.
MAX_INLINE_TABLE_ROWS = 25

# Total characters of page text this run may put into model context, across
# every web_read call. Same per-run-budget pattern as
# workspace.MAX_TEXT_CHARS_PER_RUN, and for the same reason: a per-call bound
# stops any ONE call being huge, but a model can still make many
# individually-legal calls until most of a site is in its context.
MAX_TEXT_CHARS_PER_RUN = 120_000

# Where extracted tables are parked. Inside the artifacts tree so it is
# workspace-confined and cleaned up with the job, but in its own subdirectory
# so intermediate scrape output is not presented to the user as a deliverable.
# Kept identical to artifacts.INTERNAL_DIRNAME, which is what excludes this
# directory from the user-facing deliverables listing.
EXTRACTED_DIRNAME = INTERNAL_DIRNAME

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]}]+", re.IGNORECASE)


def find_urls(text: str) -> list[str]:
    """Every http(s) URL in a piece of text, de-duplicated, order preserved.

    Used by the capability resolver to detect that a request names a specific
    page (and therefore needs web_fetch, not just web_search) without an LLM
    call. Trailing punctuation is stripped because URLs in prose are usually
    followed by a period or comma.
    """
    seen: set[str] = set()
    found: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


def _normalize_url(url: str) -> str:
    """Canonical form used for duplicate detection.

    Drops the fragment (``#section`` is the same document) and a trailing
    slash on the path. Query strings are kept -- ``?month=8`` is genuinely a
    different page.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, "")
    )


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _year_tokens(text: str) -> frozenset[str]:
    return frozenset(_YEAR_RE.findall(text or ""))


def _tables_may_combine(metadata: Iterable[dict]) -> bool:
    """Whether same-header tables describe the same time period.

    Same-header tables are normally fragments of one paginated list (months
    of a year, pages of a list) and are safe to combine. They are NOT safe to
    combine when their page title/context/URL disagree on which year the
    data is for -- e.g. two football seasons, or the same calendar month a
    year apart, sharing the identical column layout would otherwise blend
    two distinct datasets into one.

    A year mentioned on only one side is not a conflict: the common pattern
    is a list's year stated once (its first/index page) and every later
    paginated page carrying no year at all. Only two DIFFERING non-empty
    year sets count as a conflict -- this is the single check both the
    table-selector and the table saver use to tell the two cases apart.
    """
    # table_context (a table's own <caption>/nearest heading) is checked
    # FIRST and ALONE when present, not blended into the page-wide fields:
    # page_context is shared cleaned prose for the whole page, so on a
    # season-tab page whose body text naturally contains every season
    # heading (e.g. "2022-2023 Sezonu 2025-2026 Sezonu"), blending it in
    # would put the SAME union of years on every table again, silently
    # erasing the very distinction table_context exists to provide. Only
    # fall back to the page-wide fields when a table has no caption of its
    # own to go by (e.g. recovered from embedded JSON, no DOM position).
    year_sets = []
    for item in metadata:
        table_years = _year_tokens(str(item.get("table_context") or ""))
        if table_years:
            year_sets.append(table_years)
            continue
        year_sets.append(
            _year_tokens(
                " ".join(
                    [
                        str(item.get("page_title") or ""),
                        str(item.get("page_context") or ""),
                        str(item.get("url") or ""),
                    ]
                )
            )
        )
    for index, years_a in enumerate(year_sets):
        for years_b in year_sets[index + 1 :]:
            if years_a and years_b and years_a != years_b:
                return False
    return True


class WebFetchTools:
    """Table/record shaping and per-run text budget for web_read.

    One instance per run (capabilities.resolve_tools), which is what makes
    the per-run text budget (MAX_TEXT_CHARS_PER_RUN) real rather than
    per-call decoration.
    """

    def __init__(self, workspace_root=None) -> None:
        self._text_chars_delivered = 0
        # Optional: without a workspace, extracted tables can only be returned
        # inline (bounded), because there is nowhere confined to write them.
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else None

    def _spend_text(self, text: str) -> tuple[str, Optional[str]]:
        """Charge `text` against the per-run context budget, trimming if needed.

        Returns (text_to_return, note). Once the budget is spent the tools keep
        working but stop delivering page bodies -- structural results (titles,
        table counts, saved file paths) are unaffected, because those are small
        and are what the model needs to keep making progress.
        """
        remaining = MAX_TEXT_CHARS_PER_RUN - self._text_chars_delivered
        if remaining <= 0:
            return "", (
                "This run's page-text budget "
                f"({MAX_TEXT_CHARS_PER_RUN} characters) is fully used, so the "
                "body of this page was not returned. Work from what you have "
                "already retrieved, or use the saved data files."
            )
        if len(text) <= remaining:
            self._text_chars_delivered += len(text)
            return text, None
        self._text_chars_delivered = MAX_TEXT_CHARS_PER_RUN
        return text[:remaining], (
            "Trimmed: this run's page-text budget is now fully used."
        )

    def _save_tables(
        self,
        url: str,
        tables: list[dict],
        *,
        preferred_table_index: Optional[int] = None,
        safe_to_combine: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Write the FULL extracted tables to a workspace file.

        This is the same handoff the artifact tools use: the data goes from
        the network to a file without passing through a model response, and
        the model then references it by path. It is what makes
        "URL -> table -> spreadsheet" cost a constant number of tokens
        regardless of how many rows the table has.
        """
        if self._workspace_root is None:
            return None
        try:
            directory = self._workspace_root / ARTIFACTS_DIRNAME / EXTRACTED_DIRNAME
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", urlparse(url).path or "page").strip("-")
            slug = (slug or "page")[:60]

            # Records rather than rows+header: this is the shape
            # create_xlsx_artifact(from_path=...), create_csv_artifact
            # (from_path=...), create_web_bundle(data_path=...) and pandas
            # all consume directly.
            per_table: list[tuple[list[str], list[dict[str, Any]]]] = []
            for table in tables:
                header = table["header"] or [
                    f"column_{i + 1}" for i in range(table["column_count"])
                ]
                per_table.append(
                    (header, [dict(zip(header, row)) for row in table["_all_rows"]])
                )

            def _write(name: str, records: list[dict[str, Any]]) -> Path:
                path = directory / name
                path.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return path

            # `saved_to` must ALWAYS be a plain records array. It previously
            # became a {table_index, columns, records} envelope as soon as a
            # page had more than one table -- and a "yearly" page is twelve
            # monthly tables. from_path then produced a 3-row spreadsheet
            # whose columns were literally table_index/columns/records, so
            # the model abandoned the reference path and started paging the
            # rows back through its own context to build the file by hand.
            # That is the single largest source of token amplification this
            # subsystem had.
            headers = [header for header, _ in per_table]
            uniform = len(per_table) > 1 and all(h == headers[0] for h in headers)
            members: list[dict[str, Any]] = []
            if len(per_table) > 1:
                for (header, records), table in zip(per_table, tables):
                    member = _write(
                        f"{slug}-table{table['table_index']}.json", records
                    )
                    members.append(
                        {
                            "table_index": table["table_index"],
                            "path": member.relative_to(self._workspace_root).as_posix(),
                            "row_count": len(records),
                        }
                    )

            preferred = next(
                (
                    index
                    for index, table in enumerate(tables)
                    if table["table_index"] == preferred_table_index
                ),
                None,
            )
            if preferred is not None and len(per_table) > 1:
                # The acquisition controller has already selected this table
                # from the actually extracted candidates using the research
                # need and bounded table metadata. Keep that decision through
                # materialization instead of silently replacing it with the
                # largest table (or combining semantically distinct tables
                # that happen to share columns).
                target = _write(
                    f"{slug}-tables.json", per_table[preferred][1]
                )
                primary_rows = len(per_table[preferred][1])
                combined_note = (
                    f"This page has {len(per_table)} extracted tables. "
                    f"`saved_to` holds the table selected for the research "
                    f"need (table_index "
                    f"{tables[preferred]['table_index']}, "
                    f"{primary_rows} rows). Other extracted tables remain "
                    "available in `tables_saved`."
                )
            elif uniform and safe_to_combine:
                # Same columns in every table AND no conflicting year across
                # them: the page is one dataset split up for presentation
                # (months of a year, pages of a list), so the combined file
                # is what the user actually asked for. Same columns with a
                # year conflict fall through to the branch below instead --
                # see _tables_may_combine.
                combined = [record for _, records in per_table for record in records]
                target = _write(f"{slug}-tables.json", combined)
                primary_rows = len(combined)
                combined_note = (
                    f"{len(per_table)} tables on this page share the same "
                    "columns and have been combined in order into one "
                    "dataset."
                )
            elif len(per_table) > 1:
                # Genuinely different tables, OR same columns but with a year
                # conflict (uniform and not safe_to_combine -- e.g. a
                # season-selector tab or a year archive republishing the
                # identical column layout for a different dataset): pick the
                # largest as the default target so from_path always works,
                # and list the rest so the model can choose a different one
                # WITHOUT reading any of it.
                best = max(range(len(per_table)), key=lambda i: len(per_table[i][1]))
                target = _write(f"{slug}-tables.json", per_table[best][1])
                primary_rows = len(per_table[best][1])
                if uniform:
                    reason = (
                        f"{len(per_table)} tables share the same columns but "
                        "disagree on which year the data is for (e.g. "
                        "different seasons or archive years), so they were "
                        "not combined"
                    )
                else:
                    reason = (
                        f"This page has {len(per_table)} tables with "
                        "DIFFERENT columns, so they were not combined"
                    )
                combined_note = (
                    f"{reason}. `saved_to` holds the largest one (table_index "
                    f"{tables[best]['table_index']}, {primary_rows} rows). "
                    "If you need a different one, use its path from "
                    "`tables_saved` -- do not read any of these files."
                )
            else:
                target = _write(f"{slug}-tables.json", per_table[0][1])
                primary_rows = len(per_table[0][1])
                combined_note = ""

            relative = target.relative_to(self._workspace_root).as_posix()
            saved: dict[str, Any] = {
                "saved_to": relative,
                "saved_rows": primary_rows,
                "saved_bytes": target.stat().st_size,
                "instruction": (
                    f"The COMPLETE extracted data ({primary_rows} rows) is "
                    f"already in {relative}, as a JSON array of records. Pass "
                    "that path straight to create_xlsx_artifact(from_path="
                    "...), create_csv_artifact(from_path=...) or "
                    "create_web_bundle(data_path=...). Do NOT open, read, "
                    "page through or re-type this file -- it exists so the "
                    "rows never have to pass through your context."
                ),
            }
            if combined_note:
                saved["combined"] = combined_note
            if members:
                saved["tables_saved"] = members
            return saved
        except (OSError, ValueError, TypeError):
            # Saving is an optimization; a failure must not lose the
            # extraction the caller already paid for.
            return None

    def _save_records(
        self, url: str, records: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """Write aggregated records to the same reference file shape as tables.

        A research session's records come from several pages, so there is no
        single "table" to save -- but the consumer contract is identical: a
        JSON array of flat records that create_xlsx_artifact(from_path=...)
        and friends read directly, so the data never passes through a model
        response.
        """
        if self._workspace_root is None or not records:
            return None
        try:
            directory = self._workspace_root / ARTIFACTS_DIRNAME / EXTRACTED_DIRNAME
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", urlparse(url).netloc or "records")
            slug = (slug.strip("-") or "records")[:60]
            path = directory / f"{slug}-records.json"
            path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            relative = path.relative_to(self._workspace_root).as_posix()
            return {
                "saved_to": relative,
                "saved_rows": len(records),
                "saved_bytes": path.stat().st_size,
            }
        except (OSError, ValueError, TypeError):
            return None

    def _table_payloads(
        self,
        page: Any,
        selected: Optional[list[tuple[int, list[list[str]]]]] = None,
        preview_cap: int = MAX_INLINE_TABLE_ROWS,
    ) -> list[dict[str, Any]]:
        """Shape parsed HTML tables once for every acquisition path.

        Shared by web_read's single- and multi-table shaping so both use
        the same header inference, row/column limits, shared preview
        budget, and private ``_all_rows`` handoff to ``_save_tables``.
        """
        chosen = selected if selected is not None else list(enumerate(page.tables))
        tables: list[dict[str, Any]] = []
        preview_budget = MAX_INLINE_TABLE_ROWS
        cap = max(1, min(int(preview_cap or MAX_INLINE_TABLE_ROWS), MAX_INLINE_TABLE_ROWS))
        for position, table in chosen:
            if not table:
                continue
            header: list[str] = []
            body = table
            first = [str(cell or "").strip() for cell in table[0]]
            nonempty = [cell for cell in first if cell]
            alpha_cells = sum(
                1 for cell in nonempty if any(char.isalpha() for char in cell)
            )
            # Presentation tables often reserve blank columns for a logo,
            # rank-change arrow, or status icon. Requiring every header cell
            # to be non-empty erased useful names such as Team/Played/Points.
            # Requiring predominantly textual labels still avoids treating an
            # ordinary data row (one name plus many numbers) as a header.
            looks_like_header = bool(
                len(table) > 1
                and nonempty
                and alpha_cells >= max(2, (len(nonempty) + 1) // 2)
            )
            if looks_like_header:
                seen_names: dict[str, int] = {}
                for index, value in enumerate(first):
                    base = value or f"column_{index + 1}"
                    count = seen_names.get(base, 0) + 1
                    seen_names[base] = count
                    header.append(base if count == 1 else f"{base}_{count}")
                body = table[1:]
            trimmed = [row[:MAX_TABLE_COLUMNS] for row in body[:MAX_TABLE_ROWS]]
            allowance = max(0, min(cap, preview_budget))
            preview = trimmed[:allowance]
            preview_budget -= len(preview)
            tables.append(
                {
                    "table_index": position,
                    "header": header,
                    "rows": preview,
                    "row_count": len(body),
                    "preview_rows": len(preview),
                    "column_count": max((len(row) for row in table), default=0),
                    # Whether THIS truncation (body[:MAX_TABLE_ROWS] above) cut
                    # real rows -- not the upstream page's own rows_dropped
                    # flag, which no acquisition path actually sets (that flag
                    # is html_extract.py's own, much larger, MAX_TABLE_ROWS;
                    # relying on it here silently under-reported truncation
                    # that happened at THIS module's tighter cap instead).
                    "rows_dropped": len(body) > MAX_TABLE_ROWS,
                    # Consumed by _save_tables, then removed before returning.
                    "_all_rows": trimmed,
                }
            )
        return tables

    def _shape_tables(
        self,
        final_url: str,
        page: Any,
        *,
        preferred_table_index: Optional[int] = None,
        safe_to_combine: bool = True,
    ) -> dict[str, Any]:
        """Table extraction for an ALREADY-parsed page, in acquisition shape.

        Flattened for web_read, which reports one dataset rather than a
        per-table inventory.

        ``safe_to_combine`` defaults to True; web_read (single page, still
        possibly several tables) passes the real value -- see
        ``_tables_may_combine``.
        """
        tables = self._table_payloads(page)

        saved = (
            self._save_tables(
                final_url,
                tables,
                preferred_table_index=preferred_table_index,
                safe_to_combine=safe_to_combine,
            )
            if tables
            else None
        )
        selected = next(
            (
                table
                for table in tables
                if table["table_index"] == preferred_table_index
            ),
            None,
        )
        for table in tables:
            table.pop("_all_rows", None)

        result: dict[str, Any] = {
            "total_rows": (
                selected["row_count"]
                if selected is not None
                else sum(table["row_count"] for table in tables)
            ),
            "columns": (
                selected["header"]
                if selected is not None
                else (tables[0]["header"] if tables else [])
            ),
            "preview": (
                selected["rows"][:MAX_INLINE_TABLE_ROWS]
                if selected is not None
                else [row for table in tables for row in table["rows"]][
                    :MAX_INLINE_TABLE_ROWS
                ]
            ),
            "table_count": len(tables),
        }
        if selected is not None:
            result["selected_table_index"] = selected["table_index"]
        if saved:
            result["data_path"] = saved["saved_to"]
            for key in ("saved_rows", "combined", "tables_saved"):
                if key in saved:
                    result[key] = saved[key]
        if page.rows_dropped or any(table.get("rows_dropped") for table in tables):
            result["row_limit_reached"] = True
            result["incomplete_warning"] = (
                f"This page has more rows than the {MAX_TABLE_ROWS}-row "
                "per-table extraction limit, so the data captured is "
                "INCOMPLETE. Say so plainly rather than presenting it as the "
                "full dataset."
            )
        return result


class TableTransformTools:
    """Server-side structured-data transformation: filter, select, sort, limit.

    Operates entirely on server-side file references (data_path values produced
    by web_read, upload extraction, or a previous transform_table call).
    No dataset rows ever enter model context; large datasets flow directly from
    one path to the next and finally into artifact tools.

    Supported formats: JSON (array of objects), CSV, XLSX (first sheet).
    """

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self._workspace_root = Path(workspace_root) if workspace_root else None

    def transform_table(
        self,
        from_path: str,
        filters: Optional[Dict[str, Any]] = None,
        select_columns: Optional[list] = None,
        sort_by: Optional[str] = None,
        sort_desc: bool = False,
        limit: Optional[int] = None,
        dedupe: bool = False,
    ) -> Dict[str, Any]:
        """Filter, select, sort and/or limit a server-side structured dataset.

        The result is written to a new server-side file and returned as a
        ``data_path`` that can be handed directly to ``create_xlsx_artifact``
        or another ``transform_table`` call.  No row data is returned in the
        response body.

        Args:
            from_path: Relative path (within the workspace) of the source
                dataset -- a ``data_path`` from ``web_read``, an upload
                extraction, or a previous ``transform_table`` result.
            filters: Optional equality / membership / containment filters.
                Each key is a column name; each value is either a literal
                scalar (exact match) or a dict with keys ``op`` and ``value``
                where ``op`` is one of: ``eq``, ``neq``, ``contains``,
                ``startswith``, ``gt``, ``gte``, ``lt``, ``lte``.
                Example: ``{"City": "Ankara", "Month": {"op": "gte", "value": 8}}``
            select_columns: If given, keep only these columns in the output.
            sort_by: Column name to sort by.
            sort_desc: Sort descending when ``True``. (default: :obj:`False`)
            limit: Maximum rows to keep (applied after filtering and sorting).
            dedupe: Drop exact-duplicate rows when ``True``.

        Returns:
            ``{"ok": True, "data_path": "...", "row_count": N}`` on success, or
            ``{"ok": False, "error": "..."}`` on failure.
        """
        if not from_path:
            return {"ok": False, "error": "from_path is required."}
        try:
            import pandas as pd
        except ImportError:
            return {"ok": False, "error": "pandas is not installed."}

        # Resolve path relative to workspace root.
        src = Path(from_path)
        if not src.is_absolute() and self._workspace_root:
            src = self._workspace_root / src
        if not src.exists():
            return {"ok": False, "error": f"Source file not found: {from_path}"}

        try:
            suffix = src.suffix.lower()
            if suffix == ".json":
                df = pd.read_json(src, orient="records")
            elif suffix == ".csv":
                df = pd.read_csv(src)
            elif suffix in {".xlsx", ".xls"}:
                df = pd.read_excel(src, sheet_name=0)
            else:
                return {
                    "ok": False,
                    "error": f"Unsupported format '{suffix}'. Use JSON, CSV or XLSX.",
                }
        except Exception as exc:
            return {"ok": False, "error": f"Failed to load source file: {exc}"}

        try:
            # Apply filters.
            if filters:
                for col, spec in filters.items():
                    if col not in df.columns:
                        continue  # skip unknown columns, don't hard-fail
                    if isinstance(spec, dict):
                        op = str(spec.get("op", "eq")).lower()
                        val = spec.get("value")
                        if op == "eq":
                            df = df[df[col] == val]
                        elif op == "neq":
                            df = df[df[col] != val]
                        elif op == "contains":
                            df = df[
                                df[col].astype(str).str.contains(str(val), na=False, case=False)
                            ]
                        elif op == "startswith":
                            df = df[df[col].astype(str).str.startswith(str(val), na=False)]
                        elif op == "gt":
                            df = df[pd.to_numeric(df[col], errors="coerce") > float(val)]
                        elif op == "gte":
                            df = df[pd.to_numeric(df[col], errors="coerce") >= float(val)]
                        elif op == "lt":
                            df = df[pd.to_numeric(df[col], errors="coerce") < float(val)]
                        elif op == "lte":
                            df = df[pd.to_numeric(df[col], errors="coerce") <= float(val)]
                    else:
                        df = df[df[col] == spec]

            # Select columns.
            if select_columns:
                keep = [c for c in select_columns if c in df.columns]
                if keep:
                    df = df[keep]

            # Sort.
            if sort_by and sort_by in df.columns:
                df = df.sort_values(by=sort_by, ascending=not sort_desc)

            # Dedupe.
            if dedupe:
                df = df.drop_duplicates()

            # Limit.
            if limit and limit > 0:
                df = df.iloc[:limit]

        except Exception as exc:
            return {"ok": False, "error": f"Transform operation failed: {exc}"}

        # Write output to workspace.
        if self._workspace_root is None:
            return {"ok": False, "error": "No workspace root; cannot save result."}
        try:
            import re as _re
            out_dir = self._workspace_root / "artifacts" / "extracted"
            out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            stem = _re.sub(r"[^a-zA-Z0-9]+", "-", src.stem)[:40]
            out_path = out_dir / f"{stem}-filtered.json"
            # Write as JSON array (same format create_xlsx_artifact reads).
            out_path.write_text(
                df.to_json(orient="records", force_ascii=False, indent=2),
                encoding="utf-8",
            )
            rel = out_path.relative_to(self._workspace_root).as_posix()
            return {
                "ok": True,
                "data_path": rel,
                "row_count": len(df),
                "columns": list(df.columns),
                "instruction": (
                    f"{len(df)} rows written to {rel}. "
                    "Pass data_path directly to create_xlsx_artifact or another "
                    "transform_table call. Do not read or print the rows."
                ),
            }
        except Exception as exc:
            return {"ok": False, "error": f"Failed to save result: {exc}"}

    def as_tools(self) -> list:
        return [self.transform_table]


__all__ = [
    "WebFetchTools",
    "TableTransformTools",
    "find_urls",
]
