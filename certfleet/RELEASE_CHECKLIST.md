# Release Checklist

Run through this before every version bump and push to main.

---

## 0. Deferred follow-ups — check this first, every time this file is opened

Self-reminders for things that aren't due yet but shouldn't be forgotten.
Anything below with a **Check after** date on or before today's date should
be raised with the user — a quick status mention, or actually digging in
first, whichever fits the moment — then either resolved (move to Common
mistakes / delete the entry) or given a new **Check after** date.

- [ ] **Check after: 2026-09-01** — TrueNAS's own web UI warned (2026-07-06)
      that its REST API (`/api/v2.0/...` — what `devices/truenas.py` and its
      key-expiry check are entirely built on) is deprecated and will be
      removed in TrueNAS version 26.04, replaced by JSON-RPC 2.0 over
      WebSocket. Not urgent yet — REST still works and 26.04 isn't out. When
      revisited: check whether 26.04 has shipped or has a date, and if so,
      verify the new JSON-RPC/WebSocket API hands-on against a real TrueNAS
      box (auth model and call shape are both different) before writing any
      migration code — same discipline as everything else in this project.

---

## 1. Code changes complete
- [ ] All intended changes committed locally
- [ ] `run.sh` uses `#!/bin/sh` (NOT `#!/usr/bin/with-contenv bashio` — that requires HA base images)
- [ ] No hardcoded IP addresses, passwords, or test credentials left in code

