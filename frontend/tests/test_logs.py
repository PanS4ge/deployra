import re

import pytest

from deployra_panel.services import logquery
from test_resources import nodes  # noqa: F401 — fixture `nodes`


def test_query_syntax():
    q = logquery.parse('error "connection refused" /5\\d\\d / -healthcheck app:sklep container:redis node:dom level:warn')
    assert q.must[:2] == ["error", re.escape("connection refused")]
    assert q.must[2] == "5\\d\\d " and q.must[3] == logquery.LEVELS["warn"]
    assert q.exclude == ["healthcheck"] and q.apps == ["sklep"] and q.containers == ["redis"] and q.nodes == ["dom"]
    assert logquery.parse("").must == []
    with pytest.raises(ValueError):
        logquery.parse("/(/")
    with pytest.raises(ValueError):
        logquery.parse("level:loud")


def test_highlight_escapes_html():
    out = logquery.highlight('<script>alert(1)</script> ERROR at <b>', ["error"])
    assert "<script>" not in out and "&lt;script&gt;" in out and "<mark>ERROR</mark>" in out


def test_logs_page_and_search(admin, nodes):
    calls = []

    async def post(path, json=None, **_):
        calls.append((path, json))
        return {"hits": [{"container": "app-web", "app": "web", "ts": json["until"] - 30, "line": "ERROR timeout <x>"}],
                "total": 7, "buckets": [0] * 39 + [7], "scanned": 3, "since": json["since"], "step": 90, "timed_out": False}  # fmt: skip

    for node in nodes.values():
        node.post = post
    assert admin.get("/logs?q=error", headers={"HX-Request": ""}).status_code == 200
    page = admin.get("/logs/search", params={"q": "error app:web -health node:dom", "range": "1h"}).text
    assert len(calls) == 1  # tylko węzeł „dom”
    path, body = calls[0]
    assert path == "/api/logs/search" and body["must"] == ["error"] and body["exclude"] == ["health"] and body["apps"] == ["web"]
    assert 3000 < body["until"] - body["since"] < 4000
    assert "Trafienia: 7" in page and "<mark>ERROR</mark> timeout &lt;x&gt;" in page and "logFilter('container:app-web')" in page
    assert "/containers/app-web" in page
    bad = admin.get("/logs/search", params={"q": "/(/"}).text
    assert "callout err" in bad

    # Test filtering containers vs nginx
    res_cont = admin.get("/logs/search", params={"source": "containers"}).text
    assert "app-web" in res_cont
    res_nginx = admin.get("/logs/search", params={"source": "nginx"}).text
    assert "app-web" not in res_nginx
