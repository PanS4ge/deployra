import requests
from typing import Any
from utils.providers.dnsprovider import DNSProvider
from utils import settingsgen
from utils.managers import notifications

class CLOUDFLARE_TOKEN(DNSProvider):
    api_token: str
    headers: dict[str, str]

    def __init__(self) -> None:
        try:
            self.api_token = settingsgen.read_settings()["cloudflare_api_key"]
            self.headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json"
            }
            resp = requests.get("https://api.cloudflare.com/client/v4/user/tokens/verify", headers=self.headers)
            if not resp.json().get("success", True):
                notifications.add_notification(settingsgen.get_language().cloudflare_error)
                settingsgen.update_settings(usedns="None", dnsapi="")
        except Exception:
            notifications.add_notification(settingsgen.get_language().cloudflare_error)
            settingsgen.update_settings(usedns="None", dnsapi="")
        super().__init__()

    def _get_zones(self) -> list[dict]:
        resp = requests.get("https://api.cloudflare.com/client/v4/zones", headers=self.headers)
        if resp.ok:
            return resp.json().get("result", [])
        return []

    def get_domains(self) -> list[str]:
        zones = self._get_zones()
        return [zone["name"] for zone in zones]

    def get_records(self, domain: str) -> list[dict]:
        zone_id = self._get_zone_id(domain)
        if not zone_id:
            return []
        resp = requests.get(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records", headers=self.headers)
        if resp.ok:
            return resp.json().get("result", [])
        return []

    def add_record(self, domain: str, record: dict) -> bool:
        zone_id = self._get_zone_id(domain)
        if not zone_id:
            return False
        payload = {
            "type": record["type"],
            "name": record["name"],
            "content": record["content"],
            "ttl": record.get("ttl", 1),
            "proxied": record.get("proxied", False)
        }
        resp = requests.post(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records", json=payload, headers=self.headers)
        return resp.ok

    def delete_record(self, domain: str, record: dict) -> bool:
        zone_id = self._get_zone_id(domain)
        if not zone_id:
            return False

        dns_records = self.get_records(domain)
        record_id = None
        for r in dns_records:
            if r["name"] == record["name"]:
                record_id = r["id"]
                break

        if not record_id:
            return False

        resp = requests.delete(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}", headers=self.headers)
        return resp.ok

    def update_record(self, domain: str, record: dict) -> bool:
        zone_id = self._get_zone_id(domain)
        if not zone_id:
            return False

        dns_records = self.get_records(domain)
        record_id = None
        for r in dns_records:
            if r["name"] == record["name"]:
                record_id = r["id"]
                break

        if not record_id:
            return False

        payload = {
            "type": record["type"],
            "name": record["name"],
            "content": record["content"],
            "ttl": record.get("ttl", 1),
            "proxied": record.get("proxied", False)
        }
        resp = requests.put(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}", json=payload, headers=self.headers)
        return resp.ok

    def _get_zone_id(self, domain: str) -> str:
        zones = self._get_zones()
        for zone in zones:
            if zone["name"] == domain or domain.endswith("." + zone["name"]):
                return zone["id"]
        return ""