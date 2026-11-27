#!/usr/bin/env python3
"""Analyze a 2.4 GHz occupancy log produced by logger.py.

Usage: python analyze.py [data/occupancy_*.csv] [--out results/]
Without a file argument the most recent log in data/ is used.

Outputs (in --out):
  spectrogram.png       long-term spectrogram (time x frequency, 1-minute bins)
  hourly_heatmap.png    mean occupancy per hour of day and frequency
  mean_spectrum.png     average occupancy per 1 MHz channel with WiFi bands
  band_timeseries.png   occupancy over time in WiFi channels 1/6/11 and the full band
  summary.md            key numbers and the recommended nRF24 channel
"""
import argparse
import csv
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

BASE_MHZ = 2400
NUM_CHANNELS = 126
FREQS = BASE_MHZ + np.arange(NUM_CHANNELS)

# Reference palette (validated): categorical slots 1-4 and a single-hue blue ramp.
SERIES = ["#257de0", "#f47230", "#00b894", "#f2a600"]
TEXT, TEXT_2, GRID, AXIS = "#191919", "#666666", "#e7e7e7", "#c2c2c2"
SEQUENTIAL = LinearSegmentedColormap.from_list(
    "occupancy", ["#ffffff", "#cce3fd", "#84b9f3", "#338ced", "#2370cd", "#1757ac", "#0e3d7e"]
)

# WiFi 2.4 GHz channels: center frequency, ~22 MHz wide.
WIFI = {"WiFi 1": 2412, "WiFi 6": 2437, "WiFi 11": 2462}
WIFI_HALF_WIDTH = 11


def style_axes(ax) -> None:
    ax.set_facecolor("#ffffff")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=TEXT_2, labelsize=9)
    ax.xaxis.label.set_color(TEXT_2)
    ax.yaxis.label.set_color(TEXT_2)


def title(ax, main: str, sub: str) -> None:
    ax.set_title(main, loc="left", fontsize=13, fontweight="bold", color=TEXT, pad=22)
    ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=9, color=TEXT_2)


def load(path: Path) -> tuple[np.ndarray, np.ndarray]:
    times, rows = [], []
    with path.open() as handle:
        reader = csv.reader(handle)
        header = next(reader)
        first = header.index("f2400")
        passes_col = header.index("passes")
        for record in reader:
            if len(record) != first + NUM_CHANNELS:
                continue
            passes = float(record[passes_col])
            times.append(datetime.fromisoformat(record[0]))
            rows.append([int(v) / passes for v in record[first:]])
    if not rows:
        raise SystemExit(f"No sweeps found in {path}")
    return np.array(times), np.array(rows)


def bin_minutes(times: np.ndarray, occ: np.ndarray, minutes: int) -> tuple[np.ndarray, np.ndarray]:
    stamps = np.array([t.timestamp() for t in times])
    edges = np.arange(stamps[0], stamps[-1] + minutes * 60, minutes * 60)
    idx = np.digitize(stamps, edges) - 1
    out_t, out_v = [], []
    for b in range(len(edges)):
        mask = idx == b
        if mask.any():
            out_t.append(datetime.fromtimestamp(edges[b]))
            out_v.append(occ[mask].mean(axis=0))
    return np.array(out_t), np.array(out_v)


def band_mask(center: int) -> np.ndarray:
    return (FREQS >= center - WIFI_HALF_WIDTH) & (FREQS <= center + WIFI_HALF_WIDTH)


def plot_spectrogram(times, occ, out: Path) -> None:
    bt, bv = bin_minutes(times, occ, 1)
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=150)
    style_axes(ax)
    t0, t1 = mdates.date2num(bt[0]), mdates.date2num(bt[-1])
    img = ax.imshow(bv.T * 100, aspect="auto", origin="lower", cmap=SEQUENTIAL,
                    extent=[t0, max(t1, t0 + 1e-3), FREQS[0] - 0.5, FREQS[-1] + 0.5],
                    vmin=0, interpolation="nearest")
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlabel("Hora local")
    ax.set_ylabel("Frecuencia (MHz)")
    for name, fc in WIFI.items():
        ax.annotate(name, xy=(1, fc), xycoords=("axes fraction", "data"), xytext=(6, 0),
                    textcoords="offset points", va="center", fontsize=8, color=TEXT_2)
    bar = fig.colorbar(img, ax=ax, pad=0.08)
    bar.set_label("Ocupación (%)", color=TEXT_2)
    bar.ax.tick_params(colors=TEXT_2, labelsize=8)
    bar.outline.set_visible(False)
    title(ax, "Espectrograma de largo plazo, banda de 2.4 GHz",
          f"{bt[0]:%Y-%m-%d %H:%M} a {bt[-1]:%H:%M} · promedios de 1 minuto · detector RPD > -64 dBm")
    fig.tight_layout()
    fig.savefig(out / "spectrogram.png", facecolor="#ffffff")
    plt.close(fig)


