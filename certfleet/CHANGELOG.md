# Changelog

All notable changes to CertFleet (formerly HA Cert Manager) are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.3.3] — 2026-07-11

### Fixed
- **`ensure_key()` fired a false "unrecoverable data loss" error** any time `config.json` existed and `master.key` didn't — including the completely normal case of a legacy *plaintext* config being encrypted for the very first time (part of `load_config()`'s own migration path). Hit for real on a production instance's first boot with an old pre-1.0.10 plaintext config lying around: the scary error logged, but devices were never actually at risk. Fixed by checking whether `config.json` is genuinely encrypted before alarming — verified against both a real false-positive (now silent) and a real lost-key case (still alarms correctly).

### Changed
- Breadcrumb arrows ("Let's Encrypt → CertFleet → All Devices") now use the app's green accent color instead of muted gray.

---

## [1.3.2] — 2026-07-11

### Added
- **Private key / certificate pairing validation** (`cert_reader.py`) — `read_local_cert()` previously only checked the key file *exists*, never that it's a valid key or that it actually matches the certificate. A corrupted key, or a key left over from a different certificate, would have shown full "Certificate detected" success and only failed later as a cryptic per-device error. Now validated up front with a clear message, live-tested against both a mismatched pair and a corrupted key file before shipping.
- Local GitLab CI mirror (`.gitlab-ci.yml`) for test-pushing before hitting the real GitHub repo — see `RELEASE_CHECKLIST.md`'s new "4.5. Local GitLab test push" section.

### Fixed
- **"Getting Started" panel hardcoded `/ssl/fullchain.pem`** in its "no cert found" message instead of reading the actual configured cert path — would have shown the wrong path if you ever customized it away from the default. Now reads the real `certPath`.
- Reworded the staging-cert advisory (auto-deploy off case) for clarity: "auto-deploy is temporarily disabled" instead of "nothing will auto-deploy while auto-deploy stays off."

### Changed
- **Increased font sizes app-wide** — every `text-[Npx]` utility shifted up one step (11→12 through 17→18) to improve legibility while preserving the existing size hierarchy.
- **Header now uses the real CertFleet ShieldCheck logo** instead of a generic outline icon, and moved into the breadcrumb itself: "Let's Encrypt → CertFleet → All Devices."

---

## [1.3.1] — 2026-07-10 — "the vibe coding apocalypse where Dave realized he should not just ask more questions, but also read the code himself more than once"

### Added
- **Staging/test Let's Encrypt certificate detection** (`is_staging` on `LocalCert`, `cert_reader.py`) — keyed off Let's Encrypt's own `"STAGING"` branding in the issuer/root CA CN, confirmed against real staging-issued certs. The top certificate card now turns amber when a staging cert is loaded, or red specifically when Auto-deploy is also on (the genuinely dangerous combination — a cert that would otherwise have been auto-pushed to every device). The Issuer and Root CA fields that tipped this off are recolored to match.
- **Auto-deploy now self-pauses on a staging cert**, gated live off `autoDeployOnRenewal AND NOT is_staging` — no separate "suspended" flag to persist or restore; it re-enables itself automatically the moment a real certificate replaces the staging one.
- **Manual Deploy (single device or Deploy All) now requires confirmation** when the loaded certificate is staging — Verify/Check remain unrestricted since they're read-only.
- **HA notification when a staging cert is detected**, repeating every 24h for as long as it persists (not just once) so a system nobody's actively watching doesn't have this buried after one missed alert — plus a "valid certificate restored" notification once resolved.

### Fixed
- **Auto-deploy-on-renewal, the cert-unreadable notification, and now staging-cert handling all now run server-side**, via a new backend poll loop in `main.py` (`_poll_loop`/`_poll_tick`) that runs for as long as the process is alive. Previously, every one of these lived only in a `setInterval` inside the React frontend — meaning a CertFleet instance with no browser tab open silently did none of it. This was found while designing staging-cert detection and is exactly the "sitting on a shelf, nobody's watching" scenario auto-deploy and notifications exist for in the first place. See the new "Unattended operation actually runs unattended" standing check in `RELEASE_CHECKLIST.md`.
- **`auto_deploy_on_renewal` moved from browser `localStorage` to the backend config** (`/api/config`) — it has to be readable server-side for the poll loop above to act on it, and `notify_enabled`/`poll_interval_ms` already worked this way.

---

## [1.3.0] — 2026-07-10

