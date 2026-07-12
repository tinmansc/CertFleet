"""HPE Comware switch certificate deployer.

Delegates to deploy_cert_hpe_1950.py via subprocess. All switch configuration
comes from the CertFleet device editor — no manual YAML files required.

Device editor fields used:
  Username         → SSH login username
  Password         → SSH login password
  XTD CLI Password → (api_key field) xtd-cli-mode enable password
  Switch IP (SSH)  → (site_id field) management IP for SSH; defaults to hostname
  PKI domain       → Comware pki-domain name (default: hp-1950)
  SSL policy       → Comware ssl server-policy name (default: hp-1950)
  Startup config   → startup_config_path (default: flash:/startup.cfg)

Script lookup order:
  1. cfg.comware_script_path (if set)
  2. /config/scripts/deploy_cert_hpe_1950.py  (user-placed override)
  3. /app/scripts/deploy_cert_hpe_1950.py      (bundled default)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from cert_reader import LocalCert, probe_tls_fingerprint
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, strip_scheme


_USER_SCRIPT    = "/config/scripts/deploy_cert_hpe_1950.py"
_BUNDLED_SCRIPT = "/app/scripts/deploy_cert_hpe_1950.py"

# The HPE script prints a full interactive-troubleshooting-style transcript
# (raw switch CLI output, `display startup`, `dir flash:/pki`, etc.) — real
# diagnostic value, but not something to pipe line-by-line into the shared
# event log used by every other device. Kept on disk instead, one file per
# device (overwritten each run — this is "what just happened," not a
# history), downloadable from the device card.
LOG_DIR = Path("/config/certfleet/logs")


def _resolve_script(cfg: DeviceConfig) -> str:
    explicit = getattr(cfg, "comware_script_path", None)
    if explicit:
        return explicit
    if Path(_USER_SCRIPT).exists():
        return _USER_SCRIPT
    return _BUNDLED_SCRIPT


def _safe_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", name).strip("-")
    return f"{safe or 'comware-device'}.log"


def _write_transcript(cfg: DeviceConfig, output: str) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / _safe_filename(cfg.name)
    path.write_text(output)
    return str(path)


def _extract_error_summary(output: str) -> Optional[str]:
    """The script prints its own 'ERROR on <switch>: ...' line for anything
    that actually went wrong — surfacing just that gives a specific, useful
    error instead of only 'script exited 1'."""
    for line in reversed(output.splitlines()):
        if "ERROR on " in line:
            return line.strip()
    return None


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, mode="check")


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, mode="deploy")


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, mode: str) -> DeviceResult:
    script = _resolve_script(cfg)

    if not Path(script).exists():
        msg = (
            f"Comware script not found at {script}. "
            "Place deploy_cert_hpe_1950.py at /config/scripts/ or set comware_script_path."
        )
        log("error", f"Comware [{cfg.name}]: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg)

    hostname = strip_scheme(cfg.host)
    port     = cfg.port or 443

    # Build a single-switch inventory YAML for this device and pass it via --switches-file.
    # This means the user never needs to edit hpe1950_switches.yaml manually.
    switch_entry = {
        "switches": {
            cfg.name: {
                "host":           hostname,
                "ip":             hostname,
                "pki_domain":     cfg.pki_domain     or "hp-1950",
                "ssl_policy":     cfg.ssl_policy      or "hp-1950",
                "startup_config": cfg.startup_config_path or "flash:/startup.cfg",
            }
        }
    }

    switches_tmp = None
    live_fp = None  # guaranteed defined even if an exception hits before the TLS probe below
    log_file = None  # guaranteed defined even if an exception hits before the script runs
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, prefix="comware_sw_"
        ) as f:
            yaml.dump(switch_entry, f)
            switches_tmp = f.name

        log("info", f"Comware [{cfg.name}]: checking current certificate via SSH")

        # TLS fingerprint probe (uses legacy ciphers for HP 1950)
        try:
            live_fp = probe_tls_fingerprint(hostname, port, legacy=True)
            if local is not None:
                match_str = "cert matches" if live_fp == local.fingerprint else "cert differs"
                log("info", f"Comware [{cfg.name}]: TLS probe — {match_str}")
        except Exception as e:
            log("warn", f"Comware [{cfg.name}]: TLS probe failed ({e}), proceeding")
            live_fp = None

        if local is None and mode == "deploy":
            raise RuntimeError("No local certificate available to deploy")

        # Pass credentials via env vars so the user doesn't need /config/secrets.yaml
        env = os.environ.copy()
        env.update({
            "HPE_SWITCH_USER":     cfg.username or "",
            "HPE_SWITCH_PASSWORD": cfg.password  or "",
            "HPE_XTD_PASSWORD":    cfg.api_key   or "",
        })

        cmd = [sys.executable, script,
               "--switches-file", switches_tmp,
               "--target", cfg.name,
               "--check" if mode == "check" else "--apply"]

        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        combined_output = result.stdout + result.stderr
        log_file = _write_transcript(cfg, combined_output)
        log("info", f"Comware [{cfg.name}]: full switch session log saved — download it from this device's card")

        if result.returncode != 0:
            summary = _extract_error_summary(combined_output) or f"Script exited {result.returncode}"
            log("error", f"Comware [{cfg.name}]: {summary}")
            return DeviceResult(status=DeployStatus.ERROR, message=summary,
                                 live_fingerprint=live_fp, log_file=log_file)

        try:
            new_fp = probe_tls_fingerprint(hostname, port, legacy=True)
        except Exception:
            new_fp = live_fp

        if mode == "deploy":
            status = DeployStatus.DEPLOYED
        elif local is None:
            status = DeployStatus.NO_LOCAL_CERT
        elif new_fp and new_fp == local.fingerprint:
            status = DeployStatus.ALREADY_CURRENT
        else:
            status = DeployStatus.NEEDS_DEPLOY
        log("success", f"Comware [{cfg.name}]: {'deployed' if mode == 'deploy' else 'verified'} successfully")
        return DeviceResult(
            status=status,
            message=f"{'Deployed' if mode == 'deploy' else 'Verified'} successfully"
                     + ("" if local is not None else " (no local cert to compare)"),
            live_fingerprint=new_fp,
            local_fingerprint=local.fingerprint if local is not None else None,
            log_file=log_file,
        )

    except subprocess.TimeoutExpired:
        msg = "Script timed out after 300s"
        log("error", f"Comware [{cfg.name}]: {msg}")
        return DeviceResult(status=DeployStatus.ERROR, message=msg,
                             live_fingerprint=live_fp, log_file=log_file)
    except Exception as exc:
        log("error", f"Comware [{cfg.name}]: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc),
                             live_fingerprint=live_fp, log_file=log_file)
    finally:
        if switches_tmp:
            Path(switches_tmp).unlink(missing_ok=True)
