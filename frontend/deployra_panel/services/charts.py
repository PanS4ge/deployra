"""Wykresy SVG generowane po stronie serwera (zero bibliotek JS w przeglądarce)."""

from __future__ import annotations

import html
import itertools
from collections.abc import Sequence

from markupsafe import Markup

_ids = itertools.count(1)


def short_number(value: float | int | None) -> str:
    if value is None:
        return "—"
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if value >= 1000:
        return f"{value / 1000:.2f}".rstrip("0").rstrip(".") + "k"
    return f"{value:.0f}"


def _points(values: Sequence[float], width: float, height: float, pad: float, top: float | None = None) -> list[tuple[float, float]]:
    peak = top if top is not None else max(values or [0])
    peak = peak or 1
    step = (width - 2 * pad) / max(len(values) - 1, 1)
    return [(pad + i * step, height - pad - (v / peak) * (height - 2 * pad)) for i, v in enumerate(values)]


def _path(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))


def sparkline(values: Sequence[float], width: int = 132, height: int = 30, color: str = "#22d3ee") -> Markup:
    """Mała linia z wypełnieniem — jak w tabeli domen Cloudflare."""
    if not values or len(values) < 2:
        values = list(values or [0]) * 2
    gid = f"spk{next(_ids)}"
    points = _points(values, width, height, 2)
    line = _path(points)
    area = f"{line} L{points[-1][0]:.1f},{height} L{points[0][0]:.1f},{height} Z"
    return Markup(
        f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity=".45"/>'
        f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area}" fill="url(#{gid})"/><path d="{line}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/></svg>'
    )


def area_chart(
    labels: Sequence[str],
    series: Sequence[tuple[str, Sequence[float], str, bool]],
    width: int = 860,
    height: int = 240,
) -> Markup:
    """Wykres: lista serii (nazwa, wartości, kolor, czy wypełniać). Podpowiedzi przez <title>."""
    n = len(labels)
    if n == 0:
        return Markup('<div class="muted small">Brak danych</div>')
    left, right, top, bottom = 44, 12, 12, 26
    inner_w, inner_h = width - left - right, height - top - bottom
    peak = max([max(values or [0]) for _, values, _, _ in series] + [1])
    nice = _nice_max(peak)
    step = inner_w / max(n - 1, 1)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img">']
    # siatka i oś Y
    for i in range(5):
        y = top + inner_h * i / 4
        value = nice * (4 - i) / 4
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis" text-anchor="end">{short_number(value)}</text>')
    # serie
    for index, (name, values, color, fill) in enumerate(series):
        pts = [(left + i * step, top + inner_h - (v / nice) * inner_h) for i, v in enumerate(values)]
        line = _path(pts)
        if fill:
            gid = f"ch{next(_ids)}"
            parts.append(
                f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity=".35"/>'
                f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
            )
            parts.append(f'<path d="{line} L{pts[-1][0]:.1f},{top + inner_h} L{pts[0][0]:.1f},{top + inner_h} Z" fill="url(#{gid})"/>')
        dash = ' stroke-dasharray="5 4"' if index > 0 and not fill else ""
        parts.append(f'<path d="{line}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"{dash}/>')
    # podpisy osi X (maks. ~8)
    every = max(1, round(n / 8))
    for i, label in enumerate(labels):
        if i == n - 1 or (i % every == 0 and n - 1 - i >= max(every // 2, 1)):  # bez nakładania na ostatnią
            parts.append(f'<text x="{left + i * step:.1f}" y="{height - 6}" class="axis" text-anchor="middle">{html.escape(label)}</text>')
    # pionowe paski z podpowiedziami
    for i, label in enumerate(labels):
        tip = "\n".join([label] + [f"{name}: {short_number(values[i])}" for name, values, _, _ in series])
        x = left + i * step - step / 2
        parts.append(f'<rect x="{max(x, left):.1f}" y="{top}" width="{step:.1f}" height="{inner_h}" class="hover"><title>{html.escape(tip)}</title></rect>')
    parts.append("</svg>")
    return Markup("".join(parts))


def _nice_max(value: float) -> float:
    if value <= 4:
        return 4
    magnitude = 10 ** (len(str(int(value))) - 1)
    for factor in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= factor * magnitude:
            return factor * magnitude
    return value
