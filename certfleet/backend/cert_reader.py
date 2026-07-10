"""Reads the local Let's Encrypt cert from /ssl/ and exposes structured info."""
from __future__ import annotations

import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa


DEFAULT_CERT_PATH = "/ssl/fullchain.pem"
DEFAULT_KEY_PATH  = "/ssl/privkey.pem"


@dataclass
class LocalCert:
    domain: str
    issuer: str
    not_before: str
    not_after: str
    days_remaining: int
    fingerprint: str          # SHA-256, colon-separated
    serial: str               # hex, normalised
    cert_path: str
    key_path: str
    last_checked: str
    # Extended fields
    key_info: str             # e.g. "RSA 2048-bit" or "ECDSA P-256"
    sig_algorithm: str        # e.g. "SHA-256 with RSA"
    sans: list[str]           # Subject Alternative Names
    root_ca: str              # CN of last cert in chain (trust anchor)
    key_usage: str            # e.g. "Digital Signature, Key Encipherment"
    is_staging: bool          # True if issued by Let's Encrypt's staging environment


def _load_chain(path: Path) -> list[x509.Certificate]:
    if not path.exists():
        raise FileNotFoundError(
            f"Certificate file not found: {path}\n"
            f"Make sure the HA SSL integration is configured and the file exists, "
            f"or update the cert path in Settings."
        )
    if not path.is_file():
        raise ValueError(f"{path} exists but is not a file")
    data = path.read_bytes()
    if not data.strip():
        raise ValueError(f"Certificate file is empty: {path}")
    pem_blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        data,
        flags=re.S,
    )
    if not pem_blocks:
        raise ValueError(f"No valid PEM certificate block found in {path}")
    return [x509.load_pem_x509_certificate(b) for b in pem_blocks]


def _load_leaf(path: Path) -> x509.Certificate:
    return _load_chain(path)[0]


def _fingerprint(cert: x509.Certificate) -> str:
    raw = cert.fingerprint(hashes.SHA256())
    return "SHA256:" + ":".join(f"{b:02X}" for b in raw)


def _serial_hex(cert: x509.Certificate) -> str:
    s = hex(cert.serial_number)[2:].lower()
    if len(s) % 2:
        s = "0" + s
    return s


def _key_info(cert: x509.Certificate) -> str:
    pub = cert.public_key()
    if isinstance(pub, rsa.RSAPublicKey):
        return f"RSA {pub.key_size}-bit"
    if isinstance(pub, ec.EllipticCurvePublicKey):
        return f"ECDSA {pub.curve.name}"
    return type(pub).__name__.replace("PublicKey", "")


def _sig_algorithm(cert: x509.Certificate) -> str:
    try:
        hash_name = cert.signature_hash_algorithm.name.upper()
    except Exception:
        hash_name = "Unknown"
    pub = cert.public_key()
    if isinstance(pub, rsa.RSAPublicKey):
        alg = "RSA"
    elif isinstance(pub, ec.EllipticCurvePublicKey):
        alg = "ECDSA"
    else:
        alg = type(pub).__name__.replace("PublicKey", "")
    return f"{hash_name} with {alg}"


