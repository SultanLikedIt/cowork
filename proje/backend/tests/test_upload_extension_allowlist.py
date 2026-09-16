"""Regression: upload allowlist must match what the workspace can read.

Reliability triage (rel-20260813T105420Z-2a702b3b) found live.file.
md-json-yaml / logs-code / mixed-six all failed with HTTP 400
"Unsupported file type '.md'"/'.log' at upload time, even though
workspace.py's read_text_file/search_text already fully support every
extension in WORKSPACE_TEXT_EXTENSIONS once a file is inside a workspace.
main.ALLOWED_EXTENSIONS had simply never been widened to match.

Fix: main.ALLOWED_EXTENSIONS now unions in workspace.WORKSPACE_TEXT_
EXTENSIONS directly, so the two sets cannot drift apart again.

No live server, no network -- _read_uploads only needs an object with a
`.filename` attribute and an async `.read()` method, so a minimal fake
stands in for FastAPI's UploadFile. Run directly:
    python test_upload_extension_allowlist.py
"""

import asyncio
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import main  # noqa: E402
import workspace  # noqa: E402
from fastapi import HTTPException  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


class _FakeUploadFile:
    def __init__(self, filename: str, content: bytes = b"hello") -> None:
        self.filename = filename
        self._content = content

    async def read(self, _max_bytes: int) -> bytes:
        return self._content


def _accepts(filename: str) -> bool:
    """True if _read_uploads lets this filename through the extension gate."""
    try:
        asyncio.run(main._read_uploads([_FakeUploadFile(filename)]))
        return True
    except HTTPException as exc:
        if exc.status_code == 400 and "Unsupported file type" in str(exc.detail):
            return False
        raise


def test_allowlist_is_derived_from_workspace_text_extensions() -> None:
    check(
        "ALLOWED_EXTENSIONS is exactly DOCUMENT_EXTENSIONS | WORKSPACE_TEXT_EXTENSIONS | {.zip}",
        main.ALLOWED_EXTENSIONS
        == main.DOCUMENT_EXTENSIONS | workspace.WORKSPACE_TEXT_EXTENSIONS | {".zip"},
        sorted(main.ALLOWED_EXTENSIONS),
    )
    check(
        "the workspace's own text-extension set is a strict subset (single source of truth)",
        workspace.WORKSPACE_TEXT_EXTENSIONS <= main.ALLOWED_EXTENSIONS,
    )


def test_previously_rejected_extensions_are_now_accepted() -> None:
    for ext, filename in (
        (".md", "readme.md"),
        (".log", "app.log"),
        (".json", "config.json"),
        (".yaml", "config.yaml"),
        (".yml", "config.yml"),
    ):
        check(
            f"{ext} upload is no longer rejected at the extension gate",
            _accepts(filename),
            filename,
        )


def test_original_document_extensions_still_accepted() -> None:
    for filename in ("report.pdf", "brief.docx", "data.xlsx", "data.csv", "notes.txt"):
        check(
            f"pre-existing document extension {filename!r} is still accepted",
            _accepts(filename),
            filename,
        )


def test_zip_still_accepted() -> None:
    check("zip archives are still accepted", _accepts("bundle.zip"))


def test_ocr_image_extensions_accepted() -> None:
    """Images are Docling documents read through OCR, not arbitrary binaries."""
    for filename in ("photo.png", "scan.jpg", "scan.jpeg", "fax.tiff", "page.webp"):
        check(
            f"OCR image extension {filename!r} is accepted",
            _accepts(filename),
            filename,
        )


def test_genuinely_unsupported_extensions_still_rejected() -> None:
    """Malformed/binary rejection behavior must be unchanged -- only
    extensions the workspace can actually read as text were added."""
    for filename in ("anim.gif", "archive.rar", "archive.7z", "app.exe", "data.bin"):
        check(
            f"unsupported binary extension {filename!r} is still rejected",
            not _accepts(filename),
            filename,
        )


def test_workspace_cannot_read_anything_newly_allowed() -> None:
    """Every extension the upload gate now accepts (beyond the original
    document set) must genuinely be one workspace.py's own tools can read
    -- no format was added that the workspace cannot actually handle."""
    newly_allowed = main.ALLOWED_EXTENSIONS - main.DOCUMENT_EXTENSIONS - {".zip"}
    check(
        "every newly-allowed extension is a real workspace-readable text extension",
        newly_allowed <= workspace.WORKSPACE_TEXT_EXTENSIONS,
        newly_allowed - workspace.WORKSPACE_TEXT_EXTENSIONS,
    )


def main_() -> None:
    test_allowlist_is_derived_from_workspace_text_extensions()
    test_previously_rejected_extensions_are_now_accepted()
    test_original_document_extensions_still_accepted()
    test_zip_still_accepted()
    test_ocr_image_extensions_accepted()
    test_genuinely_unsupported_extensions_still_rejected()
    test_workspace_cannot_read_anything_newly_allowed()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All upload-extension-allowlist checks passed")


if __name__ == "__main__":
    main_()
