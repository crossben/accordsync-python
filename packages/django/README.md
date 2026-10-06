# accordsync-django

The Accord sync server for Django: an app with the `/v1/push`, `/v1/pull` and `/health` URLs,
management commands `accord_migrate` and `accord_compact`, and a system check. All behaviour
comes from `accordsync-server`; this package only routes to it. Part of
[Accord](https://accord.benhattab.pro).

## Install

```sh
pip install accordsync-django
```

## Configure

```python
# settings.py
INSTALLED_APPS = [..., "accordsync_django"]
ACCORD_SERVER = "myapp.sync:server"  # a ServerDefinition made with define_server(...)
ACCORD_DATABASE_URL = "postgresql://..."  # optional, see below
ACCORD_POOL_SIZE = 20  # optional
ACCORD_SCHEDULE_COMPACTION = True  # optional

# urls.py
urlpatterns = [path("sync/", include("accordsync_django.urls")), ...]
```

The database is `ACCORD_DATABASE_URL` (setting, else environment variable), or else the
`default` database's settings, which must then use `django.db.backends.postgresql`. The sync
server uses its own psycopg connection pool, opened on the first request, never Django's ORM
connections. The views are CSRF-exempt, opt out of `ATOMIC_REQUESTS`, and need no session or auth
middleware (requests authenticate with the JWT in `Authorization`).

`manage.py check` reports `accordsync.E001` when `ACCORD_SERVER` is missing, cannot be imported or
is not a `ServerDefinition`, and `accordsync.E002` when no PostgreSQL database is configured.

```sh
python manage.py accord_migrate
```

## Compaction

With `compaction.interval_ms > 0` in the definition (default one hour), each process compacts on
that interval in a background thread started by its first sync request. With several worker
processes, set `ACCORD_SCHEDULE_COMPACTION = False` and run from cron:

```sh
python manage.py accord_compact
```

## Notes

- Serve with a threaded WSGI server (e.g. waitress, gunicorn `--threads`); each request holds a
  thread while it waits on PostgreSQL.
- Rate limits are per process (in memory).
- `examples/django_app` in this repository serves the conformance profile with waitress and
  passes the shared server conformance suite (63/63). Its control API is for tests only
  (`ACCORD_CONTROL=1`).
