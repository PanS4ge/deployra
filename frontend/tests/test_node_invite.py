import yaml

from deployra_panel.services import sync


def test_add_node_guide_gives_ready_commands(admin, monkeypatch):
    panel = admin.app.state.panel
    panel.db.insert("gateway", name="Deployra-WG", bootstrap_url="http://oracle.example.com:51821", api_url="http://10.66.0.1:2751",
                    secret=panel.sealer.seal(b"h" * 32), public_host="oracle.example.com", wg_port=51820, subnet="10.66.0.0/24",
                    hub_address="10.66.0.1", hub_public_key="k", panel_address=None)  # fmt: skip

    add = admin.get("/nodes/add", headers={"HX-Request": ""}).text
    assert "oracle.example.com:51821" in add  # z którą bramką połączy się węzeł

    async def invite_node(panel, name, subnets, ttl):
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
