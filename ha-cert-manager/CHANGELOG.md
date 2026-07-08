# Changelog

All notable changes to HA Cert Manager are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.13] — 2026-07-08

### Fixed
- **Version badge and update-available indicator disappeared whenever there was no valid local certificate** — both lived inside the cert banner's "certificate successfully loaded" branch only, so on any install without a readable cert (including a fresh install, before Let's Encrypt has issued anything) they never rendered at all. Moved both into the persistent header next to the polling indicator, where they're always visible regardless of cert state.

---

## [1.0.12] — 2026-07-08

### Added
- **Standalone disaster-recovery script** — `scripts/decrypt_config.py` decrypts `config.json` using `master.key` without the app (or any other file in this repo) needing to run. Bundled into the Docker image alongside the other helper scripts. Handles both the current encrypted format and old pre-1.0.10 plaintext configs, and points at `config.json.bak` if the given key doesn't match.
- **DOCS.md — "Encrypted device configuration" section** documenting the three files under `/config/ha_cert_manager/`, why Home Assistant's own Backups feature is the only real protection against losing all of them at once (especially relevant on Pi/SD-card hardware, not just a well-backed-up server), and the manual recovery steps.
- **Self-healing config recovery** — `load_config()` now recovers automatically, with a loud log entry, in two situations instead of failing outright:
  - `config.json` is missing but `config.json.bak` exists (accidental deletion, an interrupted write) — restores from the backup and re-saves it as the live file.
  - `config.json` exists but won't decrypt with the current key, while `config.json.bak` does — the signature of an interrupted key rotation (new ciphertext written, new key file write failed partway) — restores from the backup automatically.
- **Orphaned-key detection** — generating a new encryption key when `config.json` already exists (i.e. the original key went missing separately from the data it protects) now logs a loud, specific error explaining that the existing configuration is unreadable and unrecoverable without a backup of the old key, instead of silently minting a replacement and leaving the mismatch to surface later as a generic decryption failure.
- **Preflight writability check** before key rotation or a manual key change touches anything — probes that `/config/ha_cert_manager/` is actually writable first. A read-only SD card (a real Pi failure mode) now fails cleanly upfront with nothing mutated, rather than potentially writing new ciphertext and then failing to write the matching new key, which would leave the two permanently out of sync.
- **Self-verification after key rotation / key changes** — before reporting success, both operations now actually decrypt the freshly-written `config.json` with the freshly-written key to prove the round trip works, instead of just assuming both writes landed correctly.

### Fixed
- **Inaccurate claim in `rotate_key()`'s docstring** — it asserted a crash mid-rotation could "never leave a mismatched combination," which wasn't true (a mismatch was possible if the ciphertext write succeeded but the key-file write failed). The preflight check and self-verification above close this gap for real instead of just describing it away.

---

## [1.0.11] — 2026-07-08

### Fixed
- **Copy-to-clipboard silently did nothing** — the encryption key Copy button used `navigator.clipboard?.writeText(...)`, which short-circuits to `undefined` with no error and no rejection when the Clipboard API is unavailable (notably inside a cross-origin iframe without an explicit permissions policy — exactly how Home Assistant embeds add-on ingress UIs). Added a `document.execCommand('copy')` fallback for when the modern API is blocked, and the button now shows an explicit "copy failed, select and copy manually" message instead of failing invisibly either way.
- **Settings controls always claimed "saved" even when the write failed** — `saveConfig()` only checked that `fetch()` didn't throw, but `fetch()` resolves normally on a 4xx/5xx from the server; a rejected save was previously indistinguishable from a real one. `saveConfig()` now checks `response.ok` and returns real success/failure, and the Poll Interval and Save Paths controls in Settings roll back their optimistic UI update and show a red "save failed" state when the write doesn't actually land, instead of a silent false positive.

### Added
- **Consistent "did something" feedback across Settings** — every action button (Rotate, Set/Restore Key, Save Paths) and the Poll Interval dropdown now flashes the same green used by the existing Refresh Local Cert Info button on success, and a red state on failure, so the whole app speaks one visual language for confirming an action actually happened.
- **Polling indicator pulses on every check** — the Wifi icon next to "polling" now visibly pulses each time a poll tick actually fires (success or failure), as a lightweight substitute for a countdown timer that confirms the polling loop is alive without adding UI clutter.

---

## [1.0.10] — 2026-07-08

