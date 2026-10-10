import json

from deployra_gateway.analytics import Analytics, SyslogReceiver

SECRET = b"s" * 32
DAY1 = 1_760_000_000.0  # 2025-10-09 ~08:53 UTC
DAY2 = DAY1 + 86400


def hit(a: Analytics, ip: str, ts: float, ua: str = "Mozilla/5.0 Firefox", host: str = "blog.example.com", size: int = 100):
    a.ingest({"h": host, "a": ip, "u": ua, "s": 200, "m": "GET", "b": size}, ts=ts)


def test_uniques_returning_and_bots(tmp_path):
    a = Analytics(tmp_path, SECRET)
    a.day, a.hour = "2025-10-09", int(DAY1 // 3600)
    for i in range(5):
        hit(a, f"10.0.0.{i}", DAY1)
        hit(a, f"10.0.0.{i}", DAY1 + 60)  # drugi raz tego samego dnia — nadal jeden unikalny
    hit(a, "10.0.0.99", DAY1, ua="Googlebot/2.1")
    hit(a, "10.0.0.98", DAY1, ua="")
    hit(a, "10.0.0.1", DAY1, host="shop.example.com")
    assert len(a.today["blog.example.com"]) == 5 and a.requests["blog.example.com"] == 12
    assert a.returning.get("blog.example.com", 0) == 0

    # następny dzień: 2 stare osoby wracają, 1 nowa
    hit(a, "10.0.0.1", DAY2)
    hit(a, "10.0.0.2", DAY2)
    hit(a, "10.0.0.50", DAY2)
    hit(a, "10.0.0.1", DAY2 + 10, ua="Mozilla/5.0 Chrome")  # ten sam IP, inna przeglądarka = inny odwiedzający
    hit(a, "10.0.0.2", DAY1 + 30)  # spóźniony pakiet z wczoraj nie cofa dnia
    assert a.day == "2025-10-10" and len(a.today["blog.example.com"]) == 4
    a.flush()
    days = {r["day"]: r for r in a.daily(400, ["blog.example.com"])["blog.example.com"]}
    assert days["2025-10-09"] == {"day": "2025-10-09", "requests": 12, "uniques": 5, "returning": 0, "bytes": 1200}
    assert days["2025-10-10"]["uniques"] == 4 and days["2025-10-10"]["returning"] == 2
    assert "shop.example.com" in a.daily(400)


def test_state_survives_restart(tmp_path):
    a = Analytics(tmp_path, SECRET)
    for i in range(3):
        hit(a, f"192.168.0.{i}", a.hour * 3600 + 5)
    a.flush()
    b = Analytics(tmp_path, SECRET)
    assert b.today == a.today and b.requests == a.requests
    hit(b, "192.168.0.1", b.hour * 3600 + 6)
    assert len(b.today["blog.example.com"]) == 3  # nie liczymy drugi raz po restarcie


def test_syslog_receiver_parses_nginx_lines(tmp_path):
    a = Analytics(tmp_path, SECRET)
    receiver = SyslogReceiver(a)
    record = {"h": "app.example.com", "a": "1.2.3.4", "u": "Mozilla/5.0", "s": 200, "m": "GET", "b": 512, "t": a.hour * 3600 + 1.5}
    receiver.datagram_received(b"<190>Oct 10 12:00:00 deployra: " + json.dumps(record).encode(), ("127.0.0.1", 1))
    receiver.datagram_received(b"<190>smieci bez jsona", ("127.0.0.1", 1))
    assert a.requests == {"app.example.com": 1} and len(a.today["app.example.com"]) == 1
