"""Thin async SSH helpers over asyncssh (password or key auth)."""

from __future__ import annotations

import asyncio

import asyncssh

from .models import Server


def _connect_kwargs(server: Server) -> dict:
    kwargs: dict = {
        "host": server.host,
        "port": server.ssh_port,
        "username": server.ssh_user,
        "known_hosts": None,  # we key on IPs that rotate; pinning host keys is future work
    }
    if server.ssh_key_path:
        kwargs["client_keys"] = [server.ssh_key_path]
    if server.ssh_password:
        kwargs["password"] = server.ssh_password
    return kwargs


async def run_ssh(server: Server, command: str, timeout: float = 120.0) -> str:
    async with asyncssh.connect(**_connect_kwargs(server)) as conn:
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        if result.exit_status != 0:
            raise RuntimeError(
                f"[{server.name}] command failed ({result.exit_status}): "
                f"{(result.stderr or '').strip()[:500]}"
            )
        return result.stdout or ""


async def upload_text(server: Server, remote_path: str, content: str) -> None:
    """Write text to a remote file via SFTP (parent dir must already exist)."""
    async with asyncssh.connect(**_connect_kwargs(server)) as conn:
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(remote_path, "w") as f:
                await f.write(content)


async def upload_and_run(
    server: Server, remote_path: str, content: str, command: str, timeout: float = 90.0
) -> str:
    """One SSH connection: make the parent dir, write the file, run the command."""
    return await upload_files_and_run(server, {remote_path: content}, command, timeout)


async def upload_files_and_run(
    server: Server, files: dict[str, str], command: str, timeout: float = 90.0
) -> str:
    """One SSH connection: make parent dirs, write every file, run the command.

    Folding these into a single connection matters for latency: a config push is
    otherwise several separate SSH handshakes per node."""
    parents = sorted({p.rsplit("/", 1)[0] for p in files if "/" in p})
    async with asyncssh.connect(**_connect_kwargs(server)) as conn:
        if parents:
            await conn.run("mkdir -p " + " ".join(parents), check=False)
        async with conn.start_sftp_client() as sftp:
            for remote_path, content in files.items():
                async with sftp.open(remote_path, "w") as f:
                    await f.write(content)
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        if result.exit_status != 0:
            raise RuntimeError(
                f"[{server.name}] apply failed ({result.exit_status}): "
                f"{(result.stderr or '').strip()[:400]}"
            )
        return result.stdout or ""
