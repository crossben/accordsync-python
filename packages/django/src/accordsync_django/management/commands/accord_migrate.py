from typing import Any

from accordsync_server import migrate
from django.core.management.base import BaseCommand

from ...conf import database_url


class Command(BaseCommand):
    help = "Applies the Accord migrations to the sync database (shared ledger with every server)."

    def handle(self, *args: Any, **options: Any) -> None:
        ran = migrate(database_url())
        self.stdout.write(
            f"applied {len(ran)} migration(s): {', '.join(ran)}" if ran else "up to date"
        )
