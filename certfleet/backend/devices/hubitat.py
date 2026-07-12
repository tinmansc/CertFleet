"""Hubitat Elevation certificate deployer (hubitat-cli subprocess)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, ensure_https, strip_scheme


def _verify_credentials(host: str, username: str, password: str, log: Logger) -> str:
    """POST to Hubitat login; returns a note string for status messages."""
    try:
        import requests
        s = requests.Session()
        s.verify = False
        r = s.post(
            f"{host}/login",
            data={"username": username, "password": password},
            timeout=10,
            allow_redirects=False,
        )
        loc = r.headers.get("Location", "")
        # Successful login redirects away from /login; failed stays on /login
        if r.status_code in (302, 303) and "/login" not in loc:
            log("success", "Hubitat: credentials verified")
            return " — credentials OK"
        else:
            log("warn", "Hubitat: login redirect suggests invalid credentials")
            return " — credentials FAILED"
    except Exception as e:
        log("warn", f"Hubitat: credential check failed ({e})")
        return ""


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    cli = cfg.hubitat_cli_path or "/config/scripts/hubitat-cli"

    if not Path(cli).exists():
        msg = f"hubitat-cli not found at {cli}"
        log("error", f"Hubitat: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg)

    host = ensure_https(cfg.host)
    hostname = strip_scheme(host)

    # TLS probe
    live_fp = None
    try:
        live_fp = probe_tls_fingerprint(hostname, cfg.port or 443)
    except Exception as e:
        log("warn", f"Hubitat: TLS probe failed ({e})")

    # Always test credentials in check mode
    cred_note = ""
    if not deploy:
        if cfg.username or cfg.password:
            cred_note = _verify_credentials(host, cfg.username or "", cfg.password or "", log)

        if local is None:
            log("info", "Hubitat: no local certificate to compare")
            return DeviceResult(
                status=DeployStatus.NO_LOCAL_CERT,
                message=f"Connected{cred_note or ' — no local cert to compare'}",
                live_fingerprint=live_fp,
            )

        if live_fp and live_fp == local.fingerprint:
            log("info", "Hubitat: live cert fingerprint matches local — no upload needed")
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT,
                message=f"Certificate current{cred_note}",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )
        log("info", "Hubitat: certificate differs (check-only mode)")
        return DeviceResult(
            status=DeployStatus.NEEDS_DEPLOY,
            message=f"Certificate differs — deploy required{cred_note}",
            live_fingerprint=live_fp,
        )

    env = {
        "HUBITAT_URL": host,
        "HUBITAT_USERNAME": cfg.username or "",
        "HUBITAT_PASSWORD": cfg.password or "",
        "PATH": "/usr/bin:/bin:/config/scripts",
    }

    log("info", f"Hubitat: running hubitat-cli certificate update against {host}")
    try:
        result = subprocess.run(
            [cli, "advanced", "certificate", "update",
             f"--certificate-path={local.cert_path}",
             f"--private-key-path={local.key_path}",
             "-v", "1"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        # Previously unguarded — a timeout or launch failure here propagated
        # uncaught all the way past this device's own error handling, which
        # (via asyncio.gather in deploy_all/check_all) could take the whole
        # batch's response down with it, not just this one device.
        log("error", f"Hubitat: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)

    combined = result.stdout + result.stderr
    for line in combined.splitlines():
        if line.strip():
            log("info", f"Hubitat: {line}")

    if result.returncode != 0:
        msg = f"hubitat-cli exited {result.returncode}"
        log("error", f"Hubitat: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg, live_fingerprint=live_fp)

    if "skipping update" in combined.lower():
        log("info", "Hubitat: certificate already up to date — hub did not reboot")
        try:
            fp = probe_tls_fingerprint(hostname, cfg.port or 443)
        except Exception:
            fp = None
        return DeviceResult(
            status=DeployStatus.ALREADY_CURRENT,
            message="Already current (hubitat-cli: skipping update)",
            live_fingerprint=fp,
            local_fingerprint=local.fingerprint,
        )

    log("success", "Hubitat: certificate updated — hub rebooting automatically")
    return DeviceResult(
        status=DeployStatus.DEPLOYED,
        message="Certificate deployed — hub rebooted",
        live_fingerprint=live_fp,  # last known fingerprint before the reboot was triggered
        local_fingerprint=local.fingerprint,
    )
