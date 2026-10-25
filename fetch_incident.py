"""Fetch the real data for the LaGuardia collision, 2026-03-23 03:37Z (23:37 EDT Mar 22).

- ADS-B trace of Jazz 646 (C-GNJZ, hex c06800) from the adsb.lol daily archive. Streams the tar
  and stops as soon as the trace is found, instead of downloading all 3.5 GB.
- METAR for the night from the Iowa Environmental Mesonet archive.
Writes data/incident/: jazz646_trace.json, jazz646_tracks.jsonl, weather.jsonl
"""
import csv, gzip, io, json, tarfile, urllib.request
from pathlib import Path
from record import iso

HEX = "c06800"
TAR = "https://github.com/adsblol/globe_history_2026/releases/download/v2026.03.23-planes-readsb-prod-0/v2026.03.23-planes-readsb-prod-0.tar.aa"
IEM = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=LGA&data=metar&tz=Etc/UTC"
       "&year1=2026&month1=3&day1=23&hour1=0&year2=2026&month2=3&day2=23&hour2=7&format=onlycomma")
OUT = Path("data/incident")


def trace():
    with urllib.request.urlopen(TAR) as r, tarfile.open(fileobj=r, mode="r|") as tar:
        for i, m in enumerate(tar):
            if i % 5000 == 0:
                print("scanning", m.name, flush=True)
            if m.name.endswith(f"trace_full_{HEX}.json"):
                raw = tar.extractfile(m).read()
                return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    raise SystemExit(f"{HEX} not in {TAR} (it may be in the .tar.ab part)")


def tracks(d):
    # readsb trace row: [sec_after_timestamp, lat, lon, alt_ft|"ground", gs_kt, track, ...]
    for p in d["trace"]:
        yield {"t": iso(d["timestamp"] + p[0]), "id": HEX, "kind": "aircraft", "callsign": "JZA646",
               "lat": p[1], "lon": p[2], "speed_kt": p[4], "heading": p[5],
               "on_ground": p[3] == "ground", "source": "recorded"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = csv.DictReader(io.StringIO(urllib.request.urlopen(IEM).read().decode()))
    with open(OUT / "weather.jsonl", "w") as f:
        for r in rows:  # raw METAR only; the checker parses fields the same way for both datasets
            f.write(json.dumps({"t": r["valid"].replace(" ", "T") + ":00Z", "metar": r["metar"], "source": "recorded"}) + "\n")
    d = trace()
    (OUT / "jazz646_trace.json").write_text(json.dumps(d))
    with open(OUT / "jazz646_tracks.jsonl", "w") as f:
        for t in tracks(d):
            f.write(json.dumps(t) + "\n")
    print("done", len(d["trace"]), "points")


if __name__ == "__main__":
    main()
