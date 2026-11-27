import queue
import signal
import threading

import numpy as np

import matplotlib
matplotlib.use("TkAgg")  # Interactive backend for Matplotlib windows

import matplotlib.pyplot as plt
import sounddevice as sd
from scipy.signal import fftconvolve, firwin, lfilter, lfilter_zi, welch
from rtlsdr import RtlSdr


# ============================================================
# RTL-SDR CONFIGURATION
# ============================================================

STATION_FREQ_HZ = 70_000_000  # Frequency to demodulate: 70 MHz
GAIN_DB = 40.2                # Manual gain in dB (watch "ADC clipping")

# Demodulation mode:
#   "am"        -> amplitude modulation (lab generator), tones up to 10 kHz
#   "fm_wide"   -> commercial FM radio (88-108 MHz), deviation up to 75 kHz
#   "fm_narrow" -> FM with deviation up to 5 kHz and tones up to 5 kHz
MODE = "am"

VOLUME = 0.5                  # 0.0 (mute) to 1.0 (max)

# Every size and rate ratio below is a power of 2
SAMPLE_RATE_HZ = 1_024_000    # 2^10 kS/s. Lower rate = narrower tuner
                              # filter = less ADC saturation and noise
N_SAMPLES = 2**17             # 131072 IQ samples per block (128 ms)
FFT_SIZE = 2**12              # 4096-point Welch segments
OVERLAP = FFT_SIZE // 2       # Welch overlap

# The tuner is placed fs/4 above the station so the station does not
# fall on the DC spike at the center of the spectrum.
OFFSET_HZ = SAMPLE_RATE_HZ // 4
TUNER_FREQ_HZ = STATION_FREQ_HZ + OFFSET_HZ

# Display smoothing factor
# ALPHA close to 1: faster but noisier
# ALPHA close to 0: smoother but slower
ALPHA = 0.35


# ============================================================
# DEMODULATION CONFIGURATION
# ============================================================

MODES = {
    "am":        {"max_deviation_hz": 0,      "audio_cutoff_hz": 10_000,
                  "deemphasis": False},
    "fm_wide":   {"max_deviation_hz": 75_000, "audio_cutoff_hz": 15_000,
                  "deemphasis": True},
    "fm_narrow": {"max_deviation_hz": 5_000,  "audio_cutoff_hz": 5_000,
                  "deemphasis": False},
}
IS_AM = MODE == "am"
MAX_DEVIATION_HZ = MODES[MODE]["max_deviation_hz"]
AUDIO_CUTOFF_HZ = MODES[MODE]["audio_cutoff_hz"]
USE_DEEMPHASIS = MODES[MODE]["deemphasis"]

CHANNEL_DECIM = 2**2                             # 1024 kS/s -> 256 kS/s
CHANNEL_RATE_HZ = SAMPLE_RATE_HZ // CHANNEL_DECIM
AUDIO_DECIM = 2**3                               # 256 kS/s -> 32 kS/s
AUDIO_RATE_HZ = CHANNEL_RATE_HZ // AUDIO_DECIM

# Carson's rule: half bandwidth = max deviation + max audio frequency.
# With zero deviation it gives the AM bandwidth (two sidebands).
# The margin keeps both sidebands inside the channel even if the
# transmitter is slightly off frequency.
TUNING_MARGIN_HZ = 1_000
CHANNEL_CUTOFF_HZ = MAX_DEVIATION_HZ + AUDIO_CUTOFF_HZ + TUNING_MARGIN_HZ
DEEMPHASIS_TAU_S = 75e-6      # De-emphasis used in the Americas (Colombia)

CHANNEL_TAPS = 2**11 + 1       # 2049-tap channel filter at 1024 kS/s
AUDIO_TAPS = 2**9 + 1          # 513-tap audio filter at 256 kS/s
STOPBAND_DB = 60               # Stopband rejection of both filters
KAISER_BETA = 0.1102 * (STOPBAND_DB - 8.7)

AUDIO_PLOT_BLOCKS = 4         # Audio blocks used for the modulating spectrum

