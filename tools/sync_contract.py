"""Refreshes contract/ (golden vectors and protocol schemas) from the Accord repository.

    python tools/sync_contract.py            copy from ../app (ACCORD_APP_DIR to override)
    python tools/sync_contract.py --check    fail if contract/ differs from the repository (CI)

The contract is committed, so this repository builds and tests alone. A new vector or conformance
test in the Accord repository must pass here before the next Python release.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARTS = ["vectors", "protocol/v1"]


def json_files(directory: Path) -> dict[str, bytes]:
    """Every .json file under `directory`, by path relative to it."""
    if not directory.is_dir():
        return {}
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*.json"))
        if p.is_file()
    }


def main() -> int:
    check = "--check" in sys.argv[1:]
    app = Path(os.environ.get("ACCORD_APP_DIR", ROOT.parent / "app"))
    if not (app / "vectors" / "lww.json").is_file():
        print(f"sync_contract: no Accord repository at {app}.", file=sys.stderr)
        print("Set ACCORD_APP_DIR to its path.", file=sys.stderr)
        return 1

    problems: list[str] = []
    for part in PARTS:
        source = json_files(app / part)
        current = json_files(ROOT / "contract" / part)
        for name in sorted(source.keys() | current.keys()):
            if source.get(name) == current.get(name):
                continue
            problems.append(f"{part}/{name}")
            if check:
                continue
            target = ROOT / "contract" / part / name
            if name not in source:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source[name])

    if check:
        if problems:
            print("contract/ is behind the Accord repository:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            print("Run tools/sync_contract.py, make the tests pass, and commit.", file=sys.stderr)
            return 1
        print("contract/ matches the Accord repository.")
        return 0

    commit = subprocess.run(  # noqa: S603
        ["git", "-C", str(app), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    (ROOT / "contract" / "SOURCE").write_text(
        f"crossben/accordsync {commit or '(commit unknown)'}\n", encoding="utf-8"
    )
    print(f"Updated {len(problems)} file(s)." if problems else "contract/ already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
