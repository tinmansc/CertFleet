# Release Checklist

Run through this before every version bump and push to main.

---

## 1. Code changes complete
- [ ] All intended changes committed locally
- [ ] `run.sh` uses `#!/bin/sh` (NOT `#!/usr/bin/with-contenv bashio` — that requires HA base images)
- [ ] No hardcoded IP addresses, passwords, or test credentials left in code

## 2. Version bump
- [ ] `config.yaml` → `version:` incremented (semver: patch for fixes, minor for new devices/features)
- [ ] `CHANGELOG.md` updated with the new version, date, and bullet points for every change
- [ ] Grep for stray hardcoded version strings before pushing: `grep -rn '"1\.0\.' frontend/src backend --include=*.tsx --include=*.py`
  (the UI version badge reads live from the Supervisor via `/api/supervisor/addon-info` — it should never need editing again, but check anyway in case a new hardcoded copy creeps in)

## 3. config.yaml sanity check
- [ ] `image:` field has NO tag suffix (e.g. `ghcr.io/tinmansc/certfleet` — no `:latest`, no `:{version}`)
- [ ] `url:` points to `https://github.com/tinmansc/CertFleet`
- [ ] `slug:` is `certfleet` (Docker/GHCR image names must stay lowercase even though the GitHub repo itself is `CertFleet`)
- [ ] `arch:` lists both `aarch64` and `amd64`
- [ ] `ingress: true` and `ingress_port: 8099` are present

## 4. Frontend built (if frontend changed)
- [ ] `cd frontend && npm run build` completes without errors
- [ ] `frontend/dist/` is up to date (CI rebuilds it, but verify locally if you changed React code)

## 5. Push to GitHub
- [ ] `git status` — no unintended files staged
- [ ] `git push origin main`
- [ ] GitHub Actions workflow triggered (check Actions tab)
- [ ] If you're editing the CI workflow itself, edit `../.github/workflows/build.yaml` at the true repo
      root — **not** a copy inside `certfleet/`. GitHub Actions only discovers workflow files sitting in
      `.github/workflows/` at the repository root; a nested copy is silently never run (see Common
      mistakes below — this bit us for an entire release cycle before we noticed).

## 6. Wait for CI
- [ ] Build and Push to GHCR workflow shows green for the new version
- [ ] Confirm `ghcr.io/tinmansc/certfleet:<version>` appears in GHCR packages
- [ ] GHCR package visibility is still **Public**

## 7. Verify on test HA (10.10.101.91)
- [ ] Supervisor picks up the new version (may take a few minutes to re-scan the repo)
- [ ] "Update available" appears on the add-on page
- [ ] Update completes without error
- [ ] Add-on starts and shows **Running** (green dot)
- [ ] "Open Web UI" loads the dashboard
- [ ] If upgrading from pre-1.0.10: confirm the existing plaintext `config.json` migrated to encrypted
      storage automatically (check the event log for "Migrating plaintext config.json to encrypted
      storage", and confirm existing devices still show up)
- [ ] If upgrading from the old "HA Cert Manager" slug (pre-1.1.0): confirm the event log shows a
      "Migrated ... from the old ha_cert_manager add-on directory" line, and that all previously
      configured devices/credentials appear intact before uninstalling the old add-on

## 8. Deploy to production HA
- [ ] Run `deploy.ps1` if backend or frontend changed
- [ ] In HA: Settings → Apps → CertFleet → ⋮ → **Rebuild** (not Restart)
- [ ] Add-on restarts and shows Running
- [ ] If touching config storage: confirm `/config/certfleet/master.key` and `config.json` both
      survive the Rebuild (they live under `/config`, which Rebuild does not touch — only `/data` gets
      reset to `config.yaml` defaults)

## 9. If this release touches encryption/config storage
- [ ] New sensitive device fields need NO special handling — the whole `config.json` is encrypted as one
      blob, not per-field, specifically so this class of "forgot to mark it sensitive" bug can't happen
- [ ] Never derive the encryption key from `SUPERVISOR_TOKEN` — it's reissued by the Supervisor per
      install/start and is not guaranteed to survive a backup restore onto a fresh HA instance (see
      CHANGELOG 1.0.10). The key must live in its own file under `/config` so it travels in the same
      backup archive as the data it protects.
- [ ] Any new write path to `config.json` goes through `crypto_store.save_config()` — never `.write_text()`
      directly — to keep atomic-write + backup-before-overwrite guarantees intact
- [ ] Any new operation that writes BOTH `master.key` and `config.json` (like rotate/set-key) must call
      `_check_writable()` first and self-verify (decrypt the result) after — see `rotate_key()` /
      `set_key()` in `crypto_store.py` for the pattern
- [ ] If you change `crypto_store.py`'s recovery logic, re-run its scenario test before pushing (not part
      of CI — run manually): missing `config.json` with a `.bak` present, an orphaned `config.json` with
      no `master.key`, a simulated interrupted rotation (`.bak` matches the current key but the live file
      doesn't), and a non-writable config directory. See the CHANGELOG 1.0.12 entry for what each covers.

## 10. Standing checks — every release, not just when the relevant code changed

30,000-foot-view stuff. Not a log of every bug we've hit (that's the list
below) — these are the categories of thing that need to be verified as
still true before any release ships, because a regression here is the
kind that quietly destroys trust rather than throwing an obvious error.

