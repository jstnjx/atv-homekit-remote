"""Pure-Python Apple TV HomeKit Target Control + Siri voice library."""
from __future__ import annotations

from .constants import Button, ButtonState, TargetCategory

__version__ = "0.1.0"
__all__ = ["AppleTVSiriRemote", "RemoteConfig", "SiriSession", "Button", "ButtonState", "TargetCategory"]


def __getattr__(name: str):
    if name in {"AppleTVSiriRemote", "RemoteConfig"}:
        from .remote import AppleTVSiriRemote, RemoteConfig
        return {"AppleTVSiriRemote": AppleTVSiriRemote, "RemoteConfig": RemoteConfig}[name]
    if name == "SiriSession":
        from .audio import SiriSession
        return SiriSession
    raise AttributeError(name)
