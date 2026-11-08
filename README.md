# SensorForge examples

Example notebooks and real data for [SensorForge](https://nsantis.github.io/sensorforge/), the iPhone app that records lossless raw data from Polar sensors (ECG, PPG, PP intervals, acceleration, gyroscope, magnetometer …) into plain CSV files with a `session.json` manifest.

Everything here is plain Python (pandas, NumPy, SciPy, Matplotlib) and MIT licensed: copy whatever you need into your own pipeline.

| Notebook | What it shows | |
|---|---|---|
| [01 · Getting started](notebooks/01_getting_started.ipynb) | Load a log, check sample rates, gaps and clock alignment, plot everything, HRV per marker segment, ECG R-peaks, activity, a tidy 1-second export | [![Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Nsantis/sensorforge-examples/blob/main/notebooks/01_getting_started.ipynb) |
| [02 · All signals on one clock](notebooks/02_all_signals.ipynb) | Every stream of two sensors on one zoomable time axis, 8 s of every raw channel, ECG-triggered PPG average and pulse arrival time | [![Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Nsantis/sensorforge-examples/blob/main/notebooks/02_all_signals.ipynb) |
| [03 · PPG time–frequency analysis](notebooks/03_ppg_spectrogram.ipynb) | PPG spectrogram that refines as you zoom, heart rate from the spectral peak vs. ECG, at rest and in motion | [![Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Nsantis/sensorforge-examples/blob/main/notebooks/03_ppg_spectrogram.ipynb) |
| [04 · Heart-rate variability](notebooks/04_hrv.ipynb) | RR cleaning, time / frequency / Poincaré metrics, 5-minute trend, before vs. after a marker, RR (ECG) vs. PPI (optical) | [![Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Nsantis/sensorforge-examples/blob/main/notebooks/04_hrv.ipynb) |

The notebooks are stored with their output, so you can read them on GitHub without running anything.

## Run them

```bash
git clone https://github.com/Nsantis/sensorforge-examples
cd sensorforge-examples
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
jupyter lab notebooks/
```

VS Code works too: open the folder, pick the `.venv` interpreter as the kernel. In Google Colab use the badges above; the first cell of each notebook fetches this repository.

**Zoomable figures:** with `ipympl` installed (it is in `requirements.txt`), every figure has a toolbar. Long signals are drawn as min/max envelopes and refine down to the raw samples as you zoom; spectrograms are recomputed at a finer time resolution. Without ipympl, or with `SF_STATIC=1`, the figures are static.

## Data

All logs are folders exactly as the app writes them: one CSV per sensor stream plus `session.json`. Sensor timestamps are nanoseconds since 2000-01-01 UTC (the Polar epoch).

| Folder | Content |
|---|---|
| `data/SF_20261031-105000_day-out-30min` | **Real, 30 minutes**, 55 MB. Polar H10 on the chest (ECG 130 Hz, ACC 200 Hz, HR, RR) and Polar Verity Sense on the left upper arm (PPG 55 Hz, ACC and gyroscope 52 Hz, magnetometer 100 Hz, PPI) during a science-centre visit, with a *Lunch* marker. An excerpt of a 4.8-hour log, cut with `tools/trim_log.py --anonymize` (subject details and location removed). |
| `data/SF_20261031-091553_2-sensors` | Real, 65 seconds: H10 (ECG, ACC, HR, RR) and Verity Sense (PPG, HR). Subject details are demo values. |
| `data/SF_20261030-140512_2-sensors` | Synthetic, 4 minutes: rest → activity → recovery markers, PPI and a dropout. Made by `tools/make_sample_log.py`. |

**Your own logs:** in the app, *Logs › a log › Share files* (or the Files app › SensorForge › Logs). Copy the `SF_…` folder into `data/` and change `LOG` at the top of a notebook.

## Helpers

`notebooks/sfutils.py` holds what the notebooks share: `load_session` (manifest + CSVs, everything on the phone clock), zoomable plotting (`plot_env`, `ZoomSpectrogram`), `PPGGrid`, a simple R-peak detector and the HRV functions. Each function is short and documented.

`tools/trim_log.py <log> <from> <to> <out> [--anonymize]` cuts a time window out of a log and can strip personal details before you share it.

## Notes

* SensorForge is a data-recording tool for research, education and development. It is not a medical device, and nothing here is medical advice.
* SensorForge is independent and not affiliated with or endorsed by Polar Electro Oy.
* Questions or a notebook you would like to see: open an issue.
