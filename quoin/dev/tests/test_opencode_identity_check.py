"""The launch-time identity check: an injected-opener table for every reason,
and real TLS on loopback. No test opens a non-loopback socket."""
from __future__ import annotations

import hashlib
import socket
import ssl
import ssl as _ssl
from types import SimpleNamespace

import pytest

import _opencode_tls_helpers as tls
from quoin.opencode_adapter import netpolicy

D = netpolicy.Destination
POLICY = SimpleNamespace(identity_check="required", allowed_addresses=None, pins=None)


def _dest(host="gw.example.com", kind="tls", port=443, ca=None, scheme="https"):
    return D("provider", host, scheme, port, kind, "profile", "p", ca)


def _policy(**kw):
    return SimpleNamespace(identity_check="required", allowed_addresses=None, pins=None, **kw)


class FakeSock:
    def __init__(self, replies=()):
        self.closed = False
        self.sent = b""
        self._replies = list(replies)

    def close(self):
        self.closed = True

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if self._replies:
            item = self._replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return b""


def _resolver(*addresses, error=None):
    def run(host, port, type=None):
        if error is not None:
            raise error
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]
    return run


def _checker(resolver=None, connect=None, factory=None, timeout=1.0):
    return netpolicy.SystemIdentityChecker(
        resolver=resolver or _resolver("203.0.113.5"), connect=connect or (lambda addr, t: FakeSock()),
        context_factory=factory or (lambda cafile=None: FakeContext()), timeout_s=timeout,
    )


class FakeContext:
    def __init__(self, error=None, cert=b"cert-bytes"):
        self.error, self.cert = error, cert
        self.check_hostname = False
        self.verify_mode = None

    def wrap_socket(self, sock, server_hostname=None):
        self.hostname = server_hostname
        if self.error is not None:
            raise self.error
        return SimpleNamespace(close=lambda: None, getpeercert=lambda binary_form=False: self.cert)


def _check(checker, dest=None, policy=None, proxy=None):
    return checker.check(dest or _dest(), policy=policy or POLICY, proxy=proxy)


@pytest.mark.parametrize("host", ["gw.example.invalid", "x.invalid"])
def test_invalid_names_are_refused_without_a_lookup(host):
    def boom(*a, **k):
        raise AssertionError("lookup")
    result = _check(_checker(resolver=boom), _dest(host))
    assert (result.outcome, result.reason) == ("unverifiable", "reserved-name")


def test_loopback_and_plaintext_never_connect():
    checker = _checker(resolver=lambda *a, **k: (_ for _ in ()).throw(AssertionError("lookup")))
    assert _check(checker, _dest("127.0.0.1", "loopback")).outcome == "ok"
    assert _check(checker, _dest("gw.example.com", "plaintext")).outcome == "plaintext"


def test_dns_failures_and_timeout():
    for error in (socket.gaierror(-2, "x"), OSError("x"), UnicodeError("label"), ValueError("nul")):
        assert _check(_checker(resolver=_resolver(error=error))).reason == "dns-failed"
    import threading

    gate = threading.Event()

    def slow(host, port, type=None):
        gate.wait(3)
        return []

    try:
        assert _check(_checker(resolver=slow, timeout=0.05)).reason == "dns-timeout"
    finally:
        gate.set()


def test_thread_start_failure_is_dns_failed(monkeypatch):
    import threading

    def fail(self):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(threading.Thread, "start", fail)
    assert _check(_checker()).reason == "dns-failed"


def test_resolved_addresses_are_capped_and_ipv4_mapped_folded():
    many = ["203.0.113.%d" % (i % 250 + 1) for i in range(300)] + ["2001:db8::%x" % i for i in range(100)]
    seen = []
    checker = _checker(resolver=_resolver(*many), connect=lambda addr, t: seen.append(addr) or FakeSock())
    result = _check(checker)
    assert result.addresses_count <= netpolicy.MAX_ADDRESSES
    mapped = _checker(resolver=_resolver("::ffff:203.0.113.9"), connect=lambda addr, t: seen.append(addr) or FakeSock())
    _check(mapped)
    assert seen[-1][0] == "203.0.113.9"