def plot_hourly(times, occ, out: Path) -> np.ndarray:
    hours = np.array([t.hour for t in times])
    present = sorted(set(hours.tolist()), key=lambda h: (h - hours[0]) % 24)
    grid = np.array([occ[hours == h].mean(axis=0) for h in present])
    fig, ax = plt.subplots(figsize=(11, 4.6), dpi=150)
    style_axes(ax)
    img = ax.imshow(grid * 100, aspect="auto", origin="lower", cmap=SEQUENTIAL, vmin=0,
                    extent=[FREQS[0] - 0.5, FREQS[-1] + 0.5, -0.5, len(present) - 0.5],
                    interpolation="nearest")
    ax.set_yticks(range(len(present)), [f"{h:02d}:00" for h in present])
    ax.set_xlabel("Frecuencia (MHz)")
    ax.set_ylabel("Hora del día")
    for name, fc in WIFI.items():
        ax.annotate(name, xy=(fc, 1), xycoords=("data", "axes fraction"), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=8, color=TEXT_2)
    bar = fig.colorbar(img, ax=ax, pad=0.02)
    bar.set_label("Ocupación media (%)", color=TEXT_2)
    bar.ax.tick_params(colors=TEXT_2, labelsize=8)
    bar.outline.set_visible(False)
    ax.set_title("Ocupación media por hora y frecuencia", loc="left", fontsize=13,
                 fontweight="bold", color=TEXT, pad=26)
    fig.tight_layout()
    fig.savefig(out / "hourly_heatmap.png", facecolor="#ffffff")
    plt.close(fig)
    return np.array(present)


def plot_mean_spectrum(occ, out: Path) -> np.ndarray:
    mean = occ.mean(axis=0)
    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=150)
    style_axes(ax)
    for name, fc in WIFI.items():
        ax.axvspan(fc - WIFI_HALF_WIDTH, fc + WIFI_HALF_WIDTH, color="#f2f2f2", zorder=0)
        ax.text(fc, 1.01, name, transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                fontsize=8, color=TEXT_2)
    ax.bar(FREQS, mean * 100, width=0.8, color=SERIES[0], zorder=2)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, zorder=1)
    ax.set_axisbelow(True)
    ax.set_xlim(FREQS[0] - 1, FREQS[-1] + 1)
    ax.set_xlabel("Frecuencia (MHz)")
    ax.set_ylabel("Ocupación media (%)")
    peak = int(np.argmax(mean))
    ax.set_ylim(0, max(mean.max() * 100 * 1.18, 1))
    ax.annotate(f"pico: {FREQS[peak]} MHz · {mean[peak]:.1%}", xy=(FREQS[peak], mean[peak] * 100),
                xytext=(10, 2), textcoords="offset points", fontsize=8, color=TEXT, va="bottom")
    ax.set_title("Ocupación media por canal de 1 MHz", loc="left", fontsize=13,
                 fontweight="bold", color=TEXT, pad=20)
    fig.tight_layout()
    fig.savefig(out / "mean_spectrum.png", facecolor="#ffffff")
    plt.close(fig)
    return mean


