"""The interop tests need the server from `interop/run.sh`; they are not part of the root
`uv run pytest` (whose testpaths is `packages`)."""

import sys
from pathlib import Path

# `--import-mode=importlib` does not put this directory on sys.path; the tests import `support`.
sys.path.insert(0, str(Path(__file__).parent))
