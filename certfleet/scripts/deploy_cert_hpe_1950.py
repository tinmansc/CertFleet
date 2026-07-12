#!/usr/bin/env python3
"""Deploy Let's Encrypt certificates to HPE 1950 switches via SSH/SFTP.

Usage:
  python3 deploy_cert_hpe_1950.py --target SwitchName --check
  python3 deploy_cert_hpe_1950.py --target SwitchName --apply
  python3 deploy_cert_hpe_1950.py --target all --check

Switch inventory is read from:
  /config/scripts/hpe1950_switches.yaml

Credentials are read from HA secrets:
  /config/secrets.yaml  (keys: hpe_switch_cert_update_user,
                         hpe_switch_cert_update_user_password,
                         hpe_1950_xtd_cli_mode_password)
"""
import datetime as _hpe_dt

import argparse
import hashlib
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime

import yaml
import pexpect


FULLCHAIN = Path("/ssl/fullchain.pem")
PRIVKEY = Path("/ssl/privkey.pem")
SECRETS = Path("/config/secrets.yaml")
WORKDIR_BASE = Path("/config/scripts")
BACKUP_DIR = WORKDIR_BASE / "hpe1950_backups"
ROOT_X1_SELF_SIGNED = WORKDIR_BASE / "hpe-1950-isrg-root-x1-selfsigned.pem"
ROOT_YR_ISSUED_BY_X1 = WORKDIR_BASE / "hpe-1950-isrg-root-x1.pem"

ISRG_ROOT_X1_URL = "https://letsencrypt.org/certs/isrgrootx1.pem"
ISRG_ROOT_X1_SHA1 = "CABD2A79A1076A31F21D253635CB039D4329A5E8"

OPENSSL = "/usr/bin/openssl"
CURL = "/usr/bin/curl"
SSHPASS = "/usr/bin/sshpass"
SSH = "/usr/bin/ssh"
SFTP = "/usr/bin/sftp"

SETTINGS_FILE = WORKDIR_BASE / "hpe1950_cert_deploy_settings.yaml"
SWITCHES_FILE = WORKDIR_BASE / "hpe1950_switches.yaml"


def load_settings():
    defaults = {
        "backup": {
            "download_startup_config": True,
            "create_on_switch_backup": True,
        }
    }

    if not SETTINGS_FILE.exists():
        return defaults

    try:
        with SETTINGS_FILE.open("r") as f:
            loaded = yaml.safe_load(f) or {}
    except Exception as e:
        raise DeployError(f"Could not read settings file {SETTINGS_FILE}: {e}")

    settings = defaults.copy()
    settings["backup"] = defaults["backup"].copy()
    settings["backup"].update((loaded.get("backup") or {}))
    return settings


def load_switches(switches_file=None):
    path = Path(switches_file) if switches_file else SWITCHES_FILE

    if not path.exists():
        raise SystemExit(
            f"ERROR: Switch inventory file not found: {path}\n\n"
            "Create this file with your switch definitions, or configure the switch\n"
            "directly in CertFleet (credentials and switch fields in device editor).\n\n"
            "Example manual file:\n\n"
            "  switches:\n"
            "    LoftPoESwitch:\n"
            "      host: loftpoeswitch.example.com\n"
            "      ip: 10.10.100.244\n"
            "      pki_domain: hp-1950\n"
            "      ssl_policy: hp-1950\n"
            "      startup_config: flash:/startup.cfg\n\n"
            "The switch name must match the device name configured in CertFleet."
        )

    try:
        with path.open("r") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        raise SystemExit(f"ERROR: Could not read {path}: {e}")

    switches = data.get("switches", {})
    if not switches:
        raise SystemExit(f"ERROR: No switches defined in {path}")

    return switches


SSH_OPTS = [
    "-oKexAlgorithms=ecdh-sha2-nistp256,ecdh-sha2-nistp384,diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1,diffie-hellman-group1-sha1",
    "-oHostKeyAlgorithms=ssh-rsa",
    "-oPubkeyAcceptedAlgorithms=ssh-rsa",
    "-oMACs=hmac-sha2-256,hmac-sha2-512,hmac-sha1,hmac-md5,hmac-sha1-96,hmac-md5-96",
    "-oStrictHostKeyChecking=no",
    "-oUserKnownHostsFile=/config/.ssh/known_hosts",
]


class DeployError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def run(cmd, *, input_text=None, timeout=60, check=True):
    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise DeployError(f"Command failed: {' '.join(cmd)}\n{proc.stdout}")
    return proc.stdout


