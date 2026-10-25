"""Stand in for the web app: replay one scenario into the agentic-workflow stack the way it takes input today.

What the backend accepts today (agentic-workflow c30d6d8). One message per transmission, so Tower and
Ground interleave on the same endpoint and no second audio stream is needed.
  radio    POST {asr}/transcribe   multipart file=<one transmission .wav>   (--radio audio, default)
           POST {asr}/inject       {"text", "t_start", "t_end"}             (--radio text: perfect speech-to-text)
  tracks   POST {hub}/events  type "track":   callsign, lat, lon, alt_ft, gs_kt, on_runway, clear_of_runway
  weather  POST {hub}/events  type "weather": station, visibility_sm, ceiling_ft, wind, raw, as_of, stale
  alerts   GET  {hub}/events?types=alert,...   (what the dashboard reads)
Radio is sent when a transmission ends (as the web app's mic does), weather when the METAR API would have it.
The decision service keeps its windows on the wall clock, so replay in real time (--speed 1).

  python stub_frontend.py data/sim/lga_inc01_lga_reconstruction --from 290
  --hub/--asr default to localhost; for the GB10 use an SSH tunnel (the services listen on 127.0.0.1 only).
Writes runs/<scenario>-<UTC>/: sent.jsonl, events.jsonl (every hub event during the run), alerts.jsonl, run.json.
"""
import argparse, io, json, math, re, threading, time, urllib.request, uuid, wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from simulate import AIRLINES

ZONE_M = 60  # inside the 75 m hold-short line counts as on the runway
PAD_S = 0.3  # silence kept around each transmission, like the web app's recorder
ALERT_TYPES = {"alert", "alert.update", "conflict"}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-5] + "Z"


def jl(p):
    return [json.loads(l) for l in open(p)]


