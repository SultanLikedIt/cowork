"""Tests for web_security.validate_public_url -- the live SSRF guard.

Crawl4AI is the only network-talking engine (see web_fetch.py's module
docstring); crawl4ai_engine.py validates every URL and every redirect hop
through this function. This file replaces the SSRF/scheme-validation
coverage that used to live in test_web_fetch.py against the pre-Crawl4AI
static-HTTP fetch tier (web_fetch._assert_fetchable), which was removed
once it stopped being reachable from any live code path.
"""

import web_security
from web_security import UnsafeTarget, validate_public_url

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


def _resolver(address: str):
    def resolve(host, port, *_a, **_k):
        return [(2, 1, 6, "", (address, port))]

    return resolve


def test_public_address_is_accepted() -> None:
    target = validate_public_url("https://example.com/page", resolver=_resolver("93.184.216.34"))
    check("public IPv4 address is accepted", target.hostname == "example.com", target)
    check("default https port is 443", target.port == 443, target.port)


def test_explicit_port_is_honoured() -> None:
    target = validate_public_url("http://example.com:8080/", resolver=_resolver("93.184.216.34"))
    check("explicit port is kept", target.port == 8080, target.port)


def test_pseudo_and_dangerous_schemes_are_refused() -> None:
    for url in (
        "view-source:https://example.com/page",
        "data:text/html,<p>x</p>",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/file",
    ):
        raised = None
        try:
            validate_public_url(url, resolver=_resolver("93.184.216.34"))
        except UnsafeTarget as exc:
            raised = exc
        check(f"scheme refused: {url}", raised is not None, url)


def test_credentials_in_url_are_refused() -> None:
    raised = None
    try:
        validate_public_url("https://user:pass@example.com/", resolver=_resolver("93.184.216.34"))
    except UnsafeTarget as exc:
        raised = exc
    check("embedded credentials are refused", raised is not None)


def test_private_and_special_addresses_are_refused() -> None:
    cases = {
        "loopback": "127.0.0.1",
        "link-local (cloud metadata)": "169.254.169.254",
        "private RFC1918": "10.0.0.5",
        "private RFC1918 (192.168)": "192.168.1.1",
        "reserved": "240.0.0.1",
        "multicast": "224.0.0.1",
        "unspecified": "0.0.0.0",
    }
    for label, address in cases.items():
        raised = None
        try:
            validate_public_url("https://internal.example/", resolver=_resolver(address))
        except UnsafeTarget as exc:
            raised = exc
        check(f"non-public address refused: {label} ({address})", raised is not None, address)


def test_unresolvable_host_is_refused() -> None:
    def resolve(host, port, *_a, **_k):
        raise OSError("name resolution failed")

    raised = None
    try:
        validate_public_url("https://does-not-resolve.invalid/", resolver=resolve)
    except UnsafeTarget as exc:
        raised = exc
    check("a host that cannot resolve is refused, not left to error later", raised is not None)


def test_oversized_url_is_refused() -> None:
    huge = "https://example.com/" + ("x" * web_security.MAX_URL_CHARS)
    raised = None
    try:
        validate_public_url(huge, resolver=_resolver("93.184.216.34"))
    except UnsafeTarget as exc:
        raised = exc
    check("a URL past MAX_URL_CHARS is refused", raised is not None)


def _main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all web_security checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
