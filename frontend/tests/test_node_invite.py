import yaml

from deployra_panel.services import sync


def test_add_node_guide_gives_ready_commands(admin, monkeypatch):
    panel = admin.app.state.panel
    panel.db.insert("gateway", name="Deployra-WG", bootstrap_url="http://oracle.example.com:51821", api_url="http://10.66.0.1:2751",
                    secret=panel.sealer.seal(b"h" * 32), public_host="oracle.example.com", wg_port=51820, subnet="10.66.0.0/24",
                    hub_address="10.66.0.1", hub_public_key="k", panel_address=None)  # fmt: skip

    add = admin.get("/nodes/add", headers={"HX-Request": ""}).text
    assert "oracle.example.com:51821" in add  # z którą bramką połączy się węzeł

    async def invite_node(panel, name, subnets, ttl, *args, **kwargs):
        node_id = panel.db.insert("nodes", name=name, transport="tunnel", status="pending", token_expires="2026-10-14T10:00:00+00:00")
        return node_id, "dplr1.tajny-token"

    monkeypatch.setattr(sync, "invite_node", invite_node)
    page = admin.post("/nodes/invite", data={"name": "proxmox", "ttl": "60"}).text
    for expected in ("Stacks → Add stack", "deployra-node/compose.yml", "docker run -d --name deployra-backend", "install-native.sh",
                     "journalctl -u deployra-backend", "nesting"):  # fmt: skip
        assert expected in page, expected
    assert page.count("dplr1.tajny-token") >= 4  # token wpisany w każdy wariant
    assert "invite." not in page  # wszystkie teksty przetłumaczone

    async def nothing_joined(panel):
        return 0

    monkeypatch.setattr(sync, "refresh_pending", nothing_joined)
    quiet = admin.post("/nodes/refresh?quiet=1")
    assert quiet.status_code == 204 and "HX-Trigger" not in quiet.headers  # odpytywanie w tle bez dymka co 5 s
    assert "HX-Trigger" in admin.post("/nodes/refresh").headers  # ręczne „Sprawdź” — z komunikatem

    gateway = panel.gateway()
    compose = yaml.safe_load(sync.compose_snippet(gateway, "dplr1.x", "proxmox"))  # poprawny YAML do wklejenia w Portainerze
    service = compose["services"]["deployra-backend"]
    assert service["environment"] == {"DEPLOYRA_NAME": "proxmox", "DEPLOYRA_GATEWAY": "oracle.example.com:51821", "DEPLOYRA_JOIN": "dplr1.x",
                                      "DEPLOYRA_LANG": "pl"}  # fmt: skip
    assert service["image"] == "ghcr.io/pans4ge/deployra-backend:latest" and service["network_mode"] == "host"
    shell = sync.shell_snippet(gateway, "dplr1.x", "proxmox")
    assert shell.startswith("mkdir -p ~/deployra-node") and shell.rstrip().endswith("docker compose up -d")
    assert sync.native_snippet(gateway, "dplr1.x", "proxmox").startswith("apt-get update && apt-get install -y git\ngit clone https://github.com/PanS4ge/deployra.git")


def test_unpaired_node_overview_shows_pairing_guide(admin, monkeypatch):
    panel = admin.app.state.panel
    panel.db.insert("gateway", name="Deployra-WG", bootstrap_url="http://oracle.example.com:51821", api_url="http://10.66.0.1:2751",
                    secret=panel.sealer.seal(b"h" * 32), public_host="oracle.example.com", wg_port=51820, subnet="10.66.0.0/24",
                    hub_address="10.66.0.1", hub_public_key="k", panel_address=None)
    node_id = panel.db.insert("nodes", name="unpaired-srv", transport="tunnel", status="pending", token_expires=0)

    async def mock_reinvite(panel, node, ttl=120):
        return "dplr1.nowy-token", {
            "compose": "services: mock",
            "shell": "mkdir -p ~/deployra-node && mock",
            "run": "docker run -d mock",
            "native": "bash install-native.sh mock",
        }

    monkeypatch.setattr(sync, "reinvite_node", mock_reinvite)

    # Wejście na stronę niesparowanego węzła automatycznie generuje/pokazuje instrukcję
    page = admin.get(f"/nodes/{node_id}").text
    assert "Węzeł nie jest jeszcze sparowany" in page
    assert "Wygeneruj nowy token dołączenia" in page
    assert "mkdir -p ~/deployra-node" in page
    assert "docker run -d mock" in page
    assert "install-native.sh mock" in page
    assert "Gotowe — czekamy na połączenie" in page

    # Ręczne kliknięcie reinvite przekierowuje z nowym tokenem (HX-Redirect dla żądań HTMX)
    res = admin.post(f"/nodes/{node_id}/reinvite")
    assert res.status_code == 204
    assert res.headers["HX-Redirect"] == f"/nodes/{node_id}?token=dplr1.nowy-token"


