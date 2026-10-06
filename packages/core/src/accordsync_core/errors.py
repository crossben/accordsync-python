"""Errors raised by the core."""


class AccordError(ValueError):
    """Invalid input: a malformed op, a write that does not fit the schema, a bad snapshot."""


class ClockSkewError(AccordError):
    """A remote clock too far ahead of this device's physical time."""
