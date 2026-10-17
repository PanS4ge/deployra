"""Wykresy SVG generowane po stronie serwera (zero bibliotek JS w przeglądarce)."""

from __future__ import annotations

import html
import itertools
from collections.abc import Sequence

from markupsafe import Markup

from ..i18n import t

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


def _smooth_path(points: list[tuple[float, float]], min_y: float | None = None, max_y: float | None = None) -> str:
    """Gładka krzywa sklejana Beziera (Catmull-Rom do Cubic Bezier) z ograniczeniem zakresu Y."""
    if not points:
        return ""
    if len(points) <= 2:
        return _path(points)
    d = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else points[i + 1]
        cp1x = p1[0] + (p2[0] - p0[0]) / 6.0
        cp1y = p1[1] + (p2[1] - p0[1]) / 6.0
        cp2x = p2[0] - (p3[0] - p1[0]) / 6.0
        cp2y = p2[1] - (p3[1] - p1[1]) / 6.0
        if min_y is not None:
            cp1y = max(min_y, cp1y)
            cp2y = max(min_y, cp2y)
        if max_y is not None:
            cp1y = min(max_y, cp1y)
            cp2y = min(max_y, cp2y)
        d.append(f"C{cp1x:.1f},{cp1y:.1f} {cp2x:.1f},{cp2y:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def sparkline(values: Sequence[float], width: int = 132, height: int = 30, color: str = "#22d3ee") -> Markup:
    """Mała linia z wypełnieniem — jak w tabeli domen Cloudflare."""
    if not values or len(values) < 2:
        values = list(values or [0]) * 2
    gid = f"spk{next(_ids)}"
    points = _points(values, width, height, 2)
    line = _smooth_path(points, min_y=2, max_y=height - 2)
    area = f"{line} L{points[-1][0]:.1f},{height} L{points[0][0]:.1f},{height} Z"
    return Markup(
        f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity=".45"/>'
        f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area}" fill="url(#{gid})"/><path d="{line}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>'
    )


def area_chart(
    labels: Sequence[str],
    series: Sequence[tuple[str, Sequence[float], str, bool]],
    width: int = 860,
    height: int = 200,
    scale_max: float | None = None,
) -> Markup:
    """Wykres na całą szerokość karty: lista serii (nazwa, wartości, kolor, czy wypełniać), podpowiedzi przez <title>.

    Obszar danych to SVG rozciągany bez zachowania proporcji (linie mają `non-scaling-stroke`), a podpisy osi są
    zwykłym HTML-em — dzięki temu wykres wypełnia kartę bez pustych pasów i bez rozciągniętych liter.
    `height` — wysokość obszaru danych w px; `width` zostaje dla zgodności wywołań. `scale_max` — stała skala (np. 100 dla %).
    """
    n = len(labels)
    if n == 0:
        return Markup('<div class="muted small">{}</div>').format(t("common.no_data"))
    view_w, view_h = 1000, 100
    peak = max([max(values or [0]) for _, values, _, _ in series] + [1])
    nice = scale_max or _nice_max(peak)
    step = view_w / max(n - 1, 1)
    svg = [f'<svg class="chart-svg" viewBox="0 0 {view_w} {view_h}" preserveAspectRatio="none" role="img">']
    for i in range(1, 4):
        y = view_h * i / 4
        svg.append(f'<line x1="0" x2="{view_w}" y1="{y:.1f}" y2="{y:.1f}" class="grid" vector-effect="non-scaling-stroke"/>')
    for index, (_name, values, color, fill) in enumerate(series):
        pts = [(i * step, view_h - (v / nice) * view_h) for i, v in enumerate(values)]
        line = _smooth_path(pts, min_y=0, max_y=view_h)
        if fill:
            gid = f"ch{next(_ids)}"
            svg.append(f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity=".35"/>'
                       f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>')  # fmt: skip
            svg.append(f'<path d="{line} L{pts[-1][0]:.1f},{view_h} L{pts[0][0]:.1f},{view_h} Z" fill="url(#{gid})"/>')
        dash = ' stroke-dasharray="5 4"' if index > 0 and not fill else ""
        svg.append(f'<path d="{line}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"{dash}/>')
    for i, label in enumerate(labels):
        tip = "\n".join([label] + [f"{name}: {short_number(values[i])}" for name, values, _, _ in series])
        x = max(i * step - step / 2, 0)
        svg.append(f'<rect x="{x:.1f}" y="0" width="{min(step, view_w - x):.1f}" height="{view_h}" class="hover"><title>{html.escape(tip)}</title></rect>')
    svg.append("</svg>")
    y_axis = "".join(f'<span style="top: {25 * i}%">{short_number(nice * (4 - i) / 4)}</span>' for i in range(5))
    every = max(1, round(n / 8))
    x_axis = []
    for i, label in enumerate(labels):
        if i == n - 1 or (i % every == 0 and n - 1 - i >= max(every // 2, 1)):  # bez nakładania na ostatnią
            pos = 100 * i / max(n - 1, 1)
            shift = "0" if i == 0 else ("-100%" if i == n - 1 else "-50%")
            x_axis.append(f'<span style="left: {pos:.2f}%; transform: translateX({shift})">{html.escape(label)}</span>')
    return Markup(
        f'<div class="chart" style="--chart-h: {int(height)}px"><div class="chart-y">{y_axis}</div>'
        f'<div class="chart-plot">{"".join(svg)}</div><div class="chart-x">{"".join(x_axis)}</div></div>'
    )


def _nice_max(value: float) -> float:
    if value <= 4:
        return 4
    magnitude = 10 ** (len(str(int(value))) - 1)
    for factor in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= factor * magnitude:
            return factor * magnitude
    return value
