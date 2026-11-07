"""Stability cues for the wrapped spectrum transcript preview.

Two questions matter while reading a live transcript:

* *Is this text final?*  Realtime servers stream a provisional partial that is
  later replaced by a final transcription.  When the backend reports the
  finalized-prefix boundary (``committed_length``) words are classified
  exactly; otherwise a word is treated as settled once it has stopped changing
  for a moment.

* *Did something change away from where I'm reading?*  The reader's eye sits
  at the end of the transcript, where words are constantly appended and
  revised.  Revisions further back (a final replacing its partial, a
  re-punctuated sentence) reflow lines and are easy to miss.  Every update is
  diffed against the previous one; changed words get a highlight that decays
  over time and whose strength grows with distance from the end, so routine
  edits at the cursor stay quiet while distant rewrites stand out.

This module is pure (no GTK/Cairo) so it can be unit-tested directly.  It also
owns the small sidecar file format that carries ``committed_length`` from the
hyprwhspr process to the OSD daemon next to the plain-text preview file, which
other consumers (e.g. the GNOME extension) keep reading unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
import zlib
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Sidecar metadata: committed boundary for the plain-text preview file
# ---------------------------------------------------------------------------

META_SUFFIX = "_meta"


def meta_path_for(preview_path: Path) -> Path:
    """Sidecar path holding stability metadata for ``preview_path``."""
    return preview_path.with_name(preview_path.name + META_SUFFIX)


def _checksum(text: str) -> int:
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def encode_meta(text: str, committed_length: int) -> str:
    """Serialize the boundary, bound to ``text`` by length and checksum."""
    return json.dumps(
        {
            "v": 1,
            "committed": int(committed_length),
            "length": len(text),
            "crc32": _checksum(text),
        },
        separators=(",", ":"),
    )


def decode_meta(text: str, raw: Optional[str]) -> Optional[int]:
    """Return the committed length if ``raw`` describes exactly ``text``.

    The writer replaces the sidecar and the text file separately, so a reader
    can observe one without the other.  Metadata that does not match the text
    it was read with is ignored rather than applied to the wrong words.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if (
            data.get("v") != 1
            or data.get("length") != len(text)
            or data.get("crc32") != _checksum(text)
        ):
            return None
        committed = int(data["committed"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return max(0, min(len(text), committed))


# ---------------------------------------------------------------------------
# Change tracking
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\S+")


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _smoothstep(value: float) -> float:
    value = _clamp(value)
    return value * value * (3.0 - 2.0 * value)


@dataclass(frozen=True)
class WordCue:
    """One word of the preview with its presentation cues for this frame."""

    text: str
    start: int
    breaks_before: int  # newlines between the previous word and this one
    settled: float  # 0.0 = provisional/live, 1.0 = final
    highlight: float  # 0.0..1.0 decaying "changed away from the cursor" cue


@dataclass
class _WordState:
    text: str
    start: int
    breaks_before: int
    changed_at: float
    change_strength: float
    committed: bool
    committed_at: float


def _tokenize(text: str) -> List[Tuple[str, int, int]]:
    tokens = []
    previous_end = 0
    for match in _TOKEN_RE.finditer(text):
        breaks = text.count("\n", previous_end, match.start())
        tokens.append((match.group(0), match.start(), breaks))
        previous_end = match.end()
    return tokens


def _lcs_pairs(old: Sequence[str], new: Sequence[str]) -> List[Tuple[int, int]]:
    """Longest-common-subsequence index pairs (duplicate safe)."""
    rows, cols = len(old), len(new)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        row, below = table[i], table[i + 1]
        old_word = old[i]
        for j in range(cols - 1, -1, -1):
            if old_word == new[j]:
                row[j] = below[j + 1] + 1
            else:
                row[j] = below[j] if below[j] >= row[j + 1] else row[j + 1]
    pairs = []
    i = j = 0
    while i < rows and j < cols:
        if old[i] == new[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


class TranscriptChangeTracker:
    """Diff successive preview texts and derive per-word stability cues."""

    # Changes within this many words of the end are "at the cursor" and stay
    # unhighlighted; strength then ramps to full by FAR_WORDS.
    NEAR_WORDS = 3
    FAR_WORDS = 10
    # Keep the highlight at full strength briefly so it registers in
    # peripheral vision, then decay smoothly.
    HIGHLIGHT_HOLD_SECONDS = 0.18
    # Live -> final colour transition when a word is confirmed.
    SETTLE_SECONDS = 0.45
    # Without a backend-reported boundary, a word counts as settled after it
    # has been unchanged this long.
    UNKNOWN_SETTLE_DELAY_SECONDS = 1.2
    # Bound the quadratic LCS on the changed middle region; beyond this the
    # whole region is simply treated as changed.
    MAX_DIFF_CELLS = 60_000

    def __init__(
        self,
        highlight_seconds: float = 1.8,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.highlight_seconds = max(0.0, float(highlight_seconds))
        self.enabled = bool(enabled)
        self._clock = clock
        self._words: List[_WordState] = []
        self._text: Optional[str] = None
        self._committed_length: Optional[int] = None
        self._has_boundary = False

    # -- configuration ----------------------------------------------------

    def configure(self, highlight_seconds: float, enabled: bool):
        self.highlight_seconds = max(0.0, float(highlight_seconds))
        self.enabled = bool(enabled)

    # -- updates ----------------------------------------------------------

    def reset(self):
        self._words = []
        self._text = None
        self._committed_length = None
        self._has_boundary = False

    def update(
        self,
        text: str,
        committed_length: Optional[int] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Apply a new preview; return True if anything visible changed."""
        now = self._clock() if now is None else now
        text = text or ""
        if not text.strip():
            changed = bool(self._words)
            self.reset()
            return changed

        if committed_length is not None:
            committed_length = max(0, min(len(text), int(committed_length)))
            self._has_boundary = True
        if text == self._text and committed_length == self._committed_length:
            return False

        tokens = _tokenize(text)
        old_words = self._words
        old_texts = [word.text for word in old_words]
        new_texts = [token[0] for token in tokens]
        matches = self._match(old_texts, new_texts)

        # A pure append (nothing removed, everything new sits after the old
        # text) is normal dictation, not a revision, wherever it lands.
        pure_append = (
            len(new_texts) >= len(old_texts)
            and new_texts[: len(old_texts)] == old_texts
        )

        last_index = len(tokens) - 1
        words: List[_WordState] = []
        for index, (word_text, start, breaks) in enumerate(tokens):
            committed = self._is_committed(start, committed_length, matches, index)
            previous = matches.get(index)
            if previous is not None:
                old = old_words[previous]
                committed_at = old.committed_at
                if committed and not old.committed:
                    committed_at = now
                words.append(
                    _WordState(
                        word_text,
                        start,
                        breaks,
                        old.changed_at,
                        old.change_strength,
                        committed,
                        committed_at,
                    )
                )
                continue

            strength = 0.0
            if not pure_append and old_words:
                strength = self._distance_strength(last_index - index)
            words.append(
                _WordState(
                    word_text,
                    start,
                    breaks,
                    now,
                    strength,
                    committed,
                    now,
                )
            )

        self._words = words
        self._text = text
        self._committed_length = committed_length
        return True

    def _match(self, old: Sequence[str], new: Sequence[str]) -> dict:
        """Map new word index -> old word index for unchanged words."""
        prefix = 0
        limit = min(len(old), len(new))
        while prefix < limit and old[prefix] == new[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < limit - prefix
            and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]
        ):
            suffix += 1

        matches = {index: index for index in range(prefix)}
        old_mid = old[prefix : len(old) - suffix]
        new_mid = new[prefix : len(new) - suffix]
        if old_mid and new_mid and len(old_mid) * len(new_mid) <= self.MAX_DIFF_CELLS:
            for old_index, new_index in _lcs_pairs(old_mid, new_mid):
                matches[prefix + new_index] = prefix + old_index
        for offset in range(suffix):
            matches[len(new) - 1 - offset] = len(old) - 1 - offset
        return matches

    def _is_committed(self, start, committed_length, matches, index) -> bool:
        if committed_length is not None:
            return start < committed_length
        if self._has_boundary:
            # Boundary temporarily unavailable (e.g. a torn sidecar read):
            # keep what was known rather than flashing every word live.
            previous = matches.get(index)
            return previous is not None and self._words[previous].committed
        return False

    def _distance_strength(self, distance: int) -> float:
        if distance < self.NEAR_WORDS:
            return 0.0
        span = max(1, self.FAR_WORDS - self.NEAR_WORDS)
        return _smoothstep((distance - self.NEAR_WORDS + 1) / span)

    # -- per-frame queries ------------------------------------------------

    def _highlight(self, word: _WordState, now: float) -> float:
        if word.change_strength <= 0.0 or self.highlight_seconds <= 0.0:
            return 0.0
        age = now - word.changed_at
        hold = min(self.HIGHLIGHT_HOLD_SECONDS, self.highlight_seconds)
        if age <= hold:
            return word.change_strength
        fade = self.highlight_seconds - hold
        if fade <= 0.0:
            return 0.0
        return word.change_strength * (1.0 - _smoothstep((age - hold) / fade))

    def _settled(self, word: _WordState, now: float) -> float:
        if self._has_boundary:
            if not word.committed:
                return 0.0
            return _smoothstep((now - word.committed_at) / self.SETTLE_SECONDS)
        age = now - word.changed_at - self.UNKNOWN_SETTLE_DELAY_SECONDS
        return _smoothstep(age / self.SETTLE_SECONDS)

    def cues(self, now: Optional[float] = None) -> Tuple[WordCue, ...]:
        now = self._clock() if now is None else now
        if not self.enabled:
            return tuple(
                WordCue(word.text, word.start, word.breaks_before, 1.0, 0.0)
                for word in self._words
            )
        return tuple(
            WordCue(
                word.text,
                word.start,
                word.breaks_before,
                self._settled(word, now),
                self._highlight(word, now),
            )
            for word in self._words
        )

    @property
    def has_boundary(self) -> bool:
        return self._has_boundary