### Added
- **Local certificate change logging** — the event log now records a line with serial + SHA256 whenever a certificate is first detected at startup, and again any time the fingerprint changes (i.e. a renewal). Runs server-side on every `/api/cert` read (which happens every poll tick regardless of whether the dashboard is open), not just when a browser tab happens to be watching.
- **TrueNAS API key expiry check** — `check()`/`deploy()` now look up the configured key's own `expires_at`/`revoked` status (TrueNAS API keys are formatted `<id>-<secret>`, so the id alone identifies the exact key via `GET /api_key` — no fuzzy matching needed) and surface a warning through the existing amber "Heads up" box when a key is revoked or expiring within 14 days. This is advisory only and never gates the actual connection attempt — a real auth/connection failure is still always a hard red error regardless of what the key metadata says, and a flagged key that still connects successfully still reports success.

### Fixed
- **Hubitat device form asked for the wrong credential.** `TYPE_FIELDS.hubitat` in the Add Device modal collected an "API key" field, but `hubitat.py` has only ever authenticated with username/password against the hub's web login — the API key field was silently never read, and username/password were silently never collected. Any Hubitat device added through the GUI was authenticating with blank credentials with no visible error. Fixed to collect username/password, matching the real backend; verified live in browser afterward.
- **Event log "clear" didn't stick.** `/api/events` replays its full history on every SSE connection, and the browser's `EventSource` auto-reconnects a few seconds after any network blip — so clicking "clear" (which only wiped local state) would see old entries silently reappear once a reconnect replayed the backend's buffer. Fixed by tracking a `clearedBeforeId` boundary and filtering both replayed and new entries against it, with a real "Event log cleared" marker entry recording when it happened.

---

## [1.2.0] — 2026-07-10

### Added
- **Proxmox VE support** (`devices/proxmox.py`) — official REST API, no SSH involved. Auth is an API token; reuses existing generic `DeviceConfig` columns rather than adding new schema (`username` holds the full token ID, `api_key` the token secret, `site_id` the Proxmox node name). Verified end-to-end against a real PVE 8.x node before shipping, including catching and fixing a wrong assumed API path (`/access/users/{userid}/tokens/{tokenid}` doesn't exist — the real one is singular, `/token/`) before it ever shipped.
  - **"Allow upload" toggle** (`proxmox_allow_upload`, default off) — same posture as the existing pfSense deployer: Proxmox has its own built-in ACME client, so by default CertFleet only verifies and leaves upload disabled until explicitly turned on. The device card and Deploy button share the same generalized upload-gate logic pfSense already used, rather than duplicating it per device type.
  - Fixed a real bug this surfaced: `site_id` (repurposed here as "Proxmox node name") was defaulting to the literal string `"Default"` in the Add Device form — harmless for Omada (which never actually reads `site_id` server-side — turned out to be dead code) but would have silently sent an invalid node name on every new Proxmox device.
- **Certificate coverage check**, applied to all seven device types centrally in `main.py`, not per-deployer. There's no reliable way for this app to know what hostname a browser will actually use to reach a device — `device.host` is just whatever address reaches it internally, and reverse DNS doesn't resolve that uncertainty either. So instead of guessing at "the right" hostname, this checks something knowable with certainty: whether the new certificate covers everything the device's *current* live certificate already covers (a live TLS probe against the device, comparing CN + SAN + IP-SAN lists with real wildcard-matching rules, not substring matching). If coverage would regress, the device card turns amber with a "Heads up" note and the event log gets a `warn` entry — plus a second, honestly-hedged note if the configured `device.host` itself isn't covered, since that's often but not always the real answer.
  - Caught and fixed a real false-positive risk during testing: the initial version only checked DNS-name SANs, missing IP-address SANs entirely — which would have produced an incorrect "not covered" warning on any device (like the real Proxmox box used to verify this) that's configured by IP and whose self-signed cert includes that same IP as a SAN.
  - New generic `DeviceResult.warning` field (not Proxmox-specific) — reusable for any future non-fatal "worth knowing" signal from any deployer.

---

## [1.1.0] — 2026-07-09

### Changed
- **Renamed "HA Cert Manager" to CertFleet.** New add-on slug (`ha_cert_manager` → `certfleet`), new GHCR image path (`ghcr.io/tinmansc/certfleet`), new GitHub repo (`tinmansc/CertFleet`, renamed in place — GitHub preserves history/issues/stars and redirects the old URL automatically), and the local project folder renamed to `certfleet/`.
- Every in-repo reference to the old name updated: `config.yaml`, `repository.yaml`, root `README.md`, the real CI workflow, backend app title and notification titles/IDs, frontend header and page title, `deploy.ps1`, the bundled Comware scripts, and the example automation.

### Added
- **Automatic config migration for the slug rename** — `crypto_store.migrate_from_old_slug()` runs at startup and copies `master.key`, `config.json`, and `config.json.bak` forward from the old `/config/ha_cert_manager/` directory to the new `/config/certfleet/` one, if the new location doesn't already have its own data. Home Assistant treats a slug change as installing a brand-new add-on, not an in-place rename — without this, every existing device and encrypted credential would have been silently orphaned at the old path. Copies forward only; the old directory is never modified or deleted, so nothing is at risk even if the migration is interrupted. Verified end-to-end with real device data before shipping.

### Fixed
- **A CI workflow file had been silently dead for an unknown number of releases.** `ha-cert-manager/.github/workflows/build.yml` existed nested inside the add-on folder, but GitHub Actions only ever discovers workflow files in `.github/workflows/` at the true repository root — a nested copy is never scanned or triggered. The actually-active workflow was a separate file that already existed at the repo root since the very first commit. Deleted the dead nested copy; the root workflow is now the single source of truth and was updated with the new paths/image name.
- **Duplicate, drifted `RELEASE_CHECKLIST.md`.** A stale copy existed at the repo root (from the original commit, referencing a hardcoded `APP_VERSION` constant removed back in 1.0.13) alongside the actively-maintained one inside the add-on folder. Deleted the stale root copy; the add-on folder's checklist is canonical.
- **Stale frontend `<title>` and meta description** left over from the original Figma-Make scaffold ("Certificate Upload Automation" / Omada-only description) — never actually matched the app since it grew beyond Omada. Now reflects CertFleet and its real scope.

---

## [1.0.14] — 2026-07-09

### Changed
- **Verify no longer fails outright when there's no local certificate to compare against** — previously, clicking Verify/Check on any device raised a hard error before ever contacting the device if the local Let's Encrypt cert couldn't be read (including on a fresh install, before Let's Encrypt has issued anything). Now check mode still connects, authenticates, and retrieves the device's own live certificate info — it just skips the "matches/differs" comparison and reports a new "Connected" status instead. Deploy still correctly requires a real local certificate (there's nothing to push otherwise) and fails clearly if one isn't available. Applies to all six device types.
- **Device cards now show the device's live certificate fingerprint even with no local cert to compare** — a new info box (distinct from the existing green/amber match/mismatch box) surfaces whatever fingerprint was retrieved during a "Connected" verify, instead of showing nothing.

