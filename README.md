# datasets

Datasets and eval inputs for Ground Truth, the runway safety agent. This repo produces everything the
agent is tested on: tower and ground radio audio, aircraft and vehicle positions, weather in the
aviationweather.gov API format, and an answer key per scenario.

## Quick start

```
pip install -r requirements.txt
python simulate.py                 # 25 scenarios: 13 at KLGA, 12 at KSFO (offline Windows voices)
python feed.py --latency 1.5       # the agent's input: one time-ordered feed.jsonl per scenario
python -m http.server 8000         # then open http://localhost:8000/viewer.html
```

`simulate.py` uses the built-in Windows SAPI voices by default, so it runs offline.

## ElevenLabs voices

```
cp .env.example .env               # put your key in ELEVENLABS_API_KEY
python simulate.py --voices eleven # add --budget N to change the character cap (default 130,000)
```

- A full run is about 118,000 characters (57k KLGA, 61k KSFO).
- The run stops before it spends more than `--budget` characters.
- Every clip is cached in `data/sim/_tts/`, so reruns and the offline demo cost nothing. Changing a phrase or voice re-bills only those lines.
- Tower and Ground get one fixed premade voice each. Pilots and drivers get the other premade voices.

## What each scenario contains

`data/sim/<lga|sfo>_<scenario>/`:

| File | What it is |
|---|---|
| `tower.wav`, `ground.wav` | One radio stream per frequency, 16 kHz mono, with a VHF radio effect |
| `transmissions.jsonl` | Ground truth per call: time, frequency, speaker, callsign, text, parsed clearance |
| `tracks.jsonl` | 1 Hz positions. Vehicles have `callsign: null`; the agent must identify them from the radio |
| `weather.jsonl` | Routine METARs and SPECIs. `api` holds the exact aviationweather.gov JSON object |
| `airport.json` | Runway and taxiway lines (for the viewer) |
| `expected.json` | Answer key: expected alerts with cue and detect-by times, the event, vehicle identities |
| `feed.jsonl` | Built by `feed.py`: what the agent reads, with no truth labels in the radio lines |

Scenarios: 10 incidents (KLGA only for the March 22, 2026 reconstruction) and 3 nominal runs where any
alert is a false alarm. `data/sim/index.json` lists them all.

## Scripts

| Script | Does |
|---|---|
| `simulate.py` | Builds every scenario from one script per scenario, so radio, tracks and weather agree. FAA JO 7110.65 phraseology |
| `feed.py` | Merges a scenario into the agent's raw input, `feed.jsonl` |
| `viewer.html` | Replays a scenario: waveforms, map, weather, radio log, answer key. No dependencies |
| `record.py` | Records live KLGA positions (adsb.lol, OpenSky) and METAR |
| `fetch_incident.py` | Fetches the real Jazz 646 ADS-B trace and KLGA weather for 2026-03-23 |

## Data sources and licences

- Runway and taxiway geometry: © OpenStreetMap contributors, ODbL.
- Jazz 646 trace: adsb.lol (ODbL).
- Incident weather: Iowa Environmental Mesonet.
- Incident timeline: NTSB preliminary report DCA26MA161.
- `data/live/` (OpenSky responses) and any LiveATC audio stay out of git; their terms do not allow redistribution.

## Assumptions to check before quoting

- Landing limits: 1/2 SM visibility, 200 ft ceiling, 10 kt tailwind (typical CAT I values, not taken from the FAA charts).
- Reconstruction radio wording is standard phraseology at the NTSB times, not a verbatim transcript.
- Every track other than Jazz 646 is simulated.
