"""One-line "installed component is older than this package" hint for `quoin install`.

Read-only: no lock, no validation, no writes, and the engine is never imported.
"""
from . import COMPONENT_VERSION, manifest, paths as paths_mod, payload_fingerprint


def maybe_print(*, environ=None, out=print):
    try:
        data = manifest.load_any(paths_mod.resolve(environ=environ).manifest)
        if not isinstance(data, dict):
            return False
        installed = data.get("component_version")
        fingerprint = data.get("payload_fingerprint")
        if installed == COMPONENT_VERSION and fingerprint == payload_fingerprint():
            return False
        out("quoin: the installed Stream Deck component (%s) differs from this package (%s); run "
            "`quoin streamdeck upgrade`" % (installed, COMPONENT_VERSION))
        return True
    except Exception:
        return False
