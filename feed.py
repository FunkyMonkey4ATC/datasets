"""Build the agent's raw input for one scenario: data/sim/<id>/feed.jsonl.

feed.jsonl is the only thing the agent reads. One JSON object per line, sorted by "t", the time the
line becomes available. Replay = sleep until each line's t, then emit it. Three line types:

  radio    {"type": "radio", "t": <end + STT latency>, "freq": "tower", "start": ..., "end": ...,
            "text": "Truck One, cross runway four at Delta.", "conf": 0.0-1.0,
            "words": [{"w": "cross", "s": <iso>, "e": <iso>, "p": 0.97}, ...] or null,
            "audio": {"file": "tower.wav", "offset_s": 424.0, "dur_s": 2.4}, "src": "transcript" | "stt"}
  track    a tracks.jsonl row plus "type": "track" (vehicles have callsign null)
  weather  a weather.jsonl row plus "type": "weather"

Radio lines carry only what a speech-to-text model produces: no speaker, callsign or action. The agent
must infer those; the truth stays in transmissions.jsonl and expected.json. A real STT run (Whisper with
word timestamps, one stream per frequency) writes the same radio line with src "stt".

  python feed.py [scenario_dir ...] [--latency 1.5]   # default: every scenario under data/sim
"""
import json, sys
from datetime import datetime, timedelta
from pathlib import Path

TRUTH = {"speaker", "callsign", "action", "runway", "taxiway", "readback_action"}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-5] + "Z"


def radio(tx, latency):
    start, end = ts(tx["t"]), ts(tx["t_end"])
    return {"type": "radio", "t": iso(end + timedelta(seconds=latency)), "freq": tx["freq"], "start": tx["t"], "end": tx["t_end"],
            "text": tx["text"], "conf": 1.0, "words": None,
            "audio": {"file": tx["audio"], "offset_s": tx["offset_s"], "dur_s": round((end - start).total_seconds(), 2)},
            "src": "transcript"}


def build(d, latency=0.0):
    rows = [radio(json.loads(l), latency) for l in open(d / "transmissions.jsonl")]
    rows += [{"type": "track", **json.loads(l)} for l in open(d / "tracks.jsonl")]
    for l in open(d / "weather.jsonl"):  # a report becomes available when the API has it, not at observation time
        w = json.loads(l)
        rows.append({"type": "weather", **w, "obs_t": w["t"], "t": w["api"]["receiptTime"]} if "api" in w else {"type": "weather", **w})
    rows.sort(key=lambda r: ts(r["t"]))  # parse: "…03.4Z" and "…03Z" do not sort correctly as strings
    assert not any(TRUTH & r.keys() for r in rows if r["type"] == "radio"), "truth labels leaked into the feed"
    with open(d / "feed.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    return len(rows)


if __name__ == "__main__":
    args = sys.argv[1:]
    latency = 0.0
    if "--latency" in args:
        i = args.index("--latency")
        latency = float(args.pop(i + 1))
        args.pop(i)
    dirs = [Path(a) for a in args] or \
        sorted(p.parent for p in (Path(__file__).parent / "data" / "sim").glob("*/transmissions.jsonl"))
    for d in dirs:
        print(d.name, build(d, latency), "lines")
