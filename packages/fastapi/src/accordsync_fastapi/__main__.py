"""`python -m accordsync_fastapi migrate|compact`: the server's CLI (`python -m accordsync_server`).

`migrate` needs ACCORD_DATABASE_URL (or --database-url); `compact` also needs the definition,
`--definition module:attribute` (or ACCORD_SERVER).
"""

import sys

from accordsync_server.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
