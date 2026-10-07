# accordsync

**The [Accord](https://accord.benhattab.pro) client for Python: local-first writes, background sync,
conflicts and refusals.**

A Python program becomes an Accord device like a phone or a browser: it writes to its own SQLite
database at once, online or not, and syncs with any Accord server (TypeScript, PHP or Python). It
speaks the same protocol and merges by the same rules as
[`@accordsync/client`](https://www.npmjs.com/package/@accordsync/client). Python 3.11+.

```sh
pip install accordsync
```

## Open the client

Declare the same schema as your server, then open the client with SQLite storage and the HTTP
transport.

```python
from accordsync import (
    AccordClient,
    HttpTransport,
    SqliteStorage,
    conflict,
    counter,
    define_schema,
    lww,
    set_,
)

schema = define_schema(
    {"dossier": {"agent": lww(), "visits": counter(), "docs": set_(), "status": conflict()}}
)


def get_token() -> str:
    return my_auth.current_jwt()  # your app's auth; called before every request


accord = AccordClient.open(
    schema=schema,
    storage=SqliteStorage("accord.db"),
    transport=HttpTransport("https://sync.example.com", get_token=get_token),
)
```

Leave `device_id` unset: the client generates one from a secure random source and stores it.
`SqliteStorage` takes a path or an open `sqlite3.Connection`; its tables are prefixed `accord_`, so
they can share your program's database. `MemoryStorage` keeps nothing on disk (tests).

## Write, then sync

Writes apply locally and are saved before they return. Sync on demand, or in the background.

```python
accord.assign("dossier:91", "agent", "awa")
accord.inc("dossier:91", "visits", 1)
accord.add("dossier:91", "docs", "photo-1.jpg")

accord.sync()  # one round: push the pending writes, pull everyone else's
accord.start()  # or sync in a background thread: after writes, every 30 s, backoff on errors

print(accord.read("dossier:91"))  # {'agent': 'awa', 'visits': 1, 'docs': ['photo-1.jpg']}
print(accord.status().pending)  # writes not yet acknowledged by the server

accord.close()  # stops background sync and closes the storage
```

## Events and refusals

```python
off = accord.on("change", lambda records: print("changed:", records))
accord.on("refused", lambda r: print(f"refused {r.record}.{r.field}: {r.reason}"))
accord.on("error", lambda e: print("sync failed, retrying:", e))
```

The events are `change` (records whose local state changed), `refused` (a write the server refused,
already rolled back on this device: tell the user), `synced`, `resync` and `error`. `on()` returns a
function that unsubscribes. Listeners run on the thread that caused the event.

## Conflicts

A `conflict()` field written concurrently on two devices keeps both values until someone decides.

```python
for c in accord.conflicts():
    print(c.record, c.field, [v.value for v in c.values])
    accord.resolve(c.record, c.field, c.values[0].value)
```

While conflicted, `read()` shows the field as `{"conflicted": [{"value": …, "opId": …}, …]}`; once
resolved, as `{"value": …}`.

## Same behaviour as TypeScript

The merge core ([`accordsync-core`](https://pypi.org/project/accordsync-core/)) passes the shared
golden vectors in every delivery order and reproduces the TypeScript core's random scenarios byte for
byte. The repository's `interop/` harness runs this client against the real TypeScript server, alone
and together with TypeScript devices over a network that loses requests and responses, and checks
every device ends with identical data.

Docs: [accord.benhattab.pro/docs/python](https://accord.benhattab.pro/docs/python/) ·
Source: [crossben/accordsync-python](https://github.com/crossben/accordsync-python) · Licence: Apache-2.0