def test_allowed_addresses_inside_and_outside():
    policy = _policy()
    policy.allowed_addresses = ("203.0.113.0/24",)
    assert _check(_checker(), policy=policy).outcome == "ok"
    outside = _check(_checker(resolver=_resolver("198.51.100.4")), policy=policy)
    assert (outside.outcome, outside.reason) == ("mismatch", "address-outside")
    mixed = _check(_checker(resolver=_resolver("203.0.113.5", "198.51.100.4")), policy=policy)
    assert mixed.reason == "address-outside"
    mapped = _check(_checker(resolver=_resolver("::ffff:203.0.113.7")), policy=policy)
    assert mapped.outcome == "ok"


def test_connect_failures():
    def refuse(addr, t):
        raise ConnectionRefusedError()
    def slow(addr, t):
        raise socket.timeout()
    assert _check(_checker(connect=refuse)).reason == "connect-failed"
    assert _check(_checker(connect=slow)).reason == "connect-failed"


def test_handshake_errors_map_to_reasons():
    def factory(error):
        return lambda cafile=None: FakeContext(error)
    mismatch = ssl.SSLCertVerificationError(1, "Hostname mismatch")
    mismatch.verify_code = 62
    untrusted = ssl.SSLCertVerificationError(1, "unable to get local issuer")
    untrusted.verify_code = 20
    assert _check(_checker(factory=factory(mismatch))).reason == "name-mismatch"
    assert _check(_checker(factory=factory(untrusted))).reason == "untrusted-certificate"
    assert _check(_checker(factory=factory(ssl.SSLError("alert")))).reason == "tls-failed"
    assert _check(_checker(factory=factory(socket.timeout()))).reason == "tls-failed"
    assert _check(_checker(factory=factory(UnicodeError("idna")))).reason == "tls-failed"


def test_context_failures_are_ca_file_unreadable():
    def bad(cafile=None):
        raise OSError("no such ca file")
    assert _check(_checker(factory=bad)).reason == "ca-file-unreadable"
    def sslbad(cafile=None):
        raise ssl.SSLError("garbage")
    assert _check(_checker(factory=sslbad)).reason == "ca-file-unreadable"


def test_pins_match_and_mismatch():
    cert = b"leaf-der"
    fp = hashlib.sha256(cert).hexdigest()
    policy = _policy()
    policy.pins = (fp,)
    ok = _check(_checker(factory=lambda cafile=None: FakeContext(cert=cert)), policy=policy)
    assert ok.outcome == "ok" and ok.pinned and ok.cert_sha256_prefix == fp[:16]
    policy.pins = ("0" * 64,)
    bad = _check(_checker(factory=lambda cafile=None: FakeContext(cert=cert)), policy=policy)
    assert (bad.outcome, bad.reason) == ("mismatch", "pin-mismatch")


def test_sockets_are_closed():
    made = []
    def connect(addr, t):
        made.append(FakeSock())
        return made[-1]
    _check(_checker(connect=connect))
    assert all(s.closed for s in made)


def _proxy(port=8080, host="proxy.test", approved="profile"):
    return netpolicy.ProxyRoute("HTTPS_PROXY", host, "http", port, approved)


def test_proxy_tunnel_outcomes():
    ok_sock = FakeSock([b"HTTP/1.1 200 OK\r\n\r\n"])
    checker = _checker(connect=lambda addr, t: ok_sock)
    result = _check(checker, proxy=_proxy())
    assert result.outcome == "ok" and result.kind == "via-proxy" and result.addresses_count is None
    assert b"CONNECT gw.example.com:443" in ok_sock.sent
    denied = _checker(connect=lambda addr, t: FakeSock([b"HTTP/1.1 403 Forbidden\r\n\r\n"]))
    assert _check(denied, proxy=_proxy()).reason == "proxy-refused"
    def refuse(addr, t):
        raise ConnectionRefusedError()
    assert _check(_checker(connect=refuse), proxy=_proxy()).reason == "proxy-unreachable"
    hang = _checker(connect=lambda addr, t: FakeSock([socket.timeout()]))
    assert _check(hang, proxy=_proxy()).reason == "proxy-unreachable"
    huge = _checker(connect=lambda addr, t: FakeSock([b"x" * 20000]))
    assert _check(huge, proxy=_proxy()).reason == "proxy-refused"


