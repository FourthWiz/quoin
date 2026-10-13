"""Prerequisite checks and the consent engine, driven only by FakeCtx."""
import plistlib

import pytest

import _streamdeck_lifecycle as lc
from _streamdeck_lifecycle import FakeCtx, deck_home, forbid_dangerous_subprocess  # noqa: F401
from quoin.streamdeck import prereqs as pr

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess")
PYV = pr.PY_PROBE
CLT = ("xcode-select", "-p")
MD = ("mdfind", "kMDItemCFBundleIdentifier == 'com.elgato.StreamDeck'")


def py_probe(path, version="3.12.1"):
    return {(path, "-E", "-s", "-c", PYV): (0, version + "\n", "")}


def write_app(apps, version):
    info = apps / "Elgato Stream Deck.app" / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
    return str(apps / "Elgato Stream Deck.app")


def node_probes(path, version="v22.1.0"):
    return {(path, "-e", pr.WEBSOCKET_PROBE): (0, "", ""), (path, "--version"): (0, version + "\n", "")}


def check(name, fake, results=None):
    return pr.BY_NAME[name].check(fake.ctx, results or {}, {})


# ------------------------------------------------------------------ python / CLT

SOURCES = {
    "legacy": lambda p: dict(legacy={"python": p}),
    "recorded": lambda p: dict(recorded={"python": {"path": p}}),
    "which": lambda p: dict(which={"python3": p}),
    "fallback": lambda p: dict(),
    "flag": lambda p: dict(flags={"python": p}),
}


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_clt_gate_blocks_the_stub_from_every_source(tmp_path, source):
    stub = "/usr/bin/python3"
    fake = FakeCtx(tmp_path, probes={CLT: (1, "", "no tools")}, **SOURCES[source](stub))
    res = check("python", fake)
    assert not res.ok and "CLT not installed" in res.detail
    assert fake.probed(stub) == []
    assert fake.probe_calls.count(CLT) == 1


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_clt_ok_allows_probing_the_stub(tmp_path, source):
    stub = "/usr/bin/python3"
    fake = FakeCtx(tmp_path, probes={CLT: (0, "/Library/Developer", ""), **py_probe(stub)}, **SOURCES[source](stub))
    res = check("python", fake)
    assert res.ok and res.path == stub and res.version == "3.12.1"
    assert fake.probe_calls.count(CLT) == 1


def test_clt_gate_follows_realpath(tmp_path):
    link = str(tmp_path / "bin" / "python3")
    fake = FakeCtx(tmp_path, probes={CLT: (1, "", "")}, legacy={"python": link}, realpath_map={link: "/usr/bin/python3"})
    res = check("python", fake)
    assert not res.ok and fake.probed(link) == [] and fake.probed("/usr/bin/python3") == []
    ok = FakeCtx(tmp_path, probes={CLT: (0, "", ""), **py_probe(link)}, legacy={"python": link},
                 realpath_map={link: "/usr/bin/python3"})
    assert check("python", ok).path == link


def test_no_xcode_probe_for_other_pythons(tmp_path):
    # A candidate that wins before the literal /usr/bin/python3 fallback never triggers the CLT probe.
    fake = FakeCtx(tmp_path, probes=py_probe("/opt/x/bin/python3"), legacy={"python": "/opt/x/bin/python3"})
    res = check("python", fake)
    assert res.ok and CLT not in fake.probe_calls


def test_venv_python_skipped_and_recorded_wins(tmp_path):
    fake = FakeCtx(tmp_path, probes={CLT: (1, "", "")}, which={"python3": "/venv/prefix/bin/python3"})
    assert not check("python", fake).ok and fake.probed("/venv") == []
    site = FakeCtx(tmp_path, probes={CLT: (1, "", "")}, which={"python3": "/a/site-packages/bin/python3"})
    assert not check("python", site).ok and site.probed("/a") == []
    both = FakeCtx(tmp_path, probes={**py_probe("/opt/r/python3"), **py_probe("/opt/p/python3")},
                   recorded={"python": "/opt/r/python3"}, which={"python3": "/opt/p/python3"})
    assert check("python", both).path == "/opt/r/python3" and both.probed("/opt/p") == []


def test_old_python_rejected(tmp_path):
    fake = FakeCtx(tmp_path, probes=py_probe("/opt/x/python3", "3.8.18"), legacy={"python": "/opt/x/python3"})
    assert not check("python", fake).ok


