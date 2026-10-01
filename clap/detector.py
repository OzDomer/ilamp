"""
detector.py - turn audio blocks into clap events. No microphone, no network.

A clap is a sharp spike from quiet that dies fast. Feed the detector
20 ms blocks of samples (floats, -1..1) and it returns the events each
block produced: "clap" for a single clap, and "double" when two claps
landed 0.2-0.8 s apart.

Four rules, each guarding against one kind of false alarm:

  1. ONSET      the block's peak is far above the background level, and above
                an absolute floor            (hiss in a silent room)
  2. FROM QUIET the blocks just before were at background level
                                             (a louder beat inside ongoing sound)
  3. DIES FAST  the level is back near background within ~80 ms
                                             (speech, music, a vacuum cleaner)
  4. REFRACTORY nothing counts for 150 ms after a clap
                                             (the room's echo of the clap)

Plus a music guard: sustained loud sound (the lamp's own speaker, a TV)
suspends detection until it has been quiet again for a while.

Everything is tunable through Settings; `python -m clap --monitor` shows
the live numbers so the thresholds can be set by ear in the real room.
"""

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Settings:
    sample_rate: int = 16_000
    block_ms: int = 20

    # rule 1: onset
    onset_ratio: float = 8.0  # peak must exceed background RMS by this factor...
    floor: float = 0.02  # ...and this absolute level (full scale is 1.0)
    # rule 2: from quiet
    quiet_before_ms: int = 100
    quiet_ratio: float = 3.0  # earlier blocks' RMS must be within this of background
    # rule 3: dies fast
    decay_ms: int = 80
    decay_ratio: float = 3.0  # RMS must be back within this of background in time
    # rule 4: refractory
    refractory_ms: int = 150

    # the double clap
    double_min_s: float = 0.2
    double_max_s: float = 0.8
    cooldown_s: float = 1.0  # after a double, ignore everything for this long

    # the background level: an exponential moving average of block RMS
    background_alpha: float = 0.05
    # the music guard
    loud: float = 0.05  # block RMS above this is "loud"
    loud_hold_s: float = 1.0  # loud for this long -> suspended; quiet for this long -> resumed


class Detector:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or Settings()
        self._time = 0.0  # seconds of audio seen so far
        self._background = 0.0  # RMS of the room; 0 until the first block
        self._recent_rms: deque[float] = deque(
            maxlen=max(1, self.s.quiet_before_ms // self.s.block_ms)
        )
        self._pending_since: float | None = None  # an onset waiting to die down
        self._ignore_until = 0.0  # refractory or cooldown
        self._last_clap: float | None = None  # for the double
        self._loud_since: float | None = None
        self._quiet_since: float | None = None
        self.suspended = False
        self.level = 0.0  # last block's peak, for the monitor

    @property
    def background(self) -> float:
        return self._background

    def feed(self, block: np.ndarray) -> list[str]:
        """Process one block of samples. Returns the events it produced, in order."""
        s = self.s
        now = self._time
        self._time += len(block) / s.sample_rate
        samples = np.asarray(block, dtype=np.float32).ravel()
        peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        self.level = peak

        if self._background == 0.0:
            self._background = max(rms, 1e-4)

        events: list[str] = []
        self._update_guard(rms, now)

        if self._pending_since is not None:
            # rule 3: an onset is a clap only if the level dies down in time
            if rms <= self._background * s.decay_ratio:
                onset = self._pending_since
                self._pending_since = None
                events.extend(self._clap_at(onset))
            elif now - self._pending_since > s.decay_ms / 1000:
                self._pending_since = None  # still loud: sustained sound, not a clap
        elif not self.suspended and now >= self._ignore_until:
            threshold = max(s.floor, self._background * s.onset_ratio)
            quiet_before = all(r <= self._background * s.quiet_ratio for r in self._recent_rms)
            if peak > threshold and quiet_before:  # rules 1 and 2
                self._pending_since = now
                self._ignore_until = now + s.refractory_ms / 1000  # rule 4

        if self._pending_since is None:
            # the background follows the room, but never learns from a clap in progress
            self._background += s.background_alpha * (rms - self._background)
        self._recent_rms.append(rms)
        return events

    def _clap_at(self, onset: float) -> list[str]:
        if self._last_clap is not None and (
            self.s.double_min_s <= onset - self._last_clap <= self.s.double_max_s
        ):
            self._last_clap = None
            self._ignore_until = onset + self.s.cooldown_s
            return ["clap", "double"]
        self._last_clap = onset
        return ["clap"]

    def _update_guard(self, rms: float, now: float) -> None:
        """Sustained loud sound suspends detection; sustained quiet lifts it."""
        hold = self.s.loud_hold_s
        if rms > self.s.loud:
            self._quiet_since = None
            self._loud_since = self._loud_since if self._loud_since is not None else now
            if not self.suspended and now - self._loud_since >= hold:
                self.suspended = True
                self._pending_since = None
                self._last_clap = None
        else:
            self._loud_since = None
            self._quiet_since = self._quiet_since if self._quiet_since is not None else now
            if self.suspended and now - self._quiet_since >= hold:
                self.suspended = False