# ------------------------------------------------------ identity_check()


def _census(*rows):
    return netpolicy.Census(destinations=tuple(rows))


class Recorder:
    def __init__(self, result=None, error=None):
        self.seen, self.result, self.error = [], result, error

    def check(self, destination, *, policy, proxy=None):
        self.seen.append(destination)
        if self.error is not None:
            raise self.error
        return self.result or netpolicy.IdentityResult("ok", "tls", None, 1, "abcd", False)


def test_off_runs_nothing():
    off = SimpleNamespace(identity_check="off", allowed_addresses=None, pins=None)
    recorder = Recorder()
    assert netpolicy.identity_check(_census(_dest()), off, recorder) == ([], None)
    assert recorder.seen == []


def test_loopback_rows_do_not_call_the_checker_and_plaintext_refuses():
    recorder = Recorder()
    obs, refusal = netpolicy.identity_check(_census(_dest("127.0.0.1", "loopback")), POLICY, recorder)
    assert refusal is None and obs[0]["host"] == "loopback" and recorder.seen == []
    obs, refusal = netpolicy.identity_check(_census(_dest(kind="plaintext", scheme="http")), POLICY, recorder)
    assert refusal.code == "destination-plaintext"


def test_failures_map_to_codes_with_reason_detail():
    for outcome, code in (("mismatch", "destination-identity-mismatch"), ("unverifiable", "destination-unverifiable")):
        rec = Recorder(netpolicy.IdentityResult(outcome, "tls", "name-mismatch"))
        _obs, refusal = netpolicy.identity_check(_census(_dest()), POLICY, rec)
        assert refusal == netpolicy.NetRefusal(code, "name-mismatch")


def test_an_unexpected_checker_exception_becomes_unverifiable():
    _obs, refusal = netpolicy.identity_check(_census(_dest()), POLICY, Recorder(error=RuntimeError("boom")))
    assert refusal == netpolicy.NetRefusal("destination-unverifiable", "check-error")


def test_unparseable_rows_are_unverifiable_without_calling_the_checker():
    rec = Recorder()
    row = D("provider", "unknown", None, None, "unparseable", "none", "p")
    _obs, refusal = netpolicy.identity_check(_census(row), POLICY, rec)
    assert refusal.detail == "host-unparseable" and rec.seen == []
    via = _dest()
    via = D(*[getattr(via, f) for f in ("role", "host_key", "scheme", "port", "kind", "approved_by", "provider_id", "ca_file")],
            netpolicy.ProxyRoute("HTTPS_PROXY", "unknown", "unknown", None, "none"))
    _obs, refusal = netpolicy.identity_check(_census(via), POLICY, rec)
    assert refusal.detail == "host-unparseable" and rec.seen == []


def test_a_personal_policy_that_requires_the_check_is_checked_like_work():
    personal = SimpleNamespace(identity_check="required", allowed_addresses=None, pins=None, classification="personal")
    rec = Recorder(netpolicy.IdentityResult("mismatch", "tls", "pin-mismatch"))
    _obs, refusal = netpolicy.identity_check(_census(_dest()), personal, rec)
    assert refusal.code == "destination-identity-mismatch"


def test_observations_carry_digests_not_names():
    obs, _ = netpolicy.identity_check(_census(_dest()), POLICY, Recorder())
    assert obs[0]["host"].startswith("h-") and "gw.example.com" not in repr(obs)


# ---------------------------------------------------- real TLS on loopback


def _resolver_to_loopback(name, port):
    def run(host, p, type=None):
        assert host == name
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    return run


def _real(tmp_path, names, host="gateway.test", pins=None):
    ca, leaf = tls.make_ca_and_leaf(tmp_path, names)
    return ca, leaf


def test_matching_certificate_is_ok_with_a_fingerprint(tmp_path):
    ca, leaf = _real(tmp_path, ["gateway.test"])
    with tls.TlsServer(*leaf) as server:
        checker = netpolicy.SystemIdentityChecker(resolver=_resolver_to_loopback("gateway.test", server.port), timeout_s=5)
        result = checker.check(_dest("gateway.test", port=server.port, ca=str(ca)), policy=POLICY)
    assert result.outcome == "ok" and len(result.cert_sha256_prefix) == 16


