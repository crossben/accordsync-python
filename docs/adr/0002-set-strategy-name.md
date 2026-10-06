# ADR-Y02: the add-wins set strategy is `set_()`

**Status:** accepted

## Decision

The four strategy constructors are `lww()`, `counter()`, `set_()` and `conflict()`. Schemas may also
name strategies by string (`"set"`), which is how the golden vectors declare them.

## Why

`set` is a Python builtin; shadowing it in user code breaks `set()` everywhere in that module. A
trailing underscore is the standard Python convention for this clash (PEP 8), keeps the name
recognisable next to the TypeScript and Dart `set()`, and is short.
