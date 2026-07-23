from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from oneoxygen_sandbox import cli
from oneoxygen_sandbox.browser_policies import (
    ManagedBrowserFamily,
    compile_managed_browser_policy,
)
from oneoxygen_sandbox.models import BrowserConfig, BrowserSourceProfile

runner = CliRunner()


def config() -> BrowserConfig:
    return BrowserConfig(
        source_profiles=(BrowserSourceProfile.SEC_EDGAR,),
        user_agent="OneOxygen-Test/1.0 test@example.com",
    )


def test_chrome_policy_uses_exact_hosts_and_a_deny_by_default_proxy() -> None:
    first = compile_managed_browser_policy(
        ManagedBrowserFamily.CHROME,
        config(),
        proxy_server="http://127.0.0.1:8765",
    )
    second = compile_managed_browser_policy(
        ManagedBrowserFamily.CHROME,
        config(),
        proxy_server="http://127.0.0.1:8765",
    )

    policy = first["policy"]
    assert policy["URLBlocklist"] == ["*"]
    assert "https://.www.sec.gov" in policy["URLAllowlist"]
    assert "https://*.sec.gov" not in policy["URLAllowlist"]
    assert policy["ProxyBypassList"] == ""
    assert policy["QuicAllowed"] is False
    assert first["bundle_sha256"] == second["bundle_sha256"]


def test_brave_and_firefox_receive_family_specific_lockdown() -> None:
    brave = compile_managed_browser_policy(
        ManagedBrowserFamily.BRAVE,
        config(),
        proxy_server="http://localhost:8765",
    )
    firefox = compile_managed_browser_policy(
        ManagedBrowserFamily.FIREFOX,
        config(),
        proxy_server="http://127.0.0.1:8765",
    )

    assert brave["policy"]["TorDisabled"] is True
    assert brave["policy"]["BraveVPNDisabled"] is True
    assert brave["policy"]["BraveAIChatEnabled"] is False
    assert firefox["policy"]["policies"]["WebsiteFilter"]["Block"] == ["<all_urls>"]
    assert "https://www.sec.gov/*" in firefox["policy"]["policies"]["WebsiteFilter"]["Exceptions"]
    assert firefox["policy"]["policies"]["Proxy"]["Locked"] is True


def test_safari_bundle_is_explicitly_an_mdm_input_manifest() -> None:
    bundle = compile_managed_browser_policy(
        ManagedBrowserFamily.SAFARI,
        config(),
        proxy_server="http://[::1]:8765",
    )

    assert bundle["format"] == "oneoxygen_safari_mdm_manifest_v1"
    assert bundle["policy"]["requires_mdm"] is True
    assert bundle["policy"]["requires_device_network_filter"] is True


@pytest.mark.parametrize(
    "proxy",
    [
        "https://127.0.0.1:8765",
        "http://example.com:8765",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:8765",
        "http://127.0.0.1:8765/path",
    ],
)
def test_policy_compiler_rejects_a_non_loopback_or_ambiguous_proxy(proxy: str) -> None:
    with pytest.raises(ValueError, match="proxy_server"):
        compile_managed_browser_policy(
            ManagedBrowserFamily.CHROME,
            config(),
            proxy_server=proxy,
        )


def test_browser_cli_lists_sources_and_compiles_a_policy_without_network() -> None:
    sources = runner.invoke(cli.app, ["browser", "sources"])
    policy = runner.invoke(
        cli.app,
        [
            "browser",
            "policy",
            "--family",
            "brave",
            "--profiles",
            "sec_edgar,ofac_sanctions",
            "--proxy-server",
            "http://127.0.0.1:8765",
            "--user-agent",
            "OneOxygen-Test/1.0 test@example.com",
        ],
    )

    assert sources.exit_code == 0, sources.output
    assert '"id": "sec_edgar"' in sources.output
    assert policy.exit_code == 0, policy.output
    payload = json.loads(policy.output)
    assert payload["family"] == "brave"
    assert payload["policy"]["URLBlocklist"] == ["*"]
    assert "ofac.treasury.gov" in payload["allowed_hosts"]
