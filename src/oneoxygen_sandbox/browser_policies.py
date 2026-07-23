"""Deterministic managed-browser policy bundles derived from browser source profiles."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from oneoxygen_sandbox.browser import allowed_browser_hosts, browser_policy_sha256
from oneoxygen_sandbox.models import BrowserConfig


class ManagedBrowserFamily(StrEnum):
    CHROME = "chrome"
    CHROMIUM = "chromium"
    EDGE = "edge"
    BRAVE = "brave"
    FIREFOX = "firefox"
    SAFARI = "safari"
    OPERA = "opera"
    VIVALDI = "vivaldi"


_CHROMIUM_FAMILIES = frozenset(
    {
        ManagedBrowserFamily.CHROME,
        ManagedBrowserFamily.CHROMIUM,
        ManagedBrowserFamily.EDGE,
        ManagedBrowserFamily.BRAVE,
        ManagedBrowserFamily.OPERA,
        ManagedBrowserFamily.VIVALDI,
    }
)


def compile_managed_browser_policy(
    family: ManagedBrowserFamily | str,
    config: BrowserConfig,
    *,
    proxy_server: str,
) -> dict[str, Any]:
    """Build a deterministic policy bundle; deployment must still verify effective policy."""
    normalized_family = ManagedBrowserFamily(family)
    proxy_host, proxy_port = _loopback_proxy(proxy_server)
    hosts = allowed_browser_hosts(config)
    if normalized_family in _CHROMIUM_FAMILIES:
        policy = _chromium_policy(
            normalized_family,
            hosts,
            proxy_server=f"http://{proxy_host}:{proxy_port}",
        )
        policy_format = "chromium_enterprise_policy"
    elif normalized_family is ManagedBrowserFamily.FIREFOX:
        policy = _firefox_policy(hosts, proxy_host=proxy_host, proxy_port=proxy_port)
        policy_format = "firefox_policies_json"
    else:
        policy = _safari_manifest(
            hosts,
            proxy_server=f"http://{proxy_host}:{proxy_port}",
        )
        policy_format = "oneoxygen_safari_mdm_manifest_v1"
    payload = {
        "family": normalized_family.value,
        "format": policy_format,
        "source_policy_sha256": browser_policy_sha256(config),
        "allowed_hosts": list(hosts),
        "policy": policy,
    }
    payload["bundle_sha256"] = _sha256_json(payload)
    return payload


def _chromium_policy(
    family: ManagedBrowserFamily,
    hosts: tuple[str, ...],
    *,
    proxy_server: str,
) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "URLBlocklist": ["*"],
        "URLAllowlist": [f"https://.{host}" for host in hosts],
        "ProxyMode": "fixed_servers",
        "ProxyServer": proxy_server,
        "ProxyBypassList": "",
        "QuicAllowed": False,
        "DnsOverHttpsMode": "off",
        "IncognitoModeAvailability": 1,
        "BrowserGuestModeEnabled": False,
        "BrowserSignin": 0,
        "SyncDisabled": True,
        "PasswordManagerEnabled": False,
        "AutofillAddressEnabled": False,
        "AutofillCreditCardEnabled": False,
        "DefaultSearchProviderEnabled": False,
        "DeveloperToolsAvailability": 2,
        "ExtensionInstallBlocklist": ["*"],
    }
    if family is ManagedBrowserFamily.BRAVE:
        policy.update(
            {
                "TorDisabled": True,
                "BraveRewardsDisabled": True,
                "BraveWalletDisabled": True,
                "BraveVPNDisabled": True,
                "BraveAIChatEnabled": False,
            }
        )
    return policy


def _firefox_policy(
    hosts: tuple[str, ...],
    *,
    proxy_host: str,
    proxy_port: int,
) -> dict[str, Any]:
    proxy = f"{proxy_host}:{proxy_port}"
    return {
        "policies": {
            "WebsiteFilter": {
                "Block": ["<all_urls>"],
                "Exceptions": [f"https://{host}/*" for host in hosts],
            },
            "Proxy": {
                "Mode": "manual",
                "Locked": True,
                "HTTPProxy": proxy,
                "SSLProxy": proxy,
                "UseHTTPProxyForAllProtocols": True,
                "UseProxyForDNS": True,
                "Passthrough": "",
            },
            "DNSOverHTTPS": {"Enabled": False, "Locked": True},
            "DisableFirefoxAccounts": True,
            "DisablePrivateBrowsing": True,
            "OfferToSaveLogins": False,
            "AutofillAddressEnabled": False,
            "BlockAboutProfiles": True,
        }
    }


def _safari_manifest(hosts: tuple[str, ...], *, proxy_server: str) -> dict[str, Any]:
    return {
        "deny_by_default": True,
        "allowed_origins": [f"https://{host}" for host in hosts],
        "proxy_server": proxy_server,
        "requires_mdm": True,
        "requires_safari_web_extension_dnr": True,
        "requires_device_network_filter": True,
        "note": (
            "This is an input manifest for a signed MDM/WebExtension deployment, "
            "not a directly installable Apple configuration profile."
        ),
    }


def _loopback_proxy(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy_server must be a valid loopback HTTP URL") from exc
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or hostname is None
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("proxy_server must be a loopback HTTP URL with an explicit port")
    normalized_host = hostname.lower()
    if normalized_host != "localhost":
        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError as exc:
            raise ValueError("proxy_server must use a loopback host") from exc
        if not address.is_loopback:
            raise ValueError("proxy_server must use a loopback host")
    display_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return display_host, port


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
