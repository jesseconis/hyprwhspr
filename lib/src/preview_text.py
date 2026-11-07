"""Live-preview text that can carry its finalized-prefix boundary.

Realtime backends publish preview text through a plain ``callback(text)``
chain (backend -> WhisperManager -> main -> MicOSDRunner).  ``PreviewText`` is
a ``str`` so every existing consumer keeps working unchanged, while consumers
that understand stability can read ``committed_length``: the number of leading
characters the server has finalized and will not revise.  Everything after it
is a provisional partial that may still be rewritten.
"""

from __future__ import annotations

from typing import Optional


class PreviewText(str):
    """A preview string annotated with its finalized-prefix length."""

    committed_length: Optional[int]

    def __new__(cls, text: str, committed_length: Optional[int] = None):
        instance = super().__new__(cls, text)
        if committed_length is not None:
            committed_length = max(0, min(len(instance), int(committed_length)))
        instance.committed_length = committed_length
        return instance


def committed_length_of(text) -> Optional[int]:
    """Return the finalized-prefix length a preview string carries, if any."""
    value = getattr(text, "committed_length", None)
    return value if isinstance(value, int) else None
