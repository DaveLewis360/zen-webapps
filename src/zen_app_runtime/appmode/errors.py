"""Error type shared by the app-mode modules."""

from __future__ import annotations


class AppModeError(Exception):
    """Raised for invalid app-mode input or an unsafe/unsupported environment."""