def http(url, body=None, ctype="application/json", timeout=30):
    req = urllib.request.Request(url, data=body, headers={"content-type": ctype} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def post_json(url, obj, timeout=30):
    return http(url, json.dumps(obj).encode(), timeout=timeout)


def multipart(name, data, ctype):
    b = uuid.uuid4().hex
    head = f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'
    return head.encode() + data + f"\r\n--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


class Runways:
    """Which runway zone a ground position is in, named by the active runway end ("4", not "22")."""

    def __init__(s, airport, active):
        s.lat0, s.lon0 = airport["center"]
        s.kx = 111_320 * math.cos(math.radians(s.lat0))
        s.lines = []
        for key, (p, q) in airport["runways"].items():
            ends = [e.lstrip("0") for e in key.split("/")]
            s.lines.append((next((e for e in ends if e in active), ends[0]), s.xy(*p), s.xy(*q)))

    def xy(s, lat, lon):
        return ((lon - s.lon0) * s.kx, (lat - s.lat0) * 111_320)

    def on(s, lat, lon):
        px, py = s.xy(lat, lon)
        for name, (ax, ay), (bx, by) in s.lines:
            dx, dy, L2 = bx - ax, by - ay, (bx - ax) ** 2 + (by - ay) ** 2
            t = ((px - ax) * dx + (py - ay) * dy) / L2
            if 0 <= t <= 1 and abs((px - ax) * dy - (py - ay) * dx) / math.sqrt(L2) < ZONE_M:
                return name
        return None


def backend_callsign(row, vehicles, vehicle_ids):
    """The parser's callsign format: telephony name in capitals, then digits ("DELTA 4057", "TRUCK 1")."""
    if row["kind"] == "vehicle":
        return vehicles.get(row["id"], "") if vehicle_ids == "truth" else ""
    m = re.fullmatch(r"([A-Z]{3})(\d+)", row["callsign"] or "")
    return f"{AIRLINES[m[1]].upper().removesuffix(' FLIGHT')} {m[2]}" if m else (row["callsign"] or "")  # parser: "Frontier Flight 1136" -> "FRONTIER 1136"


def clip(d, tx):
    """One transmission cut from its frequency's stream, with PAD_S of the idle hiss on each side."""
    with wave.open(str(d / tx["audio"])) as w:
        sr = w.getframerate()
        dur = (ts(tx["t_end"]) - ts(tx["t"])).total_seconds()
        w.setpos(max(0, int((tx["offset_s"] - PAD_S) * sr)))
        frames = w.readframes(int((dur + 2 * PAD_S) * sr))
        out = io.BytesIO()
        with wave.open(out, "wb") as o:
            o.setnchannels(1), o.setsampwidth(2), o.setframerate(sr)
            o.writeframes(frames)
        return out.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenario", type=Path, help="data/sim/<id>")
    ap.add_argument("--hub", default="http://127.0.0.1:7000")
    ap.add_argument("--asr", default="http://127.0.0.1:9100")
    ap.add_argument("--radio", choices=["audio", "text"], default="audio")
    ap.add_argument("--vehicle-ids", choices=["truth", "none"], default="truth",
                    help="truth: vehicle tracks carry their radio callsign, which the decision service needs today")
    ap.add_argument("--from", dest="from_s", type=float, default=0, help="start this many scenario seconds in")
    ap.add_argument("--to", type=float, default=math.inf, help="stop at this scenario second")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--tail", type=float, default=20, help="seconds to keep collecting alerts after the last message")
    a = ap.parse_args()

    d = a.scenario
    exp = json.load(open(d / "expected.json"))
    start = ts(exp["start"])
    sec = lambda t: (ts(t) - start).total_seconds()
    rwys = Runways(json.load(open(d / "airport.json")), set(exp["config"].values()))
    was_on = {}

    def track_event(r):
        on = rwys.on(r["lat"], r["lon"]) if r["on_ground"] else None
        cleared = bool(was_on.get(r["id"]) and r["on_ground"] and not on)
        was_on[r["id"]] = on or (was_on.get(r["id"]) if not r["on_ground"] else None)
        return {"type": "track", "source": "stub", "data": {
            "callsign": backend_callsign(r, exp["vehicles"], a.vehicle_ids), "lat": r["lat"], "lon": r["lon"],
            "alt_ft": 0 if r["on_ground"] else None, "gs_kt": r["speed_kt"], "heading": r["heading"], "kind": r["kind"],
            "on_runway": on, "clear_of_runway": cleared, "track_id": r["id"], "scenario_t": r["t"], "simulated": True}}

    def weather_event(w):
        o = w["api"]
        wind = f"{o['wdir']:03d}{o['wspd']:02d}" + (f"G{o['wgst']:02d}" if o.get("wgst") else "") + "KT"
        return {"type": "weather", "source": "stub", "data": {
            "station": o["icaoId"], "visibility_sm": w["visibility_sm"], "ceiling_ft": w["ceiling_ft"], "wind": wind,
            "raw": o["rawOb"], "as_of": w["t"], "stale": False, "simulated": True}}

    def send(kind, item):
        if kind == "radio" and a.radio == "audio":
            body, ctype = multipart(f"{uuid.uuid4().hex[:8]}.wav", clip(d, item), "audio/wav")
            return http(f"{a.asr}/transcribe?publish=true&stream=true", body, ctype, timeout=60)
        if kind == "radio":
            return post_json(f"{a.asr}/inject", {"text": item["text"], "t_start": sec(item["t"]), "t_end": sec(item["t_end"])})
        return post_json(f"{a.hub}/events", track_event(item) if kind == "track" else weather_event(item))

    plan = [(sec(x["t_end"]), "radio", x) for x in jl(d / "transmissions.jsonl")]
    plan += [(sec(r["t"]), "track", r) for r in jl(d / "tracks.jsonl")]
    plan += [(sec(w["api"]["receiptTime"]), "weather", w) for w in jl(d / "weather.jsonl")]
    plan = sorted((p for p in plan if a.from_s <= p[0] <= a.to), key=lambda p: (p[0], p[1] != "weather"))

    # Hub clock vs ours, so alert times can be put on the scenario clock.
    t1 = time.time()
    seq0 = post_json(f"{a.hub}/events", {"type": "stub.sync", "source": "stub", "data": {"scenario": exp["id"]}})["seq"]
    t2 = time.time()
    skew = http(f"{a.hub}/events?since={seq0 - 1}&types=stub.sync")[-1]["recv_ms"] / 1000 - (t1 + t2) / 2

    out = Path(__file__).parent / "runs" / f"{exp['id']}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True)
    wall0 = time.time() + 1
    scen_t = lambda wall: iso(start + timedelta(seconds=a.from_s + (wall - wall0) * a.speed))
    (out / "run.json").write_text(json.dumps({**vars(a), "scenario": exp["id"], "scenario_start": exp["start"], "to": None if a.to == math.inf else a.to,
                                              "first_seq": seq0, "hub_clock_skew_s": round(skew, 3), "replay_starts_at": scen_t(wall0)}, default=str, indent=1))
    sent, done, lock = open(out / "sent.jsonl", "w"), threading.Event(), threading.Lock()

    def job(t, kind, item):
        t0 = time.time()
        try:
            res, ok = send(kind, item), True
        except Exception as e:  # keep replaying; the log shows what failed
            res, ok = str(e)[:200], False
        with lock:
            sent.write(json.dumps({"scenario_t": iso(start + timedelta(seconds=t)), "kind": kind, "ok": ok, "ms": round((time.time() - t0) * 1000),
                                   "text": item.get("text") if kind == "radio" else None, "result": res if kind == "radio" else None}) + "\n")
            sent.flush()

    def collect():
        since, n_alerts = seq0, 0
        with open(out / "events.jsonl", "w") as ev_f, open(out / "alerts.jsonl", "w") as al_f:
            while not done.is_set():
                try:
                    evs = http(f"{a.hub}/events?since={since}&limit=5000", timeout=10)
                except Exception:
                    time.sleep(1)
                    continue
                for ev in evs:
                    since = max(since, ev["seq"])
                    ev["scenario_t"] = scen_t(ev["recv_ms"] / 1000 - skew)
                    ev_f.write(json.dumps(ev) + "\n")
                    if ev["type"] in ALERT_TYPES and ev.get("source") != "stub":
                        al_f.write(json.dumps(ev) + "\n")
                        n_alerts += 1
                        dd = ev["data"]
                        print(f"  {ev['scenario_t'][11:22]}  {ev['type'].upper():12} {dd.get('state', '')} {dd.get('severity', '')} {dd.get('title') or dd.get('why') or dd.get('rule', '')}", flush=True)
                ev_f.flush(), al_f.flush()
                time.sleep(0.5)

    collector = threading.Thread(target=collect, daemon=True)
    collector.start()
    print(f"{exp['id']}: {len(plan)} messages, scenario {iso(start + timedelta(seconds=a.from_s))[11:19]} onward, radio={a.radio}, "
          f"{(plan[-1][0] - a.from_s) / a.speed / 60:.1f} min. Output: {out}")
    if a.from_s > 0:
        print("  note: clearances given before --from are never sent, so aircraft already cleared then can raise false runway-entry alerts")
    for e in exp["expected_alerts"]:
        print(f"  expected: {e['rule']} cue {e['cue'][11:22]} detect by {e['detect_by'][11:22]}")
    with ThreadPoolExecutor(16) as pool:  # sends overlap so one slow call never delays the replay clock
        for t, kind, item in plan:
            time.sleep(max(0, wall0 + (t - a.from_s) / a.speed - time.time()))
            pool.submit(job, t, kind, item)
            if kind == "radio":
                print(f"  {iso(start + timedelta(seconds=t))[11:22]}  sent radio   {item['text'][:70]}", flush=True)
    time.sleep(a.tail)
    done.set()
    collector.join(timeout=5)
    sent.close()
    print(f"done. {sum(1 for _ in open(out / 'alerts.jsonl'))} alert events -> {out / 'alerts.jsonl'}")


if __name__ == "__main__":
    main()
