"""Destination census, config-level approval, enforcement rows and the
sections they fill. Table-driven over hand-built effective configurations."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import (
    compiler, effective_record, errors, inventory, isolation, launch_codes, merge, netpolicy, profile_policy,
)

PROFILE = ("profile",)
MANAGED = ("managed",)


def _view(pid="gw", host="gw.example.com", *, proxy=False, ca=None):
    return merge.ProviderView(
        id=pid, kind="openai-compatible", endpoint_family="chat", host_key=host, credential_env="K",
        ca_file=ca, use_env_proxy=proxy, base_url="https://%s/v1" % host,
    )


def _compiled(*hosts, scheme="https"):
    from quoin.opencode_adapter import names

    doc = {"provider": {}, "permission": {"webfetch": "deny"}, "agent": {
        names.role_agent_name(role): {"permission": {"webfetch": "deny", "websearch": "deny"}}
        for role in compiler.ROLES}}
    doc["agent"]["title"] = {}  # auxiliary agents carry no web denial
    for pid, host in hosts:
        view = _view(pid, host)
        doc["provider"][compiler.native_provider_id(view)] = {
            "options": {"baseURL": "%s://%s/v1" % (scheme, host)}, "whitelist": ["m"]}
    return doc


def _effective(classification="work", *, allowed=("gw.example.com",), origin=PROFILE, providers=None,
               excluded=None, findings=(), values=None):
    providers = providers if providers is not None else {"gw": _view()}
    vals = {"policy.allowed_hosts": merge.Value(tuple(allowed), origin)}
    vals.update(values or {})
    return SimpleNamespace(
        classification=classification, providers=providers, excluded_providers=excluded or {},
        findings=tuple(findings), values=vals,
    )


def _policy(effective):
    return profile_policy.view(effective)


def _inv(*entries):
    return SimpleNamespace(entries=list(entries))


def _entry(kind, **facts):
    return inventory.Entry(kind=kind, layer="global", source_index=0, key_path="k", name="n", value_sha256=None,
                           owned=False, facts=facts)


def _census(effective=None, doc=None, ambient=None, inv=None, npm=None, fetch=()):
    effective = effective or _effective()
    return netpolicy.census(
        effective=effective, compiled_doc=doc if doc is not None else _compiled(("gw", "gw.example.com")), inventory=inv or _inv(),
        ambient=ambient or {}, policy=_policy(effective), npm_userconfig=npm, fetch_dirs=fetch,
    )


def _approve(effective=None, **kw):
    effective = effective or _effective()
    return netpolicy.approve(_census(effective, **kw), _policy(effective), effective.classification)


# ------------------------------------------------------------ evaluation


def test_label_approved_provider_with_an_excluded_host_refuses():
    for reason in ("host-not-allowed", "host-denied"):
        refusal = netpolicy.evaluation_refusal(_effective(excluded={"gw": reason}))
        assert refusal == netpolicy.NetRefusal("destination-not-approved", reason)


def test_work_without_explicit_allowed_hosts_refuses():
    finding = errors.make_finding("allowed-hosts-unset", False)
    assert netpolicy.evaluation_refusal(_effective(findings=[finding])).code == "allowed-hosts-unset"


def test_a_personal_evaluation_never_runs_its_logic():
    effective = _effective("personal", excluded={"gw": "host-denied"})
    assert netpolicy.evaluation_refusal(effective) is None


def test_other_exclusion_reasons_are_not_destination_refusals():
    assert netpolicy.evaluation_refusal(_effective(excluded={"gw": "denied"})) is None


def test_provider_ids_never_approve_a_host():
    effective = _effective(
        allowed=("approved.example.com",), providers={"approved.example.com": _view("approved.example.com", "elsewhere.example.com")},
    )
    refusal = _approve(effective, doc=_compiled(("approved.example.com", "elsewhere.example.com")))
    assert refusal == netpolicy.NetRefusal("destination-not-approved", "host-not-allowed")


def test_project_only_allowed_hosts_cannot_approve():
    effective = _effective(origin=("project",))
    assert _approve(effective).code == "destination-not-approved"


def test_a_profile_or_trusted_managed_origin_approves():
    assert _approve(_effective(origin=PROFILE)) is None
    assert _approve(_effective(origin=MANAGED)) is None


# ----------------------------------------------------------------- proxies


def test_proxy_route_approved_and_not_approved():
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "proxy.test"))
    ok = _approve(effective, ambient={"HTTPS_PROXY": "http://proxy.test:8080"})
    assert ok is None
    bad = _approve(_effective(providers={"gw": _view(proxy=True)}), ambient={"HTTPS_PROXY": "http://proxy.test:8080"})
    assert bad.code == "proxy-route-not-approved"


def test_a_proxy_with_credentials_keeps_them_out_of_text():
    effective = _effective(providers={"gw": _view(proxy=True)})
    census = _census(effective, ambient={"HTTPS_PROXY": "http://user:SEEDSECRET@proxy.test:8080"})
    refusal = netpolicy.approve(census, _policy(effective), "work")
    text = repr((refusal, census)) + repr(netpolicy.network_section(census, _policy(effective)))
    assert "SEEDSECRET" not in text and "user" not in text.replace("username", "")


def test_proxy_env_without_a_proxy_provider_is_no_route():
    census = _census(ambient={"HTTPS_PROXY": "http://proxy.test:8080"})
    assert census.proxies == ()
    assert _approve(ambient={"HTTPS_PROXY": "http://proxy.test:8080"}) is None


def test_an_unparseable_proxy_refuses_under_work():
    effective = _effective(providers={"gw": _view(proxy=True)})
    refusal = _approve(effective, ambient={"HTTPS_PROXY": "not a url"})
    assert refusal.code == "proxy-route-not-approved" and refusal.detail == "host-unparseable"


def test_https_provider_with_only_http_proxy_routes_direct():
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "proxy.test"))
    census = _census(effective, ambient={"HTTP_PROXY": "http://proxy.test:8080"})
    provider = next(r for r in census.destinations if r.role == "provider")
    assert provider.via_proxy is None


def test_https_proxy_wins_when_both_are_set():
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "a.test", "b.test"))
    census = _census(effective, ambient={"HTTPS_PROXY": "http://a.test:1", "HTTP_PROXY": "http://b.test:2"})
    provider = next(r for r in census.destinations if r.role == "provider")
    assert provider.via_proxy.variable == "HTTPS_PROXY" and provider.via_proxy.host_key == "a.test"


@pytest.mark.parametrize("no_proxy", ["gw.example.com", ".example.com", "*", "other.test, gw.example.com"])
def test_no_proxy_makes_the_route_direct(no_proxy):
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "proxy.test"))
    census = _census(effective, ambient={"HTTPS_PROXY": "http://proxy.test:1", "NO_PROXY": no_proxy})
    assert next(r for r in census.destinations if r.role == "provider").via_proxy is None


# ---------------------------------------------------------------- mcp, web


def test_remote_mcp_allowed_by_name_but_host_not_approved():
    inv = _inv(_entry("mcp-remote", url_host="mcp.example.org", url_scheme="https"))
    assert _approve(inv=inv).code == "mcp-remote-denied"
    approved = _effective(allowed=("gw.example.com", "mcp.example.org"))
    assert _approve(approved, inv=inv) is None


def test_remote_mcp_with_an_unparseable_url_refuses():
    inv = _inv(_entry("mcp-remote", url_host=None, url_scheme=None))
    refusal = _approve(inv=inv)
    assert refusal.code == "mcp-remote-denied" and refusal.detail == "host-unparseable"


def test_web_tools_posture_is_checked_against_the_compiled_document():
    bad = _compiled(("gw", "gw.example.com"))
    del bad["permission"]
    effective = _effective(values={"policy.web_tools": merge.Value("deny", PROFILE)})
    assert _approve(effective, doc=bad).code == "web-tools-denied"
    assert _approve(effective) is None
    role_missing = _compiled(("gw", "gw.example.com"))
    role_missing["agent"]["quoin-planner"]["permission"] = {"webfetch": "deny"}
    assert _approve(effective, doc=role_missing).code == "web-tools-denied"


def test_web_tools_allow_needs_a_profile_or_managed_approval():
    allow_default = _effective(values={"policy.web_tools": merge.Value("allow", ("default",))})
    assert _approve(allow_default).code == "web-tools-denied"
    allow_profile = _effective(values={"policy.web_tools": merge.Value("allow", PROFILE)})
    assert _approve(allow_profile) is None


# --------------------------------------------------------------------- npm


def _npm_world(tmp_path, text):
    rc = tmp_path / "npmrc"
    if text is not None:
        rc.write_text(text)
    return rc


def _install_allowed(**kw):
    values = {"policy.package_install": merge.Value("allow", PROFILE)}
    return _effective(values=values, **kw)


def test_npm_registry_from_the_profile_npmrc(tmp_path):
    effective = _install_allowed(allowed=("gw.example.com", "npm.example.com"))
    rc = _npm_world(tmp_path, "registry=https://npm.example.com/\n")
    assert _approve(effective, npm=rc) is None
    refusal = _approve(_install_allowed(), npm=rc)
    assert refusal == netpolicy.NetRefusal("destination-not-approved", "registry-not-approved")


def test_scoped_registry_lines_count(tmp_path):
    rc = _npm_world(tmp_path, "@corp:registry=https://scoped.example.net/\n")
    assert _approve(_install_allowed(), npm=rc).detail == "registry-not-approved"


def test_unknown_registry_is_refused(tmp_path):
    rc = _npm_world(tmp_path, "registry=not a url at all\n")
    assert _approve(_install_allowed(), npm=rc).detail == "registry-unknown"


def test_default_registry_when_none_is_set(tmp_path):
    census = _census(_install_allowed(), npm=_npm_world(tmp_path, None))
    assert [r.host_key for r in census.package_registry] == ["registry.npmjs.org"]


def test_unreadable_npmrc_is_unknown(tmp_path):
    rc = tmp_path / "npmrc"
    rc.write_bytes(b"\xff\xfe\x00bad")
    census = _census(_install_allowed(), npm=rc)
    assert census.package_registry[0].kind == "unparseable"


def test_no_registry_row_without_package_install_allow():
    assert _census().package_registry == ()


# ----------------------------------------------------------- personal / robust


def test_a_personal_profile_records_everything_and_never_refuses_here():
    effective = _effective("personal", allowed=(), origin=("default",))
    inv = _inv(_entry("mcp-remote", url_host="mcp.example.org", url_scheme="https"))
    census = _census(effective, inv=inv, ambient={"HTTPS_PROXY": "not a url"})
    assert netpolicy.approve(census, _policy(effective), "personal") is None
    assert all(r.approved_by in ("none", "loopback") for r in census.destinations if r.role in ("provider", "mcp-remote"))


def test_a_malformed_compiled_document_gives_unparseable_rows_not_an_exception():
    effective = _effective()
    for doc in ({"provider": {"x": {}}}, {"provider": {"x": {"options": {"baseURL": 5}}}}, {"provider": {"x": None}},
                {"provider": "no"}, {}):
        census = _census(effective, doc=doc)
        for row in census.destinations:
            assert row.role != "provider" or row.kind == "unparseable"


def test_a_bad_port_or_bracket_is_unparseable():
    effective = _effective()
    for url in ("https://gw.example.com:99999/v", "https://[::1/v", "https://gw.example.com:abc/v", "ftp://gw.example.com/"):
        doc = {"provider": {"x": {"options": {"baseURL": url}}}}
        census = _census(effective, doc=doc)
        assert [r.kind for r in census.destinations if r.role == "provider"] == ["unparseable"]
    doc = _compiled()
    doc["provider"] = {"x": {"options": {"baseURL": "https://[::1/v"}}}
    refusal = _approve(effective, doc=doc)
    assert refusal == netpolicy.NetRefusal("destination-not-approved", "host-unparseable")


def test_loopback_and_plaintext_kinds():
    effective = _effective(allowed=("127.0.0.1", "plain.example.com"))
    doc = {"provider": {"a": {"options": {"baseURL": "http://127.0.0.1:9/v"}},
                        "b": {"options": {"baseURL": "http://plain.example.com/v"}}}}
    kinds = {r.host_key: r.kind for r in _census(effective, doc=doc).destinations if r.role == "provider"}
    assert kinds == {"127.0.0.1": "loopback", "plain.example.com": "plaintext"}


def test_census_order_is_deterministic():
    effective = _effective(allowed=("a.example.com", "b.example.com"),
                           providers={"a": _view("a", "a.example.com"), "b": _view("b", "b.example.com")})
    doc = _compiled(("a", "a.example.com"), ("b", "b.example.com"))
    reversed_doc = dict(doc)
    reversed_doc["provider"] = dict(reversed(list(doc["provider"].items())))
    assert _census(effective, doc=doc) == _census(effective, doc=reversed_doc)


def test_url_instruction_and_remote_config_rows_are_host_less():
    inv = _inv(_entry("instruction", url=True, url_host="x.example", url_scheme="https"),
               _entry("remote-config", unknown=True), _entry("account-config", unknown=True))
    effective = _effective("personal", allowed=(), origin=("default",))
    census = _census(effective, inv=inv)
    assert {r.role for r in census.destinations} >= {"instruction", "remote-config", "account-config"}
    assert "x.example" not in repr(netpolicy.network_section(census, _policy(effective)))


def test_network_controls_required_rule():
    work = _policy(_effective("work"))
    personal = _policy(_effective("personal"))
    assert netpolicy.network_controls_required(work, "convenience")
    assert not netpolicy.network_controls_required(personal, "convenience")
    assert netpolicy.network_controls_required(personal, "managed")
    required = _policy(_effective("personal", values={"policy.network.identity_check": merge.Value("required", PROFILE)}))
    assert netpolicy.network_controls_required(required, "convenience")
    assert netpolicy.network_controls_required(None, "convenience")  # uncomputable fails closed


# ----------------------------------------------- enforcement and sections

VERIFIED = isolation.IsolationVerdict(status="verified", environment="container", evidence=("attestation-current",))
UNVERIFIED = isolation.unverified(("attestation-missing",))


def _table(effective, verdict, *, isolation_value="convenience", status="planned", census=None):
    census = census if census is not None else (netpolicy.EMPTY_CENSUS if status == "unavailable" else _census(effective))
    return {
        r["control"]: r["status"] for r in netpolicy.enforcement_table(
            _policy(effective), effective.classification, verdict, census, isolation=isolation_value,
            allowed_hosts_approved=netpolicy.hosts_approved(effective), network_status=status)
    }


def test_enforcement_rows_for_a_verified_work_profile():
    table = _table(_effective("work"), VERIFIED, isolation_value="managed")
    assert table["provider-destinations"] == "enforced" and table["destination-identity"] == "enforced"
    assert table["proxy-route"] == "enforced" and table["remote-config-and-instructions"] == "enforced"
    assert table["launcher-proxy"] == table["brokered-registry"] == "not-provided"
    for control in ("shell-egress", "redirects", "dns-after-launch", "opposite-profile-files", "keychain-access", "child-trust-store"):
        assert table[control] == "delegated-verified"
    assert "npm-global-config" not in table


def test_enforcement_rows_for_an_unverified_managed_work_profile():
    table = _table(_effective("work"), UNVERIFIED, isolation_value="managed")
    assert table["shell-egress"] == "delegated-unverified"


def test_enforcement_rows_for_a_convenience_personal_profile():
    table = _table(_effective("personal", allowed=(), origin=("default",)), UNVERIFIED)
    assert set(table.values()) == {"not-enforced", "not-provided"}
    assert list(table) == sorted(table)


def test_npm_global_config_row_only_under_work_with_install():
    assert "npm-global-config" in _table(_install_allowed(), VERIFIED)


def test_unavailable_path_has_no_enforced_network_step_row():
    values = {"policy.web_tools": merge.Value("deny", PROFILE), "policy.mcp.remote": merge.Value("allow-listed", PROFILE)}
    effective = _effective("personal", allowed=(), origin=("default",), values=values)
    table = _table(effective, UNVERIFIED, status="unavailable")
    for control in netpolicy.NETWORK_STEP_CONTROLS:
        assert table[control] == "not-enforced", control
    assert table["web-tools"] == "enforced"
    normal = _table(effective, UNVERIFIED, census=netpolicy.EMPTY_CENSUS)
    assert normal["remote-mcp"] == "enforced"
    section = netpolicy.network_section(netpolicy.EMPTY_CENSUS, _policy(effective), status="unavailable")
    assert section == {"status": "unavailable", "identity_check": "off", "destinations": [], "proxies": []}


def test_network_section_uses_digests_and_never_names():
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "proxy.test"))
    census = _census(effective, ambient={"HTTPS_PROXY": "http://proxy.test:8080"})
    section = netpolicy.network_section(census, _policy(effective), egress_hosts=frozenset({"gw.example.com"}))
    text = repr(section)
    assert "gw.example.com" not in text and "proxy.test" not in text
    provider = next(d for d in section["destinations"] if d["role"] == "provider")
    assert provider["host"].startswith("h-") and provider["identity"] == "via-proxy"
    assert provider["managed_egress"] == "listed" and provider["approved_by"] == "profile"
    assert section["proxies"][0]["host"].startswith("h-")


# ------------------------------------------------------ record and registry


def test_refusal_texts_are_closed_and_user_free():
    for code, text in netpolicy.REFUSAL_TEXT.items():
        assert code in launch_codes.REGISTRY and text and "%" not in text
    assert set(netpolicy.REFUSAL_TEXT) | set(isolation.REFUSAL_TEXT) == set(launch_codes.ISOLATION_NETWORK_CODES)


def test_readme_refusal_table_matches_the_registry():
    readme = (helpers.SOURCE_DIR / "adapters" / "opencode" / "README.md").read_text(encoding="utf-8")
    section = readme.split("**Launch policy refusals.**", 1)[1].split("**Enforcement table.**", 1)[0]
    rows = set(re.findall(r"^\| `([a-z-]+)` \|", section, flags=re.M))
    assert rows == set(launch_codes.ISOLATION_NETWORK_CODES)


# ----------------------------------------- parser-disagreement and bounded reads

BACKSLASH_URL = "https://evil.example\\@api.allowed.example/"


def test_a_backslash_before_at_is_never_read_as_the_allowed_host():
    assert netpolicy._parse_endpoint(BACKSLASH_URL) == (None, None, None)
    from quoin.opencode_adapter import config
    assert config.url_is_unambiguous(BACKSLASH_URL) is False
    assert inventory.url_facts(BACKSLASH_URL) == {"url_host": None, "url_scheme": None}


def test_a_backslash_remote_mcp_url_has_no_host():
    facts = inventory.url_facts("https://evil.example\\@mcp.example.org/")
    inv = _inv(_entry("mcp-remote", **facts))
    refusal = _approve(_effective(allowed=("gw.example.com", "mcp.example.org")), inv=inv)
    assert refusal.code == "mcp-remote-denied" and refusal.detail == "host-unparseable"


def test_a_backslash_npm_registry_is_unknown(tmp_path):
    rc = _npm_world(tmp_path, "registry=" + BACKSLASH_URL + "\n")
    effective = _install_allowed(allowed=("gw.example.com", "api.allowed.example"))
    assert _approve(effective, npm=rc).detail == "registry-unknown"


def test_a_backslash_proxy_is_unparseable():
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "api.allowed.example"))
    refusal = _approve(effective, ambient={"HTTPS_PROXY": BACKSLASH_URL})
    assert refusal.code == "proxy-route-not-approved" and refusal.detail == "host-unparseable"


def test_a_symlinked_npmrc_is_unknown(tmp_path):
    target = tmp_path / "real"
    target.write_text("registry=https://npm.example.com/\n")
    rc = tmp_path / "npmrc"
    rc.symlink_to(target)
    census = _census(_install_allowed(allowed=("gw.example.com", "npm.example.com")), npm=rc)
    assert census.package_registry[0].kind == "unparseable"


def test_an_npmrc_linked_to_a_device_is_unknown_without_reading(tmp_path):
    rc = tmp_path / "npmrc"
    rc.symlink_to("/dev/zero")
    census = _census(_install_allowed(), npm=rc)
    assert census.package_registry[0].kind == "unparseable"


def test_an_oversized_npmrc_is_unknown(tmp_path):
    rc = tmp_path / "npmrc"
    rc.write_text("registry=https://npm.example.com/\n" + "#" * 70000 + "\n")
    census = _census(_install_allowed(allowed=("gw.example.com", "npm.example.com")), npm=rc)
    assert census.package_registry[0].kind == "unparseable"


ENCODED_PASSWORD_PROXY = "http://user:p%40ss@proxy.corp:8080"


def test_an_approved_proxy_with_an_encoded_password_is_accepted():
    assert netpolicy._parse_endpoint(ENCODED_PASSWORD_PROXY)[0] == "proxy.corp"
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "proxy.corp"))
    census = _census(effective, ambient={"HTTPS_PROXY": ENCODED_PASSWORD_PROXY})
    assert netpolicy.approve(census, _policy(effective), "work") is None
    assert "p%40ss" not in repr(census) and "p@ss" not in repr(census)


def test_a_percent_escape_in_the_proxy_host_is_still_unparseable():
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "proxy.corp"))
    for url in ("http://proxy%2Ecorp:8080", "http://user:p%40ss@proxy%2Ecorp:8080"):
        refusal = _approve(effective, ambient={"HTTPS_PROXY": url})
        assert refusal.code == "proxy-route-not-approved" and refusal.detail == "host-unparseable", url


def test_a_backslash_before_at_with_an_encoded_password_is_still_refused():
    url = "http://user:p%40ss@evil.example\\@proxy.corp:8080"
    effective = _effective(providers={"gw": _view(proxy=True)}, allowed=("gw.example.com", "proxy.corp"))
    refusal = _approve(effective, ambient={"HTTPS_PROXY": url})
    assert refusal.code == "proxy-route-not-approved" and refusal.detail == "host-unparseable"


SCREEN_PARITY_URLS = (
    "https://api.example.com/v1",
    "http://user:p%40ss@proxy.corp:8080",
    "http://user:pass@proxy.corp:8080",
    "https://api%2Eexample.com/",
    "http://user:p%40ss@proxy%2Ecorp:8080",
    "https://[::1%25eth0]:443/",
    BACKSLASH_URL,
    "http://user:p%40ss@evil.example\\@proxy.corp:8080",
    "https://evil.example/\\@api.allowed.example",
    "https://a.example@b.example@c.example/",
    "https://api.example.com/?q=%40",
    "https://api.example.com/#%40",
    "https://api.example .com/",
    "https://api.example.com/\t",
    "https://аpi.example.com/",
    "https://[::1/",
    "",
    "not a url",
)


@pytest.mark.parametrize("url", SCREEN_PARITY_URLS)
def test_the_two_url_screens_agree(url):
    from quoin.opencode_adapter import config
    assert config.url_is_unambiguous(url) is inventory._url_is_unambiguous(url)


def test_a_fifo_named_npmrc_is_unknown_without_blocking(tmp_path):
    import os

    rc = tmp_path / "npmrc"
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFO support")
    os.mkfifo(rc)
    census = _census(_install_allowed(allowed=("gw.example.com", "npm.example.com")), npm=rc)
    assert census.package_registry[0].kind == "unparseable"


def test_a_directory_named_npmrc_is_unknown(tmp_path):
    rc = tmp_path / "npmrc"
    rc.mkdir()
    census = _census(_install_allowed(allowed=("gw.example.com", "npm.example.com")), npm=rc)
    assert census.package_registry[0].kind == "unparseable"
