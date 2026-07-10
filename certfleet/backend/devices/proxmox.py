"""Proxmox VE certificate deployer — official REST API, no SSH involved.

Auth is an API token, created in Datacenter -> Permissions -> API Tokens.
Critically, creating the token is not enough on its own: Proxmox's
"Privilege Separation" means a token needs its *own* ACL entry even when
the underlying user already has full rights — grant it separately via
Datacenter -> Permissions -> Add -> API Token Permission, path
/nodes/{node}, role PVESysAdmin (covers Sys.Modify, which the certificate
endpoint requires). Verified against a real PVE 8.x node before shipping;
see RELEASE_CHECKLIST.md.

Proxmox has its own built-in ACME client (Datacenter/node -> Certificates)
that can request and renew Let's Encrypt certs directly, independent of
Home Assistant entirely. If you're already using that, leave upload
disabled here (the default) and use this only to verify — same posture
as the pfSense deployer, which normally defers to pfSense's own ACME
package. Deploy only actually uploads when proxmox_allow_upload=true.

Stored fields (reusing generic DeviceConfig columns — no schema change,
matching how Omada/Comware already reuse site_id for their own purposes):
  username -> full token ID, e.g. "root@pam!CertFleetAuth"
  api_key  -> token secret (the UUID after the final "=")
  site_id  -> Proxmox node name, e.g. "proxmoxdemo" (the short node name
              registered in the cluster, NOT the FQDN)
  port     -> defaults to 8006 (Proxmox's own port, not the usual 443)
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, secure_key, strip_scheme

DEFAULT_PORT = 8006


def _make_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=cfg.proxmox_allow_upload)


def _api(host: str, port: int, token: str, endpoint: str, method: str = "GET", data: Optional[dict] = None):
    url = f"https://{host}:{port}/api2/json/{endpoint}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    headers = {"Authorization": f"PVEAPIToken={token}"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, context=_make_ctx(), timeout=20) as r:
        content = r.read().decode()
        return json.loads(content) if content else None


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    hostname = strip_scheme(cfg.host)
    port = cfg.port or DEFAULT_PORT
    node = cfg.site_id or ""
    token = f"{cfg.username}={cfg.api_key}" if cfg.username and cfg.api_key else ""

    if not node:
        msg = "Proxmox node name is required (set it in the Site name field, e.g. 'proxmoxdemo')"
        log("error", f"Proxmox: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg)
    if not token:
        msg = "Proxmox API token is required (Username = full token ID, API key = token secret)"
        log("error", f"Proxmox: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg)

    try:
        log("info", f"Proxmox: probing TLS certificate on {hostname}:{port}")
        try:
            live_fp = probe_tls_fingerprint(hostname, port)
        except Exception as e:
            log("warn", f"Proxmox: TLS probe failed ({e})")
            live_fp = None

        log("info", f"Proxmox: verifying API token against node '{node}'")
        _api(hostname, port, token, f"nodes/{node}/certificates/info")
        log("success", "Proxmox: API token verified — certificates/info OK")

        if local is None:
            if deploy:
                raise RuntimeError("No local certificate available to deploy")
            log("info", "Proxmox: connected — no local certificate to compare")
            return DeviceResult(
                status=DeployStatus.NO_LOCAL_CERT,
                message="Connected — token OK (no local cert to compare)",
                live_fingerprint=live_fp,
            )

        match = bool(live_fp) and live_fp == local.fingerprint
        if match:
            log("info", "Proxmox: live cert fingerprint matches local — no upload needed")
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT,
                message="Certificate current",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        if not deploy:
            # Same convention as pfSense: this covers both a plain Verify
            # click AND a Deploy click that got gated off by
            # proxmox_allow_upload=false. Either way we deliberately don't
            # say "needs deploy" here, since clicking Deploy alone won't
            # fix it while upload stays disabled — that would be
            # misleading for a device that may be managing its own
            # certificate lifecycle via Proxmox's built-in ACME client.
            log("info", "Proxmox: fingerprint mismatch — upload skipped (verify-only mode)")
            return DeviceResult(
                status=DeployStatus.SKIPPED,
                message="Fingerprint mismatch — upload skipped (verify-only mode)",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        cert_pem = Path(local.cert_path).read_text()
        log("info", f"Proxmox: uploading certificate to node '{node}'")
        with secure_key(local.key_path) as key_ba:
            _api(hostname, port, token, f"nodes/{node}/certificates/custom", method="POST", data={
                "certificates": cert_pem,
                "key": key_ba.decode(),
                "force": 1,
                "restart": 1,
            })
        log("info", "Proxmox: certificate uploaded, waiting for pveproxy to restart…")
        time.sleep(5)

        try:
            new_fp = probe_tls_fingerprint(hostname, port)
        except Exception:
            new_fp = None

        log("success", f"Proxmox: certificate deployed to node '{node}'")
        return DeviceResult(
            status=DeployStatus.DEPLOYED,
            message="Certificate deployed",
            live_fingerprint=new_fp,
            local_fingerprint=local.fingerprint,
        )

    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        msg = f"Proxmox API error {e.code}: {body[:300]}"
        log("error", f"Proxmox: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg)
    except Exception as exc:
        log("error", f"Proxmox: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc))