def load_secrets():
    # Accept credentials from env vars (set by CertFleet's comware.py).
    # This lets the device editor supply credentials without needing secrets.yaml.
    env_user = os.environ.get("HPE_SWITCH_USER")
    env_pass = os.environ.get("HPE_SWITCH_PASSWORD")
    env_xtd  = os.environ.get("HPE_XTD_PASSWORD")

    if env_user and env_pass and env_xtd:
        return {
            "hpe_switch_cert_update_user": env_user,
            "hpe_switch_cert_update_user_password": env_pass,
            "hpe_1950_xtd_cli_mode_password": env_xtd,
        }

    if not SECRETS.exists():
        raise DeployError(
            f"Credentials not found. Either configure them in the CertFleet "
            f"device editor (Username, Password, XTD CLI Password), or add the "
            f"following keys to {SECRETS}:\n"
            f"  hpe_switch_cert_update_user\n"
            f"  hpe_switch_cert_update_user_password\n"
            f"  hpe_1950_xtd_cli_mode_password"
        )

    with SECRETS.open("r") as f:
        data = yaml.safe_load(f) or {}

    required = [
        "hpe_switch_cert_update_user",
        "hpe_switch_cert_update_user_password",
        "hpe_1950_xtd_cli_mode_password",
    ]
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise DeployError(f"Missing secrets: {', '.join(missing)}")

    return data


def split_pem_certificates(pem_text):
    certs = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        pem_text,
        flags=re.S,
    )
    if not certs:
        raise DeployError("No certificates found in PEM file")
    return [c.strip() + "\n" for c in certs]


def cert_info_from_file(path):
    out = run([
        OPENSSL, "x509",
        "-in", str(path),
        "-noout",
        "-subject", "-issuer", "-serial", "-dates", "-fingerprint", "-sha1",
    ])
    return parse_cert_info(out)


def parse_cert_info(text):
    info = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("subject="):
            info["subject"] = line[len("subject="):]
        elif line.startswith("issuer="):
            info["issuer"] = line[len("issuer="):]
        elif line.startswith("serial="):
            info["serial"] = normalize_hex(line[len("serial="):])
        elif line.startswith("notBefore="):
            info["notBefore"] = line[len("notBefore="):]
        elif line.startswith("notAfter="):
            info["notAfter"] = line[len("notAfter="):]
        elif "Fingerprint=" in line:
            info["sha1"] = normalize_hex(line.split("=", 1)[1])
    return info


def normalize_hex(value):
    return re.sub(r"[^0-9A-Fa-f]", "", value).upper()


def get_served_cert_info(connect_host, server_name, timeout=8):
    cmd = (
        f"{OPENSSL} s_client -connect {connect_host}:443 "
        f"-servername {server_name} </dev/null 2>/dev/null | "
        f"{OPENSSL} x509 -noout -subject -issuer -serial -dates"
    )
    out = run(["sh", "-c", cmd], timeout=timeout)
    if "subject=" not in out:
        raise DeployError(f"Could not read served certificate from {connect_host}")
    return parse_cert_info(out)


def ensure_root_x1():
    if not ROOT_X1_SELF_SIGNED.exists():
        log(f"Downloading ISRG Root X1 to {ROOT_X1_SELF_SIGNED}")
        run([CURL, "-L", "-o", str(ROOT_X1_SELF_SIGNED), ISRG_ROOT_X1_URL], timeout=60)

    info = cert_info_from_file(ROOT_X1_SELF_SIGNED)
    actual = info.get("sha1")
    if actual != ISRG_ROOT_X1_SHA1:
        raise DeployError(
            f"ISRG Root X1 fingerprint mismatch. Expected {ISRG_ROOT_X1_SHA1}, got {actual}"
        )

    if info.get("subject") != info.get("issuer"):
        raise DeployError("ISRG Root X1 file is not self-signed")

    return ROOT_X1_SELF_SIGNED


def find_root_yr_in_chain(certs_pem, tmp_dir):
    """Scan the fullchain cert list for the Root YR certificate.

    Root YR is typically the second-to-last cert in the chain (issued by ISRG
    Root X1, which itself is self-signed and downloaded separately).  If found,
    write it to a temp file and return the path; otherwise return None.
    """
    for idx, cert_pem in enumerate(certs_pem):
        probe = Path(tmp_dir) / f"_probe_chain_{idx:02d}.pem"
        probe.write_text(cert_pem)
        try:
            info = cert_info_from_file(probe)
            if re.search(r"CN\s*=\s*Root YR", info.get("subject", "")):
                dest = Path(tmp_dir) / "hpe-1950-root-yr-from-chain.pem"
                probe.rename(dest)
                log(f"Root YR found in fullchain.pem (cert #{idx + 1})")
                return dest
        except Exception:
            pass
        probe.unlink(missing_ok=True)
    return None


