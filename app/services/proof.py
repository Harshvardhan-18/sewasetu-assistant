# -*- coding: utf-8 -*-
"""
app/services/proof.py — Age proof document validation, secure storage, and SSRF guard.

Implements:
1. Magic-byte verification for JPG, PNG, PDF (<= 5 MB).
2. Server-side random filename generation (UUID4 + ext).
3. Client filename sanitization.
4. SSRF-safe URL fetcher with DNS verification and socket IP pinning.
"""

import os
import re
import ssl
import uuid
import socket
import http.client
import ipaddress
import urllib.parse
from typing import Tuple

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB cap
ACCEPTED_DOCUMENT_TYPES = {"birth_certificate", "school_leaving_certificate", "voter_id"}


def sanitize_filename(name: str, fallback_ext: str = ".pdf") -> str:
    """Sanitize client-supplied filename for display metadata only."""
    if not name:
        return f"document{fallback_ext}"
    base = os.path.basename(name).strip()
    # Replace unsafe characters with underscore
    clean = re.sub(r"[^\w.-]", "_", base)
    if not clean or clean.startswith("."):
        clean = f"document{fallback_ext}"
    return clean[:80]


def detect_and_validate_file(raw_bytes: bytes, client_filename: str) -> Tuple[bool, str, str, str]:
    """Validate file bytes against size and magic-byte signatures.

    Returns:
        (is_valid, error_message, detected_extension, sanitized_filename)
    """
    if not raw_bytes:
        return False, "File is empty", "", ""

    if len(raw_bytes) > MAX_FILE_BYTES:
        return False, "File exceeds 5 MB limit", "", ""

    # Check magic bytes
    if raw_bytes.startswith(b"%PDF-"):
        ext = ".pdf"
    elif raw_bytes.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    else:
        return (
            False,
            "Invalid file format. Allowed formats: PDF, JPEG, PNG (magic bytes mismatch).",
            "",
            "",
        )

    # Sanitize client filename
    display_name = sanitize_filename(client_filename, fallback_ext=ext)
    # Ensure display name has a compatible extension
    lower = display_name.lower()
    if not (lower.endswith(".pdf") or lower.endswith(".jpg") or lower.endswith(".jpeg") or lower.endswith(".png")):
        display_name += ext

    return True, "", ext, display_name


def save_proof_file(upload_dir: str, raw_bytes: bytes, client_filename: str) -> Tuple[str, str]:
    """Validate and write file to upload_dir using a random server-generated UUID filename.

    Returns:
        (server_file_path, sanitized_display_name)
    Raises:
        ValueError if validation fails.
    """
    valid, err, ext, display_name = detect_and_validate_file(raw_bytes, client_filename)
    if not valid:
        raise ValueError(err)

    os.makedirs(upload_dir, exist_ok=True)
    server_filename = f"{uuid.uuid4().hex}{ext}"
    full_path = os.path.join(upload_dir, server_filename)

    with open(full_path, "wb") as f:
        f.write(raw_bytes)

    return full_path, display_name


def validate_ip_for_ssrf(ip_str: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Check that an IP address is not private, loopback, link-local, or cloud metadata."""
    ip = ipaddress.ip_address(ip_str)

    if ip.is_loopback:
        raise ValueError(f"Access to loopback address ({ip_str}) is prohibited.")
    if ip.is_private:
        raise ValueError(f"Access to private network address ({ip_str}) is prohibited.")
    if ip.is_link_local:
        raise ValueError(f"Access to link-local address ({ip_str}) is prohibited.")
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        raise ValueError(f"Access to reserved address ({ip_str}) is prohibited.")
    if str(ip) == "169.254.169.254":
        raise ValueError("Access to cloud instance metadata service is prohibited.")

    # IPv4-mapped IPv6 check
    if hasattr(ip, "ipv4_mapped") and ip.ipv4_mapped:
        mapped = ip.ipv4_mapped
        if mapped.is_private or mapped.is_loopback or mapped.is_link_local or str(mapped) == "169.254.169.254":
            raise ValueError(f"Access to mapped restricted IP ({mapped}) is prohibited.")

    return ip


def fetch_file_url(url: str, timeout: float = 5.0) -> Tuple[bytes, str]:
    """Fetch a document via HTTPS with strict SSRF defense and IP pinning.

    Returns:
        (raw_bytes, inferred_filename)
    Raises:
        ValueError on SSRF violation, invalid scheme, redirect, timeout, or size excess.
    """
    if not url or not isinstance(url, str):
        raise ValueError("file_url must be a non-empty string.")

    if not url.startswith("https://"):
        raise ValueError("file_url must use HTTPS scheme.")

    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: missing hostname.")

    port = parsed.port or 443

    # DNS Resolution & SSRF address check
    try:
        addr_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError(f"Failed to resolve host '{hostname}': {exc}")

    if not addr_info:
        raise ValueError(f"No IP addresses resolved for '{hostname}'.")

    pinned_ip = None
    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        # Validate against loopback / private / link-local / metadata
        validate_ip_for_ssrf(ip_str)
        if pinned_ip is None:
            pinned_ip = ip_str

    if not pinned_ip:
        raise ValueError(f"No valid public IP found for host '{hostname}'.")

    # Connect socket directly to pinned IP
    try:
        sock = socket.create_connection((pinned_ip, port), timeout=timeout)
        ctx = ssl.create_default_context()
        # wrap_socket with server_hostname guarantees TLS SNI and certificate verification
        ssl_sock = ctx.wrap_socket(sock, server_hostname=hostname)

        conn = http.client.HTTPSConnection(hostname, port=port, timeout=timeout)
        conn.sock = ssl_sock

        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"

        conn.request(
            "GET",
            path,
            headers={
                "Host": hostname,
                "User-Agent": "SewaSetu-AgeProofFetcher/1.0",
                "Accept": "*/*",
            },
        )
        resp = conn.getresponse()

        # Reject redirects
        if resp.status in (301, 302, 303, 307, 308):
            conn.close()
            raise ValueError("Redirects are not permitted on file_url.")

        if resp.status != 200:
            conn.close()
            raise ValueError(f"HTTP error downloading age proof: status {resp.status}.")

        chunks = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                conn.close()
                raise ValueError("File downloaded from file_url exceeds 5 MB limit.")

        conn.close()
        raw_bytes = b"".join(chunks)

    except (ValueError, socket.timeout) as exc:
        raise exc
    except Exception as exc:
        raise ValueError(f"Failed to download file from file_url: {exc}")

    # Infer filename from path
    inferred_name = os.path.basename(parsed.path) or "downloaded_document.pdf"
    return raw_bytes, inferred_name