### Added
- **Encrypted device configuration at rest** — `config.json` (device hostnames, usernames, passwords, API keys) is now encrypted as a single whole-file Fernet token instead of stored as plaintext JSON. Whole-file encryption was chosen over per-field encryption specifically so a new sensitive field added later (like `username`, which almost shipped unencrypted) is protected automatically instead of relying on someone remembering to flag it.
  - Key material lives in its own `master.key` file next to `config.json`, both under `/config/ha_cert_manager/` — the same directory Home Assistant backs up and restores as a unit. Deliberately **not** derived from `SUPERVISOR_TOKEN`, since that token is reissued by the Supervisor on reinstall and is not guaranteed to survive a backup restore.
  - Existing plaintext `config.json` from pre-1.0.10 installs is transparently migrated to encrypted storage on first read — no manual step required.
  - Atomic writes (temp file + fsync + rename) and a one-deep `config.json.bak` backup on every save, so a crash or power loss mid-write can never leave a corrupted config file.
  - Decryption failures fail loud — surfaced as a dedicated error banner in the UI and logged at startup — instead of silently serving an empty device list.
  - The master key is read into two separate buffers and compared before use, and a decrypt failure with the cached in-memory key triggers one retry from a fresh disk read before erroring — guards against a stale or bit-flipped in-memory key copy (plausible on SBC hardware like a Pi running hot with no ECC RAM) being mistaken for genuine data corruption.
- **Settings → Encryption Key panel** — view/reveal/copy the current key, **Rotate** (generates a new random key and automatically re-encrypts existing data — safe, no data loss, no typing required), and **Restore / Set Key** (paste a previously backed-up key; restores automatically if it decrypts your existing data, otherwise requires typing `NO RECOVERY` to confirm a destructive reset).
- **Settings → Polling Interval** — configurable cadence for the dashboard's cert/device status checks: 1 min, 5 min, 15 min, 30 min, 1 hour, 6 hours, 12 hours, 1 day. Replaces the old hardcoded 60-second interval; new installs default to 15 minutes (Let's Encrypt certs don't need sub-minute freshness).
- **Home Assistant notifications** — new Settings toggle (default on). Auto-triggered deploy/check runs (fired when a cert renewal is detected, not manual button clicks) now post a persistent notification in the HA UI summarizing the result (success/failure/partial, which devices need attention). The local cert file becoming unreadable for 3 consecutive polls also notifies, with a matching "recovered" notification once it's readable again. Uses `SUPERVISOR_TOKEN` via the Supervisor's Core API proxy — no user-managed long-lived access token required (replacing the pattern an early prototype script used, which required hand-pasting an HA token into a config file).

### Fixed
- **`APP_VERSION` hardcoded string in `App.tsx`** — the version badge drifted 3 releases stale (`1.0.6` shown while the add-on was on `1.0.9`). Now reads the running version live from `/api/supervisor/addon-info`.
- **CORS methods tightened** — `allow_methods` restricted from `["*"]` to `["GET", "POST"]`, matching the API's actual surface.
- **Leftover Figma scaffold name** — `frontend/package.json` was still named `@figma/my-make-file`; renamed to `ha-cert-manager-frontend`.
- **Stale HP Switch 1950 setup note removed from the Getting Started panel** — instructed users to manually place an ISRG Root YR PEM file, which stopped being necessary when auto-extraction shipped in 1.0.6.
- **Inaccurate DOCS.md device list** — previously listed Synology, generic Unifi, Aruba, and Proxmox support that was never built. Corrected to the six device types actually implemented (TrueNAS, Brother, Hubitat, HPE Comware 7, TP-Link Omada, pfSense).
- **`.gitignore` entry for the renamed local prototype folder** — `Certificate Upload Automation-Alpha/` was ignored but the folder had since been renamed to `-OLD`, so it showed up as untracked noise in `git status`.

---

## [1.0.9] — 2026-07-07