def test_certificate_for_another_name_is_a_mismatch(tmp_path):
    """check_hostname stays on: a certificate for other.test does not satisfy gateway.test."""
    ca, leaf = _real(tmp_path, ["other.test"])
    with tls.TlsServer(*leaf) as server:
        checker = netpolicy.SystemIdentityChecker(resolver=_resolver_to_loopback("gateway.test", server.port), timeout_s=5)
        result = checker.check(_dest("gateway.test", port=server.port, ca=str(ca)), policy=POLICY)
    assert (result.outcome, result.reason) == ("mismatch", "name-mismatch")


def test_untrusted_ca_is_a_mismatch(tmp_path):
    ca, leaf = _real(tmp_path, ["gateway.test"])
    other = tls.make_ca(tmp_path, "other-ca")
    with tls.TlsServer(*leaf) as server:
        checker = netpolicy.SystemIdentityChecker(resolver=_resolver_to_loopback("gateway.test", server.port), timeout_s=5)
        result = checker.check(_dest("gateway.test", port=server.port, ca=str(other[0])), policy=POLICY)
    assert result.outcome == "mismatch" and result.reason == "untrusted-certificate"


def test_pinned_fingerprint_and_wrong_pin(tmp_path):
    ca, leaf = _real(tmp_path, ["gateway.test"])
    der = ssl.PEM_cert_to_DER_cert(leaf[0].read_text())
    good = hashlib.sha256(der).hexdigest()
    with tls.TlsServer(*leaf) as server:
        checker = netpolicy.SystemIdentityChecker(resolver=_resolver_to_loopback("gateway.test", server.port), timeout_s=5)
        policy = _policy()
        policy.pins = (good,)
        assert checker.check(_dest("gateway.test", port=server.port, ca=str(ca)), policy=policy).pinned
        policy.pins = ("1" * 64,)
        wrong = checker.check(_dest("gateway.test", port=server.port, ca=str(ca)), policy=policy)
    assert wrong.reason == "pin-mismatch"


def test_through_a_connect_tunnel(tmp_path):
    ca, leaf = _real(tmp_path, ["gateway.test"])
    with tls.TlsServer(*leaf) as server, tls.ConnectTunnel(("127.0.0.1", server.port)) as tunnel:
        checker = netpolicy.SystemIdentityChecker(timeout_s=5)
        proxy = netpolicy.ProxyRoute("HTTPS_PROXY", "127.0.0.1", "http", tunnel.port, "profile")
        result = checker.check(_dest("gateway.test", port=server.port, ca=str(ca)), policy=POLICY, proxy=proxy)
    assert result.outcome == "ok" and result.kind == "via-proxy"
    assert tunnel.targets == ["gateway.test:%d" % server.port]


def test_a_denying_and_a_hanging_tunnel(tmp_path):
    with tls.ConnectTunnel(mode="deny") as tunnel:
        proxy = netpolicy.ProxyRoute("HTTPS_PROXY", "127.0.0.1", "http", tunnel.port, "profile")
        assert netpolicy.SystemIdentityChecker(timeout_s=2).check(_dest("gateway.test"), policy=POLICY, proxy=proxy).reason == "proxy-refused"
    with tls.ConnectTunnel(mode="hang") as tunnel:
        proxy = netpolicy.ProxyRoute("HTTPS_PROXY", "127.0.0.1", "http", tunnel.port, "profile")
        assert netpolicy.SystemIdentityChecker(timeout_s=0.5).check(_dest("gateway.test"), policy=POLICY, proxy=proxy).reason == "proxy-unreachable"


def test_a_missing_and_a_garbage_ca_file(tmp_path):
    ca, leaf = _real(tmp_path, ["gateway.test"])
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a certificate")
    with tls.TlsServer(*leaf) as server:
        checker = netpolicy.SystemIdentityChecker(resolver=_resolver_to_loopback("gateway.test", server.port), timeout_s=5)
        for path in (str(tmp_path / "absent.pem"), str(garbage)):
            result = checker.check(_dest("gateway.test", port=server.port, ca=path), policy=POLICY)
            assert result.reason == "ca-file-unreadable"
