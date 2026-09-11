"""Shared pytest configuration for the backend test suite."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# tests/ lives one level under backend/; app modules import as top-level names.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# Many tests import main/db at collection time, so DATA_DIR has to be settled
# before that.
#
# This used to be setdefault, which meant "keep whatever DATA_DIR is already
# set" -- and inside the running container that is /data, the live app
# database. Running the suite there wrote the pipeline tests' fixture
# conversations ("merhaba"/"selam", "Süper Lig 2026 puan durumu", the
# notes.pdf follow-ups) straight into the user's real chat history, where
# they showed up in the UI as if the app had produced them. Two of those
# tests also failed on a FOREIGN KEY violation from leftover rows.
#
# A test run must never be able to reach real data, so the isolated
# directory now wins over an inherited one. The escape hatch stays explicit
# for anyone who genuinely needs to point the suite at a real directory.
if os.environ.get("PYTEST_USE_REAL_DATA_DIR") != "1":
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="agent_webapp_pytest_")