def test_pure_gateway_node_invite_restrictions(admin):
    panel = admin.app.state.panel
    panel.db.execute("DELETE FROM gateway")
    # Podłączamy tylko bramkę czystą (proxy bez WireGuard)
    panel.db.insert("gateway", name="Oracle-Pure", role="proxy", wg_enabled=0,
                    bootstrap_url="http://oracle.example.com:51821", api_url="http://oracle.example.com:2751",
                    secret=panel.sealer.seal(b"p" * 32), public_host="oracle.example.com",
                    wg_port=None, subnet=None, hub_address=None, hub_public_key=None, panel_address=None)

    page = admin.get("/nodes/add", headers={"HX-Request": ""}).text
    assert "Brak aktywnej bramki z WireGuardem" in page
    assert "Bezpośrednio (Direct IP)" in page

    # Próba wygenerowania zaproszenia przez tunel rzuca błąd 409 (LookupError zwracany w nagłówku HX-Trigger dla żądań HTMX lub w treści)
    res = admin.post("/nodes/invite", data={"name": "test-node", "ttl": "60"})
    assert res.status_code == 409
    assert "Brak aktywnej bramki z obs" in (res.headers.get("HX-Trigger", "") + res.text)


def test_direct_node_add_has_snippets_and_gateway_selection(admin, monkeypatch):
    panel = admin.app.state.panel
    panel.db.execute("DELETE FROM gateway")
    panel.db.insert("gateway", name="Oracle-Proxy", role="proxy", wg_enabled=0,
                    bootstrap_url="http://138.2.182.146:51821", api_url="http://127.0.0.1:2751",
                    secret=panel.sealer.seal(b"o" * 32), public_host="138.2.182.146")
    gw_cezary = panel.db.insert("gateway", name="Cezary-WG", role="proxy", wg_enabled=0,
                                bootstrap_url="http://cezary.example.com:51822", api_url="http://10.8.0.1:2752",
                                secret=panel.sealer.seal(b"c" * 32), public_host="10.8.0.1")

    # 1. Strona /nodes/add zawiera gotowe polecenia dla trybu ręcznego
    page = admin.get("/nodes/add", headers={"HX-Request": ""}).text
    assert "docker logs deployra-backend" in page
    assert "docker run -d --name deployra-backend" in page
    assert "name=\"gateway_id\"" in page
    assert "Cezary-WG" in page

    # 2. Rejestracja węzła bezpośredniego z wyborem bramki
    async def mock_pair(panel, node, code=None):
        panel.db.update("nodes", node["id"], status="paired")
        return panel.node(node["id"])

    monkeypatch.setattr(sync, "pair_node", mock_pair)
    res = admin.post("/nodes/direct", data={
        "name": "vps-dell",
        "url": "http://10.8.0.15:2750",
        "code": "DPLR-1234-5678-9012",
        "gateway_id": str(gw_cezary),
    })
    assert res.status_code == 204  # HTMX redirect
    node = panel.db.fetchone("SELECT * FROM nodes WHERE name = 'vps-dell'")
    assert node is not None
    assert node["gateway_id"] == gw_cezary
    assert node["transport"] == "direct"

    # 3. Zmiana bramki w ustawieniach węzła
    res = admin.post(f"/nodes/{node['id']}/gateway", data={"gateway_id": "1"})
    assert res.status_code == 204
    node = panel.db.get("nodes", node["id"])
    assert node["gateway_id"] == 1


