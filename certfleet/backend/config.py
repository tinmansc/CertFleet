"""Loads device configuration from the encrypted config store (crypto_store.py)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import crypto_store

# Kept as an alias — other modules import OPTIONS_FILE as "the config file path"
# for logging/display purposes. The file on disk is now an encrypted Fernet
# token, not raw JSON; always go through crypto_store.load_config() /
# save_config() to read or write it.
OPTIONS_FILE = crypto_store.CONFIG_FILE


@dataclass
class DeviceConfig:
    id: str
    name: str
    type: str          # truenas | brother | hubitat | comware | omada | pfsense | netdata
    enabled: bool
    host: str
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    site_id: Optional[str] = None
    pki_domain: Optional[str] = None
    ssl_policy: Optional[str] = None
    startup_config_path: Optional[str] = None
    hubitat_cli_path: str = "/config/scripts/hubitat-cli"
    p12_password: Optional[str] = None
    delete_old_certs: bool = True
    verify_tls: bool = True
    pfsense_allow_upload: bool = False
    proxmox_allow_upload: bool = False
    omadac_id: Optional[str] = None   # 32-char hex; auto-discovered if omitted
    # Jail-hosted devices (netdata, and future grafana/influxdb/gitlab types) —
    # `host`/`port` above stay the TLS-probe target (the jail's own dashboard
    # address), these are for reaching the TrueNAS host that owns the jail.
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    ssh_username: Optional[str] = None
    ssh_private_key: Optional[str] = None   # unencrypted PEM, pasted in device editor
    jail_name: Optional[str] = None


def load_devices(logger=None) -> list[DeviceConfig]:
    raw = crypto_store.load_config(logger=logger)
    devices = []
    for d in raw.get("devices", []):
        devices.append(DeviceConfig(
            id=d["id"],
            name=d["name"],
            type=d["type"],
            enabled=d.get("enabled", True),
            host=d["host"],
            port=d.get("port"),
            username=d.get("username"),
            password=d.get("password"),
            api_key=d.get("api_key"),
            site_id=d.get("site_id"),
            pki_domain=d.get("pki_domain"),
            ssl_policy=d.get("ssl_policy"),
            startup_config_path=d.get("startup_config_path"),
            hubitat_cli_path=d.get("hubitat_cli_path", "/config/scripts/hubitat-cli"),
            p12_password=d.get("p12_password", "changeme"),
            delete_old_certs=d.get("delete_old_certs", True),
            verify_tls=d.get("verify_tls", True),
            pfsense_allow_upload=d.get("pfsense_allow_upload", False),
            proxmox_allow_upload=d.get("proxmox_allow_upload", False),
            omadac_id=d.get("omadac_id"),
            ssh_host=d.get("ssh_host"),
            ssh_port=d.get("ssh_port", 22),
            ssh_username=d.get("ssh_username"),
            ssh_private_key=d.get("ssh_private_key"),
            jail_name=d.get("jail_name"),
        ))
    return devices
