"""SCA (Subsidiary Communications Authorization) subcarrier audio decoder.

An analogue SCA is a narrow FM subcarrier riding on the composite MPX,
normally centred on 67 kHz or 92 kHz with a few kHz of deviation and up to
about 5 kHz of audio (reading services, background music, ethnic and
religious feeds). Decoding it is a second, much narrower FM demodulation
of the composite:

    MPX -> mix down by the subcarrier frequency -> complex low-pass
        -> FM discriminate -> 5 kHz audio low-pass -> 150 us de-emphasis

The decoder keeps filter state between chunks so the audio is continuous,
and reports the subcarrier's level so the GUI can show whether one is
present at all.
"""

from __future__ import annotations

import numpy as np

SCA_FREQUENCIES_HZ = {"sca67": 67_000.0, "sca92": 92_000.0}
SCA_BANDWIDTH_HZ = 7_500.0      # complex low-pass half-width (+/- deviation)
SCA_AUDIO_HZ = 5_000.0
SCA_DEEMPHASIS_US = 150.0
# Subcarrier RMS relative to the whole MPX at which an SCA counts as present.
SCA_PRESENT_RATIO = 0.03


class SCADecoder:
    """Stateful narrow-FM decoder for one SCA subcarrier in the MPX."""

    def __init__(self, sample_rate: float, carrier_hz: float):
        from scipy.signal import butter, lfilter_zi

        self.sample_rate = float(sample_rate)
        self.carrier_hz = float(carrier_hz)
        nyq = self.sample_rate / 2.0
        self._bp_b, self._bp_a = butter(4, SCA_BANDWIDTH_HZ / nyq, btype="low")
        zi = lfilter_zi(self._bp_b, self._bp_a)
        self._bp_zi = zi.astype(np.complex128)
        self._lp_b, self._lp_a = butter(5, SCA_AUDIO_HZ / nyq, btype="low")
        self._lp_zi = lfilter_zi(self._lp_b, self._lp_a)
        de_hz = 1.0 / (2.0 * np.pi * SCA_DEEMPHASIS_US * 1e-6)
        self._de_b, self._de_a = butter(1, de_hz / nyq, btype="low")
        self._de_zi = lfilter_zi(self._de_b, self._de_a)
        self._phase = 0.0
        self._last = complex(1.0, 0.0)
        self.level_ratio = 0.0
        self.present = False

    def process(self, mpx: np.ndarray) -> np.ndarray:
        """Return SCA audio at the MPX sample rate for one chunk of MPX."""
        from scipy.signal import lfilter

        n = len(mpx)
        if n == 0:
            return np.zeros(0)
        step = 2.0 * np.pi * self.carrier_hz / self.sample_rate
        angles = self._phase + step * np.arange(n)
        self._phase = float((angles[-1] + step) % (2.0 * np.pi))
        baseband = mpx * np.exp(-1j * angles)
        baseband, self._bp_zi = lfilter(self._bp_b, self._bp_a, baseband,
                                        zi=self._bp_zi)

        mpx_rms = float(np.sqrt(np.mean(mpx * mpx))) + 1e-12
        sub_rms = float(np.sqrt(np.mean(np.abs(baseband) ** 2)))
        self.level_ratio = sub_rms / mpx_rms
        self.present = self.level_ratio > SCA_PRESENT_RATIO

        prev = np.concatenate(([self._last], baseband[:-1]))
        self._last = complex(baseband[-1])
        # Instantaneous frequency in Hz, normalised so a 6 kHz deviation
        # gives roughly full-scale audio before the AGC.
        disc = np.angle(baseband * np.conj(prev)) * self.sample_rate / (2 * np.pi)
        audio = disc / 6_000.0
        audio, self._lp_zi = lfilter(self._lp_b, self._lp_a, audio, zi=self._lp_zi)
        audio, self._de_zi = lfilter(self._de_b, self._de_a, audio, zi=self._de_zi)
        return audio

    @property
    def level_db(self) -> float:
        return 20.0 * np.log10(max(self.level_ratio, 1e-6))


def synth_sca(sample_rate: float, seconds: float, carrier_hz: float,
              tone_hz: float = 1_000.0, deviation_hz: float = 5_000.0,
              injection: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """(mpx, tone) test signal: a mono programme plus one SCA carrying a tone."""
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    programme = 0.6 * np.sin(2 * np.pi * 400.0 * t) + 0.2 * np.sin(2 * np.pi * 3_000.0 * t)
    tone = np.sin(2 * np.pi * tone_hz * t)
    phase = 2 * np.pi * carrier_hz * t + 2 * np.pi * deviation_hz * np.cumsum(tone) / sample_rate
    sca = injection * np.cos(phase)
    return programme + sca, tone
