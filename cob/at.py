"""Parsing helpers for the AT command dialect spoken over the Hands-Free Profile."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Final result codes that end an AT command with a failure.
ERROR_RESULTS = frozenset(
    {
        "ERROR",
        "+CME ERROR",
        "NO CARRIER",
        "BUSY",
        "NO ANSWER",
        "DELAYED",
        "BLACKLISTED",
    }
)

CALL_STATUS_NAMES = {
    0: "active",
    1: "held",
    2: "dialing",
    3: "alerting",
    4: "incoming",
    5: "waiting",
}

_CIND_ENTRY = re.compile(r'\(\s*"([^"]*)"\s*,\s*\(([^)]*)\)\s*\)')
_DIAL_CHARS = re.compile(r"^[0-9*#+A-Da-d,]+$")


def parse_line(line: str) -> tuple[str, str]:
    """Split a result line into its name and argument string.

    ``"+CIEV: 2,1"`` becomes ``("+CIEV", "2,1")`` and ``"RING"`` becomes
    ``("RING", "")``.
    """
    line = line.strip()
    if line.startswith("+") and ":" in line:
        name, _, rest = line.partition(":")
        return name.strip().upper(), rest.strip()
    return line.upper(), ""


def split_args(args: str) -> list[str]:
    """Split a comma separated argument list, honouring quotes and parentheses."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quoted = False
    for ch in args:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        elif not quoted and depth == 0 and ch == ",":
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current or parts:
        parts.append("".join(current).strip())
    return parts


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_int_list(args: str) -> list[int]:
    values = []
    for item in split_args(args):
        try:
            values.append(int(item))
        except ValueError:
            values.append(0)
    return values


def normalize_indicator(name: str) -> str:
    """Normalise indicator names; some phones say ``call_setup`` for ``callsetup``."""
    return name.strip().lower().replace("_", "")


def parse_cind_test(args: str) -> list[str]:
    """Parse the ``AT+CIND=?`` response into indicator names, in index order."""
    return [normalize_indicator(m.group(1)) for m in _CIND_ENTRY.finditer(args)]


def parse_chld_test(args: str) -> list[str]:
    """Parse ``+CHLD: (0,1,1x,2,2x,3,4)`` into ``["0", "1", "1x", ...]``."""
    inner = args.strip().strip("()")
    return [item.strip() for item in inner.split(",") if item.strip()]


def parse_clip(args: str) -> str | None:
    """Return the number from ``+CLIP: "<number>",<type>[,...]`` (or ``+CCWA``)."""
    parts = split_args(args)
    if not parts:
        return None
    number = unquote(parts[0])
    return number or None


def parse_cops(args: str) -> str | None:
    """Return the operator name from ``+COPS: <mode>,<format>,"<name>"``."""
    parts = split_args(args)
    if len(parts) < 3:
        return None
    return unquote(parts[2]) or None


@dataclass
class Call:
    """One entry of an ``AT+CLCC`` (list current calls) response."""

    index: int
    outgoing: bool
    status: str
    multiparty: bool
    number: str | None


def parse_clcc(args: str) -> Call | None:
    """Parse ``+CLCC: <idx>,<dir>,<status>,<mode>,<mpty>[,<number>,<type>]``."""
    parts = split_args(args)
    if len(parts) < 5:
        return None
    try:
        index, direction, status, _mode, mpty = (int(p) for p in parts[:5])
    except ValueError:
        return None
    number = unquote(parts[5]) if len(parts) > 5 else None
    return Call(
        index=index,
        outgoing=direction == 0,
        status=CALL_STATUS_NAMES.get(status, str(status)),
        multiparty=mpty == 1,
        number=number or None,
    )


def is_dialable(number: str) -> bool:
    return bool(_DIAL_CHARS.match(number))