## 2. Version bump
- [ ] `config.yaml` → `version:` incremented (semver: patch for fixes AND small additive checks/features
      like a new warning or log line; minor reserved for an actual new device type or a user-facing
      workflow change. Default to patch when in doubt — small bumps are cheap and easy to follow in the
      changelog, a bloated minor version isn't.)
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

## 4.5. Local GitLab test push (optional pre-flight, before hitting real GitHub)

Set up 2026-07-10 so we're not pushing to the real repo repeatedly while
iterating. `gitlab.daveclark.email` mirrors the GitHub CI pipeline via
`.gitlab-ci.yml` at the true repo root (same root-only rule as GitHub Actions —
see Common mistakes below). amd64-only image, pushed to GitLab's own
Container Registry — this is for validating the build itself, not a
substitute for the real multi-arch GHCR image.

- [ ] `git push gitlab main` (remote already configured — `git remote -v` should
      show both `origin` and `gitlab`)
- [ ] Check GitLab CI/CD → Pipelines — job should go green
- [ ] Once it's green, push the same commit(s) to `origin` for the real pipeline

**One-time setup, done once GitLab is reachable (not yet completed as of this
writing):**
- [ ] Add the dedicated SSH public key (`~/.ssh/gitlab_daveclark.pub`) to the
      GitLab user's SSH keys — the matching private key + an `ssh config`
      entry for `gitlab.daveclark.email` are already in place locally
- [ ] Create the `tinmansc/CertFleet` project in GitLab (empty, no README —
      we're pushing existing history)
- [ ] Confirm Container Registry is enabled for that project (usually on by
      default for self-managed GitLab CE, but instance-wide settings can
      disable it)
- [ ] `ssh -T git@gitlab.daveclark.email` once to confirm auth and accept
      the host key

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
- [ ] **Frontend/backend field parity**: for every device type, `TYPE_FIELDS`
      in `App.tsx` collects exactly the fields the matching `devices/*.py`
      deployer actually reads — no field shown that the backend ignores, no
      field the backend needs that isn't collected. This drifted silently for
      Hubitat (GUI asked for "API key", backend only ever read
      username/password) — nothing errored, it just quietly authenticated
      with empty credentials. Check this any time a device type's fields
      change on either side.
- [ ] **Event log survives a clear across an SSE reconnect**: `/api/events`
      replays the full `_log_buffer` on every new connection, and the browser
      reconnects `EventSource` on its own after any blip — a few seconds
      after clicking "clear," old entries would silently reappear unless the
      frontend's `clearedBeforeId` boundary (see `App.tsx`) is still being
      respected. If you touch the SSE handler or the log buffer, re-check
      this by hand: clear, wait >5s, confirm nothing comes back.
- [ ] **Unattended operation actually runs unattended.** Anything the user would
      reasonably expect to work with no one looking at the dashboard —
      auto-deploy-on-renewal, cert-health polling, and every notification
      (unreadable cert, decryption-key problems, encryption key
      rotation/export/import, staging-cert detection, anything else framed as
      "we'll warn you if X breaks") — must be driven from the backend, not
      from a `useEffect`/`setInterval` in `App.tsx` that only runs while a
      browser tab has the page open. A CertFleet instance sitting untouched
      on a shelf is a real, expected deployment mode, not an edge case — if a
      feature's trigger lives only in frontend React state, it silently does
      nothing for that user and they will not know. Check any new "we detect
      and warn about X" feature against this before shipping it.
- [ ] **Device deployers never silently drop info they already obtained.** Any
      new device type (or edit to an existing one) should: probe the device's
      live TLS cert independently and as early as possible (a TLS handshake
      needs no credentials, so it should still run and get reported even if
      auth/API calls fail afterward), and make sure every `return
      DeviceResult(...)` — including every `except` handler, not just the
      success paths — includes whatever `live_fingerprint` (or other info)
      was already obtained. A bad credential should never mean "we have zero
      info to show you" when a no-auth-needed probe could have run anyway.
      Also make sure the actual deploy action itself is wrapped in a
      try/except — an unguarded call that raises propagates uncaught past
      this device's own error handling (see `hubitat.py`, fixed 2026-07-11).

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
> - `TYPE_FIELDS.hubitat` in `App.tsx` asked for an "API key" — but `hubitat.py` has only ever authenticated with username/password against the hub's web login. Any Hubitat device added through the GUI silently got empty username/password and a never-used API key field; nothing surfaced an error, it just quietly authenticated with blank credentials. Caught by the user noticing the Add Device form didn't match what they remembered configuring. Fixed by correcting `TYPE_FIELDS` to match the real backend, verified live in browser afterward. Lesson: when a device's auth mechanism is ever changed, check BOTH sides — it's easy for one to drift without any error, since empty-string credentials don't throw, they just fail (or worse, quietly no-op) downstream.
> - `/api/events`'s SSE endpoint replays the entire `_log_buffer` on every new connection — which is correct and necessary for a client that just opened the page, but means the browser's automatic `EventSource` reconnect (fires a few seconds after any network hiccup) silently replays history that a user had just clicked "clear" on, since clear only ever touched local React state. Fixed by tracking a `clearedBeforeId` boundary client-side and filtering replayed/new entries against it, plus dropping a real "Event log cleared" marker into the log so the boundary is visible.
> - **Auto-deploy-on-renewal and the "certificate unreadable" HA notification both only ever ran from a `setInterval` inside `App.tsx`'s React component** — meaning both silently did nothing at all on any CertFleet instance where nobody had the dashboard open in a browser. Found while designing staging/test-cert detection: a staging cert issued on an unattended system with Auto-deploy enabled would have needed someone watching a browser tab for the "don't push this to real devices" safety logic to even run in the first place. Fixed in 1.3.1 — moved into a real backend poll loop (`_poll_loop`/`_poll_tick`, `main.py`), which now also drives staging-cert detection. See the "Unattended operation actually runs unattended" standing check above.
> - `read_local_cert()` fully validated the certificate (exists, non-empty, valid PEM) but only checked the private key file *exists* — never that it's actually a valid key, and never that it matches the certificate at all. A corrupted key file or a leftover key from a different certificate would have shown full "Certificate detected" success, only failing later as a cryptic per-device TLS/API error disconnected from the real cause. Fixed by adding `_load_private_key()` (validates PEM parseability) and `_keys_match()` (compares DER-encoded SubjectPublicKeyInfo bytes, works across RSA/EC/Ed25519 uniformly) to `cert_reader.py`, both live-tested against a real mismatched pair and a corrupted key file before shipping.
> - `ensure_key()` (`crypto_store.py`) fired its "unrecoverable data loss, encrypted under a lost key" error any time `config.json` existed and `master.key` didn't — including the completely normal, harmless case of a legacy *plaintext* config being encrypted for the very first time (`load_config()`'s own migration path calls straight into this). A real instance hit this on 2026-07-11 during its first boot with an old pre-1.0.10 plaintext config lying around — the scary error fired, devices were actually fine the whole time. Fixed by checking whether the existing `config.json` is genuinely encrypted (doesn't start with `{`) before alarming — verified against both a real false-positive (plaintext, silent now) and a real lost-key case (still alarms correctly, decryption still fails loudly afterward).
> - **Root-level static files (`favicon-32.png`, `favicon-64.png`, copied by Vite from `frontend/public/` into `dist/` root) were never actually served by the backend** — only `/assets/*` and `/static/*` were mounted, so any request for a root file fell through to the SPA catch-all (`serve_spa`) and got `index.html`'s HTML back instead of the image. Invisible as long as these were only referenced from `<link rel="icon">` (browsers fail silently), but became an obvious broken-image icon the moment one was used in a visible `<img>` tag (the header logo, added 1.3.2). Fixed with explicit routes for each known root asset in `main.py`, positioned before the SPA catch-all — verified with a real HTTP request returning the correct `image/png` content-type and byte count, not just code review. `index.html`'s favicon `<link>` tags were also using absolute `/favicon-*.png` paths, which break the same way `/api/...` (vs `./api/...`) breaks under HA's ingress URL prefix — fixed to relative paths too.
> - **Device deployers were discarding an already-obtained TLS fingerprint whenever a later, unrelated step failed** — e.g. TrueNAS's flow called the authenticated API first and only probed the device's live TLS cert afterward, so a bad/garbage API key meant the error response had zero cert info, even though a TLS handshake needs no credentials and could have succeeded independently. A real user hit this exactly: typed a garbage TrueNAS API key, got `HTTP 401` with no cert details at all. Swept **all seven deployers** for the same class of bug (found it in six of seven, to varying degrees) and fixed by: probing TLS independently and early wherever it wasn't already, and making sure every `except` handler's returned `DeviceResult` includes whatever `live_fingerprint` was already obtained rather than dropping it. `hubitat.py` additionally had its actual deploy subprocess call running with **no enclosing try/except at all** — a crash there would have propagated uncaught past this device's own error handling. Verified live against the real TrueNAS box with a deliberately garbage API key: now correctly returns both the 401 error *and* the device's real live fingerprint.
> - **The "update available" link pointed at the old HA URL scheme**, `/hassio/addon/<slug>/info` — HA's 2026.7 "add-ons → apps" rename changed the actual URL path too, not just the label; the real path is `/config/app/<slug>/info` (confirmed directly against a real instance's Let's Encrypt add-on page earlier the same session). Caused a 404 for a real user clicking it via the Nabu Casa remote-access URL. Fixed to the current path.
> - **HP Comware's post-deploy "startup config safety" check hardcoded an assumption that every switch's Startup config field matches what's actually configured on the switch** — a switch with a custom-named startup file (e.g. `HP-1950-LoftPoESwitch-Current.cfg` instead of the generic default `flash:/startup.cfg`) fails this check even though the certificate itself deployed and verified fine (`TLS probe — cert matches`, cert serial/issuer/subject all confirmed matching in the same run). Not a code bug so much as a confusing error message for a device-config mismatch — a real user read "Startup verification failed" as a deploy failure when the cert had actually already succeeded. Reworded the error to name the real cause (the device's Startup config field doesn't match the switch) and tell the user exactly what to change, instead of just reporting "expected X, got Y" with no next step.
> - **The 1.3.1 SSE "clear" fix introduced a worse bug of its own: a backend restart could silently kill the live event log for every device, indefinitely.** `clearedBeforeId` (the boundary that makes "clear" survive an SSE reconnect) is a raw comparison against the backend's `_log_counter`, which started at `0` and reset to `0` on every restart. If a user had ever clicked Clear (setting the boundary to, say, `121`) and the backend later restarted for any reason (Rebuild, update, crash), the counter's fresh climb from `1` meant every genuinely new entry had an id `≤ 121` and got silently filtered — not a display bug, an actual "the event log is dead" bug, for as long as it took real activity to climb back past the old high-water mark. Hit for real on a live instance: reported as "no device is logging anymore, is the event log even live?" Reproduced deterministically (confirmed 100% of new entries dropped after a simulated restart-after-clear) before fixing. Fixed by seeding `_log_counter` from wall-clock time instead of `0`, so a fresh boot's ids are always higher than any previous boot's and can never collide with an old boundary — verified against two real, separate backend process restarts. **Lesson for next time:** a "boundary that survives reconnects" needs to also survive the thing on the *other end* of that boundary resetting; test the restart case, not just the reconnect case.