def build_artifacts():
    if not FULLCHAIN.exists():
        raise DeployError(f"Missing {FULLCHAIN}")
    if not PRIVKEY.exists():
        raise DeployError(f"Missing {PRIVKEY}")

    tmp = Path(tempfile.mkdtemp(prefix="hpe1950-cert-", dir=str(WORKDIR_BASE)))
    fullchain_text = FULLCHAIN.read_text()
    certs = split_pem_certificates(fullchain_text)

    leaf = tmp / "hpe-1950-leaf.pem"
    leaf.write_text(certs[0])

    ca_files = []
    for idx, cert in enumerate(certs[1:], start=1):
        p = tmp / f"hpe-1950-chain-ca-{idx:02d}.pem"
        p.write_text(cert)
        ca_files.append(p)

    p12 = tmp / "hpe-1950-fullchain-legacy.p12"
    run([
        OPENSSL, "pkcs12", "-export", "-legacy",
        "-out", str(p12),
        "-inkey", str(PRIVKEY),
        "-in", str(FULLCHAIN),
        "-password", "pass:",
    ])

    root = ensure_root_x1()

    # Try to extract Root YR from the fullchain (it's usually already there).
    # Fall back to the manually-placed file for older/non-standard chains.
    root_yr = find_root_yr_in_chain(certs, tmp)
    if root_yr is None:
        if ROOT_YR_ISSUED_BY_X1.exists():
            root_yr_info = cert_info_from_file(ROOT_YR_ISSUED_BY_X1)
            if not re.search(r"CN\s*=\s*Root YR", root_yr_info.get("subject", "")):
                raise DeployError(
                    f"{ROOT_YR_ISSUED_BY_X1} does not appear to be Root YR. "
                    f"Subject was: {root_yr_info.get('subject')}"
                )
            root_yr = ROOT_YR_ISSUED_BY_X1
            log(f"Root YR loaded from {ROOT_YR_ISSUED_BY_X1}")
        else:
            raise DeployError(
                "Root YR certificate not found. It was not present in fullchain.pem "
                f"and the fallback file {ROOT_YR_ISSUED_BY_X1} does not exist.\n"
                "Your Let's Encrypt chain may use a different hierarchy. "
                "Place the Root YR PEM manually at that path to continue."
            )

    local_info = cert_info_from_file(leaf)

    return {
        "tmp": tmp,
        "leaf": leaf,
        "ca_files": ca_files,
        "root": root,
        "root_yr": root_yr,
        "p12": p12,
        "local_info": local_info,
    }


def flash_to_sftp_path(remote_flash_path):
    if not remote_flash_path.startswith("flash:/"):
        raise DeployError(f"Expected flash:/ path, got: {remote_flash_path}")
    return "/" + remote_flash_path[len("flash:/"):]


def sftp_batch(sw, username, password, batch, timeout=90):
    env = os.environ.copy()
    env["SSHPASS"] = password

    cmd = [SSHPASS, "-e", SFTP, *SSH_OPTS, f"{username}@{sw['ip']}"]
    proc = subprocess.run(
        cmd,
        input=batch,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise DeployError(f"SFTP failed for {sw['name']}:\n{proc.stdout}")
    return proc.stdout


def sftp_upload(sw, username, password, files):
    batch_lines = ["cd /pki"]
    for path in files:
        batch_lines.append(f"put {path}")
    batch_lines += ["ls", "quit"]
    batch = "\n".join(batch_lines) + "\n"

    log(f"Uploading files to {sw['name']} /pki ...")
    return sftp_batch(sw, username, password, batch)


def sftp_download(sw, username, password, remote_flash_path, local_path):
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)

    remote_sftp_path = flash_to_sftp_path(remote_flash_path)
    batch = f"get {remote_sftp_path} {local_path}\nquit\n"

    log(f"Downloading {remote_flash_path} from {sw['name']} to {local_path}")
    sftp_batch(sw, username, password, batch)

    if not local_path.exists() or local_path.stat().st_size == 0:
        raise DeployError(f"Downloaded file missing or empty: {local_path}")

    return local_path


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_startup_config_paths(display_startup_output):
    current = None
    next_main = None

    m = re.search(r"Current startup saved-configuration file:\s*(flash:/\S+)", display_startup_output)
    if m:
        current = m.group(1).rstrip("(*)")

    m = re.search(r"Next main startup saved-configuration file:\s*(flash:/\S+)", display_startup_output)
    if m:
        next_main = m.group(1).rstrip("(*)")

    return current, next_main


