"""The conformance profile's definition (shared with `tools/conformance_server.py`)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
os.environ.setdefault(
    "ACCORD_PROFILE",
    str(Path(__file__).resolve().parents[2] / "contract" / "conformance" / "profile.json"),
)

from conformance_server import definition

__all__ = ["definition"]
