"""Web panel for awg-fleet: manage nodes and clients, watch live metrics.

Binds to localhost and expects to sit behind a tunnel + access proxy, so it
carries no auth of its own. Live rotation and per-node load come from the
metrics DB that the controller writes each pass, so the panel stays fast and
never has to SSH on a page refresh; only mutations touch the nodes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from .clients import add_client, pick_home_host, qr_png, remove_client, vpn_qr_series, vpn_uri
from .models import Server
from .provision import provision_server, push_config, teardown_server
from .render import client_endpoint_host, render_client_conf
from .state import DEFAULT_STATE_PATH, State
from .stats import StatsDB

app = FastAPI(title="awg-fleet", docs_url=None, redoc_url=None)
_lock = asyncio.Lock()
_TEMPLATES = Path(__file__).parent / "templates"
_stats = StatsDB()


def _load() -> State:
    st = State(DEFAULT_STATE_PATH)
    st.load()
    return st


def _find_client(cfg, name):
    c = next((x for x in cfg.clients if x.name == name), None)
    if not c:
        raise HTTPException(404, f"no client {name!r}")
    return c


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (_TEMPLATES / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def status():
    cfg = _load().config
    rotation = _stats.get_meta("rotation", [])
    load = _stats.get_meta("load", {})
    alive = _stats.get_meta("alive", {})
    assigned: dict[str, int] = {}
    for c in cfg.clients:
        if c.home_host:
            assigned[c.home_host] = assigned.get(c.home_host, 0) + 1
    servers = []
    for s in cfg.servers:
        servers.append(
            {
                "name": s.name,
                "host": s.host,
                "region": s.region,
                "alive": alive.get(s.name),
                "load": load.get(s.name),
                "in_rotation": s.name in rotation,
                "users": _stats.node_users(s.name),
                "assigned": assigned.get(s.host, 0),
            }
        )
    host_to_name = {s.host: s.name for s in cfg.servers}
    clients = []
    online = 0
    for c in cfg.clients:
        d = _stats.client_detail(c.public_key)
        if d["online"]:
            online += 1
        clients.append(
            {
                "name": c.name,
                "address": c.address,
                "created_at": c.created_at,
                "online": d["online"],
                "rx": d["rx"],
                "tx": d["tx"],
                "last_seen": d["last_seen"],
                "favorite": d["favorite"],
                "home": host_to_name.get(c.home_host, "") if c.home_host else "",
            }
        )
    return {
        "domain": cfg.domain,
        "port": cfg.listen_port,
        "servers": servers,
        "clients": clients,
        "rotation": rotation,
        "online": online,
        "offline": len(clients) - online,
    }


@app.get("/api/overview")
async def overview():
    return {"series": _stats.overview_series()}


@app.get("/api/clients/{name}/detail")
async def client_detail(name: str):
    cfg = _load().config
    c = _find_client(cfg, name)
    d = _stats.client_detail(c.public_key)
    return {"name": c.name, "address": c.address, "created_at": c.created_at, **d}


@app.get("/api/servers/{name}/detail")
async def server_detail(name: str):
    cfg = _load().config
    s = next((x for x in cfg.servers if x.name == name), None)
    if not s:
        raise HTTPException(404, "no such server")
    return {
        "name": s.name,
        "host": s.host,
        "region": s.region,
        "load": _stats.get_meta("load", {}).get(s.name),
        "users": _stats.node_users(s.name),
        "series": _stats.node_series(s.name),
    }


class ServerIn(BaseModel):
    name: str
    host: str
    password: str | None = None
    key_path: str | None = None
    user: str = "root"
    ssh_port: int = 22
    region: str = ""


@app.post("/api/servers")
async def add_server(body: ServerIn):
    async with _lock:
        st = _load()
        cfg = st.config
        if any(s.name == body.name for s in cfg.servers):
            raise HTTPException(400, "a server with that name already exists")
        srv = Server(
            name=body.name,
            host=body.host,
            ssh_user=body.user,
            ssh_port=body.ssh_port,
            ssh_password=body.password or None,
            ssh_key_path=body.key_path or None,
            region=body.region,
        )
        try:
            await provision_server(srv, cfg)
        except Exception as exc:
            raise HTTPException(502, f"provisioning failed: {exc}")
        cfg.servers.append(srv)
        st.save()
    return {"ok": True}


@app.delete("/api/servers/{name}")
async def remove_server(name: str):
    async with _lock:
        st = _load()
        cfg = st.config
        srv = next((s for s in cfg.servers if s.name == name), None)
        if not srv:
            raise HTTPException(404, "no such server")
        try:
            await teardown_server(srv)
        except Exception:
            pass  # best effort; drop it from the fleet regardless
        cfg.servers = [s for s in cfg.servers if s.name != name]
        st.save()
    return {"ok": True}


class ClientIn(BaseModel):
    name: str


async def _sync_all(cfg) -> list[str]:
    targets = [s for s in cfg.servers if s.enabled]
    results = await asyncio.gather(
        *(push_config(s, cfg) for s in targets), return_exceptions=True
    )
    return [f"{s.name}: {r}" for s, r in zip(targets, results) if isinstance(r, Exception)]


def _host_metrics(cfg):
    """Live load / alive keyed by host (the stats DB keys by node name)."""
    load = _stats.get_meta("load", {})
    alive = _stats.get_meta("alive", {})
    by_name = {s.name: s.host for s in cfg.servers}
    load_by_host = {by_name[n]: v for n, v in load.items() if n in by_name}
    alive_by_host = {by_name[n]: v for n, v in alive.items() if n in by_name}
    return load_by_host, alive_by_host


def _steer_record(cfg, client, ips: list[str]) -> None:
    """Point (or clear) a client's steering subdomain. Best effort: if Cloudflare
    isn't reachable the controller will reconcile it on its next pass anyway."""
    if not client.home_host:
        return
    try:
        from .cloudflare import Cloudflare

        Cloudflare().reconcile_a_records(
            cfg.cf_zone_id, client_endpoint_host(cfg, client), ips, ttl=60
        )
    except Exception:
        pass


