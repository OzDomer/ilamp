"""
Tests for clap/detector.py, on synthetic audio.

A clap is a sharp spike from quiet that dies fast. These signals are built
in numpy so every rule can be checked exactly, without a microphone:
noise floor, short decaying bursts (claps), a sustained tone (music).
"""

import numpy as np

from clap.detector import Detector, Settings

SR = 16_000
BLOCK = SR // 50  # 20 ms
rng = np.random.default_rng(7)


def silence(seconds: float, noise: float = 0.002) -> np.ndarray:
    return rng.normal(0, noise, int(SR * seconds)).astype(np.float32)


def clap(amplitude: float = 0.6, length_ms: float = 30, tau_ms: float = 6) -> np.ndarray:
    """A burst of noise that decays exponentially: what a hand clap looks like."""
    n = int(SR * length_ms / 1000)
    t = np.arange(n) / SR * 1000
    return (rng.normal(0, 1, n) * np.exp(-t / tau_ms) * amplitude).astype(np.float32)


def tone(seconds: float, amplitude: float = 0.2, freq: float = 440) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)


def feed(detector: Detector, signal: np.ndarray) -> list[tuple[float, str]]:
    """Run a whole signal through the detector block by block; return (time, event) pairs."""
    out = []
    for i in range(0, len(signal) - BLOCK + 1, BLOCK):
        for event in detector.feed(signal[i : i + BLOCK]):
            out.append((round(i / SR, 2), event))
    return out


def sequence(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts)


def test_silence_produces_nothing():
    assert feed(Detector(), silence(3)) == []


def test_one_clap_is_a_clap_but_not_a_double():
    events = feed(Detector(), sequence(silence(1), clap(), silence(1.5)))
    assert [e for _, e in events] == ["clap"]
    assert abs(events[0][0] - 1.0) < 0.1


def test_two_claps_half_a_second_apart_make_a_double():
    signal = sequence(silence(1), clap(), silence(0.5), clap(), silence(1.5))
    events = [e for _, e in feed(Detector(), signal)]
    assert events == ["clap", "clap", "double"]


def test_claps_too_far_apart_are_two_singles():
    signal = sequence(silence(1), clap(), silence(1.5), clap(), silence(1.5))
    assert [e for _, e in feed(Detector(), signal)] == ["clap", "clap"]


def test_a_claps_echo_is_not_a_second_clap():
    # a quieter burst 80 ms after the clap is the room, not a hand
    signal = sequence(silence(1), clap(), silence(0.05), clap(amplitude=0.2), silence(1.5))
    assert [e for _, e in feed(Detector(), signal)] == ["clap"]


def test_a_sustained_sound_is_not_a_clap_even_with_a_loud_onset():
    # a tone starts abruptly (a spike from quiet) but doesn't die: music, speech, a vacuum
    signal = sequence(silence(1), tone(1.0), silence(1.5))
    assert feed(Detector(), signal) == []


def test_claps_during_music_are_ignored_and_detection_resumes_after():
    d = Detector()
    assert feed(d, sequence(silence(1), tone(2.0))) == []
    assert d.suspended is True  # sustained audio: the guard is up
    # claps on top of the music go nowhere
    assert feed(d, sequence(tone(0.3), clap(), tone(0.5), clap(), tone(0.5))) == []
    # music stops, the guard drops, claps count again
    assert feed(d, silence(2)) == []
    assert d.suspended is False
    events = [e for _, e in feed(d, sequence(clap(), silence(0.5), clap(), silence(1.5)))]
    assert events == ["clap", "clap", "double"]


def test_after_a_double_there_is_a_cooldown():
    # three claps 0.5 s apart: a double, then the third is swallowed by the cooldown
    signal = sequence(silence(1), clap(), silence(0.5), clap(), silence(0.5), clap(), silence(1.5))
    assert [e for _, e in feed(Detector(), signal)] == ["clap", "clap", "double"]


def test_a_quiet_tap_below_the_floor_is_ignored():
    signal = sequence(silence(1), clap(amplitude=0.01), silence(1.5))
    assert feed(Detector(Settings(floor=0.05)), signal) == []


def test_settings_are_tunable():
    # with a very short double window, our 0.5 s pair is two singles
    signal = sequence(silence(1), clap(), silence(0.5), clap(), silence(1.5))
    events = feed(Detector(Settings(double_max_s=0.3)), signal)
    assert [e for _, e in events] == ["clap", "clap"]