def parse_dir_file_size(dir_output, remote_flash_path):
    filename = remote_flash_path.split("/")[-1]
    for line in dir_output.splitlines():
        if line.rstrip().endswith(filename):
            m = re.search(r"\s-rw-\s+(\d+)\s+", line)
            if m:
                return int(m.group(1))
    return None


def backup_startup_config(sw, secrets):
    settings = load_settings()
    backup_settings = settings.get("backup", {})
    download_startup_config = bool(backup_settings.get("download_startup_config", True))
    create_on_switch_backup = bool(backup_settings.get("create_on_switch_backup", True))

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    username = secrets["hpe_switch_cert_update_user"]
    password = secrets["hpe_switch_cert_update_user_password"]
    xtd_password = secrets["hpe_1950_xtd_cli_mode_password"]

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local_backup = BACKUP_DIR / f"{sw['name']}-startup-before-hpe-cert-{timestamp}.cfg"
    remote_backup = f"flash:/backup-{sw['name']}-{timestamp}.cfg"

    with ComwareSession(sw, username, password, xtd_password, timeout=45) as cli:
        startup_out = cli.cmd("display startup", allow_error=True)
        current_startup, next_startup = parse_startup_config_paths(startup_out)

        if not current_startup:
            raise DeployError(f"Could not determine current startup config on {sw['name']}:\n{startup_out}")

        log(f"Current startup config on {sw['name']}: {current_startup}")
        log(f"Next startup config on {sw['name']}:    {next_startup or 'UNKNOWN'}")

        dir_current = cli.cmd(f"dir {current_startup}", allow_error=True)
        current_size = parse_dir_file_size(dir_current, current_startup)

        if current_size is None:
            raise DeployError(f"Could not determine size of current startup config {current_startup}:\n{dir_current}")

        if current_size < 500:
            raise DeployError(f"Current startup config looks too small ({current_size} bytes). Refusing to proceed.")

        log(f"Current startup config size: {current_size} bytes")

        if create_on_switch_backup:
            log(f"Creating on-switch backup: {remote_backup}")
            cli.interactive(
                f"copy {current_startup} {remote_backup}",
                responses=[
                    (r"(?i)overwrite.*\[Y/N\]\s*:", "N"),
                    (r"(?i)\[Y/N\]\s*:", "Y"),
                    (r"(?i)continue\? \[Y/N\]\s*:", "Y"),
                ],
                timeout=90,
                allow_error=False,
            )

            dir_backup = cli.cmd(f"dir {remote_backup}", allow_error=True)
            backup_size = parse_dir_file_size(dir_backup, remote_backup)

            if backup_size != current_size:
                raise DeployError(
                    f"On-switch backup size mismatch. Original={current_size}, backup={backup_size}\n{dir_backup}"
                )
        else:
            remote_backup = None
            log("On-switch startup backup: DISABLED by settings.")

    downloaded = None
    local_sha = None

    if download_startup_config:
        downloaded = sftp_download(sw, username, password, current_startup, local_backup)
        local_size = downloaded.stat().st_size

        if local_size != current_size:
            raise DeployError(
                f"Downloaded startup config size mismatch. Switch={current_size}, local={local_size}"
            )

        local_sha = sha256_file(downloaded)

        log(f"Local startup backup: {downloaded}")
        log(f"Local startup backup size: {local_size} bytes")
        log(f"Local startup backup SHA256: {local_sha}")
    else:
        log("Local startup config download backup: DISABLED by settings.")
        log("Skipping local startup-config download, SHA256 logging, and local backup file creation.")

    log(f"On-switch startup backup: {remote_backup or 'DISABLED'}")

    return {
        "current_startup": current_startup,
        "next_startup": next_startup,
        "remote_backup": remote_backup,
        "local_backup": str(downloaded) if downloaded else None,
        "size": current_size,
        "sha256": local_sha,
    }


