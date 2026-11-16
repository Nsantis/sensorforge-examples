#!/usr/bin/env python3
"""Receive the SensorForge live streaming API (protocol: ../LIVE-STREAM-API.md).

    connect URL    the phone is the server ("Stream on this network"): connect to it
    serve          the phone is the client ("Push to my endpoint"): run the endpoint it pushes to
    replay LOG     no phone needed: play a recorded log folder back as if it were the phone

Examples

    python tools/sensorforge_live_client.py connect ws://192.168.1.23:8765
    python tools/sensorforge_live_client.py connect ws://192.168.1.23:8765 --save live/

    export SF_LIVE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    python tools/sensorforge_live_client.py serve --port 8765 --save live/

    python tools/sensorforge_live_client.py replay data/SF_20261031-091553_2-sensors   # terminal 1
    python tools/sensorforge_live_client.py connect ws://127.0.0.1:8765                # terminal 2

Without --save a summary is printed once a second. With --save DIR every log is written to
DIR/SF_live_<start time>/ in the layout of the app's own logs (session.json + one CSV per
stream), so the notebooks can open it with load_session().

The feed is best effort; the log on the phone stays the lossless copy.
The feed and the saved files are personal physiological data: see "Security" in
LIVE-STREAM-API.md before you expose `serve` to anything but your own network.

Requires Python 3.9+ and `pip install "websockets>=13"`. MIT licensed.
"""
import argparse
import asyncio
import csv
import heapq
import hmac
import http
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.asyncio.server import serve as ws_serve
    from websockets.exceptions import ConnectionClosed, InvalidHandshake
except ImportError:
    sys.exit('This example needs: pip install "websockets>=13"')

API_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024            # one frame is a few kB; refuse anything absurd
SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")  # device ids and stream names become file names
JSON_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?")


# ---------------------------------------------------------------------------------------------
# Receiving