### Fixed
- **Verify/deploy timestamp was unreadable and permanently mislabeled UTC** — the per-device timestamp shown after Verify/Deploy used a near-black grey (`#30363d`, close to the background color) and was hardcoded with a "UTC" suffix regardless of the viewer's actual timezone. Now renders in the same green used by the Refresh Local Cert Info button, and is converted to the browser's local timezone (verified: a `04:11:26 UTC` timestamp correctly renders as `23:11:26` the previous day for a US Central browser). The event log timestamp had the same underlying issue — the backend was formatting it as a bare string with no timezone offset at all, making correct client-side conversion impossible — fixed at the source (`_emit()` now uses `.isoformat()`, matching how `last_run` was already done) and the frontend now converts it the same way.
- **`scripts/decrypt_config.py` hardening** — the recovery script had no protections against a mistyped `--out` path:
  - Refuses unconditionally if `--out` resolves to the same file as either input (would otherwise silently overwrite the live encrypted `config.json` — or the key itself — with a decrypted plaintext copy).
  - Refuses to overwrite an existing `--out` file unless `--force` is passed.
  - Writes atomically (temp file + rename), so an interrupted write can't leave a corrupted file at the target path.
  - Validates the decrypted bytes are well-formed JSON before writing anything, and gives a clean error instead of a raw traceback if the key file doesn't contain a valid Fernet key.
- **Whitespace in encryption keys wasn't consistently stripped** — a trailing newline (e.g. from saving a copied key with a text editor, which `scripts/decrypt_config.py` explicitly expects users to do for manual recovery) would silently turn a correct key into a rejected one. `master.key` reads, `set_key()`, and `decrypt_config.py`'s key-file argument are now all stripped consistently — actual device credential values (e.g. a password that legitimately has a trailing space) are never touched, only the key material itself.

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
