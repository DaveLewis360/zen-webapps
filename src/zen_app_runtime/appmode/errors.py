"""Error type shared by the app-mode modules."""

from __future__ import annotations


class AppModeError(Exception):
    """Raised for invalid app-mode input or an unsafe/unsupported environment.

    ``code`` names the kind of failure for programs (``zen-app … --json``):
    exists, notFound, running, confirm, badId, badName, badUrl, badHost,
    badColor, badIcon, badSource, noZen, io, invalid.
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code
