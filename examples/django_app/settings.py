"""Settings for the example Django project serving Accord with the conformance profile."""

import os

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "example-only-not-secret")
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
INSTALLED_APPS = ["accordsync_django"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
ROOT_URLCONF = "urls"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "unused",
        "ATOMIC_REQUESTS": True,  # the Accord views opt out (non_atomic_requests)
    }
}
ACCORD_SERVER = "definition:definition"
ACCORD_DATABASE_URL = os.environ.get("ACCORD_DATABASE_URL")
USE_TZ = True
