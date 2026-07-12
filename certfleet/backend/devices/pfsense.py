"""pfSense certificate verifier + optional deployer.

pfSense normally handles renewal via its ACME package.
This module verifies that the live cert matches the local Let's Encrypt cert.
Optional upload is available if pfsense_allow_upload=true in config and the
pfSense REST API package (API v1/v2) is installed.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, ensure_https, secure_key, strip_scheme


def _verify_credentials(host: str, username: str, password: str, log: Logger) -> str:
    """POST to pfSense login page; returns a note string for status messages."""
    import urllib.parse
    ctx = _make_ctx()
    payload = urllib.parse.urlencode({
        "usernamefld": username,
        "passwordfld": password,
        "__csrf_magic": "",
    }).encode()
    req = urllib.request.Request(
        f"{host}/",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            body = r.read(4096).decode(errors="replace").lower()
        if "logout" in body or "dashboard" in body:
            log("success", "pfSense: credentials verified")
            return " — credentials OK"
        elif "username or password" in body or "invalid" in body:
            log("warn", "pfSense: credentials appear invalid")
            return " — credentials FAILED"
        else:
            log("info", "pfSense: credential check inconclusive (unexpected response)")
            return ""
    except Exception as e:
        log("warn", f"pfSense: credential check failed ({e})")
        return ""


def _make_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=cfg.pfsense_allow_upload)


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    host = ensure_https(cfg.host)
    hostname = strip_scheme(host)
    port = cfg.port or 443

    live_fp = None  # guaranteed defined even if an exception hits before the probe below
    try:
        log("info", f"pfSense: probing TLS certificate on {hostname}:{port}")
        try:
            live_fp = probe_tls_fingerprint(hostname, port)
        except Exception as e:
            log("warn", f"pfSense: TLS probe failed ({e})")

        cred_note = ""
        if cfg.username and cfg.password:
            cred_note = _verify_credentials(host, cfg.username, cfg.password, log)

        if local is None:
            if deploy:
                raise RuntimeError("No local certificate available to deploy")
            log("info", "pfSense: TLS probe OK — no local certificate to compare")
            return DeviceResult(
                status=DeployStatus.NO_LOCAL_CERT,
                message=f"Connected{cred_note or ' — no local cert to compare'}",
                live_fingerprint=live_fp,
            )

        match = live_fp == local.fingerprint
        log(
            "success" if match else "warn",
            f"pfSense: {'✓ cert matches' if match else '✗ cert MISMATCH'} "
            f"— live={live_fp[:40]}…",
        )

        if match:
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT,
                message=f"ACME cert verified — fingerprints match{cred_note}",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        if not deploy:
            log("warn", "pfSense: fingerprint mismatch — ACME may not have run yet (verify-only mode)")
            return DeviceResult(
                status=DeployStatus.SKIPPED,
                message=f"Fingerprint mismatch — upload skipped (verify-only mode){cred_note}",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        # Optional upload via pfSense REST API package
        return _upload_via_api(cfg, local, log, hostname, live_fp)

    except Exception as exc:
        log("error", f"pfSense: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)


def _upload_via_api(
    cfg: DeviceConfig, local: LocalCert, log: Logger, hostname: str, live_fp: str
) -> DeviceResult:
    """Upload cert via pfSense API package (must be installed separately)."""
    host = cfg.host.rstrip("/")
    ctx = _make_ctx()
    auth = (cfg.username or "admin", cfg.password or "")

    import base64
    token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }

    cert_pem = Path(local.cert_path).read_text()

    log("info", "pfSense: uploading certificate via REST API")

    def api(endpoint: str, method: str = "GET", data=None):
        url = f"{host}/api/v1/{endpoint}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, context=ctx) as r:
            return json.loads(r.read().decode())

    try:
        with secure_key(local.key_path) as key_ba:
            resp = api("system/certificate", method="POST", data={
                "method": "import",
                "cert": cert_pem,
                "key": key_ba.decode(),
                "descr": f"LetsEncrypt_{local.domain}",
                "active": True,
            })
        log("success", f"pfSense: certificate imported — {resp}")
        return DeviceResult(
            status=DeployStatus.DEPLOYED,
            message="Certificate uploaded via pfSense API",
            live_fingerprint=live_fp,
            local_fingerprint=local.fingerprint,
        )
    except Exception as exc:
        msg = (
            f"pfSense API upload failed: {exc}. "
            "Ensure the pfSense API package is installed and credentials are correct."
        )
        log("error", f"pfSense: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg, live_fingerprint=live_fp)