def verify_saved_startup_config(sw, secrets, startup_config):
    settings = load_settings()
    download_startup_config = bool(
        settings.get("backup", {}).get("download_startup_config", True)
    )

    username = secrets["hpe_switch_cert_update_user"]
    password = secrets["hpe_switch_cert_update_user_password"]
    xtd_password = secrets["hpe_1950_xtd_cli_mode_password"]

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local_saved = BACKUP_DIR / f"{sw['name']}-saved-after-hpe-cert-{timestamp}.cfg"

    with ComwareSession(sw, username, password, xtd_password, timeout=45) as cli:
        dir_saved = cli.cmd(f"dir {startup_config}", allow_error=True)
        saved_size = parse_dir_file_size(dir_saved, startup_config)

        if saved_size is None:
            raise DeployError(f"Could not verify saved config exists: {startup_config}\n{dir_saved}")

        if saved_size < 500:
            raise DeployError(f"Saved config looks too small ({saved_size} bytes). Refusing to set startup file.")

    if download_startup_config:
        downloaded = sftp_download(sw, username, password, startup_config, local_saved)
        saved_sha = sha256_file(downloaded)

        text = downloaded.read_text(errors="ignore")
        required_lines = [
            f"ip https ssl-server-policy {sw['ssl_policy']}",
            "ip https enable",
            "ip http enable",
            f"pki-domain {sw['pki_domain']}",
        ]

        missing = [line for line in required_lines if line not in text]
        if missing:
            raise DeployError(
                "Saved config does not contain expected HTTPS/PKI lines. "
                f"Missing: {missing}. Refusing to set startup file."
            )

        log(f"Saved config verified: {startup_config}")
        log(f"Saved config size: {downloaded.stat().st_size} bytes")
        log(f"Saved config SHA256: {saved_sha}")
        local_saved_return = str(downloaded)
    else:
        saved_sha = None
        local_saved_return = None
        log(f"Saved config exists and has sane size: {startup_config}")
        log(f"Saved config size: {saved_size} bytes")
        log("Saved config local download/content verification: DISABLED by settings.")
        log("Reduced assurance: expected HTTPS/PKI lines were not scanned in the saved startup file.")

    with ComwareSession(sw, username, password, xtd_password, timeout=45) as cli:
        cli.cmd(f"startup saved-configuration {startup_config} main", allow_error=False)
        startup_out = cli.cmd("display startup", allow_error=True)
        _current, next_main = parse_startup_config_paths(startup_out)

        log("")
        log("--- display startup after startup-file assignment ---")
        log(startup_out.strip())

        if next_main != startup_config:
            raise DeployError(
                f"Startup filename verification failed. Expected next main {startup_config}, "
                f"but switch reports {next_main}."
            )

    return {
        "startup_config": startup_config,
        "local_saved": local_saved_return,
        "size": saved_size,
        "sha256": saved_sha,
    }


class ComwareSession:
    def __init__(self, sw, username, password, xtd_password, timeout=30):
        self.sw = sw
        self.username = username
        self.password = password
        self.xtd_password = xtd_password
        self.timeout = timeout
        self.child = None

    def __enter__(self):
        env = os.environ.copy()
        env["SSHPASS"] = self.password

        cmd = " ".join(
            [SSHPASS, "-e", SSH, *SSH_OPTS, f"{self.username}@{self.sw['ip']}"]
        )
        self.child = pexpect.spawn(
            cmd,
            env=env,
            encoding="utf-8",
            timeout=self.timeout,
            codec_errors="replace",
        )
        self.child.logfile_read = None

        i = self.child.expect([
            r"[<\[][^\r\n]+[>\]]\s*$",
            r"Are you sure you want to continue connecting",
            pexpect.EOF,
            pexpect.TIMEOUT,
        ])
        if i == 1:
            self.child.sendline("yes")
            self.child.expect(r"[<\[][^\r\n]+[>\]]\s*$")
        elif i in (2, 3):
            raise DeployError(f"SSH login failed to {self.sw['name']}")

        self.cmd("screen-length disable", timeout=10, allow_error=True)
        self.enter_xtd()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.child is not None:
            try:
                self.child.sendline("quit")
            except Exception:
                pass
            try:
                self.child.close(force=True)
            except Exception:
                pass

    def prompt(self):
        return r"[<\[][^\r\n]+[>\]]\s*$"

    def cmd(self, command, timeout=None, allow_error=False):
        timeout = timeout or self.timeout
        self.child.sendline(command)
        output_parts = []

        while True:
            i = self.child.expect([
                r"---- More ----",
                self.prompt(),
                pexpect.TIMEOUT,
                pexpect.EOF,
            ], timeout=timeout)

            output_parts.append(self.child.before or "")

            if i == 0:
                self.child.send(" ")
                continue

            if i == 1:
                out = "".join(output_parts)
                if not allow_error and ("% " in out or "Error" in out or "failed" in out.lower()):
                    raise DeployError(f"Command failed on {self.sw['name']}: {command}\n{out}")
                return out

            raise DeployError(
                f"Timeout/EOF waiting for command on {self.sw['name']}: {command}\n"
                f"Partial output:\n{''.join(output_parts)}"
            )

    def enter_xtd(self):
        self.child.sendline("xtd-cli-mode")
        transcript = ""
        while True:
            i = self.child.expect([
                r"(?i)\[Y/N\]\s*:",
                r"(?i)password\s*:",
                r"(?i)input.*password",
                r"[<\[][^\r\n]+[>\]]\s*$",
                pexpect.TIMEOUT,
                pexpect.EOF,
            ], timeout=15)
            transcript += self.child.before or ""

            if i == 0:
                self.child.sendline("Y")
            elif i in (1, 2):
                self.child.sendline(self.xtd_password)
            elif i == 3:
                return transcript
            else:
                raise DeployError(
                    f"Could not enter xtd-cli-mode on {self.sw['name']}. "
                    f"Transcript:\n{transcript}"
                )

    def interactive(self, command, responses, timeout=90, allow_error=False):
        self.child.sendline(command)
        transcript = ""
        while True:
            patterns = [r"[<\[][^\r\n]+[>\]]\s*$"]
            for pattern, _answer in responses:
                patterns.append(pattern)

            i = self.child.expect(patterns + [pexpect.TIMEOUT, pexpect.EOF], timeout=timeout)
            transcript += self.child.before or ""

            if i == 0:
                if not allow_error and ("% " in transcript or "Failed to" in transcript):
                    raise DeployError(f"Command failed on {self.sw['name']}: {command}\n{transcript}")
                return transcript

            response_index = i - 1
            if 0 <= response_index < len(responses):
                _pattern, answer = responses[response_index]
                self.child.sendline(answer)
            else:
                raise DeployError(f"Interactive command timed out/closed on {self.sw['name']}: {command}\n{transcript}")

    def save_to_file(self, filename):
        self.child.sendline("save")
        transcript = ""
        while True:
            i = self.child.expect([
                r"Are you sure\? \[Y/N\]",
                r"Please input the file name.*:",
                r"exists, overwrite\? \[Y/N\]",
                r"successfully",
                r"[<\[][^\r\n]+[>\]]\s*$",
                pexpect.TIMEOUT,
                pexpect.EOF,
            ], timeout=120)
            transcript += self.child.before or ""
            if i == 0:
                self.child.sendline("Y")
            elif i == 1:
                self.child.sendline(filename)
            elif i == 2:
                self.child.sendline("Y")
            elif i == 3:
                continue
            elif i == 4:
                return transcript
            else:
                raise DeployError(f"Save failed on {self.sw['name']}:\n{transcript}")