@app.post("/api/clients")
async def create_client(body: ClientIn):
    async with _lock:
        st = _load()
        cfg = st.config
        load_by_host, alive_by_host = _host_metrics(cfg)
        home = pick_home_host(cfg, load_by_host, alive_by_host)
        try:
            client = add_client(
                cfg,
                body.name,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                home_host=home,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        warnings = await _sync_all(cfg)
        st.save()
        _steer_record(cfg, client, [home] if home else [])
    return {"ok": True, "warnings": warnings, "home": home}


@app.delete("/api/clients/{name}")
async def delete_client(name: str):
    async with _lock:
        st = _load()
        cfg = st.config
        try:
            gone = remove_client(cfg, name)
        except KeyError:
            raise HTTPException(404, "no such client")
        warnings = await _sync_all(cfg)
        st.save()
        _steer_record(cfg, gone, [])  # drop its steering subdomain
    return {"ok": True, "warnings": warnings}


@app.get("/api/clients/{name}/config", response_class=PlainTextResponse)
async def client_config(name: str):
    cfg = _load().config
    return render_client_conf(cfg, _find_client(cfg, name))


@app.get("/api/clients/{name}/qr")
async def client_qr(name: str):
    cfg = _load().config
    png = qr_png(render_client_conf(cfg, _find_client(cfg, name)))
    return Response(png, media_type="image/png")


@app.get("/api/clients/{name}/vpn", response_class=PlainTextResponse)
async def client_vpn(name: str):
    """The Amnezia vpn:// URI, for the AmneziaVPN app."""
    cfg = _load().config
    return vpn_uri(cfg, _find_client(cfg, name))


@app.get("/api/clients/{name}/vpnqr")
async def client_vpn_qr(name: str):
    cfg = _load().config
    png = qr_png(vpn_uri(cfg, _find_client(cfg, name)), low_ec=True)
    return Response(png, media_type="image/png")


@app.get("/api/clients/{name}/vpnseries")
async def client_vpn_series(name: str):
    """How many QR codes the AmneziaVPN import needs (the app reassembles them)."""
    cfg = _load().config
    return {"count": len(vpn_qr_series(cfg, _find_client(cfg, name)))}


@app.get("/api/clients/{name}/vpnqr/{index}")
async def client_vpn_qr_chunk(name: str, index: int):
    cfg = _load().config
    series = vpn_qr_series(cfg, _find_client(cfg, name))
    if index < 0 or index >= len(series):
        raise HTTPException(404, "chunk out of range")
    return Response(series[index], media_type="image/png")