def plot_bands(times, occ, out: Path) -> dict:
    bt, bv = bin_minutes(times, occ, 5)
    series = {name: bv[:, band_mask(fc)].mean(axis=1) for name, fc in WIFI.items()}
    series["Banda completa"] = bv.mean(axis=1)
    fig, ax = plt.subplots(figsize=(11, 4.6), dpi=150)
    style_axes(ax)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    marker = "o" if len(bt) < 20 else None
    for color, (name, values) in zip(SERIES, series.items()):
        ax.plot(bt, values * 100, color=color, linewidth=2, label=name, marker=marker, markersize=8)
    ax.set_ylim(0, max(max(v.max() for v in series.values()) * 100 * 1.15, 1))
    # End-of-line labels, pushed apart so they never overlap.
    min_gap = (ax.get_ylim()[1] - ax.get_ylim()[0]) * 0.05
    ends = sorted(((v[-1] * 100, n) for n, v in series.items()))
    placed = []
    for y, name in ends:
        y = max(y, placed[-1] + min_gap) if placed else y
        placed.append(y)
        ax.annotate(name, xy=(bt[-1], y), xytext=(10, 0), textcoords="offset points",
                    va="center", fontsize=8, color=TEXT, annotation_clip=False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlabel("Hora local")
    ax.set_ylabel("Ocupación (%)")
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT_2, ncol=4, loc="lower left",
              bbox_to_anchor=(0, 1.0))
    ax.set_title("Ocupación por banda en el tiempo (promedios de 5 minutos)", loc="left",
                 fontsize=13, fontweight="bold", color=TEXT, pad=28)
    fig.tight_layout()
    fig.savefig(out / "band_timeseries.png", facecolor="#ffffff")
    plt.close(fig)
    return series


def best_channel(mean: np.ndarray, guard: int = 2) -> int:
    # Quietest channel considering its neighbors (nRF24 at 1 Mbps uses ~1 MHz).
    padded = np.pad(mean, guard, mode="edge")
    window = np.convolve(padded, np.ones(2 * guard + 1) / (2 * guard + 1), mode="valid")
    return int(np.argmin(window))


def write_summary(path: Path, times, occ, mean, hours_present, out: Path) -> None:
    duration = times[-1] - times[0]
    total = occ.mean(axis=1)
    hours = np.array([t.hour for t in times])
    by_hour = {h: total[hours == h].mean() for h in hours_present}
    busy_h = max(by_hour, key=by_hour.get)
    quiet_h = min(by_hour, key=by_hour.get)
    window = np.convolve(mean, np.ones(10) / 10, mode="valid")
    quiet_block = int(np.argmin(window))
    ch = best_channel(mean)
    lines = [
        "# Resumen de ocupación de 2.4 GHz",
        "",
        f"- Archivo: `{path.name}`",
        f"- Periodo: {times[0]:%Y-%m-%d %H:%M} a {times[-1]:%Y-%m-%d %H:%M} ({duration})",
        f"- Barridos: {len(times)} (126 canales × 30 muestras cada uno)",
        f"- Ocupación media de la banda: {total.mean():.2%}",
        f"- Canal más ocupado: {FREQS[int(np.argmax(mean))]} MHz ({mean.max():.1%})",
        f"- Hora más ocupada: {busy_h:02d}:00 ({by_hour[busy_h]:.2%}) · "
        f"más tranquila: {quiet_h:02d}:00 ({by_hour[quiet_h]:.2%})",
        f"- Bloque de 10 MHz más libre: {FREQS[quiet_block]}-{FREQS[quiet_block] + 9} MHz",
        f"- **Canal recomendado para nRF24: {ch} ({FREQS[ch]} MHz)**",
        "",
        "| Banda | Ocupación media |",
        "|---|---|",
    ]
    for name, fc in WIFI.items():
        lines.append(f"| {name} ({fc - WIFI_HALF_WIDTH}-{fc + WIFI_HALF_WIDTH} MHz) | "
                     f"{occ[:, band_mask(fc)].mean():.2%} |")
    lines.append(f"| Banda completa | {total.mean():.2%} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="?", type=Path)
    parser.add_argument("--out", type=Path, default=root / "results")
    args = parser.parse_args()
    path = args.csv or max((root / "data").glob("occupancy_*.csv"), key=lambda p: p.stat().st_mtime)
    args.out.mkdir(parents=True, exist_ok=True)

    times, occ = load(path)
    plot_spectrogram(times, occ, args.out)
    hours_present = plot_hourly(times, occ, args.out)
    mean = plot_mean_spectrum(occ, args.out)
    plot_bands(times, occ, args.out)
    write_summary(path, times, occ, mean, hours_present, args.out)
    print(f"\nFigures written to {args.out}")


if __name__ == "__main__":
    main()
