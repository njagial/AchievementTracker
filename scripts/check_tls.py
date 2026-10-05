"""Probe outbound TLS so certificate problems stop being guesswork.

The Project Directive calls out "unknown certificate verification error" as a known
environment issue. This script turns that into a concrete diagnosis: it opens a TLS
session with the exact verification settings the application uses and prints what the peer
presented, which layer failed and what to do about it.

Usage
-----
    python scripts/check_tls.py                       # probe the usual suspects
    python scripts/check_tls.py https://your.api/x   # probe specific URLs
    python scripts/check_tls.py --show-cert          # print the peer certificate chain

Never "fixes" anything by disabling verification: if the peer's chain cannot be validated,
the script says so and points at ``TROPHYCASE_CA_BUNDLE``.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
from dataclasses import dataclass

import httpx

DEFAULT_TARGETS = (
    "https://pypi.org/simple/",
    "https://api.github.com/",
    "https://oauth2.googleapis.com/",
)


@dataclass
class ProbeResult:
    url: str
    ok: bool
    detail: str
    chain: list[str]


def _short(exc: BaseException) -> str:
    """One readable line for any exception the TLS layer can throw at us."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"{type(exc).__name__}: {exc.verify_message or exc}"
    if isinstance(exc, ssl.SSLError):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout: the peer accepted the connection but never answered"
    if isinstance(exc, ConnectionError):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def probe(url: str, *, verify: bool | str, timeout: float, show_cert: bool) -> ProbeResult:
    """Connect to ``url`` with ``verify`` and describe the outcome."""
    try:
        with httpx.Client(verify=verify, timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
    except Exception as exc:  # every failure mode is a diagnosis, not a crash
        return ProbeResult(url=url, ok=False, detail=_short(exc), chain=[])

    chain: list[str] = []
    if show_cert:
        chain = _peer_chain(url, verify=verify, timeout=timeout)
    return ProbeResult(
        url=url,
        ok=response.status_code < 500,
        detail=f"HTTP {response.status_code}",
        chain=chain,
    )


def _peer_chain(url: str, *, verify: bool | str, timeout: float) -> list[str]:
    """Fetch the peer's certificate chain directly from the socket."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    context = ssl.create_default_context()
    if isinstance(verify, str):
        context.load_verify_locations(cafile=verify)
    if not verify:  # pragma: no cover - only reachable if verification is deliberately off
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
    except Exception as exc:
        return [f"(chain unavailable: {_short(exc)})"]

    if cert is None:
        return ["(peer presented no certificate)"]
    names: list[str] = []
    for entry in cert.get("subject", ()):
        for key, value in entry:
            if key == "commonName":
                names.append(f"CN={value}")
    issuer = dict(next(iter(cert.get("issuer", ())), ())) if cert.get("issuer") else {}
    issuer_cn = issuer.get("commonName", "unknown issuer")
    not_after = cert.get("notAfter", "unknown expiry")
    return [
        f"subject: {', '.join(names) or 'unknown'}",
        f"issuer: {issuer_cn}",
        f"expires: {not_after}",
        f" SANs: {', '.join(cert.get('subjectAltName', [])[:8]) or 'none'}",
    ]


def _explain(result: ProbeResult) -> str:
    if result.ok:
        return ""
    lowered = result.detail.lower()
    if "certificate verify failed" in lowered or "self-signed" in lowered:
        return (
            "  -> the peer's chain is not in your trust store.\n"
            "     Set TROPHYCASE_CA_BUNDLE=/path/to/company-ca.pem (never disable verify)."
        )
    if "unable to get local issuer" in lowered:
        return (
            "  -> intermediate certificate missing: point TROPHYCASE_CA_BUNDLE at the full chain."
        )
    if "hostname" in lowered:
        return "  -> the certificate does not cover this host name (check the URL / SNI)."
    if "refused" in lowered:
        return (
            "  -> nothing is listening (or a firewall rejected it). If this is a local URL,\n"
            "     confirm the service is up and try 127.0.0.1 rather than localhost."
        )
    if "timeout" in lowered:
        from urllib.parse import urlparse

        host = urlparse(result.url).hostname or ""
        if host in {"localhost", "127.0.0.1", "::1", ""}:
            return (
                "  -> the TCP handshake completed but no TLS record arrived. On Windows,\n"
                "     'localhost' can resolve to ::1 first and a black-holed IPv6 loopback\n"
                "     socket looks exactly like this. Use 127.0.0.1 instead of localhost."
            )
        return (
            f"  -> {host} accepted the connection but did not answer in time: a proxy,\n"
            "     firewall or TLS-inspection middlebox is the usual culprit."
        )
    return "  -> see README.md, section 'TLS and certificate verification'."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("urls", nargs="*", help="URLs to probe (default: common dependencies)")
    parser.add_argument("--show-cert", action="store_true", help="print the peer certificate chain")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    from trophycase.config import get_settings

    verify = get_settings().tls_verify
    targets = args.urls or list(DEFAULT_TARGETS)

    print(
        f"verification: {'ON (custom CA bundle)' if isinstance(verify, str) else 'ON (system CAs)'}"
    )
    if isinstance(verify, str):
        print(f"CA bundle:    {verify}")

    failures = 0
    for target in targets:
        result = probe(target, verify=verify, timeout=args.timeout, show_cert=args.show_cert)
        status = "OK  " if result.ok else "FAIL"
        print(f"[{status}] {result.url} -> {result.detail}")
        for line in result.chain:
            print(f"         {line.strip()}")
        explanation = _explain(result)
        if explanation:
            failures += 1
            print(explanation)

    if failures:
        print(f"\n{failures} of {len(targets)} probes failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
