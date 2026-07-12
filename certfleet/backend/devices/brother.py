"""Brother MFC printer certificate deployer (HTTP scraping + PKCS#12)."""
from __future__ import annotations

import io
import re
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from cert_reader import LocalCert, probe_tls_fingerprint, probe_tls_serial
from config import DeviceConfig
from devices.base import DeployStatus, DeviceResult, Logger, ensure_https, secure_key, strip_scheme


REQUEST_TIMEOUT = 30


def _normalize_serial(serial: str) -> str:
    s = re.sub(r"[^0-9a-fA-F]", "", serial).lower()
    if len(s) % 2 == 1:
        s = "0" + s
    return s


def _load_fullchain(path: str) -> list[x509.Certificate]:
    data = Path(path).read_bytes()
    blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", data, flags=re.S
    )
    return [x509.load_pem_x509_certificate(b) for b in blocks]


def _build_pkcs12(cert_path: str, key_ba: bytearray, password: str) -> bytes:
    """Build a PKCS#12 bundle. key_ba is wiped by the caller's secure_key context."""
    certs = _load_fullchain(cert_path)
    leaf, chain = certs[0], certs[1:]
    key = serialization.load_pem_private_key(bytes(key_ba), password=None)
    cn_attrs = leaf.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    name = (cn_attrs[0].value if cn_attrs else "cert").encode()
    return pkcs12.serialize_key_and_certificates(
        name=name,
        key=key,
        cert=leaf,
        cas=chain,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode()),
    )