# Continuous audio: the sound card plays from a buffer that is filled by
# the demodulator. Playback starts once PREBUFFER_S seconds are stored, so
# short pauses (e.g. while the plot redraws) never interrupt the sound.
PREBUFFER_S = 0.5
AUDIO_BUFFER_S = 4.0          # Maximum audio stored (latency limit)
IQ_QUEUE_BLOCKS = 32          # IQ blocks waiting to be demodulated (~4 s)
MIN_TONE_PROMINENCE_DB = 10   # A tone must stand this far above the noise


# ============================================================
# RECEIVER (runs in background threads)
# ============================================================

class AudioBuffer:
    """Thread-safe FIFO between the demodulator and the sound card."""

    def __init__(self, capacity, prebuffer):
        self.data = np.zeros(capacity, dtype=np.float32)
        self.read_pos = 0
        self.count = 0
        self.prebuffer = prebuffer
        self.playing = False
        self.underruns = 0    # Times the sound card ran out of audio
        self.lock = threading.Lock()

    def write(self, x):
        with self.lock:
            capacity = len(self.data)
            if self.count + len(x) > capacity:
                # Buffer full: drop the oldest audio to limit latency
                drop = self.count + len(x) - capacity
                self.read_pos = (self.read_pos + drop) % capacity
                self.count -= drop
            write_pos = (self.read_pos + self.count) % capacity
            first = min(len(x), capacity - write_pos)
            self.data[write_pos:write_pos + first] = x[:first]
            self.data[:len(x) - first] = x[first:]
            self.count += len(x)

    def read(self, n):
        out = np.zeros(n, dtype=np.float32)
        with self.lock:
            if not self.playing:
                if self.count < self.prebuffer:
                    return out            # Still filling: play silence
                self.playing = True
            k = min(n, self.count)
            capacity = len(self.data)
            first = min(k, capacity - self.read_pos)
            out[:first] = self.data[self.read_pos:self.read_pos + first]
            out[first:k] = self.data[:k - first]
            self.read_pos = (self.read_pos + k) % capacity
            self.count -= k
            if k < n:
                # Ran out of audio: refill the prebuffer before playing again
                self.playing = False
                self.underruns += 1
        return out

    def level_s(self):
        with self.lock:
            return self.count / AUDIO_RATE_HZ

class StreamingFir:
    """Low-pass FIR filter (FFT based) that keeps its history between
    blocks. It is flat (no attenuation) up to passband_hz."""

    def __init__(self, passband_hz, fs_hz, num_taps, dtype):
        # Kaiser design: transition width for the requested rejection
        transition_hz = ((STOPBAND_DB - 7.95) / (2.285 * (num_taps - 1))
                         * fs_hz / (2 * np.pi))
        self.taps = firwin(num_taps, passband_hz + transition_hz / 2,
                           window=("kaiser", KAISER_BETA), fs=fs_hz)
        self.history = np.zeros(num_taps - 1, dtype=dtype)

    def process(self, x):
        buffer = np.concatenate((self.history, x))
        self.history = buffer[-len(self.history):]
        return fftconvolve(buffer, self.taps, mode="valid")


