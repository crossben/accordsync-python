from django.apps import AppConfig


class AccordConfig(AppConfig):
    name = "accordsync_django"
    label = "accordsync"
    verbose_name = "Accord sync"

    def ready(self) -> None:
        from . import checks  # noqa: F401  (registers the system check)