class BrotherPrinter:
    def __init__(self, base_url: str, password: str, verify_tls: bool, log: Logger):
        self.base = base_url.rstrip("/") + "/"
        self.password = password
        self.log = log
        self.s = requests.Session()
        self.s.verify = verify_tls

    def _url(self, path: str) -> str:
        return urljoin(self.base, path.lstrip("/"))

    def _get(self, path: str):
        r = self.s.get(self._url(path), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r

    def _post(self, path: str, data=None, files=None, headers=None):
        r = self.s.post(self._url(path), data=data, files=files,
                        headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r

    @staticmethod
    def _soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _csrf(soup: BeautifulSoup) -> str:
        token = soup.find("input", {"name": "CSRFToken"})
        if not token or not token.get("value"):
            raise RuntimeError("Could not find CSRFToken")
        return token["value"]

    def login(self):
        self.log("info", "Brother: logging in")
        r = self._get("/home/status.html")
        soup = self._soup(r.text)
        pw_input = soup.find("input", {"type": "password"})
        if not pw_input:
            raise RuntimeError("Could not find login password field")
        field = pw_input["name"]
        self._post("/home/status.html", data={field: self.password, "loginurl": "/home/status.html"})
        self.log("info", "Brother: login successful")

    def list_cert_indexes(self) -> list[int]:
        r = self._get("/net/security/certificate/certificate.html")
        return sorted(set(int(x) for x in re.findall(r'view\.html\?idx=(\d+)', r.text)))

    def view_cert_serial(self, idx: int) -> str | None:
        r = self._get(f"/net/security/certificate/view.html?idx={idx}")
        text = self._soup(r.text).get_text("\n")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if line == "Serial Number" and i + 1 < len(lines):
                return _normalize_serial(lines[i + 1])
        return None

    def upload_pkcs12(self, p12_bytes: bytes, p12_password: str):
        r = self._get("/net/security/certificate/import.html?pageid=455")
        soup = self._soup(r.text)
        token = self._csrf(soup)
        self.log("info", "Brother: uploading PKCS#12 certificate")
        self._post(
            "/net/security/certificate/import.html",
            data={
                "pageid": "455",
                "CSRFToken": token,
                "B420": "", "B42e": "",
                "hidden_certificate_process_control": "1",
                "B340": p12_password,
                "hidden_cert_import_password": p12_password,
            },
            files={"B33f": ("brother.p12", io.BytesIO(p12_bytes), "application/x-pkcs12")},
        )

    def get_active_idx(self) -> int | None:
        r = self._get("/net/net/certificate/http.html?pageid=386")
        select = self._soup(r.text).find("select", {"name": "B439"})
        if not select:
            return None
        selected = select.find("option", selected=True)
        if not selected:
            return None
        try:
            return int(selected.get("value", "0"))
        except ValueError:
            return None

    def apply_http_certificate(self, cert_idx: int) -> bool:
        r = self._get("/net/net/certificate/http.html?pageid=386")
        soup = self._soup(r.text)
        form = soup.find("form", {"id": "http_setting"})
        if not form:
            raise RuntimeError("Could not find HTTP settings form")
        select = form.find("select", {"name": "B439"})
        options = [(o.get("value", ""), o.get_text(strip=True))
                   for o in select.find_all("option")
                   if o.get("value", "0") != "0" and o.get_text(strip=True).lower() != "preset"]
        if not options:
            raise RuntimeError("No certificate options in B439 dropdown")

        selected = select.find("option", selected=True)
        current_val = selected.get("value", "") if selected else ""
        target_val, target_text = options[-1]

        if current_val == target_val:
            self.log("info", "Brother: desired cert already selected, no change needed")
            return False

        data = {}
        for name in ["pageid", "CSRFToken", "B436", "B437", "B39e"]:
            item = form.find("input", {"name": name})
            if item:
                data[name] = item.get("value", "")
        data.update({
            "B439": target_val,
            "B389": "1", "B38a": "1", "B3a1": "1",
            "B39f": "1", "B3a0": "1",
            "B447": "1",
            "http_page_mode": "0",
        })
        self.log("info", f"Brother: applying certificate (idx value={target_val})")
        r = self._post("/net/net/certificate/http.html", data=data)

        page_lower = self._soup(r.text).get_text(" ", strip=True).lower()
        if "restart immediately" in page_lower or "device needs to restart" in page_lower:
            self._confirm_reboot(r.text)
            return True
        return False

    def _confirm_reboot(self, html: str):
        soup = self._soup(html)
        form = soup.find("form")
        if not form:
            raise RuntimeError("Could not find reboot confirmation form")
        data = {}
        for name in ["pageid", "CSRFToken"]:
            item = form.find("input", {"name": name})
            if item:
                data[name] = item.get("value", "")
        data["active_other_protocol"] = "1"
        data["http_page_mode"] = "4"
        self.log("info", "Brother: confirming reboot")
        self._post("/net/net/certificate/http.html", data=data)

    def wait_for_https(self, max_wait: int = 90):
        self.log("info", "Brother: waiting for HTTPS to come back online…")
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                r = self.s.get(self._url("/home/status.html"), timeout=10)
                if r.status_code < 500:
                    self.log("info", "Brother: HTTPS is responding again")
                    return
            except Exception:
                pass
            time.sleep(5)
        raise RuntimeError(f"Brother HTTPS did not come back within {max_wait}s")

    def delete_certificate(self, idx: int):
        self.log("info", f"Brother: deleting old certificate idx={idx}")
        r = self._get(f"/net/security/certificate/delete.html?idx={idx}")
        soup = self._soup(r.text)
        form = soup.find("form", {"id": "cert_delete"})
        if not form:
            raise RuntimeError(f"Could not find delete form for idx={idx}")
        data = {
            inp["name"]: inp.get("value", "")
            for inp in form.find_all("input", {"type": "hidden"})
            if inp.get("name")
        }
        action = form.get("action") or "/net/security/certificate/delete.html"
        self._post(
            action, data=data,
            headers={"Referer": self._url(f"/net/security/certificate/delete.html?idx={idx}"),
                     "Origin": self.base.rstrip("/")},
        )
        self.log("info", f"Brother: deleted idx={idx}")


def _resolve_base_url(host_input: str, log: Logger) -> str:
    """
    If the user supplied an explicit http:// or https:// scheme, honour it.
    Otherwise probe port 443 first; fall back to http:// if HTTPS is not yet
    available (normal for first-time cert deployment before a cert is installed).
    """
    import socket as _sock

    if host_input.startswith("http://") or host_input.startswith("https://"):
        return host_input.rstrip("/")

    hostname = strip_scheme(host_input)
    try:
        s = _sock.create_connection((hostname, 443), timeout=5)
        s.close()
        log("info", f"Brother: HTTPS reachable on port 443 — using https://{hostname}")
        return f"https://{hostname}"
    except Exception:
        log("warn", f"Brother: port 443 not reachable, falling back to http://{hostname} "
                    f"(expected on first-time deployment before a cert is installed)")
        return f"http://{hostname}"


def check(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=False)


def deploy(cfg: DeviceConfig, local: LocalCert, log: Logger) -> DeviceResult:
    return _run(cfg, local, log, deploy=True)


def _run(cfg: DeviceConfig, local: Optional[LocalCert], log: Logger, deploy: bool) -> DeviceResult:
    host = _resolve_base_url(cfg.host, log)
    hostname = strip_scheme(host)

    # Probed independently of the printer login below — a TLS handshake
    # needs no credentials, so it should still succeed (and get reported)
    # even if the login fails.
    try:
        live_fp = probe_tls_fingerprint(hostname)
    except Exception as e:
        log("warn", f"Brother: TLS probe failed ({e})")
        live_fp = None

    try:
        printer = BrotherPrinter(host, cfg.password or "", cfg.verify_tls, log)
        printer.login()

        if local is None:
            if deploy:
                raise RuntimeError("No local certificate available to deploy")
            log("info", "Brother: connected and authenticated — no local certificate to compare")
            return DeviceResult(
                status=DeployStatus.NO_LOCAL_CERT,
                message="Connected — credentials OK (no local cert to compare)",
                live_fingerprint=live_fp,
            )

        before = {idx: printer.view_cert_serial(idx) for idx in printer.list_cert_indexes()}
        matching = [idx for idx, serial in before.items() if serial == local.serial]

        if matching:
            new_idx = sorted(matching)[-1]
            log("info", f"Brother: current cert already on printer at idx={new_idx}")
            if not deploy:
                return DeviceResult(
                    status=DeployStatus.ALREADY_CURRENT,
                    message="Certificate current",
                    live_fingerprint=live_fp,
                    local_fingerprint=local.fingerprint,
                )
        else:
            if not deploy:
                log("info", "Brother: certificate differs (check-only mode)")
                return DeviceResult(
                    status=DeployStatus.NEEDS_DEPLOY,
                    message="Certificate differs — deploy required",
                    live_fingerprint=live_fp,
                    local_fingerprint=local.fingerprint,
                )

            with secure_key(local.key_path) as key_ba:
                p12 = _build_pkcs12(local.cert_path, key_ba, cfg.p12_password or "changeme")
            printer.upload_pkcs12(p12, cfg.p12_password or "changeme")
            time.sleep(5)

            after = {idx: printer.view_cert_serial(idx) for idx in printer.list_cert_indexes()}
            matching = [idx for idx, serial in after.items() if serial == local.serial]
            if not matching:
                raise RuntimeError("Uploaded certificate not found on printer by serial")
            new_idx = sorted(matching)[-1]
            log("info", f"Brother: uploaded cert found at idx={new_idx}")

        if deploy:
            rebooted = printer.apply_http_certificate(new_idx)
            if rebooted:
                printer.wait_for_https()
                printer.login()

        live_serial = probe_tls_serial(hostname)
        if deploy and live_serial != local.serial:
            raise RuntimeError(f"HTTPS verification failed: live={live_serial}, expected={local.serial}")

        if deploy and cfg.delete_old_certs:
            active_idx = printer.get_active_idx()
            cn_attrs = _load_fullchain(local.cert_path)[0].subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            local_cn = cn_attrs[0].value if cn_attrs else ""
            for idx, serial in (after if not matching else before).items():
                # re-fetch full list now
                pass
            all_certs = {i: printer.view_cert_serial(i) for i in printer.list_cert_indexes()}
            for idx in sorted(all_certs):
                if idx == active_idx:
                    continue
                if idx == new_idx:
                    continue
                try:
                    printer.delete_certificate(idx)
                except Exception as e:
                    log("warn", f"Brother: could not delete idx={idx}: {e}")

        log("success", "Brother: certificate sync complete")
        fp = probe_tls_fingerprint(hostname)
        return DeviceResult(
            status=DeployStatus.DEPLOYED if deploy else DeployStatus.ALREADY_CURRENT,
            message="Deployed successfully" if deploy else "Already current",
            live_fingerprint=fp,
            local_fingerprint=local.fingerprint,
        )

    except Exception as exc:
        log("error", f"Brother: {exc}")
        return DeviceResult(status=DeployStatus.ERROR, message=str(exc), live_fingerprint=live_fp)
