"""Helpers shared by the SensorForge example notebooks (MIT licensed).

    from sfutils import *
    setup_plots()                      # zoomable figures when ipympl is installed
    S = load_session("../data/SF_…")   # session.json + every CSV it lists

Everything is plain pandas / NumPy / SciPy / Matplotlib; copy what you need.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import signal

__all__ = ["np", "pd", "plt", "signal", "mdates", "setup_plots", "load_session", "Session",
           "decorate", "time_axis", "plot_env", "Detail", "bandpass",
           "PPGGrid", "ZoomSpectrogram", "detect_r_peaks",
           "beat_table", "ppi_table", "hrv_time", "hrv_freq", "hrv_windows"]

INTERACTIVE = False
POLAR_EPOCH_UNIX_S = 946_684_800   # 2000-01-01T00:00:00Z


# ---------------------------------------------------------------------------------------------
# Plot setup

def setup_plots(interactive=None):
    """Zoomable figures (ipympl) if available, else inline. SF_STATIC=1 forces static figures."""
    global INTERACTIVE
    from IPython import get_ipython
    want = os.environ.get("SF_STATIC") is None if interactive is None else interactive
    ip = get_ipython()
    try:
        ip.run_line_magic("matplotlib", "widget" if want else "inline")
        INTERACTIVE = want
    except Exception:
        print("ipympl not installed → static figures (pip install ipympl for zoomable ones)")
        ip.run_line_magic("matplotlib", "inline")
        INTERACTIVE = False
    plt.rcParams.update({"figure.figsize": (12, 3.2), "figure.dpi": 80 if INTERACTIVE else 100,
                         "axes.grid": True, "grid.alpha": 0.3, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.max_open_warning": 0})
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 40)
    return INTERACTIVE


# ---------------------------------------------------------------------------------------------
# Loading

@dataclass
class Session:
    path: Path
    manifest: dict
    streams: dict = field(default_factory=dict)   # (device_id, signal) -> DataFrame

    @property
    def tz(self):
        return self.manifest.get("timeZone", "UTC")

    @property
    def start(self):
        return pd.Timestamp(self.manifest["startedAt"])

    @property
    def start_local(self):
        return self.start.tz_convert(self.tz).tz_localize(None)

    @property
    def end_s(self):
        if self.manifest.get("endedAt"):
            return (pd.Timestamp(self.manifest["endedAt"]) - self.start).total_seconds()
        return max(df["elapsed_s"].max() for df in self.streams.values() if len(df))

    def to_local(self, elapsed_s):
        """Seconds since start → local wall-clock time (tz-naive, fast to plot)."""
        if np.isscalar(elapsed_s):
            return self.start_local + pd.Timedelta(seconds=float(elapsed_s))
        return self.start_local + pd.to_timedelta(np.asarray(elapsed_s, float), unit="s")

    def at(self, hms):
        """Local "HH:MM[:SS]" on the day the log started → seconds since start."""
        return (pd.Timestamp(f"{self.start_local:%Y-%m-%d} {hms}") - self.start_local).total_seconds()

    def to_elapsed(self, xnum):
        """Matplotlib date number (an axis limit) → seconds since start."""
        return (xnum - mdates.date2num(self.start_local)) * 86400

    def get(self, sig, device=None):
        """Stream `sig` of `device` (id, model or name fragment, e.g. "H10"); None = first one."""
        for (dev, s), df in self.streams.items():
            if s == sig and (device is None or device == dev or device.lower() in self.name(dev).lower()):
                return df
        return None

    def device_of(self, sig):
        return next((dev for dev, s in self.streams if s == sig), None)

    def info(self, dev):
        return next((d for d in self.manifest.get("devices", []) if d["id"] == dev), {})

    def name(self, dev):
        d = self.info(dev)
        place = ((self.manifest.get("context") or {}).get("placements") or {}).get(dev, "")
        name = d.get("name", f"{d.get('model', '')} {dev}").removeprefix("Polar ")
        return name + (f" ({place.replace('_', ' ')})" if place else "")

    @property
    def markers(self):
        mk = self.streams.get((None, "markers"))
        return mk if mk is not None else pd.DataFrame(columns=["elapsed_s", "label"])

    @property
    def outages(self):
        """Connection gaps per device from the manifest's disconnected → reconnected events."""
        rows, open_ = [], {}
        for e in sorted(self.manifest.get("events", []), key=lambda e: e["at"]):
            t = (pd.Timestamp(e["at"]) - self.start).total_seconds()
            if e["kind"] == "disconnected":
                open_.setdefault(e["deviceId"], t)
            elif e["kind"] == "reconnected" and e["deviceId"] in open_:
                rows.append((e["deviceId"], open_.pop(e["deviceId"]), t))
        rows += [(dev, t, self.end_s) for dev, t in open_.items()]
        return pd.DataFrame(rows, columns=["device", "from_s", "to_s"])


