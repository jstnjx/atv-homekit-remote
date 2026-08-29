from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import Button, ButtonState, HDSCloseReason, HDSStatus, TargetCategory, TargetOperation
from .version import __version__

if TYPE_CHECKING:
    from .remote import AppleTVSiriRemote, ButtonConfiguration, RemoteConfig, TargetConfiguration

__all__ = [
    "AppleTVSiriRemote",
    "Button",
    "ButtonConfiguration",
    "ButtonState",
    "HDSCloseReason",
    "HDSStatus",
    "RemoteConfig",
    "TargetCategory",
    "TargetConfiguration",
    "TargetOperation",
    "__version__",
]


def __getattr__(name: str) -> Any:
    if name in {"AppleTVSiriRemote", "ButtonConfiguration", "RemoteConfig", "TargetConfiguration"}:
        from . import remote

        return getattr(remote, name)
    raise AttributeError(name)
