from __future__ import annotations

import pytest

from radar import lp_http


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/hosts",
        "http://localhost",
        "http://127.0.0.1",
        "http://2130706433",
        "http://[::1]",
        "https://user@example.com",
        "https://example.com:80",
        "https://a.internal",
        "http://example.com\\@localhost",
        "https://example.com/\nabc",
    ],
)
def test_unsafe_urls_are_rejected_before_dns(url):
    with pytest.raises(lp_http.LPFetchError, match="unsafe_url"):
        lp_http.public_url(url)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.3", "100.64.0.1", "169.254.169.254", "224.0.0.5", "::1", "::ffff:8.8.8.8"],
)
def test_non_public_dns_answers_are_rejected(address):
    assert not lp_http._public_ip(address)


def test_every_redirect_is_resolved_and_connection_is_pinned(monkeypatch):
    requests = []

    def request(url, host, port, addresses, timeout, byte_limit):
        requests.append((host, port, addresses, byte_limit))
        if host == "start.example.com":
            return lp_http.PageResponse(url, 302, {"location": "https://end.example.com/product"}, b"x")
        return lp_http.PageResponse(url, 200, {"content-type": "text/html"}, b"<html>")

    monkeypatch.setattr(lp_http, "_dns", lambda host, *_: ("8.8.8.8",) if host.startswith("start") else ("1.1.1.1",))
    monkeypatch.setattr(lp_http, "_request", request)
    response = lp_http.fetch_page("https://start.example.com/", byte_limit=30)
    assert response.url == "https://end.example.com/product"
    assert len(response.chain) == 2
    assert requests == [
        ("start.example.com", 443, ("8.8.8.8",), 30),
        ("end.example.com", 443, ("1.1.1.1",), 29),
    ]


def test_redirect_cannot_connect_when_dns_is_mixed(monkeypatch):
    requests = []

    def request(url, host, *_):
        requests.append(host)
        return lp_http.PageResponse(url, 302, {"location": "https://end.example.com/"}, b"")

    monkeypatch.setattr(
        lp_http,
        "_dns",
        lambda host, *_: ("8.8.8.8",) if host.startswith("start") else ("1.1.1.1", "192.168.0.1"),
    )
    monkeypatch.setattr(lp_http, "_request", request)
    with pytest.raises(lp_http.LPFetchError, match="dns_failed_or_unsafe"):
        lp_http.fetch_page("https://start.example.com/")
    assert requests == ["start.example.com"]


def test_body_limit_aborts_curl_and_disables_automatic_redirects(monkeypatch):
    opts = {}

    class MockCurl:
        def setopt(self, key, value):
            opts[key] = value

        def perform(self):
            assert opts[lp_http.CurlOpt.WRITEFUNCTION](b"1234") == 4
            result = opts[lp_http.CurlOpt.WRITEFUNCTION](b"56")
            if result == lp_http.CURL_WRITEFUNC_ERROR:
                raise lp_http.CurlError("write aborted")

        def close(self):
            pass

    monkeypatch.setattr(lp_http, "Curl", MockCurl)
    with pytest.raises(lp_http.LPFetchError, match="body_too_large"):
        lp_http._request("https://host.example.org/", "host.example.org", 443, ("8.8.8.8",), 2, 5)
    assert opts[lp_http.CurlOpt.FOLLOWLOCATION] == 0
    assert opts[lp_http.CurlOpt.RESOLVE] == [b"host.example.org:443:8.8.8.8"]
    assert opts[lp_http.CurlOpt.PROXY] == b""


@pytest.mark.parametrize("redirect_limit", [False, True])
def test_content_type_and_redirect_limit(monkeypatch, redirect_limit):
    monkeypatch.setattr(lp_http, "_dns", lambda *_: ("8.8.8.8",))

    def request(url, *_):
        if redirect_limit:
            return lp_http.PageResponse(url, 301, {"location": "/next"}, b"")
        return lp_http.PageResponse(url, 200, {"content-type": "application/json"}, b"{}")

    monkeypatch.setattr(lp_http, "_request", request)
    reason = "redirect_limit_or_missing_location" if redirect_limit else "non_html_or_empty"
    with pytest.raises(lp_http.LPFetchError, match=reason) as error:
        lp_http.fetch_page("https://start.example.com", max_redirects=0)
    assert error.value.status == (301 if redirect_limit else 200)
