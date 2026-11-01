"""Platform gate for the Stream Deck component. Pure: no I/O."""
import platform as _platform
import sys
from dataclasses import dataclass


class UnsupportedPlatform(Exception):
    pass


@dataclass(frozen=True)
class PlatformInfo:
    system: str
    macos: str
    machine: str
    supported: bool


def detect(sys_platform=None, mac_ver=None, machine=None):
    """Describe the host. Callers inject the three probes in tests."""
    sys_platform = sys.platform if sys_platform is None else sys_platform
    mac_ver = _platform.mac_ver if mac_ver is None else mac_ver
    machine = _platform.machine if machine is None else machine
    if sys_platform == "darwin":
        version = mac_ver()[0] or ""
        return PlatformInfo("darwin", version, machine(), True)
    system = {"win32": "windows"}.get(sys_platform, sys_platform)
    return PlatformInfo(system, "", machine(), False)


def require_macos(info):
    if not info.supported:
        raise UnsupportedPlatform("quoin streamdeck: macOS only (found %s)" % info.system)
    return info