# ------------------------------------------------------------------ other checks

def test_streamdeck_app(tmp_path):
    fake = FakeCtx(tmp_path)
    assert not check("streamdeck-app", fake).ok
    write_app(fake.apps, "6.5.1")
    assert not check("streamdeck-app", fake).ok
    app = write_app(fake.apps, "7.6.0")
    res = check("streamdeck-app", fake)
    assert res.ok and res.version == "7.6.0" and res.path == app


def test_streamdeck_app_found_through_mdfind(tmp_path):
    other = tmp_path / "elsewhere"
    app = write_app(other, "6.6.0")
    fake = FakeCtx(tmp_path, probes={MD: (0, app + "\n", "")})
    assert check("streamdeck-app", fake).ok


def test_node_selection(tmp_path):
    rec = FakeCtx(tmp_path, probes=node_probes("/n/rec"), recorded={"node": {"mode": "external", "path": "/n/rec"}},
                  which={"node": "/n/path"})
    assert check("node", rec).path == "/n/rec"
    leg = FakeCtx(tmp_path, probes=node_probes("/n/leg"), legacy={"node": "/n/leg"})
    assert check("node", leg).path == "/n/leg"
    path = FakeCtx(tmp_path, probes=node_probes("/n/path"), which={"node": "/n/path"}, flags={"node": "PATH"})
    assert check("node", path).path == "/n/path"
    old = FakeCtx(tmp_path, probes={("/n/old", "-e", pr.WEBSOCKET_PROBE): (1, "", "")}, which={"node": "/n/old"})
    assert not check("node", old).ok


def test_node_bundled_only_with_new_app(tmp_path):
    fake = FakeCtx(tmp_path)
    new = {"streamdeck-app": pr.CheckResult(True, "/a", "7.1.0")}
    res = check("node", fake, new)
    assert res.ok and res.version == "bundled" and res.path is None
    assert not check("node", fake, {"streamdeck-app": pr.CheckResult(True, "/a", "7.0.9")}).ok
    assert not check("node", fake, {"streamdeck-app": pr.CheckResult(False)}).ok


def test_agentdesk(tmp_path):
    f = tmp_path / "agentdesk.zsh"
    fake = FakeCtx(tmp_path, flags={"agentdesk": str(f)})
    assert not check("agentdesk", fake).ok
    f.write_text("#!/bin/zsh\n")
    assert not check("agentdesk", fake).ok
    f.write_text("AGENTDESK_API_VERSION=0\n")
    assert not check("agentdesk", fake).ok
    f.write_text("x\nAGENTDESK_API_VERSION=2\n")
    res = check("agentdesk", fake)
    assert res.ok and res.version == "2" and res.path == str(f)


def test_zellij(tmp_path):
    good = "list-panes focus-pane-id"
    fake = FakeCtx(tmp_path, which={"zellij": "/z/zellij"}, probes={("/z/zellij", "action", "--help"): (0, good, ""),
                                                                    ("/z/zellij", "--version"): (0, "zellij 0.42\n", "")})
    res = check("zellij", fake)
    assert res.ok and res.version == "zellij 0.42"
    bad = FakeCtx(tmp_path, which={"zellij": "/z/zellij"}, probes={("/z/zellij", "action", "--help"): (0, "list-panes", "")})
    assert not check("zellij", bad).ok
    login = FakeCtx(tmp_path, probes={("/bin/zsh", "-lc", "command -v zellij"): (0, "/h/zellij\n", ""),
                                      ("/h/zellij", "action", "--help"): (0, "", good),
                                      ("/h/zellij", "--version"): (0, "zellij 1\n", "")})
    assert check("zellij", login).path == "/h/zellij"
    assert not check("zellij", FakeCtx(tmp_path)).ok


def test_ghostty(tmp_path):
    fake = FakeCtx(tmp_path)
    assert not check("ghostty", fake).ok
    sdef = fake.apps / "Ghostty.app" / "Contents" / "Resources" / "Ghostty.sdef"
    sdef.parent.mkdir(parents=True)
    sdef.write_text("<class name='window'/>")
    assert not check("ghostty", fake).ok
    sdef.write_text("<command name='new surface configuration'/>")
    assert check("ghostty", fake).ok


