"""Omada OC200 hardware controller certificate deployer.

Real API flow confirmed from HAR capture (firmware 1.41.6):

  Discovery:  GET  /                                    → redirect to /{omadacId}/login
  CSRF token: GET  /api/v2/current/login-status?needToken=true
  Login:      POST /{omadacId}/api/v2/login             body: {username, password (MD5)}
  Pre-check:  GET  /{omadacId}/api/v2/controller/setting
  Upload cert:POST /{omadacId}/api/v2/files/controller/certificate  multipart: file=<bytes>, data={"cerName":"fullchain.pem"}
  Upload key: POST /{omadacId}/api/v2/files/controller/key          multipart: file=<bytes>, data={"keyName":"privkey.pem"}
  Save:       PATCH /{omadacId}/api/v2/controller/setting           body: merged settings with new cert IDs
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
from pathlib import Path
from typing import Optional

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, ensure_https, secure_key, strip_scheme

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class OmadaClient:
    """Thin HTTP client for the OC200 private REST API."""

    def __init__(self, base: str, username: str, password: str, log: Logger,
                 omadac_id: Optional[str] = None):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.log = log
        self._omadac_id: Optional[str] = omadac_id
        self._csrf: Optional[str] = None

        if not HAS_REQUESTS:
            raise RuntimeError("requests library required for Omada deployer")
        import requests
        self._s = requests.Session()
        self._s.verify = False

    # ── Discovery ──────────────────────────────────────────────────────────

    def _discover_omadac_id(self) -> str:
        try:
            r = self._s.get(self.base + "/", allow_redirects=True, timeout=10)
            m = re.search(r"/([a-f0-9]{32})/", r.url)
            if m:
                return m.group(1)
        except Exception:
            pass
        try:
            r = self._s.get(
                f"{self.base}/api/v2/current/login-status?needToken=true",
                timeout=10,
            )
            oid = r.json().get("result", {}).get("omadacId")
            if oid:
                return oid
        except Exception:
            pass
        raise RuntimeError(
            "Could not discover omadacId automatically. "
            "Log into the OC200, note the 32-char hex ID in the URL, "
            "and set omadac_id in your device config."
        )

    @property
    def _oid(self) -> str:
        if not self._omadac_id:
            self.log("info", "Omada: discovering controller ID from redirect...")
            self._omadac_id = self._discover_omadac_id()
            self.log("info", f"Omada: omadacId = {self._omadac_id}")
        return self._omadac_id

    def _url(self, path: str, with_prefix: bool = True) -> str:
        if with_prefix and self._omadac_id:
            return f"{self.base}/{self._omadac_id}{path}"
        return f"{self.base}{path}"

    def _post_json(self, path: str, payload: dict, csrf: str = "") -> dict:
        headers = {"Csrf-Token": csrf} if csrf else {}
        for url in [self._url(path, with_prefix=False), self._url(path, with_prefix=True)]:
            try:
                r = self._s.post(url, json=payload, headers=headers, timeout=15)
                resp = r.json()
                if resp.get("errorCode") != -1600:
                    return resp
                self.log("info", f"Omada: {url} -> -1600, trying alternate path...")
            except Exception:
                pass
        raise RuntimeError("No working API path found — check host and omadac_id config")

    # ── Auth ───────────────────────────────────────────────────────────────

    def login(self):
        r = self._s.get(
            f"{self.base}/api/v2/current/login-status?needToken=true",
            timeout=10,
        )
        csrf = r.json().get("result", {}).get("csrfToken", "")

        self.log("info", f"Omada: logging in as {self.username}")

        for password_val in [_md5(self.password), self.password]:
            resp = self._post_json(
                "/api/v2/login",
                {"username": self.username, "password": password_val},
                csrf=csrf,
            )
            if resp.get("errorCode") == 0:
                break
        else:
            raise RuntimeError(
                f"Omada login failed (errorCode={resp.get('errorCode')}): "
                f"{resp.get('msg', 'unknown error')}"
            )

        r2 = self._s.get(
            f"{self.base}/api/v2/current/login-status?needToken=true",
            timeout=10,
        )
        self._csrf = r2.json().get("result", {}).get("csrfToken", "")
        self.log("info", "Omada: session established")

    def _auth_headers(self) -> dict:
        h = {}
        if self._csrf:
            h["Csrf-Token"] = self._csrf
        return h

    # ── API calls ──────────────────────────────────────────────────────────

    def get_controller_setting(self) -> dict:
        hdrs = {"Content-Type": "application/json", **self._auth_headers()}
        for url in [self._url("/api/v2/controller/setting", with_prefix=False),
                    self._url("/api/v2/controller/setting", with_prefix=True)]:
            try:
                r = self._s.get(url, headers=hdrs, timeout=15)
                resp = r.json()
                if resp.get("errorCode") != -1600:
                    return resp.get("result", {})
            except Exception:
                pass
        return {}

    def upload_cert(self, content: bytes, filename: str = "fullchain.pem") -> dict:
        """Upload certificate PEM. Returns {cerId, cerName}."""
        return self._upload_file(
            "/api/v2/files/controller/certificate",
            content,
            filename,
            data_json={"cerName": filename},
        )

    def upload_key(self, content: bytes, filename: str = "privkey.pem") -> dict:
        """Upload private key PEM. Returns {keyId, keyName}."""
        return self._upload_file(
            "/api/v2/files/controller/key",
            content,
            filename,
            data_json={"keyName": filename},
        )

    def _upload_file(self, endpoint: str, content: bytes, filename: str,
                     data_json: dict) -> dict:
        hdrs = self._auth_headers()
        last_err = None
        for url in [self._url(endpoint, with_prefix=False),
                    self._url(endpoint, with_prefix=True)]:
            try:
                r = self._s.post(
                    url,
                    files={"file": (filename, content, "application/octet-stream")},
                    data={"data": json.dumps(data_json, separators=(",", ":"))},
                    headers=hdrs,
                    timeout=60,
                )
                resp = r.json()
                ec = resp.get("errorCode")
                if ec == -1600:
                    self.log("info", f"Omada: {url} -> -1600, trying alternate path...")
                    continue
                if ec != 0:
                    raise RuntimeError(
                        f"Upload to {url} failed (errorCode={ec}): {resp.get('msg')}"
                    )
                return resp.get("result", {})
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
        raise RuntimeError(f"File upload failed on all paths: {last_err}")

    def backup(self, save_dir: str) -> str:
        """Trigger an OC200 config backup, download it, and save it to save_dir.

        Returns the absolute path of the saved file.
        """
        import os
        from datetime import datetime

        os.makedirs(save_dir, exist_ok=True)

        self.log("info", "Omada: requesting backup preparation")
        resp = self._post_json(
            "/api/v2/maintenance/backup/prepare", {}, csrf=self._csrf or ""
        )
        if resp.get("errorCode") != 0:
            raise RuntimeError(
                f"Backup prepare failed (errorCode={resp.get('errorCode')}): {resp.get('msg')}"
            )

        # Poll for completion. On OC200 hardware the backup completes almost
        # instantly so status stays 0 (idle/done) — we exit after the first
        # successful poll rather than waiting for a percent change.
        hdrs = {"Content-Type": "application/json", **self._auth_headers()}
        for attempt in range(15):
            time.sleep(2)
            r = self._s.get(
                self._url("/api/v2/maintenance/backup/result"), headers=hdrs, timeout=15
            )
            result = r.json().get("result", {})
            ec = result.get("errorCode", -1)
            status = result.get("status", -1)
            pct = result.get("percent", 0)
            self.log("info", f"Omada: backup status={status} percent={pct}")
            if ec != 0:
                raise RuntimeError(f"Backup preparation failed (errorCode={ec})")
            # status 0 = idle/complete; on small hardware controllers the
            # backup finishes before the first poll.
            if status == 0:
                break

        # Download the backup archive
        self.log("info", "Omada: downloading backup archive")
        dl_hdrs = self._auth_headers()
        last_err: Exception | None = None
        for url in [
            self._url("/api/v2/files/backup", with_prefix=False),
            self._url("/api/v2/files/backup", with_prefix=True),
        ]:
            try:
                r = self._s.get(url, headers=dl_hdrs, timeout=120, stream=True)
                ct = r.headers.get("content-type", "")
                if "json" in ct.lower():
                    data = r.json()
                    ec = data.get("errorCode")
                    if ec == -1600:
                        self.log("info", f"Omada: {url} -> -1600, trying alternate path")
                        continue
                    raise RuntimeError(
                        f"Backup download returned error (errorCode={ec}): {data.get('msg')}"
                    )
                # Binary file — save it
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                cd = r.headers.get("content-disposition", "")
                # Try to honour the server's filename
                import re as _re
                m = _re.search(r'filename=["\']?([^"\';\s]+)', cd)
                filename = m.group(1) if m else f"omada_backup_{ts}.cfg"
                path = os.path.join(save_dir, filename)
                with open(path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=65536):
                        fh.write(chunk)
                self.log("success", f"Omada: backup saved → {path}")
                return path
            except RuntimeError:
                raise
            except Exception as exc:
                last_err = exc
                self.log("warn", f"Omada: download from {url} failed: {exc}")

        raise RuntimeError(f"Backup download failed on all paths: {last_err}")

    def save_settings(self, current_settings: dict, cert_result: dict,
                      key_result: dict) -> None:
        """PATCH controller/setting to activate the newly uploaded cert+key.

        Mirrors the UI's PATCH: sends the editable settings sections with the
        certificate section updated to reference the new file IDs.
        """
        patch_body: dict = {}

        # Updated certificate section with new IDs from the upload responses
        patch_body["certificate"] = {
            "cerId": cert_result["cerId"],
            "cerName": cert_result["cerName"],
            "cerType": "PEM",
            "enable": True,
            "keyName": key_result["keyName"],
            "keyId": key_result["keyId"],
        }

        # Carry forward the other editable sections unchanged from current settings
        for section in ("loggingLevel", "webPort", "deviceManage", "firmware"):
            if section in current_settings:
                patch_body[section] = current_settings[section]

        hdrs = {"Content-Type": "application/json;charset=UTF-8", **self._auth_headers()}
        last_err = None
        for url in [self._url("/api/v2/controller/setting", with_prefix=False),
                    self._url("/api/v2/controller/setting", with_prefix=True)]:
            try:
                r = self._s.patch(url, json=patch_body, headers=hdrs, timeout=30)
                resp = r.json()
                ec = resp.get("errorCode")
                if ec == -1600:
                    self.log("info", f"Omada: {url} -> -1600, trying alternate path...")
                    continue
                if ec != 0:
                    raise RuntimeError(
                        f"Save settings failed (errorCode={ec}): {resp.get('msg')}"
                    )
                return
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
        raise RuntimeError(f"PATCH controller/setting failed on all paths: {last_err}")


# ── Public interface ───────────────────────────────────────────────────────────

BACKUP_DIR = "/config/backups/omada"


def backup_config(cfg: DeviceConfig, log: Logger) -> str:
    """Login to the OC200 and save a full config backup. Returns the saved path."""
    host = ensure_https(cfg.host)
    omadac_id = getattr(cfg, "omadac_id", None) or None

    client = OmadaClient(
        base=host,
        username=cfg.username or "admin",
        password=cfg.password or "",
        log=log,
        omadac_id=omadac_id,
    )
    client.login()
    return client.backup(BACKUP_DIR)


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    host = ensure_https(cfg.host)
    hostname = strip_scheme(host)
    port = cfg.port or 443
    omadac_id = getattr(cfg, "omadac_id", None) or None

    live_fp = None  # guaranteed defined even if an exception hits before the probe below
    try:
        log("info", f"Omada: probing TLS cert on {hostname}:{port}")
        try:
            live_fp = probe_tls_fingerprint(hostname, port)
        except Exception as e:
            log("warn", f"Omada: TLS probe failed ({e}), proceeding with login")
            live_fp = None

        # Always test credentials regardless of cert match state
        client = OmadaClient(
            base=host,
            username=cfg.username or "admin",
            password=cfg.password or "",
            log=log,
            omadac_id=omadac_id,
        )
        client.login()
        client.get_controller_setting()
        log("success", "Omada: credentials verified")

        if local is None:
            if deploy:
                raise RuntimeError("No local certificate available to deploy")
            log("info", "Omada: no local certificate to compare")
            return DeviceResult(
                status=DeployStatus.NO_LOCAL_CERT,
                message="Connected — credentials OK (no local cert to compare)",
                live_fingerprint=live_fp,
            )

        if live_fp and live_fp == local.fingerprint:
            log("info", "Omada: live fingerprint matches local cert — already current")
            return DeviceResult(
                status=DeployStatus.ALREADY_CURRENT,
                message="Certificate current — credentials OK",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        if not deploy:
            log("info", "Omada: fingerprint differs (check-only mode)")
            return DeviceResult(
                status=DeployStatus.NEEDS_DEPLOY,
                message="Certificate differs — deploy required (credentials OK)",
                live_fingerprint=live_fp,
                local_fingerprint=local.fingerprint,
            )

        # Fetch current settings before uploading (needed for PATCH body)
        log("info", "Omada: reading current controller settings")
        current_settings = client.get_controller_setting()

        # Upload cert and key
        log("info", "Omada: uploading fullchain.pem")
        cert_bytes = Path(local.cert_path).read_bytes()
        cert_result = client.upload_cert(cert_bytes, "fullchain.pem")
        log("info", f"Omada: cert uploaded (cerId={cert_result.get('cerId')})")

        log("info", "Omada: uploading privkey.pem")
        with secure_key(local.key_path) as key_ba:
            key_result = client.upload_key(bytes(key_ba), "privkey.pem")
        log("info", f"Omada: key uploaded (keyId={key_result.get('keyId')})")

        # Save — PATCH activates the new cert immediately, no reboot needed
        log("info", "Omada: saving settings to activate new certificate")
        client.save_settings(current_settings, cert_result, key_result)

        # Brief pause then re-probe to confirm
        log("info", "Omada: waiting 5s then verifying new certificate...")
        time.sleep(5)
        try:
            new_fp = probe_tls_fingerprint(hostname, port)
        except Exception:
            new_fp = None

        if new_fp and new_fp == local.fingerprint:
            log("success", "Omada: certificate deployed and verified")
        elif new_fp:
            log("warn", "Omada: cert saved but fingerprint still differs — may take a moment to apply")
        else:
            log("warn", "Omada: cert saved but TLS re-probe failed — verify manually")

        return DeviceResult(
            status=DeployStatus.DEPLOYED,
            message="Certificate deployed",
            live_fingerprint=new_fp,
            local_fingerprint=local.fingerprint,
        )

    except Exception as exc:
        log("error", f"Omada: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)
