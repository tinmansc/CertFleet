"""Shared types for all device deployers."""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Generator, Optional


class DeployStatus(str, Enum):
    ALREADY_CURRENT = "already_current"
    DEPLOYED = "deployed"        # successful deploy — cert is now current
    NEEDS_DEPLOY = "needs_deploy"  # check-only: cert differs, deploy required
    SKIPPED = "skipped"          # pfsense verify-only mode
    NO_LOCAL_CERT = "no_local_cert"  # check-only: connected/authenticated fine, nothing local to compare against
    ERROR = "error"


@dataclass
class DeviceResult:
    status: DeployStatus
    message: str
    live_fingerprint: Optional[str] = None
    local_fingerprint: Optional[str] = None
    # Non-fatal, "worth knowing about but didn't stop anything" info —
    # e.g. a cert-coverage regression, an API token nearing expiry, a
    # permission that's broader or narrower than it needs to be. Rendered
    # distinctly (amber) from the pass/fail status, and safe to leave None
    # for deployers that have nothing to report. main.py may also set or
    # append to this centrally for checks that apply to every device type
    # (like the cert-coverage comparison), not just device-specific ones.
    warning: Optional[str] = None
    # Absolute path to a full-detail log file for this run, if the deployer
    # wrote one (currently just Comware — the HPE script's raw switch
    # session transcript is too verbose for the shared event log, but the
    # detail has real diagnostic value, so it's kept on disk and made
    # downloadable instead of discarded).
    log_file: Optional[str] = None


# A logger callable that device code uses — captured by the SSE event stream.
Logger = Callable[[str, str], None]   # (level, message)


@contextmanager
def secure_key(path: str) -> Generator[bytearray, None, None]:
    """Read a private key file into a mutable bytearray, then wipe it on exit.

    Yields a bytearray so callers can decode/use it however they need.
    On exit the bytearray is overwritten with os.urandom bytes in-place,
    clearing our copy at a known address.  This is best-effort: immutable
    str/bytes copies held by HTTP libraries are beyond our control.

    Usage:
        with secure_key(local.key_path) as key_ba:
            key_str = key_ba.decode()   # str copy — not wipeable, unavoidable
            api_call(privatekey=key_str)
        # key_ba is now random garbage; key_str will be GC'd normally
    """
    ba = bytearray(Path(path).read_bytes())
    try:
        yield ba
    finally:
        os.urandom(len(ba))          # warm the CSPRNG before writing
        ba[:] = os.urandom(len(ba))  # overwrite in-place


def ensure_https(host: str) -> str:
    """Add https:// scheme if the host has no scheme."""
    host = host.rstrip("/")
    if not host.startswith("http://") and not host.startswith("https://"):
        host = "https://" + host
    return host


def strip_scheme(host: str) -> str:
    """Return just the hostname/IP (no scheme, no port).

    Handles all three forms:
      hostname or IPv4  →  host.example.com  /  192.168.1.1
      IPv6 bracketed    →  [2001:db8::1]:443  →  2001:db8::1
      bare IPv6         →  2001:db8::1        →  2001:db8::1
    """
    h = host.replace("https://", "").replace("http://", "")
    # Bracketed IPv6: [::1] or [::1]:443
    if h.startswith("["):
        end = h.find("]")
        if end != -1:
            return h[1:end]
    # Bare IPv6 (contains multiple colons — more than one colon means IPv6, not host:port)
    if h.count(":") > 1:
        return h  # return the raw address; socket handles bare IPv6 fine
    # Hostname or IPv4 — strip optional :port
    return h.split(":")[0]
