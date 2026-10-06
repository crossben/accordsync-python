"""The mixed-server fleet needs PostgreSQL (ACCORD_DATABASE_URL) and an Accord workspace
(ACCORD_APP_DIR); see server-interop/run.sh. Not part of the root `uv run pytest`."""

import sys
from pathlib import Path

# `--import-mode=importlib` does not put this directory on sys.path; the tests import `fleet`.
sys.path.insert(0, str(Path(__file__).parent))
