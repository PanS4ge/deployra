from deployra_panel.services import proxy_state


async def _applied():
    return {"ok": True, "status": "applied", "message": ""}


def test_http3_flag_round_trips_through_host_form_and_gateway_state(admin, monkeypatch):
    panel = admin.app.state.panel
    monkeypatch.setattr(proxy_state, "push", lambda panel: _applied())
    form = {"kind": "proxy", "domains": "h3.example.com", "target_type": "address", "host": "10.8.0.11", "port": "80", "scheme": "http", "cert": ""}

    page = admin.get("/proxy/hosts/new", headers={"HX-Request": ""}).text
    assert 'name="http3"' in page and 'name="http3" value="1" checked' not in page  # domyślnie wyłączony

    admin.post("/proxy/hosts/save", data={**form, "http3": "1"})
    row = next(r for r in panel.db.all("sites") if r["domains"] == ["h3.example.com"])
    assert row["http3"] == 1
    site_id = row["id"]

    edit = admin.get(f"/proxy/hosts/{site_id}", headers={"HX-Request": ""}).text
    assert 'name="http3" value="1" checked' in edit
    site = next(s for s in proxy_state.build_state(panel).sites if s.id == site_id)
    assert site.http3 is True and site.model_dump()["http3"] is True

    admin.post("/proxy/hosts/save", data={**form, "id": str(site_id)})  # odznaczony checkbox nie wysyła pola
    assert panel.db.get("sites", site_id)["http3"] == 0
    site = next(s for s in proxy_state.build_state(panel).sites if s.id == site_id)
    assert site.http3 is False
