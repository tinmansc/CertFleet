"""Send notifications to the Home Assistant UI via the Supervisor's Core API proxy.

Uses SUPERVISOR_TOKEN — injected automatically by the Supervisor into
every add-on's environment — instead of a user-managed long-lived access
token. There's no credential to generate, paste into a config file, or
accidentally leak. (An earlier prototype script in this project's history
did exactly that with a hand-pasted HA token, which is part of why it's
no longer used — see RELEASE_CHECKLIST.md.)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_SUPERVISOR_CORE_URL = "http://supervisor/core/api/services/persistent_notification/create"
_SERVICES_URL = "http://supervisor/core/api/services"
_NOTIFY_SERVICE_URL = "http://supervisor/core/api/services/notify/{target}"


def discover_mobile_targets() -> list[str]:
    """Returns notify.mobile_app_* service names registered by the HA
    Companion App, or [] if none exist (or on any failure — best-effort,
    this should never break anything else). These are the ONLY notify.*
    targets CertFleet surfaces: the bell icon (persistent_notification)
    and the Companion App push are the two channels that actually reach
    a typical single-user HA install, so there's no value in also
    listing SMTP/Telegram/etc. services almost nobody has configured."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return []
    try:
        req = urllib.request.Request(
            _SERVICES_URL,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            services = json.loads(resp.read().decode())
        for entry in services:
            if entry.get("domain") == "notify":
                return sorted(n for n in entry.get("services", {}) if n.startswith("mobile_app_"))
        return []
    except Exception:
        return []


def notify_mobile(target: str, title: str, message: str) -> bool:
    """Push a notification via a specific notify.mobile_app_* service.
    Never raises — same contract as notify_ha()."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token or not target:
        return False
    try:
        req = urllib.request.Request(
            _NOTIFY_SERVICE_URL.format(target=target),
            data=json.dumps({"title": title, "message": message}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


def notify_ha(title: str, message: str, notification_id: str = "certfleet") -> bool:
    """Create or update a persistent notification in the HA UI (bell icon).

    Reusing the same notification_id updates the existing card instead of
    stacking a new one on every call — callers should pass a stable,
    purpose-specific id per notification type (e.g. one for deploy
    results, a different one for cert-read failures) so unrelated events
    don't clobber each other.

    Returns True if the call reached Home Assistant successfully. Never
    raises — a notification failure should never break the underlying
    operation it was reporting on.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    try:
        req = urllib.request.Request(
            _SUPERVISOR_CORE_URL,
            data=json.dumps({
                "title": title,
                "message": message,
                "notification_id": notification_id,
            }).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False
