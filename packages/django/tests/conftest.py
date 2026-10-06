import sys
from pathlib import Path

import django
from django.conf import settings

sys.path.insert(0, str(Path(__file__).parent))

if not settings.configured:
    settings.configure(
        INSTALLED_APPS=["accordsync_django"],
        ROOT_URLCONF="accordsync_django.urls",
        ALLOWED_HOSTS=["testserver"],
        DATABASES={},
        MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware"],
        ACCORD_SERVER="accordsync_django_testdef:definition",
    )
    django.setup()
