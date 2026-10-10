import time

import pytest
from pydantic import ValidationError

from deployra_common import schemas, wg
from deployra_common.jobs import JobFailed, JobRegistry

HUB_PRIV, HUB_PUB = wg.genkey()
_, PEER_PUB = wg.genkey()


def test_render_quick_and_sync():
    iface = wg.WgInterface(HUB_PRIV, ["10.66.0.1/24"], listen_port=51820, post_up=["sysctl -w net.ipv4.ip_forward=1"])
    peer = wg.WgPeer(PEER_PUB, ["10.66.0.5/32", "192.168.1.0/24"], keepalive=25, comment="dom\nzły")
    quick = wg.render_quick(iface, [peer])
    assert "Address = 10.66.0.1/24" in quick
    assert "AllowedIPs = 10.66.0.5/32, 192.168.1.0/24" in quick
    assert "# dom zły" in quick  # komentarz bez nowych linii
    sync = wg.render_sync(iface, [peer])
    assert "Address" not in sync and "PostUp" not in sync and "ListenPort = 51820" in sync


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_ips": ["10.0.0.1/32\nPostUp = rm -rf /"]},
        {"allowed_ips": ["10.0.0.1/32"], "endpoint": "evil.com:51820\nPostUp=x"},
        {"allowed_ips": ["10.0.0.1/32"], "endpoint": "noport"},
    ],
)
def test_peer_rejects_injection(kwargs):
    with pytest.raises(ValueError):
        wg.WgPeer(PEER_PUB, **kwargs)


def test_allocate_ip_and_dump():
    assert wg.hub_address("10.66.0.0/24") == "10.66.0.1"
    assert wg.allocate_ip("10.66.0.0/24", {"10.66.0.2", "10.66.0.3/32"}) == "10.66.0.4"
    dump = (
        "privkey\tpubkey\t51820\toff\n"
        f"{PEER_PUB}\t(none)\t1.2.3.4:5555\t10.66.0.5/32\t1700000000\t100\t200\t25\n"
    )
    peers = wg.parse_dump(dump)
    assert peers == [
        {
            "public_key": PEER_PUB,
            "endpoint": "1.2.3.4:5555",
            "allowed_ips": ["10.66.0.5/32"],
            "latest_handshake": 1700000000,
            "rx_bytes": 100,
            "tx_bytes": 200,
            "keepalive": 25,
        }
    ]


@pytest.mark.anyio
async def test_jobs_follow_sync_and_errors():
    registry = JobRegistry()

    def work(job):
        for i in range(3):
            job.log(f"krok {i}")
            time.sleep(0.01)
        return {"done": True}

    job = registry.start("test", "Test", work)
    events = [event async for event in job.follow()]
    assert [e[1] for e in events if e[0] == "line"] == ["krok 0", "krok 1", "krok 2"]
    assert events[-1][0] == "end" and events[-1][1]["status"] == "ok"
    assert job.result == {"done": True}

    async def broken(job):
        job.log("zaczynam")
        raise JobFailed("nie wyszło")

    async def slow(job, value):
        job.log("czekam")
        return value * 2

    wrapped = registry.start("test", "Lambda z korutyną", lambda job: slow(job, 21))
    events = [event async for event in wrapped.follow()]
    assert wrapped.status == "ok" and wrapped.result == 42 and ("line", "czekam") in events

    bad = registry.start("test", "Zły", broken)
    events = [event async for event in bad.follow()]
    assert bad.status == "error" and bad.error == "nie wyszło"
    assert events[-2] == ("line", "✖ nie wyszło")
    assert registry.list()[0] is bad


def test_site_validation():
    up = {"host": "10.66.0.5", "port": 20001}
    site = schemas.Site(id=1, domains=["App.Example.com."], upstream=up)
    assert site.domains == ["app.example.com"]
    for bad in ["a.com;evil", "a b.com", "x.com\nreturn 200"]:
        with pytest.raises(ValidationError):
            schemas.Site(id=1, domains=[bad], upstream=up)
    with pytest.raises(ValidationError):
        schemas.Site(id=1, domains=["a.com"])  # proxy bez celu
    with pytest.raises(ValidationError):
        schemas.Upstream(host="evil;", port=80)
    with pytest.raises(ValidationError):
        schemas.ProxyLocation(path="/x { return 200; }", upstream=up)
    assert schemas.Site(id=2, domains=["*.example.com"], upstream=up).domains == ["*.example.com"]


def test_cert_and_app_validation():
    with pytest.raises(ValidationError, match="wildcard"):
        schemas.CertRequest(id=1, domains=["*.example.com"], email="a@b.pl")
    schemas.CertRequest(id=1, domains=["*.example.com"], email="a@b.pl", challenge="dns-cloudflare", cloudflare_token="t")
    with pytest.raises(ValidationError):
        schemas.AppSpec(slug="Zły Slug", source={"kind": "image", "image": "nginx"}, container_port=80)
    with pytest.raises(ValidationError, match="port"):
        schemas.AppSpec(slug="web", source={"kind": "image", "image": "nginx"})
    with pytest.raises(ValidationError, match="względne"):
        schemas.AppSpec(
            slug="web",
            source={"kind": "git", "git_url": "https://github.com/a/b", "dockerfile": "../etc/passwd"},
            container_port=80,
        )
    app = schemas.AppSpec(slug="web", source={"kind": "static"})
    assert app.source.kind == "static"
    with pytest.raises(ValidationError):
        schemas.ContainerSpec(name="x", image="nginx", env={"BAD-KEY": "1"})
