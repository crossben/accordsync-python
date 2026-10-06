import json
from typing import Any

from accordsync_server import compact, create_pool
from django.core.management.base import BaseCommand

from ...conf import database_url, load_definition


class Command(BaseCommand):
    help = "Runs Accord log compaction once (schedule it with cron for multi-process deployments)."

    def handle(self, *args: Any, **options: Any) -> None:
        definition = load_definition()
        with create_pool(database_url(), size=2) as pool:
            self.stdout.write(json.dumps(compact(pool, definition)))
