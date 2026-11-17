"""Profile/context domain service with session ownership checks."""

from .contracts import ProfileError
from .manager import ProfileManager

__all__ = ["ProfileError", "ProfileManager"]
