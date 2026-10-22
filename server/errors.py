"""The one refusal type, in its own module so every layer can raise it.

``intake.py`` has to be able to say "415, that file type is not accepted" without importing
the app, and the app has to be able to turn that into a response without importing the
intake gate's internals. A shared exception is the whole of the coupling between them.
"""

from __future__ import annotations


class ApiError(Exception):
    """A refusal the visitor is allowed to read.

    ``message`` is written for a stranger on a public page: a plain sentence, no jargon, no
    internal path, no library name, and never a traceback. ``code`` is the machine-readable
    half and is what tests assert on.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiError({self.status}, {self.code!r}, {self.message!r})"
