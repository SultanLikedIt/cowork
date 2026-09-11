"""Upload each sample file in sample_files/ to the running backend and
print the extracted text, so file-type support can be checked by eye.

Requires the server to already be running, e.g.:
    uvicorn main:app --reload

Run:
    python scripts/upload_smoke.py
"""
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:8000"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "sample_files"

# Listed for all 5 supported types; any not yet present under sample_files/
# are skipped with a note rather than failing the run.
SAMPLE_FILENAMES = [
    "sample.txt",
    "sample.csv",
    "sample.docx",
    "sample.xlsx",
    "sample.pdf",
]


def check_server_running() -> bool:
    try:
        requests.get(f"{BASE_URL}/docs", timeout=3)
        return True
    except requests.ConnectionError:
        return False


def upload_file(path: Path) -> dict:
    with open(path, "rb") as f:
        response = requests.post(f"{BASE_URL}/upload", files={"file": (path.name, f)})
    response.raise_for_status()
    return response.json()


def main():
    if not check_server_running():
        print(
            f"Could not reach {BASE_URL}. Start the server first, e.g.:\n"
            f"  uvicorn main:app --reload\n"
        )
        return

    for filename in SAMPLE_FILENAMES:
        path = SAMPLES_DIR / filename
        print(f"=== {filename} ===")
        if not path.exists():
            print(f"  (skipped: {path} not found)\n")
            continue

        try:
            data = upload_file(path)
        except requests.HTTPError as exc:
            print(f"  FAILED ({exc.response.status_code}): {exc.response.text}\n")
            continue

        print(f"  file_id: {data['file_id']}")
        print(f"  word_count: {data['word_count']} (truncated: {data['truncated']})")
        print("  extracted_text:")
        for line in data["extracted_text"].splitlines():
            print(f"    {line}")
        print()


if __name__ == "__main__":
    main()