def check_switch(sw, secrets):
    log("")
    log(f"===== CHECK: {sw['name']} ({sw['ip']}) =====")
    log(f"Started: {_hpe_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    local_info = cert_info_from_file(FULLCHAIN)
    served = get_served_cert_info(sw["ip"], sw["host"])

    log(f"Local cert subject:  {local_info.get('subject')}")
    log(f"Local cert issuer:   {local_info.get('issuer')}")
    log(f"Local cert serial:   {local_info.get('serial')}")
    log(f"Local cert expires:  {local_info.get('notAfter')}")
    log("")
    log(f"Served cert subject: {served.get('subject')}")
    log(f"Served cert issuer:  {served.get('issuer')}")
    log(f"Served cert serial:  {served.get('serial')}")
    log(f"Served cert expires: {served.get('notAfter')}")

    if served.get("serial") == local_info.get("serial"):
        log("Certificate status: MATCH")
    else:
        log("Certificate status: DIFFERENT / needs deployment")

    with ComwareSession(
        sw,
        secrets["hpe_switch_cert_update_user"],
        secrets["hpe_switch_cert_update_user_password"],
        secrets["hpe_1950_xtd_cli_mode_password"],
    ) as cli:
        for command in [
            "display version",
            "display current-configuration | include https",
            "display current-configuration | include http",
            "display current-configuration | include ssl",
            "display current-configuration | include pki",
            "display startup",
            "dir flash:/pki",
        ]:
            out = cli.cmd(command, allow_error=True, timeout=20)
            log("")
            log(f"--- {command} ---")
            log(out.strip())

    cert_matches = served.get("serial") == local_info.get("serial")

    if cert_matches:
        verify_startup_config_readonly(sw, secrets)
    else:
        log("")
        log("Startup-config safety verification: SKIPPED because served cert does not yet match local cert.")

    return cert_matches


def verify_startup_config_readonly(sw, secrets):
    settings = load_settings()
    download_startup_config = bool(
        settings.get("backup", {}).get("download_startup_config", True)
    )

    username = secrets["hpe_switch_cert_update_user"]
    password = secrets["hpe_switch_cert_update_user_password"]
    xtd_password = secrets["hpe_1950_xtd_cli_mode_password"]

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local_check = BACKUP_DIR / f"{sw['name']}-startup-check-{timestamp}.cfg"

    log("")
    log(f"--- startup-config safety verification: {sw['name']} ---")

    with ComwareSession(sw, username, password, xtd_password, timeout=45) as cli:
        startup_out = cli.cmd("display startup", allow_error=True)
        current_startup, next_startup = parse_startup_config_paths(startup_out)

        log(startup_out.strip())

        expected = sw["startup_config"]

        if next_startup != expected:
            raise DeployError(
                f"The 'Startup config' field configured for {sw['name']} in CertFleet is "
                f"{expected}, but the switch's actual next-boot startup file is {next_startup}. "
                f"This isn't a certificate problem — the deploy itself may well have succeeded — "
                f"it means the Startup config field for this device doesn't match what's really "
                f"configured on the switch. Edit this device in CertFleet and set Startup config "
                f"to {next_startup}, then re-run."
            )

        dir_out = cli.cmd(f"dir {expected}", allow_error=True)
        size = parse_dir_file_size(dir_out, expected)

        if size is None:
            raise DeployError(f"Could not verify startup config exists: {expected}\n{dir_out}")

        if size < 500:
            raise DeployError(f"Startup config {expected} looks too small: {size} bytes")

        log(f"Startup config exists: {expected}")
        log(f"Startup config size:   {size} bytes")

    if download_startup_config:
        downloaded = sftp_download(sw, username, password, expected, local_check)
        local_size = downloaded.stat().st_size

        if local_size != size:
            raise DeployError(
                f"Downloaded startup config size mismatch on {sw['name']}. "
                f"Switch={size}, local={local_size}"
            )

        text = downloaded.read_text(errors="ignore")
        required_lines = [
            f"ip https ssl-server-policy {sw['ssl_policy']}",
            "ip https enable",
            "ip http enable",
            f"pki-domain {sw['pki_domain']}",
        ]

        missing = [line for line in required_lines if line not in text]
        if missing:
            raise DeployError(
                f"Startup config {expected} is missing expected HTTPS/PKI lines: {missing}"
            )

        log(f"Startup config downloaded for check: {downloaded}")
        log(f"Startup config SHA256: {sha256_file(downloaded)}")
        log("Startup-config safety verification: PASS")
    else:
        log("Startup config local download/content verification: DISABLED by settings.")
        log("Skipping local startup-config download, SHA256 logging, and expected-line scan.")
        log("Startup-config safety verification: PASS with reduced assurance")


def apply_switch(sw, secrets):
    log("")
    log(f"===== APPLY: {sw['name']} ({sw['ip']}) =====")
    log(f"Started: {_hpe_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    artifacts = build_artifacts()
    local_serial = artifacts["local_info"].get("serial")
    served = get_served_cert_info(sw["ip"], sw["host"])

    if served.get("serial") == local_serial:
        log("Served cert already matches local cert. No deployment needed.")
        check_switch(sw, secrets)
        return

    log("Creating startup-config backups before making changes...")
    backup_startup_config(sw, secrets)

    upload_files = [artifacts["root"], artifacts["root_yr"], *artifacts["ca_files"], artifacts["p12"]]
    sftp_upload(
        sw,
        secrets["hpe_switch_cert_update_user"],
        secrets["hpe_switch_cert_update_user_password"],
        upload_files,
    )

    root_remote = f"flash:/pki/{artifacts['root'].name}"
    root_yr_remote = f"flash:/pki/{artifacts['root_yr'].name}"
    ca_remotes = [f"flash:/pki/{p.name}" for p in artifacts["ca_files"]]
    p12_remote = f"flash:/pki/{artifacts['p12'].name}"

    with ComwareSession(
        sw,
        secrets["hpe_switch_cert_update_user"],
        secrets["hpe_switch_cert_update_user_password"],
        secrets["hpe_1950_xtd_cli_mode_password"],
        timeout=45,
    ) as cli:
        domain = sw["pki_domain"]
        policy = sw["ssl_policy"]

        log("Configuring PKI domain, root fingerprint, and CRL behavior...")
        cli.cmd("system-view")
        cli.cmd(f"pki domain {domain}")
        cli.cmd(f"root-certificate fingerprint sha1 {ISRG_ROOT_X1_SHA1}")
        cli.cmd("undo crl check enable")
        cli.cmd("quit")

        log("Importing self-signed ISRG Root X1...")
        cli.interactive(
            f"pki import domain {domain} pem ca filename {root_remote}",
            responses=[
                (r"(?i)overwrite.*\[Y/N\]\s*:", "Y"),
                (r"(?i)\[Y/N\]\s*:", "Y"),
                (r"(?i)Continue\? \[Y/N\]\s*:", "Y"),
            ],
            timeout=90,
            allow_error=False,
        )

        log(f"Importing Root YR issued by ISRG Root X1: {root_yr_remote}")
        cli.interactive(
            f"pki import domain {domain} pem ca filename {root_yr_remote}",
            responses=[
                (r"(?i)overwrite.*\[Y/N\]\s*:", "Y"),
                (r"(?i)\[Y/N\]\s*:", "Y"),
                (r"(?i)Continue\? \[Y/N\]\s*:", "Y"),
            ],
            timeout=90,
            allow_error=False,
        )

        for ca_remote in ca_remotes:
            log(f"Importing CA/intermediate: {ca_remote}")
            cli.interactive(
                f"pki import domain {domain} pem ca filename {ca_remote}",
                responses=[
                    (r"(?i)overwrite.*\[Y/N\]\s*:", "Y"),
                    (r"(?i)overwrite it\? \[Y/N\]\s*:", "Y"),
                    (r"(?i)\[Y/N\]\s*:", "Y"),
                    (r"(?i)continue\? \[Y/N\]\s*:", "Y"),
                ],
                timeout=90,
                allow_error=False,
            )

        log("Importing local certificate/private key P12...")
        cli.interactive(
            f"pki import domain {domain} p12 local filename {p12_remote}",
            responses=[
                (r"(?i)password.*:", ""),
                (r"(?i)overwrite.*\[Y/N\]\s*:", "Y"),
                (r"(?i)overwrite it\? \[Y/N\]\s*:", "Y"),
                (r"(?i)\[Y/N\]\s*:", "Y"),
                (r"(?i)default name:.*", ""),
                (r"(?i)please enter.*1 to 64.*", ""),
                (r"(?i)continue\? \[Y/N\]\s*:", "Y"),
            ],
            timeout=120,
            allow_error=False,
        )

        log("Binding SSL policy and reloading HTTP/HTTPS in known-good order...")
        cli.cmd(f"ssl server-policy {policy}")
        cli.cmd(f"pki-domain {domain}")
        cli.cmd("quit")
        cli.cmd("undo ip http enable", allow_error=True)
        cli.cmd("undo ip https enable", allow_error=True)
        cli.cmd(f"ip https ssl-server-policy {policy}")
        cli.cmd("ip https enable")
        cli.cmd("ip http enable")
        cli.cmd("quit")

    log("Verifying served HTTPS certificate after reload...")
    served_after = get_served_cert_info(sw["ip"], sw["host"], timeout=12)
    log(f"Served-after subject: {served_after.get('subject')}")
    log(f"Served-after issuer:  {served_after.get('issuer')}")
    log(f"Served-after serial:  {served_after.get('serial')}")
    log(f"Expected serial:      {local_serial}")

    if served_after.get("serial") != local_serial:
        raise DeployError(
            "Deployment commands completed, but served certificate does not match local cert. "
            "NOT saving config."
        )

    log("Verification passed. Saving configuration to intended startup file...")
    with ComwareSession(
        sw,
        secrets["hpe_switch_cert_update_user"],
        secrets["hpe_switch_cert_update_user_password"],
        secrets["hpe_1950_xtd_cli_mode_password"],
        timeout=45,
    ) as cli:
        cli.save_to_file(sw["startup_config"])

    log("Verifying saved startup config before assigning it as next startup...")
    verify_saved_startup_config(sw, secrets, sw["startup_config"])

    log(f"SUCCESS: {sw['name']} now serves the expected certificate and config was saved.")


def main():
    parser = argparse.ArgumentParser(description="Deploy Let's Encrypt cert to HPE 1950 switches")
    parser.add_argument("--switches-file", help="Path to switch inventory YAML (default: /config/scripts/hpe1950_switches.yaml)")
    parser.add_argument("--target", required=True, help="Switch name or 'all'")
    parser.add_argument("--check", action="store_true", help="Read-only check mode")
    parser.add_argument("--apply", action="store_true", help="Deploy if cert differs")
    args = parser.parse_args()

    if args.check == args.apply:
        raise SystemExit("Choose exactly one: --check or --apply")

    switches = load_switches(args.switches_file)

    if args.target != "all" and args.target not in switches:
        raise SystemExit(
            f"ERROR: Unknown target '{args.target}'. "
            f"Available switches: {', '.join(switches.keys())}"
        )

    secrets = load_secrets()

    targets = list(switches.keys()) if args.target == "all" else [args.target]

    failures = 0
    for name in targets:
        sw = dict(switches[name])
        sw["name"] = name
        try:
            if args.check:
                check_switch(sw, secrets)
            else:
                apply_switch(sw, secrets)
        except Exception as e:
            failures += 1
            log("")
            log(f"ERROR on {name}: {e}")

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
