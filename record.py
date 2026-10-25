"""Record live KLGA positions and METAR into the Ground Truth data contract (FMK-2).

Usage: python record.py [minutes]   (default 40)
Writes data/live/<UTC start>/: tracks.jsonl, weather.jsonl, raw/*.json
Stdlib only, so it runs on the GB10 without pip.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

LAT, LON = 40.7769, -73.8740
ADSBLOL = f"https://api.adsb.lol/v2/point/{LAT}/{LON}/3"  # 3 nm radius
OPENSKY = "https://opensky-network.org/api/states/all?lamin=40.76&lomin=-73.90&lamax=40.79&lomax=-73.85&extended=1"
METAR = "https://aviationweather.gov/api/data/metar?ids=KLGA&format=json&hours=2"
VEHICLE_CATEGORIES = {"C1", "C2"}  # ADS-B emitter: surface emergency / service vehicle


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ground-truth-hackathon"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def tracks(d):
    now = d.get("now", time.time() * 1000) / 1000
    for a in d.get("ac", []):
        if "lat" not in a:
            continue
        yield {
            "t": iso(now - a.get("seen_pos", 0)),
            "id": a["hex"],
            "kind": "vehicle" if a.get("category") in VEHICLE_CATEGORIES else "aircraft",
            "callsign": (a.get("flight") or a.get("r") or a["hex"]).strip(),
            "lat": a["lat"], "lon": a["lon"],
            "speed_kt": a.get("gs", 0), "heading": a.get("track", a.get("true_heading", 0)),
            "on_ground": a.get("alt_baro") == "ground",
            "source": "recorded",
        }


def ceiling(clouds):
    return min((c["base"] for c in clouds or [] if c.get("cover") in ("BKN", "OVC", "VV") and c.get("base")), default=None)


def weather(m):
    vis = str(m.get("visib", "")).rstrip("+")
    return {
        "t": iso(m["obsTime"]), "metar": m["rawOb"],
        "wind_dir": m.get("wdir"), "wind_kt": m.get("wspd"),
        "visibility_sm": float(vis) if vis.replace(".", "").isdigit() else None,
        "ceiling_ft": ceiling(m.get("clouds")), "source": "recorded",
    }


def main(minutes):
    start = time.time()
    out = Path("data/live") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    (out / "raw").mkdir(parents=True, exist_ok=True)
    seen_metar, n = set(), 0
    with open(out / "tracks.jsonl", "a") as tf, open(out / "weather.jsonl", "a") as wf:
        while time.time() - start < minutes * 60:
            tick = time.time()
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            for name, url in (("adsblol", ADSBLOL), ("opensky", OPENSKY)):
                try:
                    d = get(url)
                    (out / "raw" / f"{name}_{stamp}.json").write_text(json.dumps(d))
                    if name == "adsblol":
                        for t in tracks(d):
                            tf.write(json.dumps(t) + "\n")
                except Exception as e:  # keep recording; a missed poll is not fatal
                    print(stamp, name, "failed:", e)
            if n % 30 == 0:  # METAR every ~5 min
                try:
                    for m in get(METAR):
                        if m["rawOb"] not in seen_metar:
                            seen_metar.add(m["rawOb"])
                            wf.write(json.dumps(weather(m)) + "\n")
                except Exception as e:
                    print(stamp, "metar failed:", e)
            tf.flush(); wf.flush(); n += 1
            print(stamp, "poll", n, flush=True)
            time.sleep(max(0, 10 - (time.time() - tick)))
    print("done:", out)


if __name__ == "__main__":
    assert weather({"obsTime": 0, "rawOb": "X", "visib": "10+", "clouds": [{"cover": "FEW", "base": 900}, {"cover": "BKN", "base": 2500}]})["ceiling_ft"] == 2500
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 40)