### Fixed
- **No-cert UX** — when no Let's Encrypt cert is present in `/ssl`, the dashboard now handles it gracefully instead of showing a confusing error state.
  - Cert banner shows a soft "no certificate loaded" placeholder when no devices are configured yet; the red error banner only appears once devices exist (where it's actionable).
  - Getting Started step 1 label switches to "Install Let's Encrypt" when no cert is detected (was always "Certificate detected" regardless of state).
  - Polling indicator shows "waiting" instead of "polling" when cert is absent — nothing productive to poll.
- **`cert.subject` undefined bug** — Getting Started panel referenced `cert.subject` which doesn't exist on `LocalCert`; corrected to `cert.domain`.

---

## [1.0.8] — 2026-07-07

### Added
- **DOCS.md** — first-time setup guide shown in the HA add-on info panel. Covers recommended control settings (Start on boot, Watchdog, Auto update, Show in sidebar) and how to open the dashboard.
- **`url:` field in config.yaml** — "Visit HA Cert Manager" link in the add-on info panel now goes to the GitHub repo instead of looping back to the add-on page.
- **RELEASE_CHECKLIST.md** — pre-push checklist documenting required steps and common mistakes (image tag format, shebang, CI timing).

### Fixed
- **Example config scrubbed** — `options.example.json` and checklist replaced personal domain references with `example.com` equivalents.

---

## [1.0.7] — 2026-07-07

### Fixed
- **Add-on startup crash** — `run.sh` shebang changed from `#!/usr/bin/with-contenv bashio` to `#!/bin/sh`. The `with-contenv` interpreter doesn't exist in the `python:3.12-alpine` base image, causing the container to exit immediately on start with "exec /run.sh: no such file or directory".
- **Add-on not appearing in HA App Store** — `image:` field in `config.yaml` had a `:{version}` tag suffix. HA Supervisor rejects any tag in the `image:` field (it appends the version itself). Removed the suffix.

### Added
- **GitHub Actions CI** — `.github/workflows/build.yml` builds a multi-arch Docker image (`linux/amd64`, `linux/arm64`) and pushes to GHCR on every push to `main` that touches relevant files. Version tag is extracted automatically from `config.yaml`.

---

## [1.0.6] — 2026-07-06

### Added
- **Auto-deploy on renewal** — toggle in Settings to automatically deploy to all devices when the local cert serial changes (i.e. Let's Encrypt renews). Serial is tracked on each 60-second poll tick; the first tick after enabling establishes the baseline so no false-positive deploy fires on load.
- **`NEEDS_DEPLOY` status** — new status distinguishes "cert differs, action required" from "just successfully deployed". Previously both used `DEPLOYED`, causing the Needs Deploy (amber) and Deployed (blue) states to be indistinguishable. All check-mode deployers (TrueNAS, Brother, Hubitat, Omada, Comware) now return `NEEDS_DEPLOY` when the cert fingerprint doesn't match.
- **Root YR auto-extraction** — `deploy_cert_hpe_1950.py` now scans `fullchain.pem` for the ISRG intermediate cert (`CN = Root YR`) and extracts it automatically. The manually placed `ROOT_YR_ISSUED_BY_X1` file is no longer required for HP 1950 deployments.

### Fixed
- **HA Settings "devices" block removed** — `config.yaml` schema now uses `options: {}` / `schema: {}`. The confusing editable JSON block that previously appeared under Settings → Apps → HA Cert Manager is gone.
- **Uvicorn log spam** — backend access logging silenced (`access_log=False`, `log_level="warning"`). Hundreds of `GET /api/cert` and `GET /api/devices` lines per hour no longer appear in the add-on log.
- **Cert path auto-normalization** — if a bare filename (e.g. `fullchain.pem`) is entered in Settings, the app now automatically prepends `/ssl/` instead of trying to read from the root filesystem.

---

## [1.0.5] — 2026-07-06

### Added
- **HP Switch 1950 — no YAML editing required** — all switch configuration (hostname, management IP, PKI domain, SSL policy, startup config, credentials) is now entered in the device editor. The app generates the switch inventory YAML automatically. No manual `/config/scripts/hpe1950_switches.yaml` needed.
- **HP Switch 1950 — new device editor fields** — Switch IP (SSH), XTD CLI Password, PKI Domain, SSL Policy, and Startup Config are now shown in the edit modal for Comware device types.
- **Setup/onboarding screen** — when no devices are configured, a "Getting Started" panel is shown with step-by-step instructions, cert detection status, and device type reference instead of a blank page.

### Fixed
- **Sync counter** — `N/N in sync` in the header now counts a device as in sync if its last deploy was successful and the live fingerprint matches the local cert. Previously a successful deploy showed "4/5 in sync" even though all certs matched.
- **HPE 1950 Root CA validation** — `deploy_cert_hpe_1950.py` checked for `CN=Root YR` (no spaces) but OpenSSL 3.x formats subjects as `CN = Root YR` (spaces around `=`). Now uses a regex that matches both forms.
- **HPE 1950 script bundled** — deployer script included in the Docker image at `/app/scripts/deploy_cert_hpe_1950.py`; no manual copy required. Lookup order: explicit `comware_script_path` → `/config/scripts/` user override → `/app/scripts/` bundled default.
- **`curl` added to Docker image** — the deployer script auto-downloads ISRG Root X1 with `curl` on first run; the binary was missing from the Alpine image.
- **`site_id` default sentinel bug** — `site_id` was defaulting to the string `"Default"` for all device types. Comware now correctly falls back to the hostname for SSH if no Switch IP is configured.

---

## [1.0.4] — 2026-07-06

### Added
- **Verify All button** — new button in the cert banner header (beside Deploy All) that runs check mode on all enabled devices simultaneously; calls the new `POST /api/devices/check-all` backend endpoint. Styled blue to visually distinguish it from Deploy All.
- **Version display** — current add-on version shown in the cert banner above the action buttons.
- **`POST /api/devices/check-all`** — backend endpoint that runs each enabled device in check (non-deploy) mode, parallel to the existing `deploy-all`.

### Changed
- **Live fingerprint returned on mismatch** — TrueNAS and Brother check mode now return the live TLS fingerprint even when the cert differs. The amber "Fingerprint mismatch" box now appears on those cards after Verify, not just after Deploy.
- **TrueNAS: explicit API key log** — Verify now logs "API key authenticated — system/general OK" so it's clear the key was validated, not just the TLS port.
- **TrueNAS: longer post-restart wait** — sleep after `ui_restart` increased from 10 s to 20 s to give TrueNAS CE time to come up before the fingerprint re-probe; probe failure is now caught gracefully instead of crashing.
- **"Certificate already current" → "Certificate current"** — normalized status message across TrueNAS and Brother to match the wording used by Hubitat and Omada.
- **Refresh button tooltip** — changed from "Refresh cert info" to "Refresh Local Cert Info" to make clear it re-reads the HA-side `/ssl/` files, not remote device certificates.

### Security
- **Private key memory wiping** — added `secure_key()` context manager in `devices/base.py`. After a deploy, the raw PEM bytes read from `/ssl/privkey.pem` are overwritten in-place with `os.urandom()` before the bytearray is released. Applied to TrueNAS, pfSense, Omada, and Brother. This is best-effort: immutable `str`/`bytes` copies held inside HTTP library buffers are beyond Python's reach, but the primary buffer (our `bytearray` read directly from disk) is cleared at a known point in time.

---

## [1.0.3] — 2026-07-04

### Changed
- **Verify now tests credentials on all devices** — the Verify button previously only probed the TLS fingerprint on most devices. Each deployer now performs a real authenticated read-only call during check mode:
  - **TrueNAS**: unchanged — `GET /api/v2.0/system/general` already validates the API key.
  - **Brother**: unchanged — full web UI login was already performed in check mode.
  - **Omada**: now always logs in and calls `GET controller/setting` even when the cert fingerprint already matches.
  - **Hubitat**: now POSTs to `/login` with username/password and checks the redirect to confirm credentials before reporting cert status.
  - **pfSense**: now POSTs to the web UI login page when username/password are configured; credential result is appended to the status message.
  - **Comware**: removed the TLS short-circuit that skipped the SSH script when the cert already matched; the script now always runs in check mode so SSH credentials are tested every time.

---

## [1.0.2] — 2026-07-04

### Fixed
- **Font sizes** — all fixed-pixel text sizes bumped +2 px across the entire UI (11 px → 13 px, 12 → 14, 13 → 15, 14 → 16); resolves text being too small on standard displays and inside the HA ingress frame.
- **Brother HTTPS fallback** — if the hostname has no explicit scheme, the deployer now probes port 443 first; if HTTPS isn't reachable (e.g. first-time deployment on a printer with no cert yet), it falls back to `http://` and logs a warning. If both fail, the error is surfaced in the event log and the device card as usual. Users can force a scheme by prefixing the hostname with `http://` or `https://` in device config.

---

## [1.0.1] — 2026-07-04

### Fixed
- **Blank UI on first load** — all API fetch calls changed from absolute (`/api/…`) to relative (`./api/…`) so they route correctly through the HA ingress proxy.
- **Assets not served** — added `/assets` StaticFiles mount in FastAPI; the SPA catch-all was intercepting Vite-built JS/CSS bundles and returning `index.html` instead.
- **Brother printer "No scheme supplied" error** — added `ensure_https()` helper applied to all six deployers; URLs without a scheme are now auto-prefixed with `https://`.
- **Brother fingerprint mismatch** — `live_fingerprint` was being populated with the serial number (`probe_tls_serial`) instead of the SHA-256 fingerprint (`probe_tls_fingerprint`), causing a guaranteed mismatch against the local cert.
- **SSE log duplication** — `EventSource` auto-reconnects on network hiccups and replayed the full buffer each time; frontend now deduplicates log entries by ID.
- **Cert/key paths not used on deploy** — `_run_device` now reads configured `cert_path`/`key_path` from options instead of always using hardcoded defaults.

### Added
- **Device config editor** — full in-app add/edit/delete UI replacing direct YAML editing; type-aware dropdowns show only the relevant fields for each device type.
- **Supported device types** — TrueNAS, pfSense, HP Switch 1950 (Comware), Hubitat C-7, TP-Link Omada OC200/OC300, Brother MFC Printer.
- **Delete device ("divorce")** — trash button in the edit modal with a two-step confirmation before removal.
- **Settings gear** (top-right header):
  - Background color picker with 6 dark presets and a custom color input; persisted to `localStorage`.
  - Configurable cert/key file paths saved to options and applied immediately to cert reads and deploys.
- **Test Connection button** — in the device modal below the hostname field; calls `POST /api/verify-host` (TCP connect, 5 s timeout) and shows latency or a specific error inline.
- **Refresh button flash** — clicking the refresh icon triggers a green pulse animation (~900 ms) so the user knows it responded.
- **Extended cert banner** — now shows: Key type/size, Signature algorithm, Root CA (trust anchor from chain), Key usage + Extended Key Usage, Subject Alternative Names (pill tags), Key file path.
- **Smart fingerprint display** — device cards show `SHA256:AA:BB:CC:DD:…:WW:XX:YY:ZZ` (first 4 + last 4 pairs) with the full value on hover; the cert banner shows the complete SHA-256.
- **Cert file error handling** — if the cert or key file is missing/empty/unreadable, the cert banner shows a descriptive error with a link to Settings rather than crashing.
- **`GET /api/config` + `POST /api/config`** — endpoints to read and write the full options file from the frontend.
- **`POST /api/verify-host`** — TCP connectivity check with latency, used by the Test Connection button.
- **`ensure_https()` / `strip_scheme()`** — shared helpers in `devices/base.py` applied to all six deployers.
- **Configurable cert paths** — `read_local_cert()` now accepts `cert_path` / `key_path` arguments with `/ssl/` HA defaults.

### Changed
- **Brother deployer** — removed `username` field (Brother web UI accepts password only).
- **Font sizes** — bumped base label sizes from 10–11 px to 12–13 px throughout for readability.
- **Issuer label** — issuer CA name in the cert header now prefixed with "Issuer:" for clarity.
- **deploy.ps1 message** — now instructs users to use ⋮ → **Rebuild** (not Restart) with an explanation of the difference.

### Security / Persistence
- **Config moved off `/data/options.json`** — HA Supervisor reinitializes `/data/options.json` from `config.yaml` on every Rebuild, wiping user config. Device config now stored at `/config/ha_cert_manager/config.json` (the HA config volume, which Supervisor never touches).
- **Automatic one-time migration** — on first boot after upgrade, any existing `/data/options.json` is copied to the new path so no devices are lost.

---

## [1.0.0] — 2026-06-28

### Added
- Initial release.
- FastAPI backend with SSE event log, six device deployers, and cert reader.
- React/Vite/Tailwind frontend served through HA ingress.
- Deployers: TrueNAS (API key), pfSense (verify + optional REST upload), HP Comware 1950 (SSH/SCP), Hubitat C-7 (HTTPS API), TP-Link Omada OC200/OC300 (session auth + config backup), Brother MFC Printer (PKCS#12 web scrape).
- Per-device Verify and Deploy buttons; Deploy All; Omada config backup + download.
- Live TLS fingerprint probing compared against local cert SHA-256.
