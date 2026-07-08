"""Cloudflare DNS steering.

The fleet endpoint (e.g. vpn.example.com) is a single plain A record pointing
at whichever node the controller wants new connections on. Reconciling it is
the whole steering story: move it when a lighter node exists, replace it when
the node dies. Records are grey-clouded (proxied=False) on purpose — WireGuard
is UDP and the client must reach the node directly, not through Cloudflare's
HTTP proxy.
"""

from __future__ import annotations

import os

import httpx

CF_API = "https://api.cloudflare.com/client/v4"


class Cloudflare:
    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("CF_API_TOKEN", "")
        if not self.token:
            raise RuntimeError("CF_API_TOKEN is not set (needs Zone:DNS:Edit on the zone)")
        self._client = httpx.Client(
            base_url=CF_API,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20.0,
        )

    def _req(self, method: str, path: str, **kw):
        r = self._client.request(method, path, **kw)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"Cloudflare API {method} {path} failed: {data.get('errors')}")
        return data.get("result")

    def list_a_records(self, zone_id: str, name: str) -> list[dict]:
        return self._req(
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={"type": "A", "name": name, "per_page": 100},
        )

    def list_zone_a_records(self, zone_id: str) -> list[dict]:
        """Every A record in the zone, paginated (used to sweep up leftovers)."""
        records: list[dict] = []
        page = 1
        while True:
            batch = self._req(
                "GET",
                f"/zones/{zone_id}/dns_records",
                params={"type": "A", "per_page": 100, "page": page},
            )
            records.extend(batch)
            if len(batch) < 100:
                return records
            page += 1

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self._req("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    def reconcile_a_records(
        self, zone_id: str, name: str, ips: list[str], ttl: int = 60
    ) -> list[str]:
        """Make the A-record set for `name` exactly `ips`. Returns the final set."""
        wanted = set(ips)
        existing = self.list_a_records(zone_id, name)
        have = {r["content"]: r for r in existing}

        for content, record in have.items():
            if content not in wanted:
                self._req("DELETE", f"/zones/{zone_id}/dns_records/{record['id']}")

        for ip in wanted:
            if ip not in have:
                self._req(
                    "POST",
                    f"/zones/{zone_id}/dns_records",
                    json={
                        "type": "A",
                        "name": name,
                        "content": ip,
                        "proxied": False,
                        "ttl": ttl,
                    },
                )
        return sorted(wanted)