class Receiver:
    """Reads IQ blocks, demodulates AM or FM and plays the audio.

    Three stages run at the same time, so no samples are lost:
      reader thread  -> librtlsdr asynchronous read (15 USB buffers)
      demod thread   -> filters and demodulates each block
      sound card     -> plays from AudioBuffer at its own pace

    Every filter keeps its state between blocks, so the audio has no
    clicks at the block boundaries.
    """

    def __init__(self, sdr):
        self.sdr = sdr
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.error = None

        # Values read by the plot (protected by self.lock)
        self.latest_samples = None
        self.audio_history = []

        # Mixer that moves the station from -OFFSET_HZ to 0 Hz
        n = np.arange(N_SAMPLES)
        step = 2 * np.pi * OFFSET_HZ / SAMPLE_RATE_HZ
        self.mixer = np.exp(1j * step * n).astype(np.complex64)
        self.mixer_block_rotation = np.exp(1j * step * N_SAMPLES)
        self.mixer_phase = 1.0 + 0j

        self.channel_filter = StreamingFir(CHANNEL_CUTOFF_HZ, SAMPLE_RATE_HZ,
                                           CHANNEL_TAPS, complex)
        self.audio_filter = StreamingFir(AUDIO_CUTOFF_HZ, CHANNEL_RATE_HZ,
                                         AUDIO_TAPS, float)
        self.first_block = True

        # FM: last channel sample, needed by the discriminator
        self.last_iq = 1.0 + 0j

        # AM: smoothed carrier amplitude, used as automatic gain control
        self.carrier_level = None

        # De-emphasis: single-pole low-pass with time constant tau
        a = 1 - np.exp(-1 / (CHANNEL_RATE_HZ * DEEMPHASIS_TAU_S))
        self.deemph_b = [a]
        self.deemph_a = [1, a - 1]
        self.deemph_zi = np.zeros(1)

        # DC blocker (~40 Hz high-pass): removes the AM carrier level and
        # the FM offset caused by a small tuning error
        self.dc_b = [1, -1]
        self.dc_a = [1, -0.999]
        self.dc_zi = None

        self.iq_queue = queue.Queue(maxsize=IQ_QUEUE_BLOCKS)
        self.lost_blocks = 0  # Blocks dropped because demodulation lagged
        self.audio_buffer = AudioBuffer(int(AUDIO_BUFFER_S * AUDIO_RATE_HZ),
                                        int(PREBUFFER_S * AUDIO_RATE_HZ))
        self.stream = None

        self.reader_thread = threading.Thread(target=self._read_loop,
                                              daemon=True)
        self.demod_thread = threading.Thread(target=self._demod_loop,
                                             daemon=True)

    def demodulate(self, samples):
        """Converts one block of IQ samples into audio samples."""
        # 1. Shift the station to 0 Hz (phase continues between blocks)
        shifted = samples * self.mixer * self.mixer_phase
        self.mixer_phase *= self.mixer_block_rotation
        self.mixer_phase /= abs(self.mixer_phase)

        # 2. Keep only the station channel and decimate to 256 kS/s
        channel = self.channel_filter.process(shifted)[::CHANNEL_DECIM]

        if IS_AM:
            # 3. Envelope detector: |x| = Ac * (1 + m * message)
            envelope = np.abs(channel)

            # 4. Divide by the carrier amplitude -> 1 + m * message
            level = np.mean(envelope)
            self.carrier_level = level if self.carrier_level is None else (
                0.8 * self.carrier_level + 0.2 * level)
            message = envelope / max(self.carrier_level, 1e-12)
        else:
            # 3. FM discriminator: phase difference between samples
            previous = np.concatenate(([self.last_iq], channel[:-1]))
            self.last_iq = channel[-1]
            inst_freq_hz = (np.angle(channel * np.conj(previous))
                            * CHANNEL_RATE_HZ / (2 * np.pi))

            # 4. Normalize: full deviation -> amplitude 1
            message = inst_freq_hz / MAX_DEVIATION_HZ

            # De-emphasis (only commercial FM uses pre-emphasis)
            if USE_DEEMPHASIS:
                message, self.deemph_zi = lfilter(
                    self.deemph_b, self.deemph_a, message, zi=self.deemph_zi
                )

        # 5. Remove DC (start from steady state to avoid a "thump")
        if self.dc_zi is None:
            self.dc_zi = lfilter_zi(self.dc_b, self.dc_a) * message[0]
        message, self.dc_zi = lfilter(
            self.dc_b, self.dc_a, message, zi=self.dc_zi
        )

        # 6. Keep the audio band and decimate to 32 kS/s
        audio = self.audio_filter.process(message)[::AUDIO_DECIM]

        # The first block only fills the filters: play it as silence
        if self.first_block:
            self.first_block = False
            return np.zeros(len(audio), dtype=np.float32)

        with self.lock:
            self.audio_history.append(audio)
            del self.audio_history[:-AUDIO_PLOT_BLOCKS]

        return np.clip(audio * VOLUME, -1.0, 1.0).astype(np.float32)

    def _on_samples(self, samples, context):
        """Called by librtlsdr for every block: only queue it (fast)."""
        if self.stop_event.is_set():
            return
        try:
            self.iq_queue.put_nowait(samples)
        except queue.Full:
            self.lost_blocks += 1

    def _read_loop(self):
        try:
            # Blocks here until cancel_read_async() is called
            self.sdr.read_samples_async(self._on_samples, N_SAMPLES)
        except Exception as exc:
            self.error = exc
        self.stop_event.set()

    def _demod_loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    samples = self.iq_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                with self.lock:
                    self.latest_samples = samples
                self.audio_buffer.write(self.demodulate(samples))
        except Exception as exc:
            self.error = exc
            self.stop_event.set()

    def _audio_callback(self, outdata, frames, time_info, status):
        """Called by the sound card whenever it needs more audio."""
        outdata[:, 0] = self.audio_buffer.read(frames)

    def snapshot(self):
        """Returns a consistent copy of the values used by the plot."""
        with self.lock:
            audio = (np.concatenate(self.audio_history)
                     if self.audio_history else None)
            return self.latest_samples, audio

    def status(self):
        """Audio stored (s), sound card underruns and lost IQ blocks."""
        return (self.audio_buffer.level_s(), self.audio_buffer.underruns,
                self.lost_blocks)

    def start(self):
        self.stream = sd.OutputStream(
            samplerate=AUDIO_RATE_HZ,
            channels=1,
            dtype="float32",
            latency="high",
            callback=self._audio_callback,
        )
        self.stream.start()
        self.demod_thread.start()
        self.reader_thread.start()

    def stop(self):
        self.stop_event.set()
        if self.reader_thread.is_alive():
            self.sdr.cancel_read_async()
            self.reader_thread.join(timeout=3)
        self.demod_thread.join(timeout=3)
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()


