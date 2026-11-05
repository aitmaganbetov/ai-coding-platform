"""Process exit codes (stable; documented in docs/cli.md)."""

from enum import IntEnum


class ExitCode(IntEnum):
    """CLI exit codes."""

    OK = 0
    INVALID = 1  # validation failed / operation refused
    NOT_FOUND = 2  # input file or directory missing
    USAGE = 3  # bad combination of options
    ABORTED = 4  # user declined a confirmation
    PENDING = 5  # run is waiting (approval, pause) — not finished, not failed
    CONFLICT = 6  # run is leased by another worker / idempotency conflict
