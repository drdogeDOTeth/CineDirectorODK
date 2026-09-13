# Copyright Roundtree. All Rights Reserved.
"""
Audio to mouth shapes.

Port of CineLipsync.cpp. Reads a wav (converting other formats through ffmpeg),
measures band energy per frame, and scores four competing vowel shapes plus
consonant hints. These are acoustic cues, not a transcript, so the scoring stays
deliberately conservative and the shapes are smoothed with separate attack and
release so speech reads as speech rather than as a flapping jaw.

Band power at a fixed frequency is what the C++ computes with Goertzel, which is
|X(f)|^2 / N. That is a single DFT bin, so with numpy present it is one dot
product per band; without numpy the classic Goertzel recurrence runs instead.
Both produce the same number.
"""

import math
import os
import subprocess
import tempfile
import wave

try:
    import numpy as _np
except Exception:                       # noqa: BLE001 - numpy is optional
    _np = None

DEFAULT_FPS = 30.0

# Band centres, in Hz. Four bands so A/I/U/O pull apart more clearly than they do
# with a single brightness measure.
LOW_FREQS = (250.0, 450.0)
MID_FREQS = (900.0, 1400.0)
HIGH_FREQS = (2200.0, 3200.0)
AIR_FREQS = (5000.0, 7000.0)
ALL_FREQS = LOW_FREQS + MID_FREQS + HIGH_FREQS + AIR_FREQS

# Softmax sharpness for the vowel contest. Higher makes one vowel lead more
# decisively instead of every frame settling into an average shape.
VOWEL_SHARPNESS = 2.75

# Attack and release per channel. Both rounded shapes release fast on purpose:
# syllable tails go dark, which scores as U, so a slow release leaves every "oo"
# hanging past the end of its word.
SMOOTHING = {
    "jaw": (0.70, 0.58),
    "wide": (0.55, 0.50),
    "pucker": (0.55, 0.72),
    "funnel": (0.58, 0.78),
    "close": (0.90, 0.68),
    "sibilant": (0.55, 0.45),
    "fv": (0.72, 0.48),
    "l": (0.58, 0.58),
    "th": (0.68, 0.42),
    "ch": (0.82, 0.44),
}

CHANNELS = tuple(SMOOTHING.keys())


class LipsyncError(Exception):
    """Raised when audio cannot be read, with a message fit for the panel."""


class VisemeFrame(object):
    """One frame of mouth-shape weights derived from audio."""

    __slots__ = ("jaw", "wide", "pucker", "funnel", "close", "sibilant",
                 "fv", "l", "th", "ch", "confidence")

    def __init__(self):
        self.jaw = 0.0
        self.wide = 0.0
        self.pucker = 0.0      # rounded OO/UW
        self.funnel = 0.0      # open-round OH
        self.close = 0.0       # consonant closure (M/B/P)
        self.sibilant = 0.0    # S/SH hiss: teeth together, slightly wide
        self.fv = 0.0
        self.l = 0.0
        self.th = 0.0
        self.ch = 0.0
        self.confidence = 0.0

    def scale_all(self, factor):
        for name in CHANNELS:
            setattr(self, name, getattr(self, name) * factor)
        self.confidence *= factor

    def clear_mouth(self):
        self.jaw = self.wide = self.pucker = self.funnel = 0.0
        self.sibilant = self.fv = self.l = self.th = self.ch = 0.0


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


# ---------------------------------------------------------------------------
# Audio loading
# ---------------------------------------------------------------------------

def _convert_with_ffmpeg(path):
    """Convert any audio file to a temporary 16-bit wav. Returns the new path."""
    from . import render

    ffmpeg = render.find_ffmpeg()
    if not ffmpeg:
        raise LipsyncError(
            "Only .wav can be read directly. Converting other formats needs "
            "ffmpeg, which was not found. Install it with "
            "\"winget install Gyan.FFmpeg\".")

    out_dir = os.path.join(tempfile.gettempdir(), "CineDirectorFace")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir,
                            os.path.splitext(os.path.basename(path))[0] + ".wav")
    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", path, "-acodec", "pcm_s16le", out_path],
            capture_output=True, text=True, timeout=300)
    except Exception as error:          # noqa: BLE001
        raise LipsyncError("ffmpeg could not be run: %s" % error)
    if completed.returncode != 0 or not os.path.isfile(out_path):
        raise LipsyncError("ffmpeg failed to convert the audio: %s"
                           % (completed.stderr or "").strip()[:400])
    return out_path


