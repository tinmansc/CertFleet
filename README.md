# HA Cert Manager

[![Release](https://img.shields.io/github/v/release/tinmansc/ha-cert-manager)](https://github.com/tinmansc/ha-cert-manager/releases)

## Origin Story

This project was developed because the author refused to let a printer, which was working perfectly, have a self-signed certificate.

Things escalated.

---

A Home Assistant add-on that deploys your Let's Encrypt certificate to every device on your network — automatically, from one dashboard.

## Supported devices

| Device | Auth method |
|--------|-------------|
| TrueNAS CORE / CE / Enterprise | API key |
| pfSense | Username + password |
| HP Switch 1950 Series (Comware 7 only) | SSH + XTD CLI |
| Hubitat C-7 | API key |
| TP-Link Omada OC200 / OC300 | Username + password |
| Brother MFC Printer | Web UI password |

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add: `https://github.com/tinmansc/ha-cert-manager`
4. Find **HA Cert Manager** in the store and install it
5. Enable **Show in sidebar** and start the add-on

## Usage

1. Open **Cert Manager** from the sidebar
2. Add your devices using the **+ Add Device** button
3. Click **Verify** to test credentials and check cert status
4. Click **Deploy** (or **Deploy All**) to push the cert
5. Enable **Auto-deploy on renewal** in Settings to have all devices updated automatically when Let's Encrypt renews your cert

## HP Switch 1950 notes

The add-on connects to HP 1950 switches via SSH and uses Comware's PKI system to import the cert. The default XTD-mode password is `foes-bent-pile-atom-ship`. If your switch uses a non-default startup config name, expand **Advanced switch settings** in the device editor and set it (run `display startup` on the switch to check).

## Development

See [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) for the version bump and release process.

Pre-built multi-arch images (amd64 + aarch64) are published to GHCR on every push to `main` via GitHub Actions.
