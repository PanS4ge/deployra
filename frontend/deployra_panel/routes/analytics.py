"""Wykresy odwiedzin (sparkline w tabelach i pełny wykres) — fragmenty HTMX."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..context import Panel, get_panel
from ..deps import current_user
from ..i18n import t
from ..services import analytics, charts
from ..web import render

router = APIRouter(dependencies=[Depends(current_user)])


def _domains(value: str) -> list[str]:
    return [d.strip().lower() for d in value.split(",") if d.strip()][:50]


@router.get("/analytics/spark")
async def spark(request: Request, domains: str = "", panel: Panel = Depends(get_panel)):
    data = await analytics.daily(panel, _domains(domains), 30)
    if data is None:
        return render(request, "partials/spark.html", data=None)
    uniques = [row["uniques"] for row in data["series"]]
    return render(request, "partials/spark.html", data=data, svg=charts.sparkline(uniques), total=charts.short_number(data["summary"]["uniques"]))


@router.get("/analytics/chart")
async def chart(request: Request, domains: str = "", all: int = 0, days: int = 30, panel: Panel = Depends(get_panel)):
    names = None if all else _domains(domains)
    days = days if days in (1, 7, 30, 90) else 30
    if days == 1:
        data = await analytics.hourly(panel, names)
        svg = None
        if data:
            svg = charts.area_chart(
                data["labels"],
                [(t("analytics.uniques"), [r["uniques"] for r in data["series"]], "#22d3ee", True),
                 (t("analytics.requests"), [r["requests"] for r in data["series"]], "#8b5cf6", False)],
            )  # fmt: skip
    else:
        data = await analytics.daily(panel, names, days)
        svg = None
        if data:
            labels = [d[5:].replace("-", ".") for d in data["labels"]]
            svg = charts.area_chart(
                labels,
                [(t("analytics.uniques"), [r["uniques"] for r in data["series"]], "#22d3ee", True),
                 (t("analytics.returning"), [r["returning"] for r in data["series"]], "#f472b6", False)],
            )  # fmt: skip
    query = "all=1" if all else f"domains={','.join(names or [])}"
    return render(request, "partials/analytics_chart.html", data=data, svg=svg, days=days, query=query, short=charts.short_number)
