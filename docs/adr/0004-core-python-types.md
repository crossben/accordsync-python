# ADR-Y04: how the Python core maps JavaScript values

**Status:** accepted

## Decision

- **Errors.** Every validation failure raises `AccordError` (a `ValueError`); a clock too far ahead
  raises `ClockSkewError`, a subclass. `canonical_json` raises `TypeError` for non-JSON values.
- **Numbers.** `bool` is never a number: `True` is refused as a counter increment and as a set
  element (set elements are strings or finite numbers only). Counter increments must be integers
  within ±(2^53 − 1); a whole float (`3.0`) is accepted and stored as `int`, since JavaScript sees
  one number. A set element integer beyond 2^53 is stored as the `float` JavaScript would parse it
  to. `1` and `1.0` are one set element; `"1"` and `1` are two.
- **Strings.** Every ordering (op ids, records, fields, object keys, set elements, HLC nodes) uses
  the UTF-16 code-unit key `s.encode("utf-16-be", "surrogatepass")`. A record id's 256-character
  limit counts UTF-16 code units, as JavaScript's regex does. Regexes use `fullmatch` and `[0-9]`,
  never `$` (accepts a trailing newline) or `\d` (accepts any Unicode digit).
- **Never-written fields.** `read_state` returns the `ABSENT` sentinel for an `lww` or `conflict`
  field with no value, and `Replica.read` leaves such fields out (TypeScript's `undefined`). A field
  assigned `None` reads `None`.
- **Shapes.** Ops are frozen dataclasses with `deps` as a tuple; `Replica.conflicts()` returns
  `FieldRef(record, field)`; `RecordSnapshot.to_json()`/`from_json()` use the TypeScript JSON shape
  (`opId`, `hlc`, `tags`, `live`), so server snapshots load unchanged.
- **Same behaviour as TypeScript, even where odd:** a write with the wrong kind for a field
  (`inc` on an `lww` field) is refused after consuming a sequence number, exactly as `writer.ts`
  does; only unknown records and fields are refused before.