def load_session(path):
    """Read session.json and every CSV it lists; add `elapsed_s` and `t` to each table.

    `elapsed_s` is seconds since the log started on the phone clock (shared by all sensors).
    Sensor timestamps (ns since 2000-01-01) are moved onto the phone clock with the device's
    clockOffsetMs. Streams without sensor timestamps (hr, rr, markers; ppi when it is all zeros)
    use the phone receive time.
    """
    path = Path(path)
    m = json.loads((path / "session.json").read_text())
    s = Session(path, m)
    epoch_ns = int(m.get("polarEpochUnixSeconds", POLAR_EPOCH_UNIX_S)) * 10**9
    offset_ms = {d["id"]: d.get("clockOffsetMs") or 0.0 for d in m.get("devices", [])}
    engine = "pyarrow" if _has("pyarrow") else None
    for meta in m.get("streams", []):
        f = path / meta["file"]
        if not f.exists():
            print("missing file:", meta["file"])
            continue
        df = pd.read_csv(f, engine=engine)
        dev = meta.get("deviceId")
        if "sensor_ts_ns" in df and (df["sensor_ts_ns"] != 0).any():
            ns = df["sensor_ts_ns"].astype("int64") + epoch_ns + int(round(offset_ms.get(dev, 0.0) * 1e6))
        else:
            ns = df["rx_unix_ms" if "rx_unix_ms" in df else "unix_time_ms"].astype("int64") * 10**6
        df["elapsed_s"] = (ns - s.start.value) / 1e9
        df["t"] = s.to_local(df["elapsed_s"].to_numpy())
        s.streams[(dev, meta["signal"])] = df
    return s


def _has(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------------------------
# Plot helpers

def decorate(ax, s, labels=False):
    """Markers as dashed lines, connection gaps shaded grey."""
    for _, o in s.outages.iterrows():
        ax.axvspan(s.to_local(o["from_s"]), s.to_local(o["to_s"]), color="0.5", alpha=0.25, lw=0)
    for _, mk in s.markers.iterrows():
        x = s.to_local(mk["elapsed_s"])
        ax.axvline(x, color="C3", ls="--", lw=1)
        if labels:
            ax.annotate(mk["label"], (x, 1), xycoords=("data", "axes fraction"), xytext=(3, -12),
                        textcoords="offset points", fontsize=8, color="C3")


def time_axis(ax):
    """Wall-clock ticks that adapt when zooming (hours → seconds)."""
    loc = mdates.AutoDateLocator(minticks=4, maxticks=10)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))


def on_zoom(ax, s, redraw):
    """Call redraw(a, b) (seconds since start) whenever the x limits change."""
    state = {"busy": False, "last": None}

    def cb(ax):
        if state["busy"]:
            return
        state["busy"] = True
        # new artists must not trigger autoscaling, which would undo the zoom
        ax._request_autoscale_view = lambda *args, **kwargs: None
        try:
            lim = tuple(map(s.to_elapsed, ax.get_xlim()))
            if state["last"] is None or not np.allclose(lim, state["last"], rtol=0, atol=1e-3):
                state["last"] = lim
                redraw(*lim)
        finally:
            del ax._request_autoscale_view
            state["busy"] = False

    if INTERACTIVE:
        ax.callbacks.connect("xlim_changed", cb)


def _bins(x, y, x0, bin_s):
    # min / mean / max per bin; empty bins become NaN so the line breaks at gaps
    k = np.floor((x - x0) / bin_s).astype(np.int64)
    starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
    lo, hi = np.minimum.reduceat(y, starts), np.maximum.reduceat(y, starts)
    mean = np.add.reduceat(y, starts) / np.diff(np.r_[starts, len(y)])
    c = x0 + (k[starts] + 0.5) * bin_s
    gap = np.flatnonzero(np.diff(k[starts]) > 1) + 1
    return (np.insert(c, gap, c[gap] - bin_s),) + tuple(np.insert(v, gap, np.nan) for v in (lo, mean, hi))


