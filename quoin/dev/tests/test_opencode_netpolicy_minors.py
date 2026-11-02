"""Remote MCP transport labels and the npm registry refusal causes."""
from __future__ import annotations

import os

import pytest

from quoin.opencode_adapter import isolation, netpolicy
from test_opencode_netpolicy import (
    _approve, _census, _effective, _entry, _inv, _install_allowed, _npm_world,
)


def _mcp_row(scheme):
    inv = _inv(_entry("mcp-remote", url_host="mcp.example.org", url_scheme=scheme))
    census = _census(_effective(allowed=("gw.example.com", "mcp.example.org")), inv=inv)
    (row,) = [r for r in census.destinations if r.role == "mcp-remote"]
    return row


@pytest.mark.parametrize("scheme,kind", [
    ("http", "plaintext"), ("ws", "plaintext"), ("https", "tls"), ("wss", "tls"),
    ("ftp", "unparseable"), ("gopher", "unparseable"), (None, "unparseable"),
])
def test_remote_mcp_transport_label_per_scheme(scheme, kind):
    assert _mcp_row(scheme).kind == kind


def test_scheme_label_is_case_insensitive():
    assert netpolicy._scheme_transport("WSS") == "tls"
    assert netpolicy._scheme_transport("Http") == "plaintext"


def test_registry_unreadable_for_an_oversized_npmrc(tmp_path):
    rc = _npm_world(tmp_path, "# " + "x" * (isolation.MAX_READ_BYTES + 10) + "\nregistry=https://registry.npmjs.org/\n")
    census = _census(_install_allowed(), npm=rc)
    assert census.registry_cause == "registry-unreadable"
    assert _approve(_install_allowed(), npm=rc).detail == "registry-unreadable"


def test_registry_unreadable_for_an_unreadable_file(tmp_path):
    rc = _npm_world(tmp_path, "registry=https://registry.npmjs.org/\n")
    rc.unlink()
    rc.mkdir()  # lexists, but not a regular file
    assert _approve(_install_allowed(), npm=rc).detail == "registry-unreadable"


def test_registry_unparseable_for_non_utf8(tmp_path):
    rc = tmp_path / "npmrc"
    rc.write_bytes(b"registry=https://\xff\xfe.example.net/\n")
    census = _census(_install_allowed(), npm=rc)
    assert census.registry_cause == "registry-unparseable"
    assert _approve(_install_allowed(), npm=rc).detail == "registry-unparseable"


def test_registry_unparseable_for_a_value_that_does_not_parse(tmp_path):
    rc = _npm_world(tmp_path, "registry=not a url at all\n")
    assert _approve(_install_allowed(), npm=rc).detail == "registry-unparseable"


def test_registry_not_approved_carries_no_read_cause(tmp_path):
    rc = _npm_world(tmp_path, "registry=https://other.example.net/\n")
    census = _census(_install_allowed(), npm=rc)
    assert census.registry_cause is None
    assert _approve(_install_allowed(), npm=rc).detail == "registry-not-approved"


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_an_npmrc_behind_a_symlinked_parent_is_read_as_a_regular_file_under_the_cap(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "npmrc").write_text("registry=https://registry.npmjs.org/\n")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    hosts, cause = netpolicy._read_registry_hosts([link / "npmrc"])
    assert hosts == ["registry.npmjs.org"] and cause is None
    assert netpolicy._read_registry_hosts([link / "npmrc"]) == (hosts, cause)


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_an_oversized_npmrc_behind_a_symlinked_parent_is_refused_at_the_cap(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    pad = "# " + "x" * (isolation.MAX_READ_BYTES + 5) + "\n"
    (real / "npmrc").write_text(pad + "registry=https://registry.npmjs.org/\n")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert netpolicy._read_registry_hosts([link / "npmrc"]) == (None, "registry-unreadable")
    # a file one byte under the cap is read whole
    (real / "npmrc").write_text("x" * (isolation.MAX_READ_BYTES - 2) + "\n")
    assert netpolicy._read_registry_hosts([link / "npmrc"]) == ([], None)


def test_a_symlinked_npmrc_file_itself_is_refused(tmp_path):
    target = tmp_path / "target"
    target.write_text("registry=https://registry.npmjs.org/\n")
    link = tmp_path / "npmrc"
    link.symlink_to(target)
    assert netpolicy._read_registry_hosts([link]) == (None, "registry-unreadable")
