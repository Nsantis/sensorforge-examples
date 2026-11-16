# SensorForge live streaming API (v1)

SensorForge can forward every sensor frame in real time while a log is running. The data goes from the phone straight to your own code: nothing passes through SensorForge servers, and there are no accounts or API keys. Live streaming is part of **Pro**; turn it on in the app under *Settings › Live streaming API*.

The live feed carries the **same columns and values as the CSV files**, so code written for finished logs also works on the live feed.

A ready-made receiver is in [`tools/sensorforge_live_client.py`](tools/sensorforge_live_client.py); see [Example](#example).

## Transports

| Mode | How | When |
|---|---|---|
| **Phone as server** (*Stream on this network*) | The phone listens on `ws://<phone-ip>:8765/` (the port is configurable) and shows the address in the app. It is advertised over Bonjour as `_sensorforge._tcp`. Several clients can connect at once. | Computer on the same Wi-Fi as the phone |
| **Phone as client** (*Push to my endpoint*) | The phone connects out to a `ws://` or `wss://` URL you enter, optionally with an `Authorization: Bearer <token>` header. It reconnects with backoff. | Phone on cellular, or you want the data on your own server |

Both modes send the same messages. Each WebSocket **text** message holds one JSON object. Messages from the other side are ignored.

Delivery is best effort. A client that falls behind (more than 256 messages in flight) loses messages, and nothing is buffered while the phone-as-client connection is down. **The log on the phone is always the lossless copy.**

## Messages

### `hello`

Sent first on every connection.

```json
{"type":"hello","api":1,"app":"SensorForge","appVersion":"1.0"}
```

### `session`

Sent when a log starts or stops. A client that connects in the middle of a log receives the `started` message right after `hello`. `manifest` is the log's `session.json` as it is at the start: devices, firmware, stream settings, time zone, the Polar epoch, and the session details entered in the app.

```json
{"type":"session","state":"started","manifest":{ ... }}
{"type":"session","state":"stopped"}
```

### `data`

One message per Bluetooth frame per stream.

```json
{"type":"data","device":"A1B2C3D4","stream":"acc",
 "columns":["sensor_ts_ns","frame","rx_unix_ms","x_mg","y_mg","z_mg"],
 "rows":[[846123456789000000,812,1793350212345,12,-980,33],
         [846123456809000000,812,1793350212345,14,-978,30]]}
```

* `device` is the Polar device id. One log can contain several devices.
* `stream` is the name the CSV file has after the device id (`A1B2C3D4_acc.csv` → `acc`).
* `columns` are the CSV header of that stream, and each row is one CSV line. Numbers are JSON numbers; anything else, such as the PPG `frame_type` (`"ppg3_ambient1"`), is a string.
* `sensor_ts_ns` is nanoseconds since **2000-01-01T00:00:00Z** on the sensor's clock (the Polar epoch). It can be larger than 2⁵³, so parse it as a 64-bit integer where the language does not do that by default (in JavaScript a plain `JSON.parse` rounds it).
* `rx_unix_ms` is the phone's clock (Unix milliseconds) when the frame arrived. It is shared by all devices, so use it to align devices with each other. Rows of one frame share `frame` and `rx_unix_ms`.

| `stream` | Columns after the timestamps |
|---|---|
| `hr` | `hr_bpm, corrected_hr_bpm, ppg_quality, rr_available, contact, contact_supported` |
| `rr` | `rr_ms` |
| `ecg` | `ecg_uv` |
| `acc` | `x_mg, y_mg, z_mg` |
| `gyro` | `x_dps, y_dps, z_dps` |
| `mag` | `x_gauss, y_gauss, z_gauss` |
| `ppg` | `frame_type, ch0 … chN` (N depends on the sensor and the frame type) |
| `ppi` | `ppi_ms, error_estimate_ms, hr_bpm, blocker, skin_contact, skin_contact_supported` |
| `temperature`, `skintemp` | `temperature_c` |
| `pressure` | `pressure_bar` |

`hr` and `rr` start with `rx_unix_ms, frame`; the others with `sensor_ts_ns, frame, rx_unix_ms`. Read the column names from the message rather than relying on positions: columns may be added.

### `marker`

```json
{"type":"marker","rx_unix_ms":1793350212345,"label":"Rest"}
```

### `event`

```json
{"type":"event","kind":"disconnected","device":"A1B2C3D4","rx_unix_ms":1793350212345}
```

Kinds: `disconnected`, `reconnected`, `streamError`, `settingsError`. The error kinds carry a `detail` text.

## Example

```bash
pip install "websockets>=13"

# Phone as server: connect to the address shown in the app
python tools/sensorforge_live_client.py connect ws://192.168.1.23:8765

# Phone as client: run the endpoint the phone pushes to
export SF_LIVE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "$SF_LIVE_TOKEN"        # enter this as the bearer token in the app
python tools/sensorforge_live_client.py serve --port 8765
```

Both print a summary once a second. Add `--save live/` to write each log to `live/SF_live_<start time>/` in the layout of the app's own logs, which the notebooks open with `load_session()`.

No phone at hand? `replay` plays a recorded log back in real time as if it were the phone:

```bash
python tools/sensorforge_live_client.py replay data/SF_20261031-091553_2-sensors   # terminal 1
python tools/sensorforge_live_client.py connect ws://127.0.0.1:8765                # terminal 2
```

`replay --push ws://127.0.0.1:8765` does the same against a running `serve`.

The smallest possible client:

```python
import asyncio, json
from websockets.asyncio.client import connect

async def main():
    async with connect("ws://192.168.1.23:8765") as ws:
        async for text in ws:
            msg = json.loads(text)
            if msg["type"] == "data" and msg["stream"] == "hr":
                row = dict(zip(msg["columns"], msg["rows"][-1]))
                print(msg["device"], row["hr_bpm"], "bpm")

asyncio.run(main())
```

## Security

The feed is physiological data, and the `session` manifest can include subject details, notes and location. Treat it and anything you save from it as personal data.

* **Phone as server** has no authentication or encryption: anyone on the same network can connect while it is on. Use it on a network you trust, and turn it off when you are done.
* **Phone as client:** set a long random bearer token and check it on your server before accepting the connection, as the example does. The token is sent in an HTTP header, so over plain `ws://` it and the data are readable on the way. Use `wss://` (for example a reverse proxy with a TLS certificate in front of the receiver) for anything that leaves your own network.
* Keep tokens out of code, command lines and repositories. The example reads `SF_LIVE_TOKEN` from the environment.
* Treat incoming messages as untrusted input on a server: the example limits the message size and only accepts plain device and stream names before using them as file names.
* `live/` is in this repository's `.gitignore` so that received data is not committed by accident. Before sharing a saved log, `tools/trim_log.py --anonymize` removes the personal details from `session.json`.

## Versioning

`hello.api` increases only on breaking changes. New message types, fields and columns may be added at any time, so ignore what you do not know.