class Detail:
    """A long signal that refines when zooming: min/max envelope in N_BINS bins,
    raw samples once fewer than MAX_RAW are in view."""
    MAX_RAW, N_BINS = 6000, 2000

    def __init__(self, ax, s, df, col, color="C0", label=None, line=True):
        x, y = df["elapsed_s"].to_numpy(), np.asarray(df[col], float)
        if np.any(np.diff(x) < 0):
            o = np.argsort(x, kind="stable")
            x, y = x[o], y[o]
        self.ax, self.s, self.x, self.y, self.color, self.show_line = ax, s, x, y, color, line
        self.fill = None
        (self.line,) = ax.plot([], [], color=color, lw=0.6, label=label)
        self.draw(x[0], x[-1])
        on_zoom(ax, s, self.draw)

    def draw(self, a, b):
        i0 = max(np.searchsorted(self.x, a) - 1, 0)
        i1 = min(np.searchsorted(self.x, b) + 1, len(self.x))
        x, y = self.x[i0:i1], self.y[i0:i1]
        if self.fill is not None:
            self.fill.remove()
            self.fill = None
        if len(x) < 2:
            self.line.set_data([], [])
            return
        if len(x) <= self.MAX_RAW:
            dt = np.diff(x)
            gap = np.flatnonzero(dt > max(0.5, 5 * np.median(dt))) + 1
            self.line.set_data(self.s.to_local(np.insert(x, gap, x[gap])), np.insert(y, gap, np.nan))
            self.line.set_linewidth(0.9)
        else:
            c, lo, mean, hi = _bins(x, y, x[0], (b - a) / self.N_BINS)
            t = self.s.to_local(c)
            self.fill = self.ax.fill_between(t, lo, hi, color=self.color, alpha=0.3, lw=0)
            self.line.set_data(t, mean if self.show_line else np.full_like(mean, np.nan))
            self.line.set_linewidth(0.6)


def plot_env(ax, s, df, col, color="C0", label=None, line=True):
    return Detail(ax, s, df, col, color, label, line)


# ---------------------------------------------------------------------------------------------
# Signal processing

def bandpass(x, fs, lo, hi, order=3):
    sos = signal.butter(order, [lo, hi], btype="band", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, x)


class PPGGrid:
    """PPG on a uniform time grid: mean of `channels`, band-passed per continuous piece, zeros in
    gaps. `valid` marks samples that have data. In Polar raw PPG larger values mean more blood
    (the averaged beat rises fast and decays slowly), so no inversion is needed; invert=True flips it."""

    def __init__(self, s, ppg, channels=("ch0", "ch1", "ch2"), lo=0.8, hi=5.0, max_gap_s=0.1, min_len_s=10,
                 invert=False):
        self.s, self.lo, self.hi = s, lo, hi
        self.fs = 1e9 / np.median(np.diff(ppg["sensor_ts_ns"].to_numpy()))
        t = ppg["elapsed_s"].to_numpy()
        x = ppg[list(channels)].mean(axis=1).to_numpy(float) * (-1 if invert else 1)
        self.t0 = t[0]
        n = int(np.ceil((t[-1] - self.t0) * self.fs)) + 1
        self.x, self.valid = np.zeros(n), np.zeros(n, bool)
        br = np.r_[0, np.where(np.diff(t) > max_gap_s)[0] + 1, len(t)]
        for a, b in zip(br[:-1], br[1:]):
            if t[b - 1] - t[a] < min_len_s:
                continue
            i0, i1 = int(np.ceil((t[a] - self.t0) * self.fs)), int(np.floor((t[b - 1] - self.t0) * self.fs))
            grid = self.t0 + np.arange(i0, i1 + 1) / self.fs
            self.x[i0:i1 + 1] = bandpass(np.interp(grid, t[a:b], x[a:b]), self.fs, lo, hi)
            self.valid[i0:i1 + 1] = True

    def spectrogram(self, t_from=None, t_to=None, win_s=16, step_s=4, nfft=4096, normalize=True):
        """→ freq (Hz), time (s since start), power in dB. Windows >10 % in a gap are NaN.
        normalize: each column relative to its own maximum, so the heart-rate line stays visible
        when overall power changes with movement."""
        fs = self.fs
        i0 = 0 if t_from is None else max(0, int((t_from - self.t0) * fs))
        i1 = len(self.x) if t_to is None else min(len(self.x), int((t_to - self.t0) * fs))
        nper, step = int(win_s * fs), max(1, int(step_s * fs))
        f, tt, S = signal.spectrogram(self.x[i0:i1], fs=fs, window="hann", nperseg=nper, noverlap=nper - step,
                                      nfft=max(nfft, nper), detrend=False, scaling="density", mode="psd")
        starts = (tt * fs).astype(int) - nper // 2
        cover = np.array([self.valid[i0 + a:i0 + a + nper].mean() for a in starts])
        keep = (f >= self.lo * 0.8) & (f <= self.hi)
        f, S = f[keep], S[keep]
        S[:, cover < 0.9] = np.nan
        with np.errstate(invalid="ignore", divide="ignore"):
            if normalize:
                S = S / S.max(axis=0, keepdims=True)
            S = 10 * np.log10(S)
        return f, self.t0 + i0 / fs + tt, S


