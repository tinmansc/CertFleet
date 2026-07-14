"""Shared SSH + iocage-jail primitives for TrueNAS-jail-hosted device types
(netdata today; grafana/influxdb/gitlab are the same shape and should reuse
this rather than re-implementing SSH/jail plumbing).

`cfg.host`/`cfg.port` stay the TLS-probe target (the jail's own dashboard
address) for every device type, matching every other deployer in this app.
The `ssh_*`/`jail_name` fields here are only about reaching the TrueNAS host
that owns the jail — a separate box/network path from the jail's own
dashboard address.
"""
from __future__ import annotations

import io
from contextlib import contextmanager
from typing import Generator, Optional

import paramiko

from config import DeviceConfig


class JailCommandError(RuntimeError):
    def __init__(self, command: str, exit_code: int, stderr: str):
        super().__init__(f"`{command}` exited {exit_code}: {stderr.strip() or '(no stderr)'}")
        self.exit_code = exit_code
        self.stderr = stderr


def _load_private_key(key_text: str) -> paramiko.PKey:
    # No single paramiko loader auto-detects key type — try each in turn.
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey, paramiko.DSSKey):
        try:
            return cls.from_private_key(io.StringIO(key_text))
        except paramiko.SSHException:
            continue
    raise ValueError("Could not parse SSH private key (tried Ed25519/ECDSA/RSA/DSS) — "
                      "check it was pasted in full, including the BEGIN/END lines")


@contextmanager
def connect(cfg: DeviceConfig) -> Generator[paramiko.SSHClient, None, None]:
    missing = [f for f in ("ssh_host", "ssh_username", "ssh_private_key") if not getattr(cfg, f, None)]
    if missing:
        raise ValueError(f"Missing SSH config field(s): {', '.join(missing)}")
    key = _load_private_key(cfg.ssh_private_key)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.ssh_host,
        port=cfg.ssh_port or 22,
        username=cfg.ssh_username,
        pkey=key,
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        yield client
    finally:
        client.close()


def run(client: paramiko.SSHClient, command: str, stdin_data: Optional[bytes] = None,
        timeout: int = 60) -> str:
    """Run a command on the TrueNAS host itself (not inside a jail)."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if stdin_data is not None:
        stdin.write(stdin_data)
        stdin.channel.shutdown_write()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        raise JailCommandError(command, exit_code, err)
    return out


def _sh_quote(command: str) -> str:
    """Single-quote `command` for safe embedding inside another shell's `sh -c '...'`."""
    return "'" + command.replace("'", "'\\''") + "'"


def jail_exec(client: paramiko.SSHClient, jail_name: str, remote_command: str,
              stdin_data: Optional[bytes] = None, timeout: int = 60) -> str:
    """Run `remote_command` inside `jail_name` via `sudo iocage exec`.

    Confirmed against a real jail (netdata13, 2026-07-13) that `iocage exec`
    forwards piped stdin through to the jailed process — needed for the
    cat-into-place file writes below without requiring sftp/scp into a jail
    that has no sshd of its own.
    """
    full_cmd = f"sudo iocage exec {jail_name} sh -c {_sh_quote(remote_command)}"
    return run(client, full_cmd, stdin_data=stdin_data, timeout=timeout)


def write_file_in_jail(client: paramiko.SSHClient, jail_name: str, remote_path: str,
                        content: bytes, owner: Optional[str] = None,
                        mode: Optional[str] = None, timeout: int = 60) -> None:
    """Write `content` to `remote_path` inside the jail, then optionally chown/chmod."""
    jail_exec(client, jail_name, f"cat > {remote_path}", stdin_data=content, timeout=timeout)
    if owner:
        jail_exec(client, jail_name, f"chown {owner} {remote_path}", timeout=timeout)
    if mode:
        jail_exec(client, jail_name, f"chmod {mode} {remote_path}", timeout=timeout)


def restart_jail_service(client: paramiko.SSHClient, jail_name: str, service_name: str,
                          timeout: int = 60) -> None:
    """Restart exactly one rc.d service inside the jail — never the jail itself.

    Jails here can host more than one service (e.g. Grafana + InfluxDB share
    one jail), so `iocage restart <jail>` would take down every service in
    it just to pick up a cert for one. Always scope to the specific service.
    """
    jail_exec(client, jail_name, f"service {service_name} restart", timeout=timeout)
