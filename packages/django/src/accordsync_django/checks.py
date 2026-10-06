"""`manage.py check`: ACCORD_SERVER names a ServerDefinition and a PostgreSQL database is set."""

from __future__ import annotations

from typing import Any

from django.core.checks import CheckMessage, Error, register
from django.core.exceptions import ImproperlyConfigured

from .conf import database_url, load_definition


@register()
def check_accord(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    try:
        load_definition()
    except ImproperlyConfigured as e:
        errors.append(Error(str(e), id="accordsync.E001"))
    try:
        database_url()
    except ImproperlyConfigured as e:
        errors.append(Error(str(e), id="accordsync.E002"))
    return errors
