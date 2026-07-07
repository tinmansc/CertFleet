# HA Cert Manager

Unified Let's Encrypt certificate deployment dashboard for all your network devices. Deploy your HA-managed certs to firewalls, switches, NAS devices, and more — all from one place.

## First-time setup

After installing the add-on, configure these controls before hitting Start:

| Control | Recommended | Why |
|---|---|---|
| **Start on boot** | On | Add-on starts automatically when HA restarts |
| **Watchdog** | On | HA restarts the add-on if it crashes |
| **Auto update** | On | Picks up new device support and bug fixes |
| **Show in sidebar** | **On** | Adds Cert Manager to the HA sidebar for one-click access |

> **Show in sidebar is off by default.** Make sure to toggle it on — otherwise you won't have a shortcut to the dashboard.

Once all four are set, click **Start**.

## Opening the dashboard

After the add-on starts, click **Open Web UI** at the top of the add-on page, or use the **Cert Manager** entry in your sidebar (if you enabled Show in sidebar above).

## Configuring devices

On first launch the dashboard will be empty. Click **Add Device** and fill in the connection details for each network device you want to deploy certificates to. Supported device types:

- **Unifi** — UDM, UDM Pro, UDM SE, UniFi OS consoles
- **Synology** — DSM 6 and DSM 7 NAS devices
- **QNAP** — QTS NAS devices
- **HPE / Aruba** — Comware 7 switches (1950 series)
- **Proxmox** — PVE and PBS nodes
- **Generic HTTPS** — any device that accepts a cert upload via HTTPS

## SSL certificate path

The add-on reads your Let's Encrypt certificates from the HA `/ssl` volume (mounted read-only). The standard HA cert paths are:

```
/ssl/fullchain.pem
/ssl/privkey.pem
```

If you use a custom path (e.g. from the Duck DNS or Let's Encrypt add-ons), match those paths in each device's configuration.

## Support

Open an issue at [github.com/tinmansc/ha-cert-manager](https://github.com/tinmansc/ha-cert-manager).