class ZoomSpectrogram:
    """PPG spectrogram on `ax` that is recomputed when zooming: with more than FULL_S in view
    16 s windows / 4 s steps (computed once); below that 8 s windows with a step that follows
    the view (≥ 0.25 s), i.e. finer time resolution the further you zoom in."""
    FULL_S = 40 * 60

    def __init__(self, ax, grid, a=None, b=None, hr=None, vmin=-25):
        self.ax, self.g, self.vmin, self.mesh, self.full = ax, grid, vmin, None, None
        s = grid.s
        if hr is not None:
            ax.plot(s.to_local(hr.index.to_numpy()), hr.to_numpy(), color="cyan", lw=0.7, alpha=0.85,
                    label="ECG heart rate", zorder=3)
            ax.legend(loc="upper right", fontsize=8)
        self.draw(grid.t0 if a is None else a, s.end_s if b is None else b)
        ax.set_ylabel("frequency (beats/min)")
        ax.grid(False)
        on_zoom(ax, s, self.draw)

    def draw(self, a, b):
        if b - a > self.FULL_S:
            if self.full is None:
                self.full = self.g.spectrogram(win_s=16, step_s=4)
            f, el, S = self.full
        else:
            f, el, S = self.g.spectrogram(a - 8, b + 8, win_s=8, step_s=max(0.25, (b - a) / 1500))
        if len(el) < 2:
            return
        if self.mesh is not None:
            self.mesh.remove()
        self.mesh = self.ax.pcolormesh(self.g.s.to_local(el), f * 60, S, shading="nearest", cmap="magma",
                                       vmin=self.vmin, vmax=0, rasterized=True, zorder=0.5)


def detect_r_peaks(ecg):
    """Simple R-peak detector: 5–20 Hz band-pass, peaks ≥ 300 ms apart above an adaptive
    threshold. → elapsed_s of the peaks. For publication work use e.g. NeuroKit2."""
    t = ecg["elapsed_s"].to_numpy()
    fs = 1e9 / np.median(np.diff(ecg["sensor_ts_ns"].to_numpy()))
    env = np.abs(bandpass(ecg["ecg_uv"].to_numpy(float), fs, 5, 20))
    thr = pd.Series(env).rolling(int(2 * fs), center=True, min_periods=1).max().to_numpy() * 0.45
    peaks, _ = signal.find_peaks(env, distance=int(0.3 * fs))
    peaks = peaks[env[peaks] > thr[peaks]]
    # refine to the raw maximum within ±40 ms (band-pass shifts the envelope slightly)
    raw, w = ecg["ecg_uv"].to_numpy(float), int(0.04 * fs)
    peaks = np.array([p - w + np.argmax(raw[max(p - w, 0):p + w + 1]) if p >= w else p for p in peaks])
    return t[peaks]


# ---------------------------------------------------------------------------------------------
# HRV

def _valid_intervals(ms, rel_tol=0.20, window=11):
    s = pd.Series(ms)
    in_range = s.between(300, 2000)
    med = s.where(in_range).rolling(window, center=True, min_periods=3).median()
    return (in_range & ((s - med).abs() <= rel_tol * med)).to_numpy()


def beat_table(s, rr):
    """RR intervals → one row per beat: elapsed_s (end of the interval), ms, valid, pair.

    Within one notification the last interval ends at the arrival time; earlier ones are
    rebuilt backwards from it. valid: 300–2000 ms and within 20 % of the local median (11 beats).
    pair: this and the previous interval are valid and adjacent (no dropout between them)."""
    ends_ms = (rr.groupby("frame")["rx_unix_ms"].transform("first")
               - rr[::-1].groupby("frame")["rr_ms"].cumsum()[::-1] + rr["rr_ms"]).to_numpy(float)
    ms = rr["rr_ms"].to_numpy(float)
    valid = _valid_intervals(ms)
    contiguous = np.r_[False, np.abs(np.diff(ends_ms) - ms[1:]) < 500]
    el = (ends_ms * 1e6 - s.start.value) / 1e9
    return pd.DataFrame({"elapsed_s": el, "t": s.to_local(el), "ms": ms, "valid": valid,
                         "pair": valid & np.r_[False, valid[:-1]] & contiguous})


