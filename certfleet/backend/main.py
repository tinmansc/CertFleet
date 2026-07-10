"""CertFleet — FastAPI backend."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import crypto_store
import notify
from cert_reader import hostname_covered, probe_tls_names, read_local_cert
from config import DeviceConfig, load_devices, OPTIONS_FILE
from devices.base import DeployStatus, DeviceResult, Logger
from devices.base import strip_scheme
import devices.truenas as truenas
import devices.brother as brother
import devices.hubitat as hubitat
import devices.comware as comware
import devices.omada as omada
import devices.pfsense as pfsense
import devices.proxmox as proxmox


# ── Event log (ring buffer, SSE) ──────────────────────────────────────────────

MAX_LOG = 200
_log_buffer: deque[dict] = deque(maxlen=MAX_LOG)
_log_subscribers: list[asyncio.Queue] = []
_log_counter = 0


def _emit(level: str, message: str, device_id: str | None = None):
    global _log_counter
    _log_counter += 1
    entry = {
        "id": _log_counter,
        # ISO 8601 with a UTC offset (not a bare strftime string) so the
        # frontend can correctly parse it and render it in the browser's
        # local timezone instead of displaying raw UTC unlabeled.
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg": message,
        "device": device_id,
    }
    _log_buffer.appendleft(entry)
    for q in _log_subscribers:
        q.put_nowait(entry)


def _make_logger(device_id: str) -> Logger:
    def log(level: str, message: str):
        _emit(level, message, device_id)
    return log


# ── Local cert change tracking ────────────────────────────────────────────────

_last_cert_fingerprint: str | None = None


def _note_cert_if_changed(local) -> bool:
    """Log serial + SHA256 once at startup and again whenever the fingerprint
    changes, so a renewal is visible in the event log even if no one has the
    dashboard open when it happens. Returns True only for a genuine renewal
    (not the first-time detection at startup) — callers use this to decide
    whether to trigger auto-deploy/check."""
    global _last_cert_fingerprint
    if local is None or local.fingerprint == _last_cert_fingerprint:
        return False
    first_time = _last_cert_fingerprint is None
    _last_cert_fingerprint = local.fingerprint
    _emit(
        "info" if first_time else "success",
        f"{'Certificate detected' if first_time else 'Certificate changed'}: "
        f"{local.domain} — serial {local.serial}, SHA256 {local.fingerprint}",
    )
    return not first_time


# ── Staging/test certificate tracking ─────────────────────────────────────────

STAGING_NOTIFY_INTERVAL_SECONDS = 24 * 60 * 60
_staging_active = False
_staging_last_notified: datetime | None = None


def _check_staging(local, notify_enabled: bool) -> None:
    """Runs every poll tick (independent of whether the cert just changed) so
    a staging cert that sits loaded for days keeps getting flagged, not just
    once at the moment it first appeared — this is meant to reach someone who
    only glances at HA notifications occasionally, not just an open tab."""
    global _staging_active, _staging_last_notified
    if local is None:
        return
    now = datetime.now(timezone.utc)
    if local.is_staging:
        due = _staging_last_notified is None or \
            (now - _staging_last_notified).total_seconds() >= STAGING_NOTIFY_INTERVAL_SECONDS
        if due:
            _emit("warn", f"Test/staging certificate detected at {local.cert_path} — "
                           f"auto-deploy is paused until a valid certificate is issued")
            if notify_enabled:
                notify.notify_ha(
                    "CertFleet — test/staging certificate active",
                    f"A Let's Encrypt STAGING (untrusted) certificate is currently loaded at "
                    f"{local.cert_path}. Auto-deploy is paused — devices will not receive this "
                    f"certificate until a valid one is issued.",
                    notification_id="certfleet_staging",
                )
            _staging_last_notified = now
        _staging_active = True
    else:
        if _staging_active:
            _emit("success", f"Valid certificate restored at {local.cert_path} — "
                               f"auto-deploy has resumed normal operation")
            if notify_enabled:
                notify.notify_ha(
                    "CertFleet — valid certificate restored",
                    f"The certificate at {local.cert_path} is no longer a staging/test "
                    f"certificate. Auto-deploy has resumed normal operation.",
                    notification_id="certfleet_staging",
                )
        _staging_active = False
        _staging_last_notified = None


# ── Server-side poll loop ─────────────────────────────────────────────────────
# Auto-deploy, cert-change detection, and the notifications below previously
# only ran from a setInterval in the React frontend — meaning a CertFleet
# instance with no browser tab open did none of this. This loop is the fix:
# it runs for as long as the backend process is alive, regardless of the UI.

CERT_FAIL_NOTIFY_THRESHOLD = 3
DEFAULT_POLL_INTERVAL_MS = 900_000  # 15 min — matches the frontend's own default
_cert_fail_streak = 0
_cert_fail_notified = False
_poller_task: asyncio.Task | None = None


async def _poll_tick() -> None:
    global _cert_fail_streak, _cert_fail_notified
    from cert_reader import DEFAULT_CERT_PATH, DEFAULT_KEY_PATH  # noqa: PLC0415

    cfg = crypto_store.load_config(logger=_emit)
    notify_enabled = cfg.get("notify_enabled", True)
    cert_path = cfg.get("cert_path") or DEFAULT_CERT_PATH
    key_path = cfg.get("key_path") or DEFAULT_KEY_PATH

    try:
        local = read_local_cert(cert_path, key_path)
    except Exception:
        _cert_fail_streak += 1
        if _cert_fail_streak == CERT_FAIL_NOTIFY_THRESHOLD and not _cert_fail_notified:
            _cert_fail_notified = True
            _emit("warn", f"Certificate unreadable for {CERT_FAIL_NOTIFY_THRESHOLD} consecutive checks "
                           f"({cert_path})")
            if notify_enabled:
                notify.notify_ha(
                    "CertFleet — certificate unreadable",
                    f"The local certificate file has failed to read for {CERT_FAIL_NOTIFY_THRESHOLD} "
                    "consecutive checks. Check the cert paths in Settings.",
                    notification_id="certfleet_cert_error",
                )
        return

    _cert_fail_streak = 0
    if _cert_fail_notified:
        _cert_fail_notified = False
        _emit("info", "Certificate readable again after a period of failure")
        if notify_enabled:
            notify.notify_ha(
                "CertFleet — certificate readable again",
                "The local certificate file is readable again after a period of failure.",
                notification_id="certfleet_cert_error",
            )

    changed = _note_cert_if_changed(local)
    _check_staging(local, notify_enabled)

    if not changed:
        return

    if local.is_staging:
        await check_all(auto=True)
    elif cfg.get("auto_deploy_on_renewal", False):
        await deploy_all(auto=True)
    else:
        await check_all(auto=True)


async def _poll_loop() -> None:
    while True:
        try:
            await _poll_tick()
        except Exception as exc:
            _emit("error", f"Poll loop error: {exc}")
        try:
            cfg = crypto_store.load_config()
            interval_ms = cfg.get("poll_interval_ms") or DEFAULT_POLL_INTERVAL_MS
        except Exception:
            interval_ms = DEFAULT_POLL_INTERVAL_MS
        await asyncio.sleep(max(interval_ms, 5_000) / 1000)


# ── Device status cache ───────────────────────────────────────────────────────

_device_status: dict[str, dict] = {}   # id -> {status, last_run, last_result}
_running: set[str] = set()
_last_backup: dict[str, str] = {}      # device_id -> absolute path of last backup file


def _status_for(dev: DeviceConfig) -> dict:
    s = _device_status.get(dev.id, {})
    return {
        "id": dev.id,
        "name": dev.name,
        "type": dev.type,
        "enabled": dev.enabled,
        "host": dev.host,
        "running": dev.id in _running,
        "last_run": s.get("last_run"),
        "last_status": s.get("last_status"),
        "last_message": s.get("last_message"),
        "live_fingerprint": s.get("live_fingerprint"),
        "last_warning": s.get("last_warning"),
        "pfsense_allow_upload": getattr(dev, "pfsense_allow_upload", False),
        "proxmox_allow_upload": getattr(dev, "proxmox_allow_upload", False),
    }


# ── App lifespan ──────────────────────────────────────────────────────────────

def _migrate_legacy_config():
    """Move /data/options.json → OPTIONS_FILE if the new location doesn't exist yet.

    Writes plain JSON, matching the legacy file's format — crypto_store's
    own load_config() transparently encrypts it the first time it's read,
    so no separate encryption step is needed here.
    """
    legacy = Path("/data/options.json")
    if not OPTIONS_FILE.exists() and legacy.exists():
        OPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        OPTIONS_FILE.write_text(legacy.read_text())
        _emit("info", f"Migrated config from {legacy} → {OPTIONS_FILE}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    OPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_store.migrate_from_old_slug(logger=_emit)
    _migrate_legacy_config()
    try:
        crypto_store.load_config(logger=_emit)
    except crypto_store.DecryptionError as exc:
        # Fail loud at startup rather than silently serving an empty device
        # list — the UI surfaces this via /api/config and /api/devices too,
        # but logging it immediately means it's the first thing visible.
        _emit("error", f"Could not decrypt device configuration: {exc}. "
                        "Use Settings → Encryption Key to restore or reset the key.")
    _emit("info", "CertFleet started")
    global _poller_task
    _poller_task = asyncio.create_task(_poll_loop())
    yield
    if _poller_task:
        _poller_task.cancel()
        try:
            await _poller_task
        except asyncio.CancelledError:
            pass
    _emit("info", "CertFleet shutting down")


app = FastAPI(title="CertFleet", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

STATIC = Path("/app/static")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    assets_dir = STATIC / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    try:
        cfg = crypto_store.load_config(logger=_emit)
        return cfg if cfg else {"devices": []}
    except crypto_store.DecryptionError as e:
        raise HTTPException(409, f"Could not decrypt device configuration: {e}")


@app.post("/api/config")
def save_config(body: dict):
    crypto_store.save_config(body)
    _emit("info", "Device configuration saved")
    return {"ok": True}


# ── Encryption key management ────────────────────────────────────────────────

@app.get("/api/security/key")
def get_key():
    """Return the current master key so it can be displayed/copied in Settings.

    Reachable only through the HA ingress-authenticated proxy, same trust
    boundary as every other endpoint here — see DOCS.md for the security
    model.
    """
    return {"key": crypto_store.ensure_key(logger=_emit).decode()}


@app.post("/api/security/rotate-key")
def rotate_key():
    """Generate a new random key and re-encrypt existing config under it. Safe — no data loss."""
    try:
        crypto_store.rotate_key(logger=_emit)
        return {"ok": True, "key": crypto_store.ensure_key(logger=_emit).decode()}
    except Exception as e:
        raise HTTPException(500, f"Key rotation failed: {e}")


@app.post("/api/security/set-key")
def set_key(body: dict):
    """Adopt a manually-provided key — restores if it decrypts existing data,
    otherwise requires force=true (the UI's typed "NO RECOVERY" gate) since
    that path discards every stored credential."""
    key = (body.get("key") or "").strip()
    force = bool(body.get("force", False))
    if not key:
        raise HTTPException(400, "No key provided")
    try:
        data = crypto_store.set_key(key, force=force, logger=_emit)
        return {"ok": True, "devices": len(data.get("devices", []))}
    except crypto_store.DecryptionError:
        raise HTTPException(409, "This key does not match the existing configuration. "
                                  "Retry with force=true to accept permanent data loss.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to set key: {e}")


@app.post("/api/notify")
def notify_route(body: dict):
    """Passthrough for client-detected conditions the frontend wants surfaced
    in the HA UI (e.g. the local cert becoming unreadable for several
    consecutive polls) — auto-triggered device deploy/check results are
    notified directly from the backend instead, see deploy_all/check_all."""
    ok = notify.notify_ha(
        body.get("title", "CertFleet"),
        body.get("message", ""),
        body.get("notification_id", "certfleet"),
    )
    return {"ok": ok}


@app.post("/api/verify-host")
def verify_host(body: dict):
    import socket as _socket
    import time as _time
    from devices.base import strip_scheme
    raw  = (body.get("host") or "").strip()
    port = int(body.get("port") or 443)
    host = strip_scheme(raw)
    if not host:
        return {"ok": False, "message": "No hostname provided", "latency_ms": -1}
    start = _time.time()
    try:
        sock = _socket.create_connection((host, port), timeout=5)
        sock.close()
        ms = int((_time.time() - start) * 1000)
        return {"ok": True, "message": f"Reachable — {ms} ms", "latency_ms": ms}
    except _socket.timeout:
        return {"ok": False, "message": f"Timed out after 5 s ({host}:{port})", "latency_ms": -1}
    except ConnectionRefusedError:
        return {"ok": False, "message": f"Connection refused on port {port}", "latency_ms": -1}
    except Exception as e:
        return {"ok": False, "message": str(e), "latency_ms": -1}


@app.get("/api/cert")
def get_cert():
    from cert_reader import DEFAULT_CERT_PATH, DEFAULT_KEY_PATH  # noqa: PLC0415
    try:
        cfg = crypto_store.load_config()
        cert_path = cfg.get("cert_path") or DEFAULT_CERT_PATH
        key_path  = cfg.get("key_path")  or DEFAULT_KEY_PATH
        local = read_local_cert(cert_path, key_path)
        return asdict(local)
    except crypto_store.DecryptionError as e:
        raise HTTPException(409, f"Could not decrypt device configuration: {e}")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, f"Permission denied reading certificate file: {e}")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/supervisor/addon-info")
def get_addon_info():
    """Query the HA Supervisor for update availability."""
    import urllib.request as _urllib
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {"update_available": False, "version_latest": None, "version": None}
    try:
        req = _urllib.Request(
            "http://supervisor/addons/self/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _urllib.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        addon = data.get("data", {})
        return {
            "update_available": addon.get("update_available", False),
            "version_latest": addon.get("version_latest"),
            "version": addon.get("version"),
        }
    except Exception:
        return {"update_available": False, "version_latest": None, "version": None}


def _load_devices_or_409() -> list[DeviceConfig]:
    """load_devices(), converting a decryption failure into a clear 409
    instead of a bare 500 — every route that touches devices goes through
    this so the failure mode is consistent no matter which endpoint hit it."""
    try:
        return load_devices()
    except crypto_store.DecryptionError as e:
        raise HTTPException(409, f"Could not decrypt device configuration: {e}")


@app.get("/api/devices")
def get_devices():
    devices = _load_devices_or_409()
    return [_status_for(d) for d in devices]


@app.post("/api/devices/{device_id}/check")
async def check_device(device_id: str):
    return await _run_device(device_id, deploy=False)


@app.post("/api/devices/{device_id}/deploy")
async def deploy_device(device_id: str):
    return await _run_device(device_id, deploy=True)


@app.post("/api/devices/{device_id}/backup")
async def backup_device(device_id: str):
    """Trigger an OC200 config backup and save it to /config/backups/omada/."""
    devices = _load_devices_or_409()
    dev = next((d for d in devices if d.id == device_id), None)
    if dev is None:
        raise HTTPException(404, f"Device '{device_id}' not found")
    if dev.type != "omada":
        raise HTTPException(400, "Backup is only supported for Omada devices")
    if device_id in _running:
        raise HTTPException(409, f"Device '{device_id}' is already running")

    _running.add(device_id)
    _emit("info", f"Starting config backup for {dev.name}", device_id)
    try:
        log = _make_logger(device_id)
        path: str = await asyncio.get_event_loop().run_in_executor(
            None, omada.backup_config, dev, log
        )
        _last_backup[device_id] = path
        _emit("success", f"{dev.name}: backup saved → {path}", device_id)
        return {"path": path, "filename": Path(path).name}
    except Exception as exc:
        _emit("error", f"{dev.name}: backup failed — {exc}", device_id)
        raise HTTPException(500, str(exc))
    finally:
        _running.discard(device_id)


@app.get("/api/devices/{device_id}/backup/latest")
async def download_latest_backup(device_id: str):
    """Download the most recently saved backup file for this device."""
    path = _last_backup.get(device_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "No backup available — run a backup first")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=Path(path).name,
    )


def _notify_deploy_summary(result_map: dict, triggered_by: str) -> None:
    """Send an HA notification summarizing an auto-triggered deploy run.

    Only called when auto=true (a cert renewal was detected, not a
    manual button click) — these are infrequent (every 60-90 days per
    device with Let's Encrypt) so there's no need to throttle or
    deduplicate the way we do for the cert-read-failure alert below.
    """
    total = len(result_map)
    failed = [r for r in result_map.values() if r.get("last_status") == "error"]
    if failed:
        title = "CertFleet — deploy had failures"
        message = (f"Auto-deploy after {triggered_by}: {total - len(failed)}/{total} devices succeeded.\n"
                   f"Failed: {', '.join(r['name'] for r in failed)}")
    else:
        title = "CertFleet — deploy succeeded"
        message = f"Auto-deploy after {triggered_by}: all {total} device(s) updated successfully."
    notify.notify_ha(title, message, notification_id="certfleet_deploy")


def _notify_needs_deploy(result_map: dict, triggered_by: str) -> None:
    """Notify when a cert renewal was detected but auto-deploy is off —
    the user needs to know a manual deploy is now waiting on them."""
    needs = [r for r in result_map.values() if r.get("last_status") == "needs_deploy"]
    if needs:
        notify.notify_ha(
            "CertFleet — certificate renewed",
            f"{triggered_by}. {len(needs)} device(s) need a manual deploy: "
            f"{', '.join(r['name'] for r in needs)}",
            notification_id="certfleet_renewal",
        )


@app.post("/api/devices/deploy-all")
async def deploy_all(auto: bool = False):
    enabled = [dev for dev in _load_devices_or_409() if dev.enabled]
    results = await asyncio.gather(*[_run_device(dev.id, deploy=True) for dev in enabled])
    result_map = dict(zip([dev.id for dev in enabled], results))
    if auto:
        _notify_deploy_summary(result_map, triggered_by="a detected certificate renewal")
    return result_map


@app.post("/api/devices/check-all")
async def check_all(auto: bool = False):
    enabled = [dev for dev in _load_devices_or_409() if dev.enabled]
    results = await asyncio.gather(*[_run_device(dev.id, deploy=False) for dev in enabled])
    result_map = dict(zip([dev.id for dev in enabled], results))
    if auto:
        _notify_needs_deploy(result_map, triggered_by="A new certificate was detected")
    return result_map


@app.get("/api/events")
async def event_stream():
    q: asyncio.Queue = asyncio.Queue()
    _log_subscribers.append(q)

    async def generate() -> AsyncGenerator[str, None]:
        # Send buffered history first
        for entry in reversed(list(_log_buffer)):
            yield f"data: {json.dumps(entry)}\n\n"
        try:
            while True:
                entry = await asyncio.wait_for(q.get(), timeout=30)
                yield f"data: {json.dumps(entry)}\n\n"
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                _log_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/{full_path:path}", response_class=HTMLResponse)
async def serve_spa(full_path: str = ""):
    index = STATIC / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>CertFleet</h1><p>Frontend not built yet.</p>")


# ── Cert coverage check ───────────────────────────────────────────────────────
#
# There is no way to know, from inside this app, what hostname a browser
# will actually type in to reach a given device — device.host is just
# whatever address we use to connect to it, which is very often an
# internal-only name or a bare IP, not the externally-meaningful one.
# Reverse DNS doesn't solve this either (PTR records are frequently stale,
# generic, or unset on internal networks). So this deliberately does NOT
# try to determine "the right" hostname. Instead it checks two things we
# CAN know with certainty:
#   1. Regression: does the new cert cover everything the device's
#      CURRENT live certificate already covers? This needs no DNS trust
#      at all — we read both certs directly and compare. If the device is
#      currently reachable under 4 names and the new cert only covers 1,
#      that's real, unambiguous signal regardless of which name "really"
#      matters.
#   2. A hedged, best-effort note: is the configured device.host itself
#      covered? Often it genuinely is the externally-meaningful name, so
#      this has real value — but the message says plainly that it's just
#      a heads-up based on configuration, not a verified fact.
# Best-effort throughout: any probe failure here is swallowed silently —
# this is a bonus safety net, not a requirement, and the device's own
# check/deploy logic already handles real connectivity failures.
def _check_cert_coverage(dev: DeviceConfig, local) -> Optional[str]:
    if local is None:
        return None

    new_names = set(local.sans) | {local.domain}
    hostname = strip_scheme(dev.host)
    port = dev.port or 443
    legacy = dev.type == "comware"

    warnings = []

    try:
        live_names = probe_tls_names(hostname, port, legacy=legacy)
        missing = sorted(n for n in live_names if not hostname_covered(n, new_names))
        if missing:
            warnings.append(
                f"New certificate does not cover {', '.join(missing)}, which the device's "
                f"current certificate does cover — access via those names may break."
            )
    except Exception:
        pass  # best-effort only; the deployer's own probe already surfaces real connectivity issues

    if not hostname_covered(hostname, new_names):
        warnings.append(
            f"Note: the configured hostname for this device ({hostname}) is not covered by "
            f"the new certificate. This is only a heads-up based on how the device is set up "
            f"here — if this is an internal-only address and the device is actually reached "
            f"externally under a different name, this may not apply."
        )

    return " ".join(warnings) if warnings else None


# ── Device dispatch ───────────────────────────────────────────────────────────

_DEPLOYERS = {
    "truenas": (truenas.check, truenas.deploy),
    "brother": (brother.check, brother.deploy),
    "hubitat": (hubitat.check, hubitat.deploy),
    "comware": (comware.check, comware.deploy),
    "omada":   (omada.check,   omada.deploy),
    "pfsense": (pfsense.check, pfsense.deploy),
    "proxmox": (proxmox.check, proxmox.deploy),
}


async def _run_device(device_id: str, deploy: bool) -> dict:
    devices = _load_devices_or_409()
    dev = next((d for d in devices if d.id == device_id), None)
    if dev is None:
        raise HTTPException(404, f"Device '{device_id}' not found")

    if device_id in _running:
        raise HTTPException(409, f"Device '{device_id}' is already running")

    deployers = _DEPLOYERS.get(dev.type)
    if deployers is None:
        raise HTTPException(400, f"Unknown device type '{dev.type}'")

    _running.add(device_id)
    action = "deploy" if deploy else "check"
    _emit("info", f"Starting {action} for {dev.name}", device_id)

    try:
        cfg_data = crypto_store.load_config()
        from cert_reader import DEFAULT_CERT_PATH, DEFAULT_KEY_PATH
        log = _make_logger(device_id)
        try:
            local = read_local_cert(
                cfg_data.get("cert_path") or DEFAULT_CERT_PATH,
                cfg_data.get("key_path")  or DEFAULT_KEY_PATH,
            )
        except FileNotFoundError:
            if deploy:
                # Deploying means pushing a real cert — there's nothing to push.
                raise
            # Verify/check can still be useful with no local cert to compare:
            # it proves credentials and connectivity work, and reports the
            # device's own live cert info. Deployers treat local=None as
            # "connected fine, nothing to compare against" rather than crashing.
            local = None
            log("warn", f"{dev.name}: no local certificate available — "
                        f"verifying connectivity and credentials only")
        fn = deployers[1] if deploy else deployers[0]

        result: DeviceResult = await asyncio.get_event_loop().run_in_executor(
            None, fn, dev, local, log
        )

        coverage_warning = await asyncio.get_event_loop().run_in_executor(
            None, _check_cert_coverage, dev, local
        )
        combined_warning = " ".join(w for w in (result.warning, coverage_warning) if w) or None
        if combined_warning:
            _emit("warn", f"{dev.name}: {combined_warning}", device_id)

        _device_status[device_id] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_status": result.status.value,
            "last_message": result.message,
            "live_fingerprint": result.live_fingerprint,
            "last_warning": combined_warning,
        }
        level = "success" if result.status != DeployStatus.ERROR else "error"
        _emit(level, f"{dev.name}: {result.message}", device_id)
        return _status_for(dev)

    except Exception as exc:
        _device_status[device_id] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_status": "error",
            "last_message": str(exc),
            "live_fingerprint": None,
        }
        _emit("error", f"{dev.name}: unexpected error — {exc}", device_id)
        raise HTTPException(500, str(exc))
    finally:
        _running.discard(device_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="warning", access_log=False)
