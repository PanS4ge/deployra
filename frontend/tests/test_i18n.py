import re

import pytest

from deployra_common.i18n import Catalog, fill
from deployra_panel.i18n import catalog

from test_pages import PAGES

POLISH = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
TAG = re.compile(r"</?(a|b|code|span|i|br|em|strong|ul|li|p|h4)[\s>]")


def test_packs_have_same_keys_params_and_tags():
    pl, en = catalog.pack("pl"), catalog.pack("en")
    assert set(pl) == set(en), set(pl) ^ set(en)
    for key in pl:
        assert set(PARAM.findall(pl[key])) == set(PARAM.findall(en[key])), key
        assert sorted(TAG.findall(pl[key])) == sorted(TAG.findall(en[key])), key
    assert not [key for key, text in en.items() if POLISH.search(text) and key != "_language"]


def test_catalog_fallback_and_params(tmp_path):
    (tmp_path / "pl.json").write_text('{"a": {"b": "Cześć {name}"}, "only_pl": "tylko"}', encoding="utf-8")
    (tmp_path / "en.json").write_text('{"a": {"b": "Hi {name}"}}', encoding="utf-8")
    cat = Catalog(tmp_path)
    assert cat.languages == ["en", "pl"]
    assert cat.raw("a.b", "en") == "Hi {name}" and cat.raw("only_pl", "en") == "tylko" and cat.raw("nope", "en") == "nope"
    assert cat.negotiate("de-DE,en-US;q=0.9,pl;q=0.8") == "en" and cat.negotiate("fr") is None
    assert fill("{name}: { return 200; }", {"name": "x"}) == "x: { return 200; }"


@pytest.mark.parametrize("path", PAGES)
def test_pages_in_english_have_no_polish(admin, path):
    admin.cookies.set("dpl_lang", "en")
    admin.app.state.panel.db.execute("UPDATE users SET lang = 'en'")
    admin.app.state.panel.db.execute("DELETE FROM notifications")  # zapisane wcześniej zostają w swoim języku
    html = admin.get(path, headers={"HX-Request": ""}).text
    html = html.replace("/ł/g", "")  # transliteracja w kreatorze aplikacji (kod JS, nie tekst)
    found = POLISH.findall(html)
    assert not found, [line.strip() for line in html.splitlines() if POLISH.search(line)][:5]


def test_language_switch(admin):
    panel = admin.app.state.panel
    response = admin.post("/language", data={"lang": "en"})
    assert response.status_code == 204 and "dpl_lang=en" in response.headers["set-cookie"]
    assert panel.db.fetchone("SELECT lang FROM users")["lang"] == "en"
    assert '<html lang="en">' in admin.get("/settings", headers={"HX-Request": ""}).text
    assert admin.post("/language", data={"lang": "xx"}).status_code == 400
    toast = admin.post("/settings/alerts/channels", data={"kind": "webhook", "f_url": "ftp://x"}).headers["HX-Trigger"]
    assert "must start with http" in toast  # komunikaty z Pythona też po angielsku
