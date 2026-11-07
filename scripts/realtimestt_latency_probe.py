#!/usr/bin/env python3
"""Measure live-preview latency of a RealtimeSTT ``/ws/transcribe`` server.

Streams a 16 kHz mono WAV at real-time pace using hyprwhspr's packet format
and logs every ``realtime``/``final`` event: arrival time relative to the
audio clock, words gained per event, and the server's own timing fields
(queue delay, inference duration).  It separates server/model latency from
anything hyprwhspr adds downstream (OSD file throttle, poll, draw).

    python3 scripts/realtimestt_latency_probe.py --wav speech.wav \
        --url ws://127.0.0.1:18011/ws/transcribe
"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import threading
import time
import wave

import websocket


def load_pcm(path):
    with wave.open(path, "rb") as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit("expected 16 kHz mono 16-bit WAV (convert with sox/ffmpeg)")
        return wav.readframes(wav.getnframes())


def packet(pcm: bytes) -> bytes:
    meta = json.dumps(
        {"sampleRate": 16000, "channels": 1, "format": "pcm_s16le", "frames": len(pcm) // 2},
        separators=(",", ":"),
    ).encode()
    return struct.pack("<I", len(meta)) + meta + pcm


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wav", required=True)
    parser.add_argument("--url", default="ws://127.0.0.1:18011/ws/transcribe")
    parser.add_argument("--chunk-ms", type=float, default=64.0, help="hyprwhspr sends ~64 ms blocks")
    parser.add_argument("--tail-seconds", type=float, default=4.0)
    parser.add_argument("--json", action="store_true", help="dump raw events as JSON lines")
    args = parser.parse_args()

    pcm = load_pcm(args.wav)
    ws = websocket.create_connection(args.url, timeout=10)
    while json.loads(ws.recv()).get("type") != "ready":
        pass
    ws.send(json.dumps({"type": "clear"}))
    ws.send(json.dumps({"type": "start"}))

    events = []
    state = {"t0": None, "audio_sent": 0.0, "done": False}

    def receiver():
        ws.settimeout(0.5)
        while not state["done"]:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                return
            if isinstance(raw, bytes):
                continue
            event = json.loads(raw)
            if event.get("type") in ("realtime", "final") and state["t0"] is not None:
                event["_t"] = time.monotonic() - state["t0"]
                event["_audio"] = state["audio_sent"]
                events.append(event)
                if args.json:
                    print(json.dumps(event), flush=True)

    thread = threading.Thread(target=receiver, daemon=True)
    thread.start()

    chunk_bytes = int(16000 * args.chunk_ms / 1000) * 2
    state["t0"] = time.monotonic()
    for offset in range(0, len(pcm), chunk_bytes):
        chunk = pcm[offset : offset + chunk_bytes]
        target = state["t0"] + offset / 2 / 16000
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        ws.send(packet(chunk), opcode=websocket.ABNF.OPCODE_BINARY)
        state["audio_sent"] = (offset + len(chunk)) / 2 / 16000
    time.sleep(args.tail_seconds)
    ws.send(json.dumps({"type": "stop"}))
    time.sleep(2.0)
    state["done"] = True
    thread.join(1.0)
    ws.close()

    realtime = [e for e in events if e["type"] == "realtime"]
    finals = [e for e in events if e["type"] == "final"]
    if not args.json:
        print(f"{'t(s)':>6} {'seg':>3} {'words':>5} {'+w':>3} {'queue':>6} {'infer':>6}  text tail")
    gaps, gains, infers, queues = [], [], [], []
    last_t, last_words = {}, {}
    for e in events:
        seg = e.get("segmentId")
        text = e.get("displayText") or e.get("text") or ""
        words = len(text.split())
        timing = e.get("timing") or {}
        gain = words - last_words.get(seg, 0)
        if e["type"] == "realtime":
            if seg in last_t:
                gaps.append(e["_t"] - last_t[seg])
            gains.append(gain)
            if timing.get("inference_duration_seconds") is not None:
                infers.append(timing["inference_duration_seconds"])
            if timing.get("queue_delay_seconds") is not None:
                queues.append(timing["queue_delay_seconds"])
            last_t[seg] = e["_t"]
        last_words[seg] = words
        if not args.json:
            kind = "F" if e["type"] == "final" else " "
            print(
                f"{e['_t']:6.2f} {seg!s:>3} {words:5d} {gain:+3d} "
                f"{timing.get('queue_delay_seconds', float('nan')):6.2f} "
                f"{timing.get('inference_duration_seconds', float('nan')):6.2f} {kind} "
                f"…{text[-60:]}"
            )

    def summary(name, values, unit="s"):
        if values:
            values = sorted(values)
            p90 = values[int(0.9 * (len(values) - 1))]
            print(f"  {name:<28} median {statistics.median(values):.3f}{unit}  p90 {p90:.3f}{unit}  max {values[-1]:.3f}{unit}")

    print(f"\n{len(realtime)} realtime events, {len(finals)} finals over {state['audio_sent']:.1f}s audio")
    summary("gap between realtime events", gaps)
    summary("server inference per pass", infers)
    summary("server queue delay", queues)
    summary("words gained per event", gains, unit="")


if __name__ == "__main__":
    main()