def _sans(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        return ext.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return []


def _key_usage(cert: x509.Certificate) -> str:
    usages: list[str] = []
    try:
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        checks = [
            ("digital_signature",  "Digital Signature"),
            ("key_encipherment",   "Key Encipherment"),
            ("content_commitment", "Non-Repudiation"),
            ("data_encipherment",  "Data Encipherment"),
            ("key_agreement",      "Key Agreement"),
            ("key_cert_sign",      "Cert Sign"),
            ("crl_sign",           "CRL Sign"),
        ]
        for attr, label in checks:
            try:
                if getattr(ku, attr):
                    usages.append(label)
            except ValueError:
                pass
    except x509.ExtensionNotFound:
        pass
    # Also pull Extended Key Usage for TLS Server / Client Auth labels
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        oid_labels = {
            "1.3.6.1.5.5.7.3.1": "TLS Server",
            "1.3.6.1.5.5.7.3.2": "TLS Client",
        }
        for oid in eku:
            label = oid_labels.get(oid.dotted_string)
            if label:
                usages.append(label)
    except x509.ExtensionNotFound:
        pass
    return ", ".join(usages) if usages else "Not specified"


def _root_ca(chain: list[x509.Certificate]) -> str:
    # Walk back to find a self-signed cert (root); fall back to last in chain
    for cert in reversed(chain):
        try:
            if cert.issuer == cert.subject:
                attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
                return attrs[0].value if attrs else "Unknown"
        except Exception:
            pass
    # No self-signed cert found (chain may be incomplete) — use last issuer
    last = chain[-1]
    attrs = last.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return attrs[0].value if attrs else "Unknown"


def read_local_cert(
    cert_path: str = DEFAULT_CERT_PATH,
    key_path: str = DEFAULT_KEY_PATH,
) -> LocalCert:
    cp = Path(cert_path)
    kp = Path(key_path)

    chain = _load_chain(cp)
    cert  = chain[0]

    if not kp.exists():
        raise FileNotFoundError(
            f"Private key file not found: {kp}\n"
            f"Update the key path in Settings."
        )

    now = datetime.now(timezone.utc)
    not_after = cert.not_valid_after_utc
    days = (not_after - now).days

    cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    domain = cn_attrs[0].value if cn_attrs else "unknown"

    issuer_attrs = cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    issuer = issuer_attrs[0].value if issuer_attrs else "unknown"

    root_ca = _root_ca(chain)
    # Let's Encrypt deliberately brands every staging-environment CA name
    # with "(STAGING)" (e.g. "(STAGING) Pretend Pear X1") specifically so
    # this is detectable — confirmed against real staging-issued certs.
    is_staging = "staging" in issuer.lower() or "staging" in root_ca.lower()

    return LocalCert(
        domain=domain,
        issuer=issuer,
        not_before=cert.not_valid_before_utc.strftime("%Y-%m-%d %H:%M:%S"),
        not_after=not_after.strftime("%Y-%m-%d %H:%M:%S"),
        days_remaining=max(days, 0),
        fingerprint=_fingerprint(cert),
        serial=_serial_hex(cert),
        cert_path=str(cp),
        key_path=str(kp),
        last_checked=now.strftime("%Y-%m-%d %H:%M:%S"),
        key_info=_key_info(cert),
        sig_algorithm=_sig_algorithm(cert),
        sans=_sans(cert),
        root_ca=root_ca,
        key_usage=_key_usage(cert),
        is_staging=is_staging,
    )


def _tls_ctx(legacy: bool = False) -> ssl.SSLContext:
    """Return an SSL context suitable for fingerprint probing.

    legacy=True drops to SECLEVEL 0 for devices (e.g. HP Comware switches)
    that only support older cipher suites rejected by the default context.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if legacy:
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    return ctx


def probe_tls_fingerprint(host: str, port: int = 443, legacy: bool = False) -> str:
    """Connect to host:port and return the SHA-256 fingerprint of the served cert."""
    ctx = _tls_ctx(legacy)
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    return _fingerprint(cert)


def probe_tls_serial(host: str, port: int = 443, legacy: bool = False) -> str:
    """Connect to host:port and return the serial hex of the served cert."""
    ctx = _tls_ctx(legacy)
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    return _serial_hex(cert)


def probe_tls_names(host: str, port: int = 443, legacy: bool = False) -> list[str]:
    """Connect to host:port and return the CN + SAN DNS names + SAN IP
    addresses of the currently-served certificate, all as strings.

    IP SANs matter here specifically — self-signed certs on appliances
    like Proxmox routinely include the box's own management IP as a SAN
    (confirmed against a real device: 10.10.101.92 was in the IP SAN list
    even though it's also literally how that device is configured as a
    CertFleet target), and plenty of devices in this app are configured
    by IP rather than hostname. Missing that would produce a false
    "not covered" warning on exactly the devices most likely to be
    configured that way.

    Used for a coverage comparison (does the new cert cover everything the
    device's *current* cert covers), not for hostname verification — this
    app disables TLS hostname/cert-chain checking everywhere on purpose,
    since most of these devices present internally-issued or self-signed
    certs with names that were never going to validate against any public
    CA trust store to begin with.
    """
    ctx = _tls_ctx(legacy)
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    names = set(_sans(cert))
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names.update(str(ip) for ip in ext.value.get_values_for_type(x509.IPAddress))
    except x509.ExtensionNotFound:
        pass
    cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    if cn_attrs:
        names.add(cn_attrs[0].value)
    return sorted(names)


def hostname_matches(hostname: str, pattern: str) -> bool:
    """RFC 6125-style single-label wildcard match.

    "*.example.com" covers "foo.example.com" but deliberately NOT
    "example.com" itself or "a.b.example.com" — a wildcard covers exactly
    one label, same rule real browsers use. This is a coverage check, not
    proof the hostname is "correct" in any absolute sense (see
    hostname_covered_by_cert's docstring in main.py for why that's not a
    thing this app can actually determine).
    """
    hostname = hostname.lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")
    if pattern == hostname:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        if not hostname.endswith(suffix):
            return False
        prefix = hostname[: -len(suffix)]
        return len(prefix) > 0 and "." not in prefix
    return False


def hostname_covered(hostname: str, names: list[str]) -> bool:
    """True if any name in `names` (CN/SAN list) covers `hostname`."""
    return any(hostname_matches(hostname, n) for n in names)