def load_mono(path):
    """
    Read an audio file as mono floats in -1..1.
    Returns (samples, sample_rate, wav_path). Non-wav input is converted first,
    and the converted path comes back so the same file can be imported for
    Sequencer playback.
    """
    if not path or not os.path.isfile(path):
        raise LipsyncError("Audio file not found: %s" % path)

    wav_path = path
    if os.path.splitext(path)[1].lower() != ".wav":
        wav_path = _convert_with_ffmpeg(path)

    try:
        with wave.open(wav_path, "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            count = handle.getnframes()
            raw = handle.readframes(count)
    except Exception as error:          # noqa: BLE001
        raise LipsyncError("Could not read '%s' as a wav: %s"
                           % (os.path.basename(wav_path), error))

    if count == 0 or rate <= 0:
        raise LipsyncError("'%s' contains no audio." % os.path.basename(wav_path))

    samples = _decode_pcm(raw, width, channels, count)
    return samples, rate, wav_path


def _decode_pcm(raw, width, channels, count):
    """PCM bytes to a mono float list, averaging channels."""
    if _np is not None:
        dtype = {1: _np.uint8, 2: _np.int16, 4: _np.int32}.get(width)
        if dtype is not None:
            data = _np.frombuffer(raw, dtype=dtype)
            if width == 1:
                values = (data.astype(_np.float32) - 128.0) / 128.0
            elif width == 2:
                values = data.astype(_np.float32) / 32768.0
            else:
                values = data.astype(_np.float32) / 2147483648.0
            usable = (len(values) // channels) * channels
            values = values[:usable].reshape(-1, channels)
            return values.mean(axis=1)
        if width == 3:
            return _decode_24bit(raw, channels)
        raise LipsyncError("Unsupported wav sample width: %d bytes." % width)

    return _decode_pcm_slow(raw, width, channels, count)


def _decode_24bit(raw, channels):
    total = len(raw) // 3
    values = []
    for i in range(total):
        chunk = raw[i * 3:i * 3 + 3]
        value = chunk[0] | (chunk[1] << 8) | (chunk[2] << 16)
        if value & 0x800000:
            value -= 0x1000000
        values.append(value / 8388608.0)
    frames = len(values) // channels
    mono = [sum(values[f * channels:(f + 1) * channels]) / channels
            for f in range(frames)]
    return _np.asarray(mono, dtype=_np.float32) if _np is not None else mono


def _decode_pcm_slow(raw, width, channels, count):
    mono = []
    stride = width * channels
    for frame in range(count):
        base = frame * stride
        if base + stride > len(raw):
            break
        total = 0.0
        for channel in range(channels):
            offset = base + channel * width
            if width == 1:
                total += (raw[offset] - 128) / 128.0
            elif width == 2:
                value = int.from_bytes(raw[offset:offset + 2], "little", signed=True)
                total += value / 32768.0
            elif width == 3:
                value = int.from_bytes(raw[offset:offset + 3], "little", signed=True)
                total += value / 8388608.0
            elif width == 4:
                value = int.from_bytes(raw[offset:offset + 4], "little", signed=True)
                total += value / 2147483648.0
            else:
                raise LipsyncError("Unsupported wav sample width: %d bytes." % width)
        mono.append(total / channels)
    return mono


# ---------------------------------------------------------------------------
# Spectral measurement
# ---------------------------------------------------------------------------

class _BandMeter(object):
    """Band power for a fixed window length, vectorised when numpy is present."""

    def __init__(self, rate, freqs):
        self.rate = rate
        self.freqs = tuple(freqs)
        self._matrices = {}

    def _matrix(self, length):
        matrix = self._matrices.get(length)
        if matrix is None:
            n = _np.arange(length, dtype=_np.float64)
            angles = _np.outer(
                _np.asarray(self.freqs, dtype=_np.float64) * (2.0 * math.pi / self.rate),
                n)
            matrix = _np.exp(-1j * angles)
            self._matrices[length] = matrix
        return matrix

    def powers(self, window):
        """Power at each frequency, same normalisation as Goertzel."""
        length = len(window)
        if length == 0:
            return [0.0] * len(self.freqs)

        if _np is not None:
            block = _np.asarray(window, dtype=_np.float64)
            spectrum = self._matrix(length) @ block
            return (_np.abs(spectrum) ** 2) / length

        return [_goertzel(window, self.rate, freq) for freq in self.freqs]


def _goertzel(samples, rate, freq):
    omega = 2.0 * math.pi * freq / rate
    coeff = 2.0 * math.cos(omega)
    s1 = 0.0
    s2 = 0.0
    for value in samples:
        s0 = value + coeff * s1 - s2
        s2 = s1
        s1 = s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return max(0.0, power) / max(1, len(samples))


def _rms(window):
    if not len(window):
        return 0.0
    if _np is not None:
        block = _np.asarray(window, dtype=_np.float64)
        return float(_np.sqrt(_np.mean(block * block)))
    total = sum(v * v for v in window)
    return math.sqrt(total / len(window))


def _zero_crossings(window):
    if len(window) < 2:
        return 0
    if _np is not None:
        block = _np.asarray(window)
        return int(_np.count_nonzero(_np.diff(_np.signbit(block))))
    count = 0
    for i in range(1, len(window)):
        if (window[i - 1] < 0.0) != (window[i] < 0.0):
            count += 1
    return count


def smooth(values, attack, release):
    """One-pole smoothing with separate rise and fall rates. Higher is faster."""
    previous = 0.0
    for i, value in enumerate(values):
        alpha = attack if value > previous else release
        previous = previous + (value - previous) * alpha
        values[i] = previous
    return values


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return 0.0
    index = int(len(sorted_values) * fraction)
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(samples, rate, fps=DEFAULT_FPS):
    """Audio to a list of VisemeFrame, one per output frame."""
    hop = max(1, int(round(rate / float(fps))))
    total = len(samples)
    frame_count = max(1, total // hop)
    frames = [VisemeFrame() for _ in range(frame_count)]

    meter = _BandMeter(rate, ALL_FREQS)

    energy = [0.0] * frame_count
    low_ratio = [0.0] * frame_count
    mid_ratio = [0.0] * frame_count
    high_ratio = [0.0] * frame_count
    sibilance = [0.0] * frame_count
    noisiness = [0.0] * frame_count

    peak_rms = 0.0
    window_len = hop * 2

    for i in range(frame_count):
        start = i * hop
        stop = min(start + window_len, total)
        window = samples[start:stop]
        if not len(window):
            continue

        value = _rms(window)
        energy[i] = value
        peak_rms = max(peak_rms, value)

        powers = meter.powers(window)
        low = float(powers[0] + powers[1])
        mid = float(powers[2] + powers[3])
        high = float(powers[4] + powers[5])
        air = float(powers[6] + powers[7])
        span = low + mid + high + air + 1e-9

        low_ratio[i] = low / span
        mid_ratio[i] = mid / span
        high_ratio[i] = (high + air * 0.5) / span
        sibilance[i] = air / (low + mid + 1e-9)
        noisiness[i] = _zero_crossings(window) / max(1, len(window) - 1)

    # A 95th-percentile normaliser, so a few peaks do not squash the whole take.
    ordered = sorted(energy)
    noise_floor = _percentile(ordered, 0.20)
    norm_peak = max(_percentile(ordered, 0.95), peak_rms * 0.55, 1e-9)
    if noise_floor > norm_peak * 0.55:
        # The noise-floor term only means anything when the clip contains silence
        # to measure it in. On a clip with no pauses, a tightly trimmed word or a
        # steady hum, the 20th percentile sits at the speech level itself and
        # 2.5x it would gate the entire take shut, so use the peak floor alone.
        speech_gate = norm_peak * 0.07
    else:
        speech_gate = max(norm_peak * 0.07, noise_floor * 2.5)

    for i in range(frame_count):
        frame = frames[i]
        raw_level = _clamp(energy[i] / norm_peak, 0.0, 1.35)
        level = _clamp(math.pow(raw_level, 0.55) * 1.18, 0.0, 1.0)
        gate = energy[i] / max(speech_gate, 1e-6)

        # Below the speech floor this is silence; the closure pass handles it.
        if gate < 0.45:
            continue

        # Marginal frames still get full shape analysis, so quiet speech is not
        # reduced to a jaw opening and closing.
        open_amount = level * _clamp(gate, 0.0, 1.0) * 0.9 if gate < 1.0 else level

        low = low_ratio[i]
        mid = mid_ratio[i]
        high = high_ratio[i]
        noise = noisiness[i]
        attack = max(0.0, (energy[i] - energy[i - 1]) / norm_peak) if i > 0 else 0.0

        frame.confidence = _clamp((gate - 0.45) / 1.35, 0.0, 1.0)

        unvoiced = _clamp((noise - 0.035) * 5.5, 0.0, 1.0)
        frame.fv = _clamp((mid * 0.65 + high * 0.55 + unvoiced * 0.35
                           - sibilance[i] * 0.08 - 0.20) * open_amount * 1.2, 0.0, 0.82)
        frame.th = _clamp((unvoiced * 0.65 + high * 0.35 - sibilance[i] * 0.12 - 0.18)
                          * open_amount, 0.0, 0.72)
        frame.ch = _clamp((attack * 1.8 + min(sibilance[i], 1.5) * 0.28 - 0.18)
                          * open_amount, 0.0, 0.82)
        frame.l = _clamp((mid * 0.9 + low * 0.45 - unvoiced * 0.55 - 0.24)
                         * open_amount, 0.0, 0.72)

        # Competing vowel scores from formant-ish band cues.
        #   A (ah): open mid, neither bright nor dark.
        #   I/E (ee/eh): high-band energy.
        #   U (oo): low-band, dark.
        #   O (oh): a balanced round, kept selective so it is not the filler vowel.
        score_a = _clamp(mid * 2.05 + low * 0.5 - high * 0.9 + 0.10, 0.0, 1.0)
        score_i = _clamp(high * 2.75 - low * 1.05 - mid * 0.12 - 0.10, 0.0, 1.0)
        score_u = _clamp(low * 2.95 - high * 1.4 - mid * 0.28 - 0.16, 0.0, 1.0)
        score_o = _clamp(low * 1.15 + mid * 1.20 - high * 1.40
                         - abs(low - mid) * 0.75 - 0.14, 0.0, 1.0)

        # Pure mid is A territory; a very dark low is U territory.
        if mid > low * 1.30:
            score_o *= 0.62
        if low > 0.48 and low > mid * 1.15:
            score_o *= 0.55
            score_u = max(score_u, score_o * 1.1)

        if sibilance[i] > 1.15 and level > 0.08:
            frame.sibilant = _clamp((sibilance[i] - 1.15) * 0.85, 0.0, 1.0)
            # S and SH show teeth with slight width, not a big open A.
            score_i = max(score_i, frame.sibilant)
            score_a *= 0.28
            score_u *= 0.25
            score_o *= 0.22

        weight_a = math.pow(max(score_a, 1e-4), VOWEL_SHARPNESS)
        weight_i = math.pow(max(score_i, 1e-4), VOWEL_SHARPNESS)
        weight_u = math.pow(max(score_u, 1e-4), VOWEL_SHARPNESS)
        weight_o = math.pow(max(score_o, 1e-4), VOWEL_SHARPNESS)
        weight_sum = weight_a + weight_i + weight_u + weight_o

        shape_amount = _clamp(open_amount * 1.28, 0.0, 1.0)
        frame.jaw = shape_amount * (weight_a / weight_sum)
        frame.wide = shape_amount * (weight_i / weight_sum)
        frame.pucker = shape_amount * (weight_u / weight_sum)
        frame.funnel = shape_amount * (weight_o / weight_sum)

        # Weak spectral contrast: seed A, I and U rather than inject a synthetic O,
        # which would otherwise become the default shape for unclear speech.
        best_score = max(score_a, score_i, score_u, score_o)
        if best_score < 0.20 and open_amount > 0.16:
            phase = math.fmod(i * 0.37, 3.0)
            if phase < 1.0:
                frame.jaw = max(frame.jaw, open_amount * 0.85)
            elif phase < 2.0:
                frame.wide = max(frame.wide, open_amount * 0.8)
                frame.jaw = max(frame.jaw, open_amount * 0.2)
            else:
                frame.pucker = max(frame.pucker, open_amount * 0.85)

        # A dip surrounded by speech is a consonant closure, not a pause.
        if 1 < i < frame_count - 2:
            around = (energy[i - 2] + energy[i - 1] + energy[i + 1] + energy[i + 2]) \
                / (4.0 * norm_peak)
            if around > 0.11 and raw_level < around * 0.42:
                frame.clear_mouth()
                frame.close = 1.0

    _smooth_channels(frames)

    # Mild expansion so mid-syllable shapes read bigger on camera.
    for frame in frames:
        for name in ("jaw", "wide", "pucker", "funnel"):
            value = getattr(frame, name)
            setattr(frame, name, _clamp(math.pow(value, 0.72) * 1.12, 0.0, 1.0))

    # Fade out under the gate and shut the lips through real silence.
    for i, frame in enumerate(frames):
        gate = energy[i] / max(speech_gate, 1e-6)
        if gate < 0.42:
            scale = 0.0 if gate < 0.18 else _clamp(gate / 0.42, 0.0, 1.0)
            frame.scale_all(scale)
            if gate < 0.18:
                frame.close = max(frame.close, 0.85)

    return frames


def _smooth_channels(frames):
    for name, (attack, release) in SMOOTHING.items():
        series = [getattr(f, name) for f in frames]
        smooth(series, attack, release)
        for frame, value in zip(frames, series):
            setattr(frame, name, value)


# ---------------------------------------------------------------------------
# Procedural talking, for a performance with no audio
# ---------------------------------------------------------------------------

def synthesize_talking(duration_seconds, fps=DEFAULT_FPS, seed=1):
    """
    Plausible mouth movement without any audio.

    Syllables of varying length with closures between words, so it reads as
    speech rather than a chattering jaw. Deterministic for a given seed.
    """
    frame_count = max(1, int(round(max(duration_seconds, 0.1) * fps)))
    frames = [VisemeFrame() for _ in range(frame_count)]

    state = (seed * 2654435761) & 0xFFFFFFFF

    def rand():
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state / 4294967296.0

    index = 0
    while index < frame_count:
        # A word of two to five syllables, then a breath.
        syllables = 2 + int(rand() * 4)
        for _ in range(syllables):
            length = max(2, int(3 + rand() * 5))
            shape = rand()
            amplitude = 0.45 + rand() * 0.5
            for step in range(length):
                if index >= frame_count:
                    break
                # Rise and fall across the syllable.
                envelope = math.sin(math.pi * (step + 0.5) / length) * amplitude
                frame = frames[index]
                if shape < 0.34:
                    frame.jaw = envelope
                elif shape < 0.62:
                    frame.wide = envelope
                    frame.jaw = envelope * 0.25
                elif shape < 0.84:
                    frame.funnel = envelope
                    frame.jaw = envelope * 0.2
                else:
                    frame.pucker = envelope
                frame.confidence = 0.5
                index += 1
            # A brief closure between syllables.
            if index < frame_count and rand() < 0.45:
                frames[index].close = 0.7
                frames[index].confidence = 0.4
                index += 1

        pause = 3 + int(rand() * 7)
        for _ in range(pause):
            if index >= frame_count:
                break
            frames[index].close = 0.5
            index += 1

    _smooth_channels(frames)
    return frames