def test_homebrew_by_which_or_login_shell(tmp_path):
    assert check("homebrew", FakeCtx(tmp_path, which={"brew": "/b/brew"})).path == "/b/brew"
    login = FakeCtx(tmp_path, probes={("/bin/zsh", "-lc", "command -v brew"): (0, "/l/brew\n", "")})
    assert check("homebrew", login).path == "/l/brew"
    assert not check("homebrew", FakeCtx(tmp_path)).ok


def test_run_checks_order_and_exception_isolation(tmp_path):
    def boom(argv):
        raise RuntimeError("x")
    fake = FakeCtx(tmp_path, probes={MD: boom})
    results = pr.run_checks(fake.ctx)
    assert list(results) == ["homebrew", "python", "streamdeck-app", "node", "agentdesk", "zellij", "ghostty"]
    assert results["streamdeck-app"].detail == "RuntimeError" and not results["streamdeck-app"].ok


def test_legacy_parsers():
    cmd = "/usr/bin/python3 '/Users/x/Library/Application Support/QuoinStreamDeck/monitor.py' claude"
    assert pr.parse_legacy_python(cmd) == "/usr/bin/python3"
    assert pr.parse_legacy_python("'/odd path/py' a b") == "/odd path/py"
    assert pr.parse_legacy_python("a 'b") is None
    assert pr.parse_launch_sh('#!/bin/sh\ncd x\nexec /opt/x/bin/node plugin.js "$@"\n') == "/opt/x/bin/node"
    assert pr.parse_launch_sh("exec '/a b/node' plugin.js\n") == "/a b/node"
    assert pr.parse_launch_sh("echo hi\n") is None


# ------------------------------------------------------------------ consent

def ctx_with_python(tmp_path, **kw):
    probes = dict(py_probe("/opt/x/python3"))
    probes.update(kw.pop("probes", {}))
    which = dict({"brew": "/b/brew"}, **kw.pop("which", {}))
    return FakeCtx(tmp_path, probes=probes, which=which, legacy={"python": "/opt/x/python3"}, **kw)


def app_install(fake):
    write_app(fake.apps, "6.6.0")


def node_install(fake):
    fake.which_table["node"] = "/n/node"
    fake.probes.update(node_probes("/n/node"))


def agentdesk_install(fake):
    path = tmp_home(fake) / ".config" / "agentdesk" / "agentdesk.zsh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("AGENTDESK_API_VERSION=1\n")


def tmp_home(fake):
    import pathlib
    return pathlib.Path(fake.ctx.home)


def zellij_install(fake):
    fake.which_table["zellij"] = "/z/zellij"
    fake.probes[("/z/zellij", "action", "--help")] = (0, "list-panes focus-pane-id", "")
    fake.probes[("/z/zellij", "--version")] = (0, "zellij 1\n", "")


def ghostty_install(fake):
    sdef = fake.apps / "Ghostty.app" / "Contents" / "Resources" / "Ghostty.sdef"
    sdef.parent.mkdir(parents=True, exist_ok=True)
    sdef.write_text("new surface configuration")


INSTALLS = {
    "streamdeck-app": (("brew", "install", "--cask", "elgato-stream-deck"), app_install),
    "node": (("brew", "install", "node"), node_install),
    "agentdesk": ("deploy", agentdesk_install),
    "zellij": (("brew", "install", "zellij"), zellij_install),
    "ghostty": (("brew", "install", "--cask", "ghostty"), ghostty_install),
}
ORDER = ["streamdeck-app", "node", "agentdesk", "zellij", "ghostty"]


def make(tmp_path, accept=None, runner_rc=None, deploy_rc=0, effective=True, **kw):
    installs = {key: act for key, act in INSTALLS.values()} if effective else {}
    answers = ["y" if name == accept else "n" for name in ORDER]
    return ctx_with_python(tmp_path, stdin_tty=True, stdout_tty=True, answers=answers, installs=installs,
                           runner_rc=runner_rc, deploy_rc=deploy_rc, **kw)


@pytest.mark.parametrize("name", ORDER)
def test_accept_installs_and_rechecks(tmp_path, name):
    fake = make(tmp_path, accept=name)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx))
    argv = INSTALLS[name][0]
    if argv == "deploy":
        assert fake.deployed == [True] and fake.runner_calls == []
    else:
        assert fake.runner_calls == [argv]
    assert out.prereqs[name]["state"] == "installed"
    assert name not in out.core_missing
    others = [n for n in ORDER if n != name]
    assert all(out.prereqs[n]["state"] == "declined" for n in others)