- [ ] **Config safety**: a fresh install, an upgrade from the previous version,
      and a Rebuild all preserve existing devices/config — nothing gets
      silently wiped or reset to empty.
- [ ] **GUI feedback**: every button or icon a user can click gives some visible
      response when clicked — color change, spin, flash to the shared green,
      or a clear red failure state. No control that just sits there and
      leaves the user wondering if anything happened.
- [ ] **File handle safety**: any code path that writes to disk goes through
      `crypto_store._atomic_write()` (temp file + rename) — never a direct
      `.write_text()`/`.write_bytes()` — so an interrupted write can't leave
      a corrupted file at a live path.
- [ ] **Encryption key verification on save/close**: any operation that writes
      `master.key` and/or `config.json` decrypts the result to actually prove
      it before reporting success — not just "the write call didn't throw."

---

> **Common mistakes we've already made:**
> - `image:` field had `:{version}` tag → Supervisor rejects it silently, add-on never appears in store
> - `run.sh` shebang was `#!/usr/bin/with-contenv bashio` → crashes on startup in plain Python base image
> - Pushed two version bumps back-to-back before CI finished the first → update UI shows error while image is still building
> - Forgot to wait for GHCR build before testing update on HA → "image not found" pull error
> - UI version badge was a hardcoded `APP_VERSION` string in `App.tsx` that drifted 3 releases behind → now reads live from `/api/supervisor/addon-info`
> - Nearly derived the config encryption key from `SUPERVISOR_TOKEN` for convenience → would have made every stored device credential permanently unrecoverable after any HA backup restore, since that token isn't guaranteed to survive reinstall
> - `rotate_key()`'s docstring claimed a crash mid-rotation could never leave a mismatched key/config pair — untrue if the ciphertext write succeeded but the key-file write failed (e.g. a Pi's SD card going read-only mid-operation). Fixed in 1.0.12 with a preflight writability check, a post-write self-verify, and automatic recovery from `config.json.bak` if it happens anyway.
> - A CI workflow file existed at `ha-cert-manager/.github/workflows/build.yml` (nested inside the add-on folder) for an unknown number of releases and was **never actually run** — GitHub Actions only discovers workflows in `.github/workflows/` at the true repository root. The real, active workflow was a separate file at the repo root (`../.github/workflows/build.yaml`) the whole time. Found and deleted the dead copy during the CertFleet rename (1.1.0). If CI ever looks like it "isn't picking up" a workflow change, check you're editing the root copy, not a nested one.
> - Renamed the add-on slug from `ha_cert_manager` to `certfleet` (1.1.0) — this makes Supervisor treat it as installing a brand-new add-on rather than an in-place rename, which would silently orphan every existing device/credential at the old `/config/ha_cert_manager/` path. Added `migrate_from_old_slug()` in `crypto_store.py`, called at startup, to copy the old directory's contents forward automatically. If you ever change the slug again, you need this same migration step — it does not happen automatically just because the code changed.
> - Assumed the Proxmox API path for reading a token's own metadata was `/access/users/{userid}/tokens/{tokenid}` (plural "tokens") — wrong, and confirmed wrong by testing against a real node before it ever shipped: that path returns "not implemented," the real one is singular, `/access/users/{userid}/token/{tokenid}`. Always verify an assumed API path against the real device before writing the calling code around it, not after.
> - `EMPTY_DEVICE.site_id` in `App.tsx` defaulted to the literal string `"Default"` instead of an empty string with a placeholder — harmless for Omada (whose deployer, it turns out, never actually reads `site_id` at all) but became a real landmine once Proxmox repurposed the same field as a *required* node name: every new Proxmox device silently started with an invalid node name unless the user noticed and cleared the field.
> - The first version of the cert-coverage check (1.2.0) only read DNS-name SANs off a device's live certificate, missing IP-address SANs entirely — would have produced a false "not covered" warning on any device configured by IP whose self-signed cert includes that same IP as a SAN, which is a common pattern (caught by testing against the real Proxmox box, which does exactly this).
