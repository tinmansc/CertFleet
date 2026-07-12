"""TrueNAS CORE 13.x certificate deployer (REST API v2.0)."""
from __future__ import annotations

import datetime
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, ensure_https, secure_key, strip_scheme


def _make_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


KEY_EXPIRY_WARN_DAYS = 14


def _check_key_expiry(api, api_key: str, log: Logger) -> Optional[str]:
    """Advisory only — never gates the actual connection attempt above this.
    TrueNAS API keys are formatted '<id>-<secret>'; the id tells us which
    entry in GET /api_key is ours without needing the plaintext secret to
    match anything. Any failure here (older TrueNAS without this endpoint,
    unexpected format, etc.) is swallowed — this is a nice-to-have, not a
    reason to fail an otherwise-successful check/deploy."""
    try:
        key_id = int(api_key.split("-", 1)[0])
        keys = api("api_key", exit_on_error=False)
        if not keys:
            return None
        mine = next((k for k in keys if k.get("id") == key_id), None)
        if mine is None:
            return None
        if mine.get("revoked"):
            reason = mine.get("revoked_reason") or "no reason given"
            log("warn", f"TrueNAS: API key '{mine.get('name')}' is revoked ({reason})")
            return f"This device's API key ('{mine.get('name')}') has been revoked ({reason})."
        expires = mine.get("expires_at")
        if not expires or not expires.get("$date"):
            return None
        expires_dt = datetime.datetime.fromtimestamp(expires["$date"] / 1000, tz=datetime.timezone.utc)
        days_left = (expires_dt - datetime.datetime.now(datetime.timezone.utc)).days
        if days_left < 0:
            log("warn", f"TrueNAS: API key '{mine.get('name')}' expired {-days_left} day(s) ago")
            return f"This device's API key ('{mine.get('name')}') expired {-days_left} day(s) ago."
        if days_left <= KEY_EXPIRY_WARN_DAYS:
            log("warn", f"TrueNAS: API key '{mine.get('name')}' expires in {days_left} day(s)")
            return f"This device's API key ('{mine.get('name')}') expires in {days_left} day(s)."
        return None
    except Exception:
        return None


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _run(cfg: DeviceConfig, local: LocalCert, log: Logger, deploy: bool) -> DeviceResult:
    host = ensure_https(cfg.host)
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    ctx = _make_ctx()

    def api(endpoint: str, method: str = "GET", data=None, exit_on_error: bool = True):
        url = f"{host}/api/v2.0/{endpoint}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx) as r:
                content = r.read().decode()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code}: {e.read().decode()[:200]}"
            if exit_on_error:
                raise RuntimeError(msg)
            log("warn", f"TrueNAS API warning: {msg}")
            return None

    hostname = strip_scheme(host)

    # Probed independently of the API key below — a TLS handshake needs no
    # credentials, so this should still succeed (and get reported) even if
    # the API key itself is wrong, missing, or revoked. Previously the API
    # auth call ran first and any failure there skipped this entirely, so a
    # bad API key meant "we probably could have told you the device's live
    # cert info, but we bailed out before ever trying."
    try:
        live_fp = probe_tls_fingerprint(hostname)
    except Exception as e:
        log("warn", f"TrueNAS: TLS probe failed ({e})")
        live_fp = None

    try:
        log("info", f"TrueNAS: checking current certificate at {host}")
        general = api("system/general")
        log("info", "TrueNAS: API key authenticated — system/general OK")
        key_warning = _check_key_expiry(api, cfg.api_key or "", log)
        cert_info = general.get("ui_certificate")

        current_id = None
        current_content = None
        if isinstance(cert_info, dict):
            current_id = cert_info.get("id")
            current_content = cert_info.get("certificate")
        elif isinstance(cert_info, int):
            current_id = cert_info

        if current_content is None and current_id is not None:
            detail = api(f"certificate/id/{current_id}")
            current_content = detail.get("certificate") if detail else None

        if local is None:
            if deploy:
                raise RuntimeError("No local certificate available to deploy")
            log("info", "TrueNAS: connected and authenticated — no local certificate to compare")
            return DeviceResult(
                status=DeployStatus.NO_LOCAL_CERT,
                message="Connected — credentials OK (no local cert to compare)",
                live_fingerprint=live_fp,
                warning=key_warning,
            )

        local_content = Path(local.cert_path).read_text().strip()

        if current_content and current_content.strip() == local_content:
            log("info", "TrueNAS: certificate already matches — no update needed")
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT,
                message="Certificate current",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
                warning=key_warning,
            )

        if not deploy:
            log("info", "TrueNAS: certificate differs (check-only mode)")
            return DeviceResult(
                status=DeployStatus.NEEDS_DEPLOY,
                message="Certificate differs — deploy required",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
                warning=key_warning,
            )

        cert_name = f"LetsEncrypt_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log("info", f"TrueNAS: creating certificate '{cert_name}'")

        with secure_key(local.key_path) as key_ba:
            api("certificate", method="POST", data={
                "name": cert_name,
                "create_type": "CERTIFICATE_CREATE_IMPORTED",
                "certificate": local_content,
                "privatekey": key_ba.decode(),
            })

        log("info", "TrueNAS: waiting for certificate to be processed…")
        time.sleep(10)

        certs = api("certificate")
        new_id = next((c["id"] for c in certs if c["name"] == cert_name), None)
        if new_id is None:
            raise RuntimeError(f"Could not find uploaded certificate '{cert_name}'")

        log("info", f"TrueNAS: setting certificate {cert_name} (id={new_id}) as active")
        api("system/general", method="PUT", data={"ui_certificate": new_id})

        log("info", "TrueNAS: restarting web UI to apply new certificate")
        api("system/general/ui_restart", method="POST")

        log("success", f"TrueNAS: certificate deployed — keeping old id={current_id} as backup")

        log("info", "TrueNAS: waiting 20 s for web UI to come back up…")
        time.sleep(20)
        try:
            fp = probe_tls_fingerprint(hostname)
        except Exception:
            fp = None
        return DeviceResult(
            status=DeployStatus.DEPLOYED,
            message=f"Deployed {cert_name}",
            live_fingerprint=fp,
            local_fingerprint=local.fingerprint,
            warning=key_warning,
        )

    except Exception as exc:
        log("error", f"TrueNAS: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)