@pytest.mark.parametrize("name", ORDER)
def test_decline_lists_features_and_runs_nothing(tmp_path, name):
    fake = make(tmp_path, accept=None)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx))
    assert fake.runner_calls == [] and fake.deployed == []
    assert out.prereqs[name]["state"] == "declined"
    assert set(pr.BY_NAME[name].features) <= set(out.features_unavailable)
    assert out.features_unavailable == sorted(out.features_unavailable)


def test_failed_install_continues_to_the_next_item(tmp_path):
    fake = make(tmp_path, accept="node", runner_rc=lambda argv: 1)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx))
    assert out.prereqs["node"]["state"] == "failed"
    assert len(fake.prompts) == 5
    assert out.prereqs["zellij"]["state"] == "declined"


def test_accepted_but_still_missing_is_failed(tmp_path):
    fake = make(tmp_path, accept="zellij", effective=False)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx))
    assert fake.runner_calls == [("brew", "install", "zellij")]
    assert out.prereqs["zellij"]["state"] == "failed"


@pytest.mark.parametrize("tty", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("mode", ["ask", "all", "none"])
def test_not_interactive_never_installs_or_prompts(tmp_path, tty, mode):
    fake = ctx_with_python(tmp_path, stdin_tty=tty[0], stdout_tty=tty[1], answers=["y"] * 6)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx), mode=mode)
    assert fake.runner_calls == [] and fake.deployed == [] and fake.prompts == []
    text = "\n".join(out.lines)
    for name in ORDER:
        assert out.prereqs[name]["state"] == "missing"
        assert pr.BY_NAME[name].install.display in text
        assert pr.BY_NAME[name].why in text
    assert "non-interactive run" in text


def test_none_on_a_tty_declines_without_asking(tmp_path):
    fake = make(tmp_path)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx), mode="none")
    assert fake.prompts == [] and fake.runner_calls == []
    assert all(out.prereqs[n]["state"] == "declined" for n in ORDER)


def test_all_on_a_tty_accepts_without_asking(tmp_path):
    fake = make(tmp_path)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx), mode="all")
    assert fake.prompts == []
    assert out.prereqs["zellij"]["state"] == "installed" and out.prereqs["ghostty"]["state"] == "installed"
    assert ("brew", "install", "--cask", "ghostty") in fake.runner_calls


def test_eof_declines_the_rest(tmp_path):
    fake = ctx_with_python(tmp_path, stdin_tty=True, stdout_tty=True, answers=["y"],
                           installs={INSTALLS["streamdeck-app"][0]: app_install})
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx))
    assert out.prereqs["streamdeck-app"]["state"] == "installed"
    assert all(out.prereqs[n]["state"] == "declined" for n in ("node", "agentdesk", "zellij", "ghostty"))


def test_missing_homebrew_reports_instead_of_running(tmp_path):
    fake = FakeCtx(tmp_path, probes={**py_probe("/opt/x/python3"), ("/bin/zsh", "-lc", "command -v brew"): (1, "", "")},
                   legacy={"python": "/opt/x/python3"}, stdin_tty=True, stdout_tty=True, answers=["y"] * 6)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx))
    assert fake.runner_calls == []
    assert "install Homebrew first (https://brew.sh)" in "\n".join(out.lines)
    assert out.prereqs["zellij"]["state"] == "missing"
    assert out.prereqs["homebrew"]["state"] == "missing"
    assert fake.deployed == [True]  # agentdesk does not need Homebrew


def test_core_missing_and_checked_at(tmp_path):
    fake = FakeCtx(tmp_path, stdin_tty=False)
    out = pr.resolve(fake.ctx, pr.run_checks(fake.ctx))
    assert out.core_missing == ["python", "streamdeck-app", "node"]
    assert out.prereqs["python"]["checked_at"] == "2026-10-13T12:00:00Z"
    ok = ctx_with_python(tmp_path)
    write_app(ok.apps, "7.6.0")
    done = pr.resolve(ok.ctx, pr.run_checks(ok.ctx))
    assert done.core_missing == [] and done.prereqs["node"]["state"] == "ok"
