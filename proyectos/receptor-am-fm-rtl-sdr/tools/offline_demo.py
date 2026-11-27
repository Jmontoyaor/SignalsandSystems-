#!/usr/bin/env python3
"""Run the receiver's demodulation chain on synthetic IQ samples (no hardware).

Generates an AM signal and a narrow-band FM signal, both modulated by a 1 kHz tone,
places them where the RTL-SDR would see them (OFFSET_HZ below the tuner frequency),
adds noise and pushes the blocks through Receiver.demodulate(). The recovered tone
frequency and modulation depth are printed and plotted to docs/offline_demo.png.

Usage: python tools/offline_demo.py
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "receiver"))
import rtl_sdr_receiver as rx  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
from scipy.signal import welch  # noqa: E402

plt.switch_backend("Agg")

TONE_HZ = 1_000
AM_DEPTH = 0.5
FM_DEVIATION_HZ = 3_000
SNR_DB = 30
BLOCKS = 8
SEED = 7

INK = "#3b3b3b"
MUTED = "#6b6b6b"
SERIES = "#257de0"
BAND = "#e9e9e9"


def configure(mode: str) -> None:
    """Switch the module-level parameters the Receiver reads at construction."""
    params = rx.MODES[mode]
    rx.MODE = mode
    rx.IS_AM = mode == "am"
    rx.MAX_DEVIATION_HZ = params["max_deviation_hz"]
    rx.AUDIO_CUTOFF_HZ = params["audio_cutoff_hz"]
    rx.USE_DEEMPHASIS = params["deemphasis"]
    rx.CHANNEL_CUTOFF_HZ = rx.MAX_DEVIATION_HZ + rx.AUDIO_CUTOFF_HZ + rx.TUNING_MARGIN_HZ


def synthesize(mode: str, rng: np.random.Generator) -> np.ndarray:
    n = np.arange(BLOCKS * rx.N_SAMPLES)
    t = n / rx.SAMPLE_RATE_HZ
    tone = np.cos(2 * np.pi * TONE_HZ * t)
    if mode == "am":
        baseband = 1 + AM_DEPTH * tone
    else:
        phase = 2 * np.pi * FM_DEVIATION_HZ * np.cumsum(tone) / rx.SAMPLE_RATE_HZ
        baseband = np.exp(1j * phase)
    # The station sits OFFSET_HZ below the tuner center frequency
    iq = 0.3 * baseband * np.exp(-2j * np.pi * rx.OFFSET_HZ * t)
    noise_power = np.mean(np.abs(iq) ** 2) / 10 ** (SNR_DB / 10)
    iq = iq + np.sqrt(noise_power / 2) * (rng.standard_normal(len(n)) + 1j * rng.standard_normal(len(n)))
    return iq.astype(np.complex64)


def demodulate(mode: str, iq: np.ndarray) -> np.ndarray:
    configure(mode)
    receiver = rx.Receiver(sdr=None)
    blocks = iq.reshape(BLOCKS, rx.N_SAMPLES)
    audio = [receiver.demodulate(block) for block in blocks]
    return np.concatenate(audio[1:]) / rx.VOLUME  # first block only primes the filters


def measure(audio: np.ndarray) -> tuple[float, float]:
    """Tone frequency (FFT peak) and amplitude (RMS * sqrt 2) of the recovered audio."""
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    freqs = np.fft.rfftfreq(len(audio), 1 / rx.AUDIO_RATE_HZ)
    return float(freqs[np.argmax(spectrum)]), float(np.sqrt(2) * np.std(audio))


def style(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#bdbdbd")
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color="#ececec", linewidth=0.8)
    ax.set_axisbelow(True)


def plot(results: dict) -> Path:
    fig, axes = plt.subplots(2, 2, figsize=(12, 7), constrained_layout=True)
    for row, (mode, (iq, audio, tone_hz, amplitude, channel_hz, audio_hz, deviation_hz)) in enumerate(
            results.items()):
        ax_rf, ax_audio = axes[row]
        freqs, psd = welch(iq[: rx.N_SAMPLES], fs=rx.SAMPLE_RATE_HZ, nperseg=rx.FFT_SIZE,
                           return_onesided=False, scaling="density")
        order = np.argsort(freqs)
        rf_khz = (freqs[order] + rx.OFFSET_HZ) / 1e3  # relative to the station
        ax_rf.axvspan(-channel_hz / 1e3, channel_hz / 1e3, color=BAND, zorder=0)
        ax_rf.plot(rf_khz, 10 * np.log10(psd[order]), color=SERIES, linewidth=1.2)
        ax_rf.set_xlim(-60, 60)
        ax_rf.set_xlabel("Frecuencia relativa a la estación (kHz)", color=MUTED, fontsize=9)
        ax_rf.set_ylabel("PSD (dB/Hz)", color=MUTED, fontsize=9)
        ax_rf.text(0, 0.03, f"filtro de canal ±{channel_hz / 1e3:.0f} kHz",
                   transform=ax_rf.get_xaxis_transform(), ha="center", va="bottom", color=MUTED, fontsize=8)

        fa, pa = welch(audio, fs=rx.AUDIO_RATE_HZ, nperseg=2**12)
        ax_audio.plot(fa / 1e3, 10 * np.log10(pa + 1e-20), color=SERIES, linewidth=1.2)
        ax_audio.set_xlim(0, rx.AUDIO_RATE_HZ / 2e3)
        ax_audio.axvline(audio_hz / 1e3, color=MUTED, linewidth=0.8, linestyle="--")
        ax_audio.text(audio_hz / 1e3, 0.6, f" corte de audio {audio_hz / 1e3:.0f} kHz",
                      transform=ax_audio.get_xaxis_transform(), color=MUTED, fontsize=8)
        ax_audio.set_xlabel("Frecuencia de audio (kHz)", color=MUTED, fontsize=9)
        ax_audio.set_ylabel("PSD (dB/Hz)", color=MUTED, fontsize=9)
        peak = 10 * np.log10(pa.max())
        ax_audio.annotate(f"tono recuperado: {tone_hz:.0f} Hz", xy=(tone_hz / 1e3, peak),
                          xytext=(8, -4), textcoords="offset points", color=INK, fontsize=9)
        ax_audio.set_ylim(top=peak + 10)

        if mode == "am":
            title = f"AM · m = {AM_DEPTH} → medido {amplitude:.2f}"
        else:
            title = (f"FM banda estrecha · desviación {FM_DEVIATION_HZ / 1e3:.0f} kHz → "
                     f"medida {amplitude * deviation_hz / 1e3:.2f} kHz")
        ax_rf.set_title(f"{title}\nEspectro de RF (IQ)", loc="left", color=INK, fontsize=10)
        ax_audio.set_title("Audio demodulado", loc="left", color=INK, fontsize=10)
        style(ax_rf)
        style(ax_audio)

    fig.suptitle(f"Cadena de demodulación con señales sintéticas (tono de {TONE_HZ} Hz, SNR {SNR_DB} dB)",
                 x=0.01, ha="left", fontsize=13, fontweight="bold", color="#1a1a1a")
    out = ROOT / "docs" / "offline_demo.png"
    out.parent.mkdir(exist_ok=True)
    fig.savefig(out, dpi=130, facecolor="white")
    return out


def main() -> None:
    rng = np.random.default_rng(SEED)
    results = {}
    for mode in ("am", "fm_narrow"):
        iq = synthesize(mode, rng)
        audio = demodulate(mode, iq)
        tone_hz, amplitude = measure(audio)
        results[mode] = (iq, audio, tone_hz, amplitude,
                         rx.CHANNEL_CUTOFF_HZ, rx.AUDIO_CUTOFF_HZ, rx.MAX_DEVIATION_HZ)
        if mode == "am":
            print(f"AM:        tone {tone_hz:7.1f} Hz | depth m = {amplitude:.3f} (expected {AM_DEPTH})")
        else:
            print(f"FM narrow: tone {tone_hz:7.1f} Hz | deviation = {amplitude * rx.MAX_DEVIATION_HZ:.0f} Hz "
                  f"(expected {FM_DEVIATION_HZ})")
    print(f"Figure written to {plot(results)}")


if __name__ == "__main__":
    main()