class Receiver:
    """Handles the messages of one feed: prints a summary, and saves CSVs when `save_dir` is set."""

    def __init__(self, save_dir=None):
        self.save_dir = Path(save_dir) if save_dir else None
        self.log_dir = None      # folder of the running log
        self.files = {}          # file name -> (handle, csv writer)
        self.counts = {}         # "device/stream" -> [rows since the last summary, last row]
        self.printed = time.monotonic()

    def handle(self, text):
        try:
            msg = json.loads(text)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "hello":
            print(f'hello from {msg.get("app")} {msg.get("appVersion", "")} (api {msg.get("api")})')
            if msg.get("api") != API_VERSION:
                print(f"  note: this example was written for api {API_VERSION}")
        elif kind == "session":
            self.on_session(msg)
        elif kind == "data":
            self.on_data(msg)
        elif kind == "marker":
            print(f'marker "{msg.get("label")}"')
            self.write("markers", ["unix_time_ms", "label"], [[msg.get("rx_unix_ms"), msg.get("label")]])
        elif kind == "event":
            print(f'event {msg.get("kind")} {msg.get("device", "")} {msg.get("detail", "")}'.rstrip())
        # Anything else is ignored on purpose: new message types may be added without a new api number.

    def on_session(self, msg):
        manifest = msg.get("manifest") or {}
        if msg.get("state") == "started":
            devices = ", ".join(str(d.get("name", d.get("id"))) for d in manifest.get("devices", []))
            print(f'log started {manifest.get("startedAt", "")}: {devices}')
            if self.save_dir:
                self.open_log(manifest)
        else:
            print("log stopped")
            self.close()

    def on_data(self, msg):
        device, stream = msg.get("device"), msg.get("stream")
        columns, rows = msg.get("columns"), msg.get("rows")
        # The names come from the network and end up in a file path, so check them.
        if not (isinstance(device, str) and SAFE_NAME.fullmatch(device)
                and isinstance(stream, str) and SAFE_NAME.fullmatch(stream)
                and isinstance(columns, list) and isinstance(rows, list) and rows):
            return
        count = self.counts.setdefault(f"{device}/{stream}", [0, None])
        count[0] += len(rows)
        count[1] = dict(zip(columns, rows[-1]))
        self.write(f"{device}_{stream}", columns, rows)
        self.summary()

    def summary(self):
        now = time.monotonic()
        elapsed = now - self.printed
        if elapsed < 1:
            return
        for name, (n, last) in sorted(self.counts.items()):
            if n:
                values = {k: v for k, v in last.items() if k not in ("sensor_ts_ns", "frame", "rx_unix_ms")}
                print(f"  {name:<22}{n / elapsed:7.1f} rows/s   {values}")
        self.counts = {name: [0, last] for name, (n, last) in self.counts.items()}
        self.printed = now

    # Saving -----------------------------------------------------------------------------------

    def open_log(self, manifest):
        """One folder per log; a reconnect during the same log continues in the same folder."""
        started = re.sub(r"[^0-9T]", "", str(manifest.get("startedAt", "")))[:15]
        folder = self.save_dir / f'SF_live_{started or time.strftime("%Y%m%dT%H%M%S")}'
        if folder != self.log_dir:
            self.close()
            self.log_dir = folder
            folder.mkdir(parents=True, exist_ok=True)
            print(f"saving to {folder}")
        # session.json holds whatever was entered in the app (subject details, notes, location).
        (folder / "session.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), "utf-8")

    def write(self, name, columns, rows):
        if not self.save_dir:
            return
        if self.log_dir is None:      # data without a `session` message; should not happen
            self.open_log({})
        entry = self.files.get(name)
        if entry is None:
            handle = open(self.log_dir / f"{name}.csv", "a", newline="", encoding="utf-8")
            entry = self.files[name] = (handle, csv.writer(handle, lineterminator="\n"))
            if handle.tell() == 0:
                entry[1].writerow(columns)
        entry[1].writerows(rows)
        entry[0].flush()

    def close(self):
        for handle, _ in self.files.values():
            handle.close()
        self.files, self.log_dir = {}, None


async def run_connect(url, receiver):
    """Phone as server: connect to ws://<phone-ip>:<port>/ and reconnect when the link drops."""
    while True:
        try:
            async with ws_connect(url, max_size=MAX_MESSAGE_BYTES) as ws:
                print(f"connected to {url}")
                async for text in ws:
                    receiver.handle(text)
        except (OSError, ConnectionClosed, InvalidHandshake) as error:
            print(f"no connection ({error}); retrying in 2 s")
        await asyncio.sleep(2)


async def run_serve(host, port, token, receiver):
    """Phone as client: the phone connects to us and sends `Authorization: Bearer <token>`."""

    def check_token(connection, request):
        sent = request.headers.get("Authorization", "")
        if token and not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")

    async def handler(ws):
        print(f"phone connected from {ws.remote_address[0]}")
        try:
            async for text in ws:
                receiver.handle(text)
        except ConnectionClosed:
            pass
        print("phone disconnected")

    async with ws_serve(handler, host, port, process_request=check_token, max_size=MAX_MESSAGE_BYTES):
        print(f"listening on ws://{host}:{port}  (in the app: Push to my endpoint → ws://<this computer>:{port})")
        if not token:
            print("WARNING: no token set, so anything that can reach this port can send data to it.\n"
                  "         Set SF_LIVE_TOKEN here and the same value as the bearer token in the app.")
        await asyncio.Future()


# ---------------------------------------------------------------------------------------------
# Replay: a stand-in for the phone, to try the receiver (or your own client) without one

def log_frames(folder, manifest):
    """Yield (rx_unix_ms, message) of a recorded log in time order, one message per BLE frame."""

    def value(field):
        return json.loads(field) if JSON_NUMBER.fullmatch(field) else field

    def stream_frames(entry):
        with open(folder / entry["file"], newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            columns = next(reader, None)
            if not columns:
                return
            if entry.get("signal") == "markers":
                for row in reader:
                    yield int(row[0]), {"type": "marker", "rx_unix_ms": int(row[0]), "label": row[1]}
                return
            t_col, f_col = columns.index("rx_unix_ms"), columns.index("frame")
            head = {"type": "data", "device": entry.get("deviceId"), "stream": entry.get("signal"),
                    "columns": columns}
            frame, rows = None, []
            for row in reader:
                if row[f_col] != frame and rows:
                    yield rows[0][t_col], {**head, "rows": rows}
                    rows = []
                frame = row[f_col]
                rows.append([value(field) for field in row])
            if rows:
                yield rows[0][t_col], {**head, "rows": rows}

    streams = [stream_frames(e) for e in manifest.get("streams", []) if (folder / e["file"]).is_file()]
    return heapq.merge(*streams, key=lambda item: item[0])


async def play(send, folder, speed):
    manifest = json.loads((folder / "session.json").read_text("utf-8"))
    await send(json.dumps({"type": "hello", "api": API_VERSION, "app": "SensorForge replay", "appVersion": "example"}))
    await send(json.dumps({"type": "session", "state": "started", "manifest": manifest}))
    t0 = wall0 = None
    for rx_ms, message in log_frames(folder, manifest):
        if t0 is None:
            t0, wall0 = rx_ms, time.monotonic()
        delay = (rx_ms - t0) / 1000 / speed - (time.monotonic() - wall0)
        if delay > 0:
            await asyncio.sleep(delay)
        await send(json.dumps(message, separators=(",", ":"), ensure_ascii=False))
    await send(json.dumps({"type": "session", "state": "stopped"}))


async def run_replay(folder, host, port, push_url, token, speed):
    if push_url:      # act like "Push to my endpoint"
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            async with ws_connect(push_url, additional_headers=headers) as ws:
                print(f"pushing {folder.name} to {push_url}")
                await play(ws.send, folder, speed)
        except (OSError, ConnectionClosed, InvalidHandshake) as error:
            sys.exit(f"could not push to {push_url}: {error}")
        return

    async def handler(ws):      # act like "Stream on this network"; every client gets the log from the start
        print("client connected")
        try:
            await play(ws.send, folder, speed)
            await ws.wait_closed()      # stay connected, as the phone does after a log
        except ConnectionClosed:
            pass
        print("client disconnected")

    async with ws_serve(handler, host, port):
        print(f"replaying {folder.name} on ws://{host}:{port}")
        await asyncio.Future()


# ---------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("connect", help="connect to the phone (phone as server)")
    c.add_argument("url", help="ws://<phone-ip>:8765, as shown in the app")
    s = sub.add_parser("serve", help="run the endpoint the phone pushes to (phone as client)")
    s.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    s.add_argument("--port", type=int, default=8765)
    for p in (c, s):
        p.add_argument("--save", metavar="DIR", help="write each log to DIR/SF_live_<start time>/")
    r = sub.add_parser("replay", help="play a recorded log back as if it were the phone")
    r.add_argument("log", type=Path, help="log folder, e.g. data/SF_20261031-091553_2-sensors")
    r.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--push", metavar="URL", help="push to a `serve` endpoint instead of listening")
    r.add_argument("--speed", type=float, default=1.0, help="2 = twice as fast as recorded")
    args = ap.parse_args()

    # The token is read from the environment so that it stays out of the shell history and `ps`.
    token = os.environ.get("SF_LIVE_TOKEN", "")
    try:
        if args.mode == "connect":
            asyncio.run(run_connect(args.url, Receiver(args.save)))
        elif args.mode == "serve":
            asyncio.run(run_serve(args.host, args.port, token, Receiver(args.save)))
        else:
            asyncio.run(run_replay(args.log, args.host, args.port, args.push, token, args.speed))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
