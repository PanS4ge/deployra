"""Harmonogram zadań cyklicznych: pięciopolowe wyrażenia cron (bez zależności)."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime

PRESETS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}
# minuta, godzina, dzień miesiąca, miesiąc, dzień tygodnia (0 i 7 = niedziela)
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


@dataclass(frozen=True)
class Spec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minutes or when.hour not in self.hours or when.month not in self.months:
            return False
        dom = when.day in self.days
        dow = when.isoweekday() % 7 in self.weekdays  # poniedziałek = 1 … niedziela = 0
        if self.dom_restricted and self.dow_restricted:  # standard cron: wystarczy dzień miesiąca albo tygodnia
            return dom or dow
        return dom and dow


def _number(text: str, lo: int, hi: int) -> int:
    if not text.isdigit():
        raise ValueError(f"expected a number, got {text!r}")
    value = int(text)
    if not lo <= value <= hi:
        raise ValueError(f"{value} out of range {lo}-{hi}")
    return value


def _field(text: str, lo: int, hi: int) -> frozenset[int]:
    """Jedno pole: lista (`a,b`) z elementów `*`, `n`, `a-b`, każdy opcjonalnie z krokiem (`/n`)."""
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = _number(step_text, 1, hi) if slash else 1
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _number(first, lo, hi), _number(last, lo, hi)
        else:
            start = _number(base, lo, hi)
            end = hi if slash else start  # `5/15` = od 5 co 15 do końca pola
        if start > end:
            raise ValueError(f"empty range in {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@functools.lru_cache(maxsize=512)
def _compile(expr: str) -> Spec:
    text = " ".join(expr.split())
    text = PRESETS.get(text.lower(), text)
    parts = text.split(" ")
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(parts)}")
    fields = [_field(part, lo, hi) for part, (lo, hi) in zip(parts, _FIELDS)]
    return Spec(
        minutes=fields[0],
        hours=fields[1],
        days=fields[2],
        months=fields[3],
        weekdays=frozenset(day % 7 for day in fields[4]),
        dom_restricted=not parts[2].startswith("*"),
        dow_restricted=not parts[4].startswith("*"),
    )


def validate(expr: str) -> None:
    """Rzuca ValueError, gdy wyrażenie nie jest poprawnym cronem (także presetem `@daily` itp.)."""
    _compile(expr or "")


def matches(expr: str, when: datetime) -> bool:
    """Czy minuta `when` (czas lokalny serwera) spełnia wyrażenie."""
    return _compile(expr or "").matches(when)
