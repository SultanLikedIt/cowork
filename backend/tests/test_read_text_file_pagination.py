"""Tests for read_text_file's offset-based pagination (workspace.py).

Regression coverage for the failure where a Document Specialist run against
a large .xlsx.extracted.txt companion file re-read the same 20,000-char
prefix 12 times: read_text_file had no way to advance past the first page,
and the prompt wrongly implied one call always returned the complete file.

No model or network access. Run directly:
    python test_read_text_file_pagination.py
"""

from tests_support.file_pagination import run_read_text_file_pagination_checks

failures: list[str] = []


def check(name, condition, detail=""):
    print(
        f"[{'PASS' if condition else 'FAIL'}] {name}"
        + (f" -- {detail}" if not condition else "")
    )
    if not condition:
        failures.append(name)


def main() -> None:
    run_read_text_file_pagination_checks(check)
    print(f"\n{'FAILED -- see above' if failures else 'All read_text_file pagination tests passed'}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
