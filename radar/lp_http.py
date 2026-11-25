"""Bounded, direct HTTP for inspecting an explicitly selected landing page."""

from __future__ import annotations

import ipaddress
import queue
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from curl_cffi import Curl, CurlError, CurlInfo, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

REDIRECTS = {301, 302, 303, 307, 308}
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class LPFetchError(ValueError):
    """An inspection that must not provide evidence."""

    def __init__(
        self,
        reason: str,
        *,
        chain: tuple[str, ...] = (),
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(reason)
        self.chain = chain
        self.status = status
        self.headers = headers or {}


@dataclass(frozen=True)
class PageResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    chain: tuple[str, ...] = ()


def public_url(value: str) -> tuple[str, str, int]:
    """Restrict URLs before DNS and use the same normalized host for TLS and pinning."""
    if len(value) > 4096 or any(ord(ch) < 33 or ord(ch) == 127 or ch == "\\" for ch in value):
        raise LPFetchError("unsafe_url")
    try:
        parts = urlsplit(value)
        scheme = parts.scheme
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        expected_port = {"http": 80, "https": 443}[scheme]
        port = parts.port if parts.port is not None else expected_port
    except (ValueError, UnicodeError, KeyError) as exc:
        raise LPFetchError("unsafe_url") from exc
    labels = host.split(".")
    if (
        parts.username is not None
        or port != expected_port
        or len(host) > 253
        or len(labels) < 2
        or not labels[-1].isalpha()
        or not all(HOST_LABEL.fullmatch(label) for label in labels)
        or labels[-1] in {"localhost", "local", "internal", "test", "invalid", "example", "onion"}
    ):
        raise LPFetchError("unsafe_url")
    # A DNS name with a punycode final label is conservatively skipped.
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    return urlunsplit((scheme, host, path, query, "")), host, port


def _public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.scope_id is not None or ip.ipv4_mapped is not None or ip.sixtofour or ip.teredo
    ):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def _dns(host: str, port: int, timeout: float) -> tuple[str, ...]:
    # getaddrinfo has no deadline. A daemon keeps late system DNS from extending the request.
    result: queue.Queue[tuple[str, ...]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            answers = socket.getaddrinfo(host + ".", port, type=socket.SOCK_STREAM)
            result.put(tuple(dict.fromkeys(address[4][0] for address in answers)))
        except OSError:
            result.put(())

    threading.Thread(target=resolve, daemon=True).start()
    try:
        return result.get(timeout=timeout)
    except queue.Empty as exc:
        raise LPFetchError("dns_timeout") from exc


def _request(
    url: str, host: str, port: int, addresses: tuple[str, ...], timeout: float, byte_limit: int
) -> PageResponse:
    body = bytearray()
    headers: dict[str, str] = {}
    wanted = {"content-type", "content-encoding", "location"}
    header_bytes = 0
    failure = None

    def receive_header(data: bytes) -> int:
        nonlocal header_bytes, failure
        header_bytes += len(data)
        if header_bytes > 32768:
            failure = "headers_too_large"
            return CURL_WRITEFUNC_ERROR
        if data.startswith(b"HTTP/"):
            headers.clear()
        name, separator, value = data.decode("latin-1").partition(":")
        if separator and (name := name.lower()) in wanted:
            if name in headers:
                failure = "ambiguous_headers"
                return CURL_WRITEFUNC_ERROR
            headers[name] = value.strip()
        return len(data)

    def receive_body(data: bytes) -> int:
        nonlocal failure
        if len(body) + len(data) > byte_limit:
            failure = "body_too_large"
            return CURL_WRITEFUNC_ERROR
        body.extend(data)
        return len(data)

    pins = ",".join(f"[{address}]" if ":" in address else address for address in addresses)
    options = {
        CurlOpt.URL: url.encode("ascii"),
        CurlOpt.RESOLVE: [f"{host}:{port}:{pins}".encode("ascii")],
        CurlOpt.PROXY: b"",
        CurlOpt.FOLLOWLOCATION: 0,
        CurlOpt.NOSIGNAL: 1,
        CurlOpt.TIMEOUT_MS: max(1, int(timeout * 1000)),
        CurlOpt.CONNECTTIMEOUT_MS: max(1, int(min(timeout, 4) * 1000)),
        CurlOpt.SSL_VERIFYPEER: 1,
        CurlOpt.SSL_VERIFYHOST: 2,
        CurlOpt.HTTP_CONTENT_DECODING: 0,
        CurlOpt.HTTPHEADER: [b"Accept: text/html,application/xhtml+xml", b"Accept-Encoding: identity"],
        CurlOpt.USERAGENT: b"RadarLowTicket-LP/1.0",
        CurlOpt.HEADERFUNCTION: receive_header,
        CurlOpt.WRITEFUNCTION: receive_body,
    }
    curl = Curl()
    try:
        for key, value in options.items():
            curl.setopt(key, value)
        curl.perform()
        status = int(curl.getinfo(CurlInfo.RESPONSE_CODE))
    except CurlError as exc:
        code = getattr(exc, "code", None)
        reason = "timeout" if code == 28 else "transport_error"
        raise LPFetchError(failure or reason) from exc
    finally:
        curl.close()
    return PageResponse(url, status, headers, bytes(body))


def fetch_page(
    url: str,
    *,
    timeout: float = 8,
    byte_limit: int = 384 * 1024,
    max_redirects: int = 3,
    before_request: Callable[[str], None] | None = None,
) -> PageResponse:
    if not 0 < timeout <= 30 or not 0 < byte_limit <= 1024 * 1024 or not 0 <= max_redirects <= 5:
        raise ValueError("invalid LP HTTP limits")
    deadline = time.monotonic() + timeout
    chain: list[str] = []
    remaining_bytes = byte_limit
    for redirects in range(max_redirects + 1):
        try:
            url, host, port = public_url(url)
        except LPFetchError as exc:
            exc.chain = tuple(chain)
            raise
        if url in chain:
            raise LPFetchError("redirect_loop")
        chain.append(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LPFetchError("timeout")
        try:
            addresses = _dns(host, port, remaining)
            if not addresses or not all(_public_ip(address) for address in addresses):
                raise LPFetchError("dns_failed_or_unsafe")
            if before_request is not None:
                before_request(host)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LPFetchError("timeout")
            response = _request(url, host, port, addresses, remaining, remaining_bytes)
        except LPFetchError as exc:
            exc.chain = tuple(chain)
            raise
        remaining_bytes -= len(response.body)
        if response.status in REDIRECTS:
            location = response.headers.get("location")
            if not location or redirects == max_redirects:
                raise LPFetchError(
                    "redirect_limit_or_missing_location",
                    chain=tuple(chain),
                    status=response.status,
                    headers=response.headers,
                )
            next_url = urljoin(url, location)
            if url.startswith("https:") and not next_url.startswith("https:"):
                raise LPFetchError(
                    "https_downgrade", chain=tuple(chain), status=response.status, headers=response.headers
                )
            url = next_url
            continue
        if response.status != 200:
            raise LPFetchError(
                f"http_{response.status}", chain=tuple(chain), status=response.status, headers=response.headers
            )
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise LPFetchError(
                "unsupported_encoding", chain=tuple(chain), status=response.status, headers=response.headers
            )
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"} or not response.body:
            raise LPFetchError(
                "non_html_or_empty", chain=tuple(chain), status=response.status, headers=response.headers
            )
        return PageResponse(url, response.status, response.headers, response.body, tuple(chain))
    raise LPFetchError("redirect_limit")
