"""Web panel for awg-fleet: manage nodes and clients, watch live metrics.

The panel binds to localhost behind Cloudflare Tunnel, but authenticates its own
administrator with a signed, HTTPS-only session cookie. It fails closed if the
required credentials are absent, so removing an external access proxy cannot
accidentally expose the fleet controls.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel

from .bench import apply_bench, benchmark_server
from .clients import (
    add_client,
    move_client,
    pick_node,
    qr_png,
    remove_client,
    vpn_qr_series,
    vpn_uri,
)
from .models import Server
from .provision import provision_server, push_config, teardown_server
from .render import render_client_conf
from .state import DEFAULT_STATE_PATH, State
from .stats import StatsDB

app = FastAPI(title="awg-fleet", docs_url=None, redoc_url=None)
_lock = asyncio.Lock()
_TEMPLATES = Path(__file__).parent / "templates"
_stats = StatsDB()
_SESSION_COOKIE = "__Host-awg-fleet-session"
_SESSION_TTL_SECONDS = 12 * 60 * 60
_PUBLIC_PATHS = {"/login", "/api/login", "/healthz"}


def _panel_auth() -> tuple[str, str, bytes] | None:
    """Read credentials lazily so the process can be configured through systemd.

    A missing or weak session secret returns ``None``; the middleware then fails
    closed instead of serving the panel without authentication.
    """
    username = os.getenv("PANEL_USERNAME", "")
    password = os.getenv("PANEL_PASSWORD", "")
    secret = os.getenv("PANEL_SESSION_SECRET", "")
    if not username or not password or len(secret) < 32:
        return None
    return username, password, secret.encode("utf-8")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _new_session(username: str, secret_key: bytes) -> str:
    expires = int(time.time()) + _SESSION_TTL_SECONDS
    payload = f"{username}\n{expires}\n{secrets.token_urlsafe(18)}".encode("utf-8")
    signature = hmac.new(secret_key, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def _session_user(token: str | None, auth: tuple[str, str, bytes] | None) -> str | None:
    if not token or not auth:
        return None
    username, _, secret_key = auth
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
        expected = hmac.new(secret_key, payload, hashlib.sha256).digest()
        token_user, expires, _ = payload.decode("utf-8").split("\n", 2)
        if int(expires) < time.time() or token_user != username:
            return None
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return token_user if hmac.compare_digest(signature, expected) else None


def _auth_required(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.middleware("http")
async def require_panel_login(request: Request, call_next):
    """Protect every panel route except login and a no-detail health probe."""
    if request.url.path not in _PUBLIC_PATHS:
        auth = _panel_auth()
        if auth is None:
            return JSONResponse(
                {"detail": "panel authentication is not configured"}, status_code=503
            )
        if not _session_user(request.cookies.get(_SESSION_COOKIE), auth):
            return _auth_required(request)

    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


_LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход · awg-fleet</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c0e11;color:#e8ebef;font:15px/1.5 system-ui,sans-serif}
main{width:min(360px,calc(100% - 32px));background:#14171c;border:1px solid #262b33;border-radius:12px;padding:28px}
h1{margin:0 0 6px;font-size:22px}.sub{margin:0 0 22px;color:#8b94a3}label{display:block;margin:13px 0 5px;color:#b9c0ca;font-size:13px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #343b46;border-radius:7px;background:#0c0e11;color:#e8ebef;font:inherit}
button{width:100%;margin-top:20px;padding:10px;border:0;border-radius:7px;background:#35d0a5;color:#05201a;font:600 14px system-ui;cursor:pointer}
#error{min-height:20px;margin-top:12px;color:#e0574c;font-size:13px}</style></head>
<body><main><h1>awg-fleet</h1><p class="sub">Вход в панель управления</p>
<form id="login"><label for="username">Логин</label><input id="username" autocomplete="username" required autofocus>
<label for="password">Пароль</label><input id="password" type="password" autocomplete="current-password" required>
<button type="submit">Войти</button><div id="error" role="alert"></div></form></main>
<script>document.querySelector('#login').addEventListener('submit',async e=>{e.preventDefault();const error=document.querySelector('#error');error.textContent='';const r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:username.value,password:password.value})});if(r.ok)location.assign('/');else error.textContent='Неверный логин или пароль';});</script>
</body></html>"""


