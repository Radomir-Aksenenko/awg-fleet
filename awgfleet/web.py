"""Web panel for awg-fleet: manage nodes and clients from a browser.

Binds to localhost by design and is meant to sit behind a tunnel + access proxy
(the same way the CLI expects), so it carries no auth of its own. Every mutation
takes a lock, then load-modify-saves state.json atomically; the steering
controller reloads that file each pass, so changes are picked up on their own.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from .clients import add_client, qr_png, remove_client
from .controller import decide_rotation
from .health import probe
from .models import Server
from .provision import provision_server, push_config, teardown_server
from .render import render_client_conf
from .state import DEFAULT_STATE_PATH, State

app = FastAPI(title="awg-fleet", docs_url=None, redoc_url=None)
_lock = asyncio.Lock()
_TEMPLATES = Path(__file__).parent / "templates"


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
    probes = list(await asyncio.gather(*(probe(s) for s in cfg.servers))) if cfg.servers else []
    rotation = decide_rotation(cfg, probes)
    return {
        "domain": cfg.domain,
        "port": cfg.listen_port,
        "servers": [
            {
                "name": p.server.name,
                "host": p.server.host,
                "region": p.server.region,
                "alive": p.alive,
                "load": p.load,
                "in_rotation": p.server.host in rotation,
            }
            for p in probes
        ],
        "clients": [
            {"name": c.name, "address": c.address, "created_at": c.created_at}
            for c in cfg.clients
        ],
        "rotation": rotation,
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
    warnings: list[str] = []
    for s in cfg.servers:
        if not s.enabled:
            continue
        try:
            await push_config(s, cfg)
        except Exception as exc:
            warnings.append(f"{s.name}: {exc}")
    return warnings


@app.post("/api/clients")
async def create_client(body: ClientIn):
    async with _lock:
        st = _load()
        cfg = st.config
        try:
            add_client(
                cfg, body.name, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        warnings = await _sync_all(cfg)
        st.save()
    return {"ok": True, "warnings": warnings}


@app.delete("/api/clients/{name}")
async def delete_client(name: str):
    async with _lock:
        st = _load()
        cfg = st.config
        try:
            remove_client(cfg, name)
        except KeyError:
            raise HTTPException(404, "no such client")
        warnings = await _sync_all(cfg)
        st.save()
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
