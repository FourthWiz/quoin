"""Add-ons: discovered through the quoin.addons entry points, pip-installed into
this environment when chosen and missing, then installed by their own code."""

import argparse
import sys

import pytest

from quoin import addons, cli


class FakeAddon:
    def __init__(self):
        self.installed, self.removed = [], 0

    def install(self, options):
        self.installed.append(options)
        return 0

    def remove(self):
        self.removed += 1
        return 0


class Point:
    def __init__(self, obj, value="pkg.mod:addon"):
        self.obj, self.value = obj, value

    def load(self):
        return self.obj


@pytest.fixture
def registry(monkeypatch):
    found = {}
    monkeypatch.setattr(addons, "discovered", lambda: found)
    return found


def test_installed_addon_runs_its_own_install_without_pip(registry):
    fake = FakeAddon()
    registry["cockpit"] = Point(fake)
    calls = []
    assert addons.install("cockpit", runner=calls.append, printer=lambda _l: None) == 0
    assert calls == [] and fake.installed == [{}]


def test_missing_addon_is_pip_installed_into_this_environment(registry):
    fake = FakeAddon()
    calls = []

    def pip(argv):
        calls.append(argv)
        registry["cockpit"] = Point(fake)
        return 0

    assert addons.install("cockpit", runner=pip, printer=lambda _l: None) == 0
    assert calls == [[sys.executable, "-m", "pip", "install", "quoin-cockpit"]]
    assert fake.installed == [{}]


def test_a_spec_overrides_the_package(registry):
    calls = []

    def pip(argv):
        calls.append(argv)
        registry["cockpit"] = Point(FakeAddon())
        return 0

    addons.install("cockpit", "git+https://example.com/quoin-cockpit", runner=pip, printer=lambda _l: None)
    assert calls[0][-1] == "git+https://example.com/quoin-cockpit"


def test_unavailable_and_unknown_addons_are_refused_without_a_spec(registry):
    out = []
    assert addons.install("opencode", runner=lambda a: pytest.fail("pip ran"), printer=out.append) == 2
    assert "not yet ported" in out[-1]
    assert addons.install("nope", runner=lambda a: pytest.fail("pip ran"), printer=out.append) == 2


def test_pip_failure_and_a_package_without_an_entry_point_are_reported(registry):
    out = []
    assert addons.install("cockpit", runner=lambda a: 1, printer=out.append) == 1
    assert "could not install" in out[-1]
    assert addons.install("cockpit", runner=lambda a: 0, printer=out.append) == 1
    assert "entry point" in out[-1]


def test_status_lists_known_and_third_party_addons(registry):
    registry["cockpit"] = Point(FakeAddon())
    registry["extra"] = Point(FakeAddon(), "extra.addon:addon")
    rows = {r["name"]: r for r in addons.status()}
    assert rows["cockpit"]["installed"] and not rows["opencode"]["available"]
    assert rows["extra"]["note"] == "third-party"


def _args(**kw):
    return argparse.Namespace(with_addon=kw.get("with_addon"), no_addons=kw.get("no_addons", False))


def test_install_question_offers_available_addons_only(registry, monkeypatch):
    asked = []
    monkeypatch.setattr(cli.install_prompts, "is_interactive", lambda: True)
    monkeypatch.setattr(cli.install_prompts, "ask_yes_no", lambda q, default: asked.append((q, default)) or True)
    assert cli._choose_addons(_args()) == (("cockpit", None),)
    assert len(asked) == 1 and "Cockpit add-on" in asked[0][0] and asked[0][1] is False


def test_flags_decide_without_asking(registry, monkeypatch):
    monkeypatch.setattr(cli.install_prompts, "is_interactive", lambda: True)
    monkeypatch.setattr(cli.install_prompts, "ask_yes_no", lambda q, default: pytest.fail("asked"))
    assert cli._choose_addons(_args(with_addon=["cockpit=git+https://x/y"], no_addons=True)) == (
        ("cockpit", "git+https://x/y"),)
    assert cli._choose_addons(_args(no_addons=True)) == ()


def test_non_interactive_install_asks_nothing(registry, monkeypatch):
    monkeypatch.setattr(cli.install_prompts, "is_interactive", lambda: False)
    monkeypatch.setattr(cli.install_prompts, "ask_yes_no", lambda q, default: pytest.fail("asked"))
    assert cli._choose_addons(_args()) == ()


def test_an_installed_addon_is_refreshed_without_asking(registry, monkeypatch):
    registry["cockpit"] = Point(FakeAddon())
    monkeypatch.setattr(cli.install_prompts, "is_interactive", lambda: True)
    monkeypatch.setattr(cli.install_prompts, "ask_yes_no", lambda q, default: pytest.fail("asked"))
    assert cli._choose_addons(_args()) == (("cockpit", None),)


def test_addons_command_lists_and_removes(registry, capsys):
    fake = FakeAddon()
    registry["cockpit"] = Point(fake)
    assert cli.main(["addons"]) == 0
    assert "cockpit    installed" in capsys.readouterr().out
    assert cli.main(["addons", "remove", "cockpit"]) == 0 and fake.removed == 1
    assert cli.main(["addons", "remove", "opencode"]) == 1