def _load() -> State:
    st = State(DEFAULT_STATE_PATH)
    st.load()
    return st


def _find_client(cfg, name):
    c = next((x for x in cfg.clients if x.name == name), None)
    if not c:
        raise HTTPException(404, f"no client {name!r}")
    return c


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    auth = _panel_auth()
    if auth is None:
        raise HTTPException(503, "panel authentication is not configured")
    if _session_user(request.cookies.get(_SESSION_COOKIE), auth):
        return RedirectResponse("/", status_code=303)
    return _LOGIN_PAGE


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: LoginIn):
    auth = _panel_auth()
    if auth is None:
        raise HTTPException(503, "panel authentication is not configured")
    username, password, secret_key = auth
    valid = hmac.compare_digest(body.username, username) and hmac.compare_digest(body.password, password)
    if not valid:
        await asyncio.sleep(0.35)  # keeps online password guessing slow without storing attempts
        raise HTTPException(401, "invalid credentials")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        _SESSION_COOKIE,
        _new_session(username, secret_key),
        max_age=_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return response


@app.get("/api/session")
async def session(request: Request):
    auth = _panel_auth()
    return {"authenticated": True, "username": _session_user(request.cookies.get(_SESSION_COOKIE), auth)}


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
        if c.node_host:
            assigned[c.node_host] = assigned.get(c.node_host, 0) + 1
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
                "weight": s.weight,
            }
        )
    name_by_host = {s.host: s.name for s in cfg.servers}
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
                "node": name_by_host.get(c.node_host, "") if c.node_host else "",
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
    name_by_host = {s.host: s.name for s in cfg.servers}
    return {
        "name": c.name,
        "address": c.address,
        "created_at": c.created_at,
        "node": name_by_host.get(c.node_host, "") if c.node_host else "",
        "port": c.port,
        "servers": [s.name for s in cfg.servers],
        **d,
    }


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
        "weight": s.weight,
        "bench": s.bench,
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
        try:  # measure the box so placement knows its capacity from day one
            bench = await benchmark_server(srv)
            if bench:
                apply_bench(srv, bench)
        except Exception:
            pass  # weight stays 1.0; the weekly benchmark will catch it
        cfg.servers.append(srv)
        st.save()
    return {"ok": True, "weight": srv.weight, "bench": srv.bench}


@app.post("/api/servers/{name}/bench")
async def bench_server(name: str):
    """Re-measure one node's capacity now and refresh its placement weight."""
    async with _lock:
        st = _load()
        cfg = st.config
        srv = next((s for s in cfg.servers if s.name == name), None)
        if not srv:
            raise HTTPException(404, "no such server")
        try:
            bench = await benchmark_server(srv)
        except Exception as exc:
            raise HTTPException(502, f"benchmark failed: {exc}")
        if not bench:
            raise HTTPException(502, "benchmark unusable (curl missing or speedtest unreachable)")
        apply_bench(srv, bench)
        st.save()
    return {"ok": True, "weight": srv.weight, "bench": srv.bench}


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


@app.post("/api/clients")
async def create_client(body: ClientIn):
    async with _lock:
        st = _load()
        cfg = st.config
        load_by_host, alive_by_host = _host_metrics(cfg)
        node = pick_node(cfg, load_by_host, alive_by_host)
        try:
            add_client(
                cfg,
                body.name,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                node_host=node,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        warnings = await _sync_all(cfg)
        st.save()
    return {"ok": True, "warnings": warnings, "node": node}


class MoveIn(BaseModel):
    node: str  # server *name* to pin the client to


@app.post("/api/clients/{name}/node")
async def repin_client(name: str, body: MoveIn):
    """Move a client's home node. Their port (identity) is stable, so a pinned
    client's issued config keeps working — only the steering target changes.
    Pinning a legacy client needs one config re-issue (flagged in the reply)."""
    async with _lock:
        st = _load()
        cfg = st.config
        srv = next((s for s in cfg.servers if s.name == body.node), None)
        if not srv:
            raise HTTPException(404, "no such server")
        try:
            _, reissue = move_client(cfg, name, srv.host)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        warnings = await _sync_all(cfg)
        st.save()
    return {"ok": True, "warnings": warnings, "reissue": reissue}


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