# ============================================================
# MEASUREMENTS
# ============================================================

def peak_frequency(freqs, power_db, search_mask):
    """Frequency of the strongest component inside search_mask, refined
    with a parabola through the three highest bins."""
    valid = np.flatnonzero(search_mask)
    i = valid[np.argmax(power_db[valid])]
    if 0 < i < len(power_db) - 1:
        left, center, right = power_db[i - 1], power_db[i], power_db[i + 1]
        denom = left - 2 * center + right
        if denom != 0:
            return freqs[i] + 0.5 * (left - right) / denom * (freqs[1] - freqs[0])
    return freqs[i]


def carrier_frequency(freqs, psd, search_mask):
    """Spectral centroid inside the channel after removing the noise floor.
    For FM it equals the carrier frequency, with or without modulation."""
    noise_floor = np.median(psd)
    power = np.clip(psd[search_mask] - noise_floor, 0, None)
    if power.sum() == 0:
        return np.mean(freqs[search_mask])
    return np.sum(freqs[search_mask] * power) / np.sum(power)


def tone_amplitude(freqs, psd, tone_hz):
    """Amplitude of the modulating tone in the demodulated signal, computed
    from its power in the spectrum (noise barely affects it).
    AM: modulation index m.  FM: deviation / MAX_DEVIATION_HZ."""
    df = freqs[1] - freqs[0]
    near_tone = np.abs(freqs - tone_hz) <= 4 * df
    amplitude = np.sqrt(2 * np.sum(psd[near_tone]) * df)
    if USE_DEEMPHASIS:
        # Undo the de-emphasis attenuation at the tone frequency
        amplitude *= np.hypot(1, 2 * np.pi * tone_hz * DEEMPHASIS_TAU_S)
    return amplitude


def modulation_text(amplitude):
    """Modulation index (AM) or frequency deviation (FM) as text."""
    if IS_AM:
        return f"modulation index m: {amplitude * 100:5.1f} %"
    return f"deviation: {amplitude * MAX_DEVIATION_HZ / 1e3:6.2f} kHz"


# ============================================================
# OPEN RTL-SDR
# ============================================================