def ppi_table(s, ppi):
    """PP intervals of an optical sensor, same columns as beat_table. Additionally drops
    blocker == 1 (motion) and skin_contact == 0."""
    ok = ((ppi["blocker"] == 0) & ((ppi["skin_contact"] == 1) | (ppi["skin_contact_supported"] == 0))).to_numpy()
    ms = ppi["ppi_ms"].to_numpy(float)
    valid = ok & _valid_intervals(ms)
    el = ppi["elapsed_s"].to_numpy()
    return pd.DataFrame({"elapsed_s": el, "t": s.to_local(el), "ms": ms, "valid": valid,
                         "pair": valid & np.r_[False, valid[:-1]]})


def hrv_time(w):
    """Time-domain and Poincaré metrics of a beat table (or a slice of one)."""
    ms = w.loc[w["valid"], "ms"].to_numpy()
    if len(ms) < 20:
        return {}
    allms, p = w["ms"].to_numpy(), w["pair"].to_numpy()
    x, y = allms[:-1][p[1:]], allms[1:][p[1:]]
    d = y - x
    hist, _ = np.histogram(ms, bins=np.arange(250, 2050, 1000 / 128))
    sd1, sd2 = np.std(d / np.sqrt(2), ddof=1), np.std((y + x) / np.sqrt(2), ddof=1)
    return {"beats": len(w), "artefact %": 100 * (1 - w["valid"].mean()),
            "mean RR ms": ms.mean(), "mean HR": 60000 / ms.mean(),
            "min HR": 60000 / np.percentile(ms, 99), "max HR": 60000 / np.percentile(ms, 1),
            "SDNN ms": ms.std(ddof=1), "RMSSD ms": np.sqrt(np.mean(d ** 2)), "SDSD ms": d.std(ddof=1),
            "pNN50 %": 100 * np.mean(np.abs(d) > 50), "HRV tri index": len(ms) / hist.max(),
            "SD1 ms": sd1, "SD2 ms": sd2, "SD2/SD1": sd2 / sd1}


def hrv_freq(w, fs=4.0, min_cover=0.8, return_psd=False):
    """Frequency-domain HRV: valid intervals → 4 Hz tachogram → linear detrend → Welch
    (256 s segments or the whole window). Needs ≥ 120 s and ≥ min_cover of the span in beats."""
    v = w[w["valid"]]
    if len(v) < 60:
        return {}
    t, ms = v["elapsed_s"].to_numpy(), v["ms"].to_numpy()
    span = t[-1] - t[0]
    if span < 120 or ms.sum() / 1000 < min_cover * span:
        return {}
    grid = np.arange(t[0], t[-1], 1 / fs)
    tach = signal.detrend(np.interp(grid, t, ms))
    f, p = signal.welch(tach, fs=fs, window="hann", nperseg=min(len(grid), int(256 * fs)))
    trapz = getattr(np, "trapezoid", None) or np.trapz
    band = lambda lo, hi: trapz(p[(f >= lo) & (f < hi)], f[(f >= lo) & (f < hi)])
    vlf, lf, hf = band(0.0033, 0.04), band(0.04, 0.15), band(0.15, 0.40)
    hfm = (f >= 0.15) & (f < 0.40)
    out = {"VLF ms²": vlf, "LF ms²": lf, "HF ms²": hf, "total ms²": vlf + lf + hf, "LF/HF": lf / hf,
           "LF nu": 100 * lf / (lf + hf), "HF nu": 100 * hf / (lf + hf),
           "HF peak /min": 60 * f[hfm][np.argmax(p[hfm])]}
    return (out, f, p) if return_psd else out


def hrv_windows(s, beats, width_s=300, step_s=300):
    """HRV in consecutive windows (default: 5 min, the usual short-term standard)."""
    rows = []
    for a in np.arange(0, s.end_s - width_s / 2, step_s):
        w = beats[(beats["elapsed_s"] >= a) & (beats["elapsed_s"] < a + width_s)]
        rows.append({"start_s": a, "t": s.to_local(a + width_s / 2), **hrv_time(w), **hrv_freq(w)})
    return pd.DataFrame(rows)
