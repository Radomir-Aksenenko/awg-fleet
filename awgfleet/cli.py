"""awg-fleet command line: manage the fleet and run the steering controller."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import typer

from . import __version__
from .clients import add_client, remove_client, write_client_bundle
from .cloudflare import Cloudflare
from .controller import ReconcileResult, reconcile_once, run_controller
from .keys import generate_keypair, generate_obfuscation
from .models import FleetConfig, Server
from .provision import provision_server, push_config, teardown_server
from .state import DEFAULT_STATE_PATH, State

app = typer.Typer(add_completion=False, help="One AmneziaWG config, a whole fleet behind it.")
server_app = typer.Typer(help="Manage fleet nodes.")
client_app = typer.Typer(help="Manage VPN clients.")
app.add_typer(server_app, name="server")
app.add_typer(client_app, name="client")


def _load() -> tuple[State, FleetConfig]:
    st = State(DEFAULT_STATE_PATH)
    if not st.exists():
        typer.secho(f"no {DEFAULT_STATE_PATH}; run `awgfleet init` first", fg="red")
        raise typer.Exit(1)
    return st, st.load()


async def _sync_all(cfg: FleetConfig) -> None:
    """Push the current shared config to every enabled node concurrently."""
    targets = [s for s in cfg.servers if s.enabled]
    if not targets:
        return
    results = await asyncio.gather(
        *(push_config(s, cfg) for s in targets), return_exceptions=True
    )
    for s, r in zip(targets, results):
        if isinstance(r, Exception):
            typer.secho(f"  ! {s.name}: {r}", fg="yellow")
        else:
            typer.secho(f"  ok {s.name} synced", fg="green")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.command()
def version():
    """Print the version."""
    typer.echo(f"awg-fleet {__version__}")


@app.command()
def init(
    domain: str = typer.Option(..., help="Fleet endpoint hostname, e.g. vpn.example.com"),
    zone_id: str = typer.Option(..., help="Cloudflare zone id for that domain"),
    port: int = typer.Option(51820, help="AmneziaWG listen port (same on every node)"),
    subnet: str = typer.Option("10.8.0.0/24", help="Tunnel subnet"),
    mtu: int = typer.Option(1280, help="Tunnel MTU (kept low so media never black-holes)"),
):
    """Create state.json with a fresh shared server identity."""
    st = State(DEFAULT_STATE_PATH)
    if st.exists():
        typer.secho(f"{DEFAULT_STATE_PATH} already exists, refusing to overwrite", fg="red")
        raise typer.Exit(1)
    priv, pub = generate_keypair()
    st.config = FleetConfig(
        domain=domain,
        cf_zone_id=zone_id,
        listen_port=port,
        server_private_key=priv,
        server_public_key=pub,
        obfuscation=generate_obfuscation(),
        subnet=subnet,
        mtu=mtu,
    )
    st.save()
    typer.secho(f"initialised fleet for {domain} -> {DEFAULT_STATE_PATH}", fg="green")
    typer.echo("next: `awgfleet server add ...`, then `awgfleet client add ...`")


@server_app.command("add")
def server_add(
    name: str = typer.Option(..., help="Short node name, e.g. de1"),
    host: str = typer.Option(..., help="Public IP of the node"),
    password: str = typer.Option(None, help="SSH root password"),
    key: str = typer.Option(None, help="Path to an SSH private key"),
    user: str = typer.Option("root"),
    ssh_port: int = typer.Option(22),
    region: str = typer.Option(""),
):
    """Add a node, install AmneziaWG on it and push the shared config."""
    st, cfg = _load()
    if any(s.name == name for s in cfg.servers):
        typer.secho(f"server {name!r} already exists", fg="red")
        raise typer.Exit(1)
    srv = Server(
        name=name, host=host, ssh_user=user, ssh_port=ssh_port,
        ssh_password=password, ssh_key_path=key, region=region,
    )
    typer.echo(f"provisioning {name} ({host}) ...")
    asyncio.run(provision_server(srv, cfg))
    cfg.servers.append(srv)
    st.save()
    typer.secho(f"ok {name} joined the fleet", fg="green")


@server_app.command("rm")
def server_rm(name: str = typer.Argument(...)):
    """Tear down AmneziaWG on a node and drop it from the fleet."""
    st, cfg = _load()
    srv = next((s for s in cfg.servers if s.name == name), None)
    if not srv:
        typer.secho(f"no server {name!r}", fg="red")
        raise typer.Exit(1)
    try:
        asyncio.run(teardown_server(srv))
    except Exception as exc:
        typer.secho(f"  ! teardown warning: {exc}", fg="yellow")
    cfg.servers = [s for s in cfg.servers if s.name != name]
    st.save()
    typer.secho(f"ok {name} removed", fg="green")


@server_app.command("list")
def server_list():
    """List fleet nodes."""
    _, cfg = _load()
    if not cfg.servers:
        typer.echo("(no servers yet)")
        return
    for s in cfg.servers:
        flag = "" if s.enabled else " [disabled]"
        typer.echo(f"  {s.name:12} {s.host:16} {s.region or '-':6}{flag}")


@app.command()
def sync():
    """Re-push the current shared config to every node."""
    _, cfg = _load()
    typer.echo("syncing config to all nodes ...")
    asyncio.run(_sync_all(cfg))


@client_app.command("add")
def client_add(
    name: str = typer.Argument(...),
    out: str = typer.Option("clients", help="Directory for the .conf / QR / link"),
    no_psk: bool = typer.Option(False, help="Skip the pre-shared key"),
):
    """Create a client, mirror it to every node and emit its config bundle."""
    st, cfg = _load()
    try:
        client = add_client(cfg, name, created_at=_now(), use_psk=not no_psk)
    except ValueError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1)
    typer.echo(f"added {name} as {client.address}; syncing nodes ...")
    asyncio.run(_sync_all(cfg))
    st.save()
    files = write_client_bundle(cfg, client, out_dir=out)
    typer.secho(f"ok {name} ready", fg="green")
    for kind, path in files.items():
        typer.echo(f"    {kind:5} {path}")
    typer.echo("    import the .conf or scan the QR in the AmneziaWG / AmneziaVPN app")


@client_app.command("rm")
def client_rm(name: str = typer.Argument(...)):
    """Remove a client and drop it from every node."""
    st, cfg = _load()
    try:
        remove_client(cfg, name)
    except KeyError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1)
    asyncio.run(_sync_all(cfg))
    st.save()
    typer.secho(f"ok {name} removed", fg="green")


@client_app.command("list")
def client_list():
    """List clients."""
    _, cfg = _load()
    if not cfg.clients:
        typer.echo("(no clients yet)")
        return
    for c in cfg.clients:
        typer.echo(f"  {c.name:16} {c.address:18} {c.created_at}")


@app.command()
def status():
    """Probe every node and show who would be in DNS rotation right now."""
    _, cfg = _load()
    cf = Cloudflare()
    result: ReconcileResult = asyncio.run(reconcile_once(cfg, cf))
    for p in result.probes:
        load = "?" if p.load is None else f"{p.load:.2f}"
        state = "up" if p.alive else "DOWN"
        rot = "in-rotation" if p.server.host in result.in_rotation else "out"
        typer.echo(f"  {p.server.name:12} {p.server.host:16} {state:4} load={load:5} {rot}")
    typer.echo(f"published to {cfg.domain}: {', '.join(result.in_rotation) or '(none)'}")


@app.command()
def run():
    """Run the steering controller loop (health-check + DNS reconcile forever)."""
    _, cfg = _load()
    cf = Cloudflare()

    def on_pass(result):
        stamp = _now()
        if isinstance(result, Exception):
            typer.secho(f"[{stamp}] pass error: {result}", fg="yellow")
        else:
            typer.echo(f"[{stamp}] rotation: {', '.join(result.in_rotation) or '(none)'}")

    typer.echo(f"controller up; reconciling {cfg.domain} every {cfg.health_interval}s")
    try:
        asyncio.run(run_controller(cfg, cf, on_pass=on_pass))
    except KeyboardInterrupt:
        typer.echo("bye")


if __name__ == "__main__":
    app()