def main():
    sdr = None
    receiver = None

    try:
        sdr = RtlSdr()

        sdr.sample_rate = SAMPLE_RATE_HZ
        sdr.center_freq = TUNER_FREQ_HZ
        sdr.gain = GAIN_DB

        print("RTL-SDR configured correctly")
        print(f"Station:          {STATION_FREQ_HZ / 1e6:.3f} MHz")
        print(f"Tuner frequency:  {TUNER_FREQ_HZ / 1e6:.3f} MHz")
        print(f"Sample rate:      {SAMPLE_RATE_HZ / 1e6:.3f} MS/s")
        print(f"Gain:             {sdr.gain:.1f} dB")
        print(f"Mode:             {MODE} (channel "
              f"+/-{CHANNEL_CUTOFF_HZ / 1e3:.0f} kHz, audio up to "
              f"{AUDIO_CUTOFF_HZ / 1e3:.0f} kHz)")
        print(f"Audio:            {AUDIO_RATE_HZ} Hz, volume {VOLUME}")
        print("Press Ctrl+C (or close the window) to stop")

        receiver = Receiver(sdr)
        receiver.start()

        # Ctrl+C only raises a stop flag. A KeyboardInterrupt would be
        # swallowed by Tkinter if it arrives while the window is redrawing.
        signal.signal(signal.SIGINT, lambda signum, frame: receiver.stop_event.set())

        # ============================================================
        # MATPLOTLIB REAL-TIME PLOT SETUP
        # ============================================================

        plt.ion()

        fig, (ax_rf, ax_audio) = plt.subplots(2, 1, figsize=(11, 8))

        # --- RF spectrum (modulated signal) ---
        freq_axis_mhz = np.linspace(
            (TUNER_FREQ_HZ - SAMPLE_RATE_HZ / 2) / 1e6,
            (TUNER_FREQ_HZ + SAMPLE_RATE_HZ / 2) / 1e6,
            FFT_SIZE,
            endpoint=False
        )
        line_rf, = ax_rf.plot(freq_axis_mhz, np.full(FFT_SIZE, -150.0))

        # Mark the station being demodulated and its channel filter
        ax_rf.axvline(STATION_FREQ_HZ / 1e6, color="red", linestyle="--",
                      label=f"Demodulating ({MODE}): "
                            f"{STATION_FREQ_HZ / 1e6:.3f} MHz")
        ax_rf.axvspan((STATION_FREQ_HZ - CHANNEL_CUTOFF_HZ) / 1e6,
                      (STATION_FREQ_HZ + CHANNEL_CUTOFF_HZ) / 1e6,
                      color="red", alpha=0.1, label="Channel filter")
        ax_rf.legend(loc="upper right")

        ax_rf.set_xlabel("Frequency [MHz]")
        ax_rf.set_ylabel("PSD [dB/Hz]")
        ax_rf.grid(True)
        ax_rf.set_xlim(freq_axis_mhz[0], freq_axis_mhz[-1])
        ax_rf.set_ylim(-150, -20)

        # --- Demodulated (modulating) signal spectrum ---
        audio_axis_khz = np.linspace(0, AUDIO_RATE_HZ / 2e3, FFT_SIZE // 2 + 1)
        line_audio, = ax_audio.plot(audio_axis_khz,
                                    np.full(len(audio_axis_khz), -150.0))
        peak_marker, = ax_audio.plot([], [], "ro")
        ax_audio.axvline(AUDIO_CUTOFF_HZ / 1e3, color="gray", linestyle=":",
                         label="Audio filter cutoff")
        ax_audio.legend(loc="upper right")

        ax_audio.set_xlabel("Frequency [kHz]")
        ax_audio.set_ylabel("PSD [dB/Hz]")
        ax_audio.grid(True)
        ax_audio.set_xlim(0, AUDIO_RATE_HZ / 2e3)
        ax_audio.set_ylim(-120, -20)

        plt.tight_layout()
        plt.show(block=False)

        rf_db_avg = None
        audio_db_avg = None

        # ============================================================
        # REAL-TIME PLOT LOOP (audio keeps running in the background)
        # ============================================================

        while plt.fignum_exists(fig.number) and not receiver.stop_event.is_set():
            samples, audio = receiver.snapshot()

            if samples is None or audio is None or len(audio) < FFT_SIZE:
                plt.pause(0.05)
                continue

            # Percentage of ADC samples at the limits (saturation)
            clipping = np.mean((np.abs(samples.real) > 0.99)
                               | (np.abs(samples.imag) > 0.99)) * 100

            # --- RF spectrum ---
            # Remove DC component to reduce the center spike
            samples = samples - np.mean(samples)

            # Estimate PSD using Welch method
            freqs, psd = welch(
                samples,
                fs=SAMPLE_RATE_HZ,
                window="hann",
                nperseg=FFT_SIZE,
                noverlap=OVERLAP,
                return_onesided=False,
                scaling="density"
            )

            # Shift zero frequency to the center
            freqs = np.fft.fftshift(freqs)
            psd = np.fft.fftshift(psd)

            # Convert baseband frequency to RF frequency
            rf_freq_hz = TUNER_FREQ_HZ + freqs

            # Convert PSD to dB/Hz and smooth it
            rf_db = 10 * np.log10(np.maximum(psd, 1e-20))
            rf_db_avg = rf_db if rf_db_avg is None else (
                ALPHA * rf_db + (1 - ALPHA) * rf_db_avg)

            line_rf.set_xdata(rf_freq_hz / 1e6)
            line_rf.set_ydata(rf_db_avg)

            # Carrier: in AM it is the strongest line of the spectrum; in FM
            # the power spreads, so the spectral centroid is used instead
            in_channel = np.abs(rf_freq_hz - STATION_FREQ_HZ) <= CHANNEL_CUTOFF_HZ
            if IS_AM:
                carrier_hz = peak_frequency(rf_freq_hz, rf_db_avg, in_channel)
            else:
                carrier_hz = carrier_frequency(
                    rf_freq_hz, 10 ** (rf_db_avg / 10), in_channel)
            carrier_mhz = carrier_hz / 1e6
            ax_rf.set_title(
                f"Modulated signal (RF)  |  carrier: {carrier_mhz:.6f} MHz  |  "
                f"ADC clipping: {clipping:.2f}%"
                + ("  -> lower GAIN_DB" if clipping > 0.1 else "")
            )

            # --- Modulating signal spectrum ---
            audio_freqs, audio_psd = welch(
                audio,
                fs=AUDIO_RATE_HZ,
                window="hann",
                nperseg=FFT_SIZE,
                noverlap=OVERLAP,
                scaling="density"
            )
            audio_db = 10 * np.log10(np.maximum(audio_psd, 1e-20))
            audio_db_avg = audio_db if audio_db_avg is None else (
                ALPHA * audio_db + (1 - ALPHA) * audio_db_avg)

            in_audio_band = (audio_freqs >= 50) & (audio_freqs <= AUDIO_CUTOFF_HZ)
            tone_hz = peak_frequency(audio_freqs, audio_db_avg, in_audio_band)
            tone_db = np.interp(tone_hz, audio_freqs, audio_db_avg)
            noise_db = np.median(audio_db_avg[in_audio_band])

            line_audio.set_ydata(audio_db_avg)

            # Only report a tone that clearly stands out from the noise
            if tone_db - noise_db >= MIN_TONE_PROMINENCE_DB:
                modulation = modulation_text(
                    tone_amplitude(audio_freqs, audio_psd, tone_hz))
                tone_text = f"tone: {tone_hz:7.1f} Hz | {modulation}"
                peak_marker.set_data([tone_hz / 1e3], [tone_db])
            else:
                tone_text = "no clear tone (no modulation or too much noise)"
                peak_marker.set_data([], [])

            ax_audio.set_title(f"Demodulated (modulating) signal  |  {tone_text}")

            buffered_s, audio_gaps, lost_blocks = receiver.status()
            print(f"\rCarrier: {carrier_mhz:.6f} MHz | {tone_text} | clipping: "
                  f"{clipping:4.2f}% | audio buffer {buffered_s:.2f} s, "
                  f"gaps {audio_gaps}, lost {lost_blocks}      ",
                  end="", flush=True)

            # Refresh figure
            fig.canvas.draw_idle()
            plt.pause(0.1)

        if receiver.error is not None:
            print(f"\nReceiver error: {receiver.error}")
        else:
            print("\nStopping acquisition...")

    except KeyboardInterrupt:
        print("\nStopping acquisition...")

    finally:
        if receiver is not None:
            receiver.stop()
        if sdr is not None:
            sdr.close()
            print("\nRTL-SDR closed.")


if __name__ == "__main__":
    main()
