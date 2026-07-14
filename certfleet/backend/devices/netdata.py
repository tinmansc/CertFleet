"""Netdata dashboard certificate deployer.

Netdata (this install: v2.6.3, FreeBSD jail via iocage on TrueNAS CORE) has
no cert-update API — TLS material is read once at process start from two
fixed files, `ssl/cert.pem` and `ssl/key.pem`, inside its config directory.
Enabling HTTPS also requires the `[web] bind to` line in netdata.conf to
carry a `^SSL=` suffix; confirmed (2026-07-13) this device currently has
neither the cert files nor that suffix, i.e. plain HTTP only today.

Reached via SSH to the TrueNAS host + `iocage exec <jail>`, since the jail
itself has no sshd of its own. See devices/jail_ssh.py for the shared
SSH/jail primitives reused here (and intended for grafana/influxdb/gitlab
device types later, since they're hosted the same way).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices import jail_ssh
from devices.base import DeployStatus, DeviceResult, Logger, secure_key, strip_scheme

NETDATA_CONF_PATH = "/usr/local/etc/netdata/netdata.conf"
SSL_DIR           = "/usr/local/etc/netdata/ssl"
CERT_PATH         = f"{SSL_DIR}/cert.pem"
KEY_PATH          = f"{SSL_DIR}/key.pem"


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _ensure_ssl_bind(conf_text: str) -> tuple[str, bool]:
    """Add a `^SSL=optional` suffix to the `[web] bind to` line if missing.

    Returns (new_text, changed). Only touches the `bind to` line inside the
    `[web]` section — everything else in the file is left untouched.
    """
    if "^SSL=" in conf_text:
        return conf_text, False

    lines = conf_text.splitlines(keepends=True)
    in_web = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_web = (stripped.lower() == "[web]")
            continue
        if in_web and re.match(r"^\s*bind to\s*=", line):
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = line.rstrip("\n") + "^SSL=optional" + newline
            return "".join(lines), True

    # No existing `bind to` line in [web] — add the section (or the line) explicitly.
    if not in_web and "[web]" not in conf_text:
        conf_text = conf_text.rstrip("\n") + "\n\n[web]\n\tbind to = *^SSL=optional\n"
        return conf_text, True
    # [web] section exists but had no `bind to` line — append one right after the header.
    out = []
    for line in lines:
        out.append(line)
        if line.strip().lower() == "[web]":
            out.append("\tbind to = *^SSL=optional\n")
    return "".join(out), True


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    jail = cfg.jail_name or "netdata13"
    hostname = strip_scheme(cfg.host)
    port = cfg.port or 19999

    live_fp = None
    try:
        live_fp = probe_tls_fingerprint(hostname, port)
    except Exception as e:
        log("info", f"Netdata [{cfg.name}]: no TLS currently served ({e}) — expected before first deploy")

    try:
        with jail_ssh.connect(cfg) as client:
            log("info", f"Netdata [{cfg.name}]: connected to {cfg.ssh_host}, checking jail '{jail}'")

            try:
                current_content = jail_ssh.jail_exec(client, jail, f"cat {CERT_PATH}")
            except jail_ssh.JailCommandError:
                current_content = None  # no cert deployed yet

            if local is None:
                if deploy:
                    raise RuntimeError("No local certificate available to deploy")
                return DeviceResult(
                    status=DeployStatus.NO_LOCAL_CERT,
                    message="Connected — jail reachable (no local cert to compare)",
                    live_fingerprint=live_fp,
                )

            local_content = Path(local.cert_path).read_text().strip()

            if current_content and current_content.strip() == local_content:
                log("info", f"Netdata [{cfg.name}]: certificate already matches — no update needed")
                return DeviceResult(
                    status=DeployStatus.ALREADY_CURRENT,
                    message="Certificate current",
                    live_fingerprint=live_fp,
                    local_fingerprint=local.fingerprint,
                )

            if not deploy:
                log("info", f"Netdata [{cfg.name}]: certificate differs (check-only mode)")
                return DeviceResult(
                    status=DeployStatus.NEEDS_DEPLOY,
                    message="Certificate differs — deploy required",
                    live_fingerprint=live_fp,
                    local_fingerprint=local.fingerprint,
                )

            log("info", f"Netdata [{cfg.name}]: writing certificate into jail '{jail}'")
            jail_ssh.write_file_in_jail(client, jail, CERT_PATH, local_content.encode(),
                                         owner="netdata:netdata", mode="640")

            with secure_key(local.key_path) as key_ba:
                jail_ssh.write_file_in_jail(client, jail, KEY_PATH, bytes(key_ba),
                                             owner="netdata:netdata", mode="640")

            conf_text = jail_ssh.jail_exec(client, jail, f"cat {NETDATA_CONF_PATH}")
            new_conf, changed = _ensure_ssl_bind(conf_text)
            if changed:
                log("info", f"Netdata [{cfg.name}]: enabling HTTPS in netdata.conf")
                jail_ssh.write_file_in_jail(client, jail, NETDATA_CONF_PATH, new_conf.encode(),
                                             owner="root:wheel", mode="644")

            log("info", f"Netdata [{cfg.name}]: restarting netdata service in jail '{jail}'")
            jail_ssh.restart_jail_service(client, jail, "netdata")

        log("info", f"Netdata [{cfg.name}]: waiting for service to come back up…")
        time.sleep(5)
        try:
            new_fp = probe_tls_fingerprint(hostname, port)
        except Exception as e:
            log("warn", f"Netdata [{cfg.name}]: TLS probe after restart failed ({e})")
            new_fp = None

        log("success", f"Netdata [{cfg.name}]: certificate deployed")
        return DeviceResult(
            status=DeployStatus.DEPLOYED,
            message="Deployed",
            live_fingerprint=new_fp,
            local_fingerprint=local.fingerprint,
        )

    except Exception as exc:
        log("error", f"Netdata [{cfg.name}]: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)
