"""Render an evaluation as readable text or JSON.

The explanation is assembled once as a plain dictionary, and the text form is
rendered from that dictionary, so both carry the same facts. Everything shown
is either a closed token with fixed text, an identifier that already passed
the identifier grammar, or a value the user wrote in their own configuration
(base URLs, host lists, credential references). Credentials themselves are
never resolved here, so a secret cannot appear; `--redact` additionally masks
the endpoint host and port, host list entries, keychain accounts and the
output location so the result can be pasted into a ticket.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import compiler, errors, merge, roles
from .secrets import parse as parse_credential_ref

EXPLAIN_FORMAT = 1
REDACTED = "***"

# Sentinel: build the result here (only when nothing blocks compilation).
AUTO = object()

REASON_TEXT: Dict[str, str] = {
    "not-allowed": "the provider is not on an allow list",
    "denied": "the provider is on a deny list",
    "host-not-allowed": "the provider's host is not on a host allow list",
    "host-denied": "the provider's host is on a host deny list",
    "managed-not-allowed": "the managed policy does not allow the provider",
    "unreadable": "the qualification record could not be read",
    "bad-schema": "the qualification record has an unknown schema",
    "bad-shape": "the qualification record is malformed",
    "unsafe-permissions": "the qualification record or its directory is writable by others or owned by another user",
    "future-timestamp": "the qualification record is dated in the future",
    "not-qualified": "the probe found the model not qualified",
    "could-not-run": "the probe could not run against the model",
    "model-mismatch": "the record was measured for a different model id",
    "endpoint-mismatch": "the record was measured against a different endpoint",
    "runtime-mismatch": "the record was measured on a different runtime version",
    "too-old": "the qualification record is older than the allowed age",
    "override": "chosen by a run override",
    "role-mapping": "chosen by the role mapping",
    "default-model": "the profile's default model",
    "auxiliary-model": "the profile's auxiliary model",
    "classification-incompatible": "the model's provider or profile cannot serve a work project",
    "qualification-missing": "the model has no qualification record",
    "qualification-failed": "the model's qualification failed",
    "qualification-stale": "the model's qualification is too old",
    "qualification-mismatched": "the model's qualification does not match the current endpoint, model id or runtime",
    "qualification-malformed": "the model's qualification record is malformed",
    "effort-max": "the max effort is never mapped",
    "effort-no-capability": "the model's capability record does not report reasoning support; the effort takes effect once a probe records it",
    "effort-no-mapping": "no option key is recorded for this provider kind and endpoint family",
    "effort-unqualified": "the role does not run, so no effort applies",
}

DIAGNOSTIC_TEXT: Dict[str, str] = {
    "effort-max": REASON_TEXT["effort-max"],
    "effort-no-capability": REASON_TEXT["effort-no-capability"],
    "effort-no-mapping": REASON_TEXT["effort-no-mapping"],
    "effort-unqualified": REASON_TEXT["effort-unqualified"],
}

SUMMARY_NOTE = "not used by the pinned runtime; not emitted"
NO_MANAGED_TEXT = "no managed policy"
_HOST_LIST_KEYS = ("policy.allowed_hosts", "policy.denied_hosts")


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    return "%s://%s%s" % (parts.scheme, REDACTED, parts.path)


def _credential_text(ref: str, redact: bool) -> str:
    if not redact:
        return ref
    try:
        return parse_credential_ref(ref).masked()
    except ValueError:
        return REDACTED


def _plain(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def try_build(ev: compiler.Evaluation) -> Tuple[Optional[compiler.CompileResult], Optional[str]]:
    """Build only when nothing blocks compilation and the evaluation did not
    accept unqualified models. Returns (result, failed gate or None)."""
    if ev.allow_unqualified or compiler.compile_blockers(ev):
        return None, None
    try:
        return compiler.build(ev), None
    except compiler.CompileGateError as exc:
        return None, exc.gate
    except compiler.CompileBlocked:
        return None, None


def _finding_dict(finding: errors.Finding) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "code": finding.code,
        "blocking": finding.blocking,
        "message": errors.FINDING_MESSAGES[finding.code],
        "subject": list(finding.subject),
    }
    if finding.error is not None:
        out["detail"] = finding.error.message
        out["fix"] = finding.error.fix
    return out


def _resolution_dict(ev: compiler.Evaluation, res: roles.RoleResolution) -> Dict[str, Any]:
    out = compiler.canonical_resolution(ev, res)
    out["reason_text"] = REASON_TEXT[res.reason]
    out["block_text"] = REASON_TEXT[res.block_reason] if res.block_reason else None
    out["diagnostic_text"] = DIAGNOSTIC_TEXT[res.effort_diagnostic] if res.effort_diagnostic else None
    out["note"] = SUMMARY_NOTE if res.role == "summary" else None
    return out


def explain_document(
    ev: compiler.Evaluation,
    *,
    redact: bool,
    compile_result: Any = AUTO,
    output_dir: Optional[Path] = None,
    gate_failure: Optional[str] = None,
) -> Dict[str, Any]:
    if compile_result is AUTO:
        compile_result, gate_failure = try_build(ev)
    eff = ev.effective

    providers: List[Dict[str, Any]] = []
    for pid, view in sorted(eff.providers.items()):
        excluded = eff.excluded_providers.get(pid)
        url = merge.provider_base_url(view)
        providers.append(
            {
                "id": pid,
                "native_id": compiler.native_provider_id(view),
                "kind": view.kind,
                "endpoint_family": view.endpoint_family,
                "base_url": _redact_url(url) if redact else url,
                "credential_ref": _credential_text(merge.provider_credential_ref(view), redact),
                "credential_env": view.credential_env,
                "status": "excluded" if excluded else "effective",
                "reason": excluded,
                "reason_text": REASON_TEXT[excluded] if excluded else None,
            }
        )

    models: List[Dict[str, Any]] = []
    for name, view in sorted(eff.models.items()):
        qual = ev.qualifications[name]
        ref = view.qualification_ref
        record = "qualifications/%s.json" % ref.split(":", 1)[1] if ref.startswith("local:") else None
        models.append(
            {
                "name": name,
                "provider": view.provider,
                "model_id": view.model_id,
                "qualification_state": qual.state,
                "reason": qual.reason,
                "reason_text": REASON_TEXT[qual.reason] if qual.reason else None,
                "probed_at": qual.probed_at,
                "record": record,
            }
        )

    values: Dict[str, Any] = {}
    for key, item in sorted(eff.values.items()):
        shown = _plain(item.value)
        if redact and key in _HOST_LIST_KEYS:
            shown = [REDACTED for _ in shown]
        values[key] = {"value": shown, "origin": list(item.origin)}

    hosts_allowed = list(_plain(eff.values["policy.allowed_hosts"].value)) if "policy.allowed_hosts" in eff.values else []
    hosts_denied = list(_plain(eff.values["policy.denied_hosts"].value)) if "policy.denied_hosts" in eff.values else []
    if redact:
        hosts_allowed = [REDACTED for _ in hosts_allowed]
        hosts_denied = [REDACTED for _ in hosts_denied]
    notes = [
        errors.FINDING_MESSAGES[f.code]
        for f in compiler.all_findings(ev)
        if f.code == "provider-ids-are-labels"
    ]
    security = {
        "effective_providers": list(eff.effective_providers),
        "excluded_providers": {pid: reason for pid, reason in sorted(eff.excluded_providers.items())},
        "allowed_hosts": hosts_allowed,
        "denied_hosts": hosts_denied,
        "notes": notes,
    }

    findings = [_finding_dict(f) for f in compiler.all_findings(ev)]
    if gate_failure is not None:
        findings.append(
            {
                "code": "gate-" + gate_failure,
                "blocking": True,
                "message": compiler.GATE_MESSAGES[gate_failure],
                "subject": [],
            }
        )

    if compile_result is not None:
        digest, digest_note = compile_result.digest, None
    elif gate_failure is not None:
        digest, digest_note = None, "not available: a compile gate failed"
    elif ev.allow_unqualified:
        digest, digest_note = None, "not available: unqualified models were accepted"
    else:
        digest, digest_note = None, "not available: blocking findings"

    if output_dir is None:
        location = None
    elif redact:
        location = "$XDG_STATE_HOME/quoin/opencode/%s/%s" % (ev.profile, ev.project_key)
    else:
        location = str(output_dir)

    return {
        "explain_format": EXPLAIN_FORMAT,
        "profile": {"name": eff.profile_name, "classification": eff.profile_classification},
        "project": {"state": eff.project_state, "file": ".quoin/runtime.json"},
        "classification": eff.classification,
        "managed_policy": "present" if eff.managed_present else NO_MANAGED_TEXT,
        "providers": providers,
        "models": models,
        "values": values,
        "security": security,
        "roles": [_resolution_dict(ev, r) for r in ev.resolutions.roles],
        "auxiliaries": [_resolution_dict(ev, r) for r in ev.resolutions.auxiliary],
        "findings": findings,
        "digest": digest,
        "digest_note": digest_note,
        "launchable": bool(compile_result is not None and compile_result.launchable),
        "output_location": location,
    }


# ------------------------------------------------------------------ text


def _text(doc: Dict[str, Any]) -> str:
    lines: List[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    def heading(text: str) -> None:
        add()
        add(text)
        add("-" * len(text))

    add("Configuration explanation")
    heading("Profile")
    add("name: %s" % doc["profile"]["name"])
    add("profile classification: %s" % doc["profile"]["classification"])
    heading("Project")
    add("file: %s" % doc["project"]["file"])
    add("classification state: %s" % doc["project"]["state"])
    add("effective classification: %s" % doc["classification"])
    add("managed policy: %s" % doc["managed_policy"])

    heading("Providers")
    for item in doc["providers"]:
        add("%s (native id %s): %s" % (item["id"], item["native_id"], item["status"]))
        add("  kind: %s, endpoint family: %s" % (item["kind"], item["endpoint_family"]))
        add("  base URL: %s" % item["base_url"])
        add("  credential reference: %s" % item["credential_ref"])
        add("  credential variable: %s" % item["credential_env"])
        if item["reason"]:
            add("  excluded: %s (%s)" % (item["reason"], item["reason_text"]))

    heading("Models")
    for item in doc["models"]:
        add("%s: provider %s, model id %s" % (item["name"], item["provider"], item["model_id"]))
        add("  qualification: %s" % item["qualification_state"])
        if item["reason"]:
            add("  reason: %s (%s)" % (item["reason"], item["reason_text"]))
        if item["probed_at"]:
            add("  probed at: %s" % item["probed_at"])
        if item["record"]:
            add("  record: %s" % item["record"])

    heading("Effective values")
    for key, item in doc["values"].items():
        add("%s = %s (origin: %s)" % (key, json.dumps(item["value"], ensure_ascii=False), ", ".join(item["origin"])))

    heading("Security merge result")
    sec = doc["security"]
    add("effective providers: %s" % (", ".join(sec["effective_providers"]) or "none"))
    if sec["excluded_providers"]:
        add(
            "excluded providers: %s"
            % ", ".join("%s (%s)" % (pid, reason) for pid, reason in sec["excluded_providers"].items())
        )
    add("allowed hosts: %s" % (", ".join(sec["allowed_hosts"]) or "none"))
    add("denied hosts: %s" % (", ".join(sec["denied_hosts"]) or "none"))
    for note in sec["notes"]:
        add("note: %s" % note)

    def role_lines(items: List[Dict[str, Any]]) -> None:
        for item in items:
            add("%s -> %s (%s)" % (item["role"], item["model"], item["status"]))
            add("  provider: %s (native %s)" % (item["provider"], item["native_provider"]))
            add("  native reference: %s" % item["native_model"])
            add("  chosen by: %s (%s), origin %s" % (item["reason"], item["reason_text"], item["origin"]))
            if item["block_reason"]:
                add("  blocked or unqualified: %s (%s)" % (item["block_reason"], item["block_text"]))
            add("  qualification: %s" % item["qualification_state"])
            if item["effort"] is not None:
                add("  effort: %s (origin %s)" % (item["effort"], item["effort_origin"]))
            if item["variant"]:
                add("  emitted variant: %s" % item["variant"])
            if item["diagnostic_text"]:
                add("  effort not applied: %s" % item["diagnostic_text"])
            if item["note"]:
                add("  note: %s" % item["note"])

    heading("Role resolutions")
    role_lines(doc["roles"])
    heading("Auxiliary resolutions")
    role_lines(doc["auxiliaries"])

    heading("Findings")
    for item in doc["findings"]:
        subject = " [%s]" % ", ".join(item["subject"]) if item["subject"] else ""
        add("%s%s: %s%s" % ("blocking " if item["blocking"] else "", item["code"], item["message"], subject))
        if item.get("fix"):
            add("  fix: %s" % item["fix"])
    if not doc["findings"]:
        add("none")

    heading("Result")
    add("digest: %s" % (doc["digest"] if doc["digest"] else "null (%s)" % doc["digest_note"]))
    add("launchable: %s" % ("true" if doc["launchable"] else "false"))
    add("compiled output location: %s" % (doc["output_location"] or "not applicable"))
    return "\n".join(lines) + "\n"


def render(
    ev: compiler.Evaluation,
    *,
    redact: bool,
    as_json: bool,
    compile_result: Any = AUTO,
    output_dir: Optional[Path] = None,
    gate_failure: Optional[str] = None,
) -> str:
    doc = explain_document(
        ev, redact=redact, compile_result=compile_result, output_dir=output_dir,
        gate_failure=gate_failure,
    )
    if as_json:
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    return _text(doc)
