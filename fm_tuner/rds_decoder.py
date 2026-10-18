"""RDS subcarrier decoder.

`MPXRDSDecoder` takes an FM multiplex (MPX) signal, recovers the 57 kHz
RDS subcarrier, demodulates its biphase symbols coherently and assembles
RDS groups with syndrome checking and burst (<=2 bit) error correction.

Signal chain: 54-60 kHz bandpass -> analytic signal -> mixed down with a
free-running 57 kHz -> 2.4 kHz lowpass -> residual carrier offset removed
by squaring the baseband (which cancels the BPSK modulation and leaves a
tone at twice the offset) -> biphase (Manchester) matched filter at the
recovered symbol timing -> BPSK axis estimate -> differential decode ->
syndrome sync.

The carrier is recovered from the RDS signal itself rather than from the
19 kHz pilot: tripling a noisy or absent pilot's phase produces a useless
reference, and plenty of stations carrying RDS transmit in mono.

`rds_worker_process` runs a decoder in its own OS process so the decoding
never competes with the audio thread for the GIL.
"""

from __future__ import annotations


class MPXRDSDecoder:
    """Decode RDS groups from a wideband FM multiplex (MPX) signal.

    Requires: numpy, scipy.

    The input must be the composite MPX signal (the FM discriminator
    output), not demodulated speaker audio. The sample rate must be at
    least 120 kHz so the 57 kHz RDS subcarrier is below Nyquist; the tuner
    feeds it 240 kHz.
    """

    RDS_FREQ = 57000.0       # Hz - 3rd harmonic of the 19 kHz pilot
    BIT_RATE = 1187.5        # bps - RDS data rate
    # RDS2 (IEC 62106-2) adds three more 1187.5 bps streams on subcarriers
    # above the RDS one, spaced 4.75 kHz apart. Stream 0 is classic RDS.
    STREAM_FREQS = (57000.0, 66500.0, 71250.0, 76000.0)
    SUBCARRIER_HALF_BW = 3000.0   # Hz either side kept by the bandpass
    # RDS2 streams sit only 4.75 kHz apart, so their bandpass must stop
    # short of the neighbour's sidebands and the baseband lowpass has to
    # be sharp enough to reject a neighbour 4.75 kHz off.
    RDS2_HALF_BW = 2200.0
    # Generator polynomial: x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
    GEN_POLY = 0x5B9

    # Offset words from the RDS standard. The syndrome here is
    # checkword XOR CRC(data), which for a valid block is the offset word
    # itself, so these are the offset words and not the shift-register
    # syndromes quoted by implementations that use the other convention.
    OFFSET_A = 0x0FC
    OFFSET_B = 0x198
    OFFSET_C = 0x168
    OFFSET_CP = 0x350   # C' for type B groups
    OFFSET_D = 0x1B4
    _ALL_OFFSETS = frozenset((OFFSET_A, OFFSET_B, OFFSET_C, OFFSET_CP, OFFSET_D))

    # Samples decoded per pass, and the longest bit backlog kept while
    # hunting for block sync.
    DECODE_BLOCK_SECONDS = 0.2   # ~2 groups per pass: low latency, same yield
    MAX_BIT_BUFFER = 8000

    # Error correction tables: syndrome -> 26-bit error pattern for every
    # burst of up to N bits. The code corrects bursts to 5 bits, but while
    # hunting for sync anything past 2 passes corrupt blocks as valid
    # (measured off air: 2 gives +44% groups and no false groups, 5 gives
    # +60% with a fifth wrong). Once a group has been found the next one
    # sits exactly 104 bits on, so there the full 5-bit table is safe.
    MAX_BURST = 2
    MAX_BURST_SYNCED = 5
    SYNC_MISSES = 4          # groups lost in a row before sync is dropped
    PI_LOCK_GROUPS = 3       # same PI this often locks it

    @staticmethod
    def _build_syndromes(max_burst, gen=0x5B9):
        def syndrome_of(error):
            reg = error
            for i in range(25, 9, -1):
                if reg & (1 << i):
                    reg ^= gen << (i - 10)
            return reg & 0x3FF

        table = {}
        for length in range(1, max_burst + 1):
            for start in range(0, 26 - length + 1):
                for inner in range(1 << max(0, length - 2)):
                    # burst: first and last bit set, anything in between
                    pattern = 1 << (length - 1) | 1 if length > 1 else 1
                    pattern |= inner << 1
                    error = pattern << start
                    table.setdefault(syndrome_of(error), error)
        return table

    _ERROR_SYNDROMES = _build_syndromes.__func__(MAX_BURST)
    _DEEP_SYNDROMES = _build_syndromes.__func__(MAX_BURST_SYNCED)

    def __init__(self, sample_rate=192000, stream=0):
        self.sample_rate = sample_rate
        self.samples_per_bit = sample_rate / self.BIT_RATE
        self.stream = stream
        self.carrier_hz = self.STREAM_FREQS[stream]
        self._group_sync = False
        self._sync_misses = 0
        self._pi_votes = {}
        self.locked_pi = None
        self.substituted_pi = 0
        self._a_substituted = False

        # Carrier / timing state carried between chunks
        self.carrier_phase = 0.0
        self._free_phase_samples = 0
        self._sym_phase = 0.0        # symbol timing offset into the buffer
        self._locked = False         # True once groups are being assembled
        self._have_timing = False    # True once symbol timing is tracked
        self._blocks_without_groups = 0
        self._prev_raw_symbol = 0    # for differential decoding
        self._freq_offset = 0.0      # residual carrier offset, Hz

        # Filters
        self._pilot_bp_b = None
        self._pilot_zi = None
        self._measured_pilot_freq = 19000.0
        self._rds_bp_b = None
        self._rds_bp_zi = None
        self._lp_b = None
        self._lp_a = None
        self._filter_zi = None       # baseband lowpass, in-phase
        self._filter_zi_q = None     # baseband lowpass, quadrature

        # Buffers
        self.baseband_buffer = None  # complex64 samples awaiting symbols
        self.bit_buffer = []         # decoded bits awaiting group assembly

        # Statistics
        self.total_bits = 0
        self.total_groups = 0
        self.total_errors = 0
        self.rds_level = 0.0         # 57 kHz subcarrier level
        self.symbol_snr_db = None    # how BPSK-like the recovered symbols are
        self.block_hits = 0.0        # valid block syndromes per 1000 bits

        self.decoded_groups = []     # last (b1, b2, b3, b4) hex tuples

    def _setup_filter(self):
        """Create the subcarrier, pilot and baseband filters."""
        from scipy.signal import firwin, lfilter_zi
        nyquist = self.sample_rate / 2

        # +/-3 kHz bandpass isolates this stream's subcarrier before mixing
        # (54-60 kHz for classic RDS).
        half_bw = self.SUBCARRIER_HALF_BW if self.stream == 0 \
            else self.RDS2_HALF_BW
        lo = self.carrier_hz - half_bw
        hi = self.carrier_hz + half_bw
        if hi < nyquist:
            self._rds_bp_b = firwin(255, [lo / nyquist, hi / nyquist],
                                    pass_zero=False, window="hamming")
            self._rds_bp_zi = lfilter_zi(self._rds_bp_b, [1.0])
        else:
            self._rds_bp_b = None
            self._rds_bp_zi = None

        # Baseband lowpass. The biphase spectrum peaks at +/-1187 Hz, so
        # 2.4 kHz keeps the symbols intact while rejecting everything else.
        cutoff = min(2400.0, nyquist * 0.9)
        self._lp_b = firwin(65 if self.stream == 0 else 257,
                            cutoff / nyquist, window="hamming")
        self._lp_a = [1.0]
        self._filter_zi = lfilter_zi(self._lp_b, self._lp_a)
        self._filter_zi_q = lfilter_zi(self._lp_b, self._lp_a)

        # 18.5-19.5 kHz bandpass for the stereo pilot.
        self._pilot_bp_b = firwin(255, [18500.0 / nyquist, 19500.0 / nyquist],
                                  pass_zero=False, window="hamming")
        self._pilot_zi = lfilter_zi(self._pilot_bp_b, [1.0])

    def _to_baseband(self, samples):
        """Mix the 57 kHz subcarrier down to complex baseband."""
        import numpy as np
        from scipy.signal import lfilter

        if self._rds_bp_b is not None:
            filtered, self._rds_bp_zi = lfilter(self._rds_bp_b, [1.0], samples,
                                                zi=self._rds_bp_zi)
        else:
            filtered = samples

        # Free-running local oscillator with continuous phase between
        # chunks. Whatever offset it leaves is measured and removed below.
        t = ((np.arange(len(samples)) + self._free_phase_samples)
             / self.sample_rate)
        self._free_phase_samples += len(samples)
        reference = np.exp(-2j * np.pi * self.carrier_hz * t)

        # Mixing the real bandpassed signal is enough: the image lands at
        # -114 kHz and the lowpass below removes it. A per-chunk Hilbert
        # transform would add FFT edge artifacts at every chunk boundary.
        mixed = filtered * reference
        real, self._filter_zi = lfilter(self._lp_b, self._lp_a, mixed.real,
                                        zi=self._filter_zi)
        imag, self._filter_zi_q = lfilter(self._lp_b, self._lp_a, mixed.imag,
                                          zi=self._filter_zi_q)
        return (real + 1j * imag).astype("complex64")

    def _remove_carrier_offset(self, buf):
        """Measure and remove the residual 57 kHz offset from a block.

        Squaring the baseband cancels the +/-1 BPSK modulation and leaves
        a tone at twice the carrier offset, so the offset can be read off
        an FFT and de-rotated away. Without this, a few Hz of dongle
        clock error turns the symbols round several times per block and
        nothing decodes.
        """
        import numpy as np

        squared = (buf.astype("complex128")) ** 2
        spectrum = np.abs(np.fft.fft(squared))
        freqs = np.fft.fftfreq(len(squared), 1.0 / self.sample_rate)
        search = np.abs(freqs) < 800.0
        if np.any(search):
            peak = float(freqs[search][np.argmax(spectrum[search])]) / 2.0
            if self._locked:
                self._freq_offset = 0.6 * self._freq_offset + 0.4 * peak
            else:
                self._freq_offset = peak

        # Index the de-rotation by absolute sample position: the tail of
        # this block is processed again in the next one, and it must come
        # out with the same phase both times.
        start = self._free_phase_samples - len(buf)
        t = (start + np.arange(len(buf))) / self.sample_rate
        phase = 2 * np.pi * self._freq_offset * t
        return (buf * np.exp(-1j * phase)).astype("complex64")

    def _symbol_values(self, buf, offset):
        """Biphase matched filter over `buf` starting at `offset`.

        Each RDS symbol is sent as a Manchester pair, so the first half of
        a symbol minus its second half is the matched filter; its
        magnitude is largest at the correct symbol timing.
        """
        import numpy as np

        spb = self.samples_per_bit
        count = int((len(buf) - offset - 1) / spb)
        if count < 8:
            return None
        positions = offset + np.arange(count) * spb
        starts = positions.astype(np.int64)
        mids = (positions + spb / 2.0).astype(np.int64)
        ends = (positions + spb).astype(np.int64)
        cumulative = np.concatenate(([0], np.cumsum(buf.astype("complex128"))))
        return (cumulative[mids] - cumulative[starts]) - (cumulative[ends]
                                                          - cumulative[mids])

    def _symbol_axis(self, values):
        """Track the BPSK axis along the block.

        Squaring cancels the +/-1 modulation, so a moving average of the
        squared symbols follows the carrier phase as it wanders without
        the data affecting it. A single axis for the whole block only
        works if the phase holds still for 0.4 s, which off air it does
        not. Where the tracked axis flips by pi the differential decode
        costs one bit, not the rest of the block.
        """
        import numpy as np

        squared = values ** 2
        window = min(128, max(8, len(squared) // 4))
        kernel = np.ones(window) / window
        smoothed = np.convolve(squared, kernel, mode="same")
        return 0.5 * np.angle(smoothed)

    def _best_offset(self, buf):
        """Find the symbol timing, tracked narrowly once bits are locked."""
        import numpy as np

        spb = self.samples_per_bit
        if self._have_timing:
            # Creep along with the clock rather than re-acquiring: a jump
            # between blocks slips a symbol, which breaks group assembly
            # even though individual blocks still decode.
            candidates = [self._sym_phase + d
                          for d in (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5)]
        else:
            candidates = list(np.arange(0.0, spb, 1.0))

        best_offset, best_score = candidates[0], -1.0
        for offset in candidates:
            values = self._symbol_values(buf, offset)
            if values is None:
                continue
            score = float(np.mean(np.abs(values) ** 2))
            if score > best_score:
                best_score, best_offset = score, offset
        return best_offset

    def process_samples(self, samples):
        """Process a chunk of MPX samples. Returns a list of group tuples."""
        import numpy as np

        if samples is None or len(samples) == 0:
            return []

        try:
            samples = np.asarray(samples, dtype=np.float64)
            if self._pilot_bp_b is None:
                self._setup_filter()
            self._measure(samples)

            baseband = self._to_baseband(samples)
            if self.baseband_buffer is None or len(self.baseband_buffer) == 0:
                self.baseband_buffer = baseband
            else:
                self.baseband_buffer = np.concatenate(
                    [self.baseband_buffer, baseband])

            block = int(self.DECODE_BLOCK_SECONDS * self.sample_rate)
            if len(self.baseband_buffer) < block:
                return []

            buf = self._remove_carrier_offset(self.baseband_buffer)
            offset = self._best_offset(buf)
            values = self._symbol_values(buf, offset)
            if values is None:
                return []

            # BPSK: the symbols lie on one axis of the complex plane, and
            # squaring them cancels the +/-1 modulation so their common
            # phase can be measured and rotated away.
            axis = self._symbol_axis(values)
            soft = np.real(values * np.exp(-1j * axis))
            self.symbol_snr_db = self._symbol_snr_db(soft)

            # Differential decode: the transmitter sends data XOR previous.
            raw = (soft > 0).astype(np.uint8)
            previous = np.concatenate(([self._prev_raw_symbol], raw[:-1]))
            bits = np.bitwise_xor(raw, previous)
            self._prev_raw_symbol = int(raw[-1])

            self.bit_buffer.extend(int(b) for b in bits)
            self.total_bits += len(bits)
            if len(self.bit_buffer) > self.MAX_BIT_BUFFER:
                del self.bit_buffer[:len(self.bit_buffer) - self.MAX_BIT_BUFFER]

            # Keep the tail that did not make up a whole symbol so timing
            # stays continuous across blocks.
            self._have_timing = True
            consumed = offset + len(values) * self.samples_per_bit
            keep = int(consumed)
            self._sym_phase = consumed - keep
            self.baseband_buffer = self.baseband_buffer[keep:]

            self.block_hits = self._block_hit_rate()
            groups = self._assemble_groups()
            if groups:
                self._locked = True
                self._blocks_without_groups = 0
            else:
                self._blocks_without_groups += 1
                if self._blocks_without_groups > 5:
                    self._locked = False
                if self._blocks_without_groups > 20:
                    self._have_timing = False   # re-acquire from scratch
                    self._blocks_without_groups = 0
            self.decoded_groups = list(groups)
            return groups

        except Exception:
            return []   # never let a DSP error kill the decoder process

    def _measure(self, samples):
        """Update the measured pilot frequency and 57 kHz level."""
        import numpy as np

        n = len(samples)
        if n <= 100:
            return
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, 1.0 / self.sample_rate)
        pilot_mask = (freqs > 18000) & (freqs < 20000)
        if np.any(pilot_mask):
            self._measured_pilot_freq = float(
                freqs[pilot_mask][np.argmax(spectrum[pilot_mask])])
        rds_mask = ((freqs > self.carrier_hz - 1500)
                    & (freqs < self.carrier_hz + 1500))
        if np.any(rds_mask):
            scale = np.sum(spectrum) + 1e-12
            self.rds_level = float(np.sum(spectrum[rds_mask]) / scale)

    @staticmethod
    def _symbol_snr_db(soft):
        """Rough BPSK SNR: how far the symbols sit from the decision point."""
        import numpy as np

        signal = float(np.mean(np.abs(soft)))
        noise = float(np.mean(soft ** 2)) - signal ** 2
        if signal <= 0 or noise <= 0:
            return None
        return float(10.0 * np.log10(signal ** 2 / noise))

    def _block_hit_rate(self):
        """Valid block syndromes per 1000 bits, over the recent bit buffer.

        Random bits hit one of the five offset words about 5 times per
        1000; a real RDS bit stream hits about 38 (four blocks every 104
        bits). It separates "the bits are noise" from "the bits are RDS
        but group assembly is failing".
        """
        bits = self.bit_buffer[-1200:]
        if len(bits) < 200:
            return 0.0
        offsets = (self.OFFSET_A, self.OFFSET_B, self.OFFSET_C,
                   self.OFFSET_D)
        value = 0
        for bit in bits[:26]:
            value = (value << 1) | bit
        hits = 0
        for index in range(26, len(bits)):
            data, checkword = (value >> 10) & 0xFFFF, value & 0x3FF
            if self._compute_syndrome(data, checkword) in offsets:
                hits += 1
            value = ((value << 1) | bits[index]) & 0x3FFFFFF
        return round(1000.0 * hits / (len(bits) - 26), 1)

    def _group_at(self, buf, start, deep=False):
        """Decode the group whose block A starts at buf[start]; None if any
        block fails. In sync a damaged block A is replaced by the locked PI."""
        block_a = self._decode_block(buf[start:start + 26], self.OFFSET_A)
        if block_a is None and not deep:
            return None
        block_b = self._decode_block(buf[start + 26:start + 52], self.OFFSET_B, deep)
        if block_b is None:
            return None
        block_c = self._decode_block(buf[start + 52:start + 78], self.OFFSET_C, deep)
        if block_c is None:
            block_c = self._decode_block(buf[start + 52:start + 78], self.OFFSET_CP, deep)
        if block_c is None:
            return None
        block_d = self._decode_block(buf[start + 78:start + 104], self.OFFSET_D, deep)
        if block_d is None:
            return None
        if block_a is None:
            # In sync a damaged PI block is filled from the locked PI; a
            # cleanly decoded PI is always believed as-is.
            if self.locked_pi is None:
                return None
            block_a = (self.locked_pi, self.OFFSET_A)
            self.substituted_pi += 1
            self._a_substituted = True
        else:
            self._a_substituted = False
        return (block_a[0], block_b[0], block_c[0], block_d[0])

    def reset_sync(self):
        """Forget group sync, the PI lock and pending bits (on retune)."""
        self._group_sync = False
        self._sync_misses = 0
        self._pi_votes = {}
        self.locked_pi = None
        self.bit_buffer = []

    def _note_pi(self, pi):
        """Lock onto a PI seen PI_LOCK_GROUPS times; a different clean PI
        seen that many times in a row takes the lock over (station change)."""
        if pi == self.locked_pi:
            self._pi_votes = {pi: self.PI_LOCK_GROUPS}
            return
        self._pi_votes[pi] = self._pi_votes.get(pi, 0) + 1
        for other in list(self._pi_votes):
            if other != pi:
                self._pi_votes[other] -= 1
                if self._pi_votes[other] <= 0:
                    del self._pi_votes[other]
        if self._pi_votes[pi] >= self.PI_LOCK_GROUPS:
            self.locked_pi = pi
            self._pi_votes = {pi: self.PI_LOCK_GROUPS}

    def _assemble_groups(self):
        """Assemble RDS groups from the bit buffer.

        Out of sync: slide a 104-bit window with light (2-bit) correction
        until all four blocks check. In sync: the next group starts 104
        bits after the last, so decode it there with full 5-bit burst
        correction; after SYNC_MISSES lost groups fall back to searching.
        """
        groups = []
        buf = self.bit_buffer

        while len(buf) >= 104:
            start = 0
            group = None
            if self._group_sync:
                group = self._group_at(buf, 0, deep=True)
                if group is None:
                    self._sync_misses += 1
                    if self._sync_misses > self.SYNC_MISSES:
                        self._group_sync = False
                    else:
                        del buf[:104]
                        continue
            if group is None:
                for start in range(len(buf) - 103):
                    group = self._group_at(buf, start)
                    if group is not None:
                        break
                if group is None:
                    break   # keep the bits, wait for more

            self._group_sync = True
            self._sync_misses = 0
            if not self._a_substituted:
                self._note_pi(group[0])
            groups.append(tuple(f"{word:04X}" for word in group))
            self.total_groups += 1
            del buf[:start + 104]

        return groups

    def _decode_block(self, bits, expected_offset=None, deep=False):
        """Decode a 26-bit block. Returns (data_16bit, syndrome_10bit) or None.

        If expected_offset is provided, attempts single-bit error correction:
        if the syndrome doesn't match, checks if a single bit flip would fix it.
        """
        if len(bits) < 26:
            return None

        # Convert 26 bits to integer
        value = 0
        for bit in bits[:26]:
            value = (value << 1) | (1 if bit else 0)

        data = (value >> 10) & 0xFFFF       # upper 16 bits = data
        checkword = value & 0x3FF            # lower 10 bits = checkword+offset

        syndrome = self._compute_syndrome(data, checkword)

        # If expected offset is given, try error correction
        if expected_offset is not None:
            if syndrome == expected_offset:
                return (data, syndrome)  # no errors

            # A block that is valid for a different offset word is not a
            # damaged copy of this one: the offsets differ by short
            # bursts, so "correcting" it would turn a PI block into a
            # fake block B and so on.
            if syndrome in self._ALL_OFFSETS:
                return None
            # Burst error correction (up to MAX_BURST bits)
            table = self._DEEP_SYNDROMES if deep else self._ERROR_SYNDROMES
            error = table.get(syndrome ^ expected_offset)
            if error is not None:
                corrected_data = data ^ (error >> 10)
                corrected_check = checkword ^ (error & 0x3FF)
                if self._compute_syndrome(corrected_data,
                                          corrected_check) == expected_offset:
                    return (corrected_data, expected_offset)

            return None  # uncorrectable error

        return (data, syndrome)

    def _compute_syndrome(self, data, checkword):
        """Compute RDS syndrome: checkword XOR (data * x^10 mod g(x))."""
        gen = self.GEN_POLY  # degree-10 polynomial (11 bits)

        # Compute (data << 10) mod g(x)
        reg = data << 10
        for i in range(25, 9, -1):
            if reg & (1 << i):
                reg ^= gen << (i - 10)

        check = reg & 0x3FF
        return checkword ^ check

    def get_stats(self):
        """Return decoder statistics."""
        return {
            "total_bits": self.total_bits,
            "total_groups": self.total_groups,
            "total_errors": self.total_errors,
            "rds_level": self.rds_level,
            "buffer_bits": len(self.bit_buffer),
            "symbol_snr_db": self.symbol_snr_db,
            "block_hits": self.block_hits,
        }


# Queued MPX chunks above which an RDS2 worker skips chunks to catch up.
RDS2_BACKLOG_LIMIT = 4
# An RDS2 stream that has produced no groups for this long is treated as
# absent: its worker then only decodes SNIFF_ON_SECONDS out of every
# SNIFF_PERIOD_SECONDS, so a station without RDS2 costs almost no CPU.
RDS2_IDLE_SECONDS = 8.0
SNIFF_ON_SECONDS = 1.5
SNIFF_PERIOD_SECONDS = 6.0


def rds2_streams_fit(sample_rate) -> bool:
    """True if the MPX rate keeps the 76 kHz stream-3 subcarrier below Nyquist."""
    return (MPXRDSDecoder.STREAM_FREQS[-1]
            + MPXRDSDecoder.SUBCARRIER_HALF_BW) < sample_rate / 2


RDS_RESET = "reset"   # queue sentinel: station changed, drop sync/PI lock


def rds_worker_process(in_queue, out_queue, sample_rate, stop_event,
                       enabled=None, stream=0):
    """Decode one RDS stream from demodulated MPX chunks in its own process.

    Chunks of demodulated MPX arrive on `in_queue` as numpy arrays. Each
    report pushed to `out_queue` is tagged with `stream` (0 = classic RDS,
    1-3 = RDS2 subcarriers) and carries the groups decoded since the last
    report as (b1, b2, b3, b4) hex-string tuples. `enabled` is an optional
    shared flag with a `.value`; while it is false the worker drains its
    queue without decoding. A worker that falls behind (more than
    RDS2_BACKLOG_LIMIT chunks queued) skips chunks and counts them in
    `skipped` so it never stalls the receiver.
    """
    import time

    decoder = MPXRDSDecoder(sample_rate=sample_rate, stream=stream)
    last_report = 0.0
    skipped = 0
    last_group = time.time()
    sniffing = False

    def backlog():
        try:
            return in_queue.qsize()
        except NotImplementedError:
            return 0

    while not stop_event.is_set():
        try:
            demod = in_queue.get(timeout=0.5)
        except Exception:
            continue
        if isinstance(demod, str):
            if demod == RDS_RESET:
                decoder.reset_sync()
            continue
        if demod is None or len(demod) == 0:
            continue
        if enabled is not None and not enabled.value:
            continue
        now = time.time()
        if stream != 0:
            if backlog() > RDS2_BACKLOG_LIMIT:
                skipped += 1
                continue
            sniffing = now - last_group > RDS2_IDLE_SECONDS
            if sniffing and (now % SNIFF_PERIOD_SECONDS) > SNIFF_ON_SECONDS:
                continue
        try:
            groups = decoder.process_samples(demod)
        except Exception as exc:  # keep the worker alive on DSP errors
            print(f"[rds{stream or ''}] decoder error: {exc}")
            continue
        if groups:
            last_group = now
        if not groups and now - last_report < 1.0:
            continue
        last_report = now
        payload = {
            "stream": stream,
            "groups": list(groups),
            "rds_level": float(decoder.rds_level),
            "pilot_hz": float(decoder._measured_pilot_freq),
            "total_groups": int(decoder.total_groups),
            "total_bits": int(decoder.total_bits),
            "symbol_snr_db": decoder.symbol_snr_db,
            "block_hits": decoder.block_hits,
            "skipped": skipped,
            "sniffing": sniffing,
        }
        try:
            out_queue.put_nowait(payload)
        except Exception:
            pass  # result queue full: drop this batch rather than stall
