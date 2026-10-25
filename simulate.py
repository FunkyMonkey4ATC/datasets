"""Simulate Ground Truth scenarios: tower + ground radio, tracks, weather and an answer key, per airport.

One scenario script drives every stream, so radio, positions and weather agree by construction:
each controller instruction is booked together with its readback, and each movement starts from the
readback that allowed it. Phraseology follows FAA JO 7110.65. Weather is emitted the way the
aviationweather.gov METAR API delivers it: routine METARs once an hour, SPECIs on significant changes,
each with the API's JSON object and a receipt delay.

  python simulate.py                                  # KLGA and KSFO, Windows SAPI voices (offline)
  python simulate.py --airport KSFO inc02 nom01       # some scenarios at one airport
  python simulate.py --voices eleven --budget 120000  # ElevenLabs voices (ELEVENLABS_API_KEY in env or .env)

Needs numpy (miniconda python). Writes data/sim/<lga|sfo>_<scenario>/: tower.wav, ground.wav,
transmissions.jsonl (ground-truth transcript and parsed clearance per call), tracks.jsonl,
weather.jsonl, airport.json, expected.json (answer key); data/sim/index.json.
"""
import hashlib, itertools, json, math, os, re, subprocess, sys, urllib.error, urllib.request, wave
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "sim"
SR = 16000
KT, NM = 0.514444, 1852.0
HOLD_M = 75  # hold-short line distance from runway centerline
TWR, GND = "tower", "ground"
SPECI_DELAY, METAR_DELAY = 90, 240  # seconds from observation to the API having it
# ponytail: assumed limits (typical CAT I ILS, common airline tailwind limit). Check the FAA ILS chart before quoting them.
LIMITS = {"visibility_sm": 0.5, "ceiling_ft": 200, "tailwind_kt": 10}

# Each airport names its roles; the scenarios only use roles. side: +1 if the terminal is right of the runway heading.
AIRPORTS = {
    "KLGA": dict(icao="KLGA", short="lga", name="LaGuardia", center=(40.777, -73.872), elev=6, osm="osm_klga.json",
                 tower_freq="one one eight point seven", ground_short="ground point seven", departure="New York departure",
                 arr="4", exit="E", dep="13", dep_cross="G", veh_a="C", veh_b="D", sweep="R", single="B", ramp="B",
                 side={"4": 1, "13": 1}, gap=130, metar_minute=51, utc_night=3,
                 airlines=["DAL", "AAL", "UAL", "JBU", "SWA", "RPA", "EDV", "FFT", "SKW"],
                 wx=dict(wind_dir=40, wind_kt=7, gust_kt=None, visibility_sm=10.0, ceiling_ft=None, wx="", temp=12, dew=4, altim=3002)),
    "KSFO": dict(icao="KSFO", short="sfo", name="San Francisco", center=(37.6188, -122.3754), elev=13, osm="osm_ksfo.json",
                 tower_freq="one two zero point five", ground_short="ground point eight", departure="NorCal departure",
                 arr="28L", exit="L", dep="28R", dep_cross="F2", veh_a="N", veh_b="P", sweep="N", single="F2", ramp="A",
                 side={"28L": -1, "28R": -1}, gap=150, metar_minute=56, utc_night=6,
                 airlines=["UAL", "ASA", "SKW", "DAL", "AAL", "SWA", "JBU"],
                 wx=dict(wind_dir=290, wind_kt=14, gust_kt=None, visibility_sm=10.0, ceiling_ft=None, wx="", temp=15, dew=10, altim=3000)),
}
AP = None  # the airport in use; set by use_airport()

# ---------- airport geometry (OpenStreetMap, local metres: x east, y north) ----------


def to_xy(lat, lon):
    return np.array([(lon - AP["center"][1]) * AP["mx"], (lat - AP["center"][0]) * 111_320.0])


def to_ll(p):
    return round(AP["center"][0] + p[1] / 111_320.0, 6), round(AP["center"][1] + p[0] / AP["mx"], 6)


def _hdg(a, b):
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def _seg(p, q, r, s):  # where segment p-q crosses r-s, as a fraction of p-q
    d = (q[0] - p[0]) * (s[1] - r[1]) - (q[1] - p[1]) * (s[0] - r[0])
    if abs(d) < 1e-9:
        return None
    t = ((r[0] - p[0]) * (s[1] - r[1]) - (r[1] - p[1]) * (s[0] - r[0])) / d
    u = ((r[0] - p[0]) * (q[1] - p[1]) - (r[1] - p[1]) * (q[0] - p[0])) / d
    return t if 0 <= t <= 1 and 0 <= u <= 1 else None


def num(ref):
    return int(re.match(r"\d+", ref)[0])


class Runway:
    def __init__(s, ref):
        key = next(k for k in AP["rwy_ways"] if ref in [x.lstrip("0") for x in k.split("/")])
        a, b = AP["rwy_ways"][key]
        lo = key.split("/")[0].lstrip("0")
        if abs((_hdg(a, b) - num(lo) * 10 + 180) % 360 - 180) > 90:
            a, b = b, a  # a is now the threshold of runway `lo`
        if ref != lo:
            a, b = b, a
        s.ref, s.thr, s.end = ref, a, b
        s.length = float(np.linalg.norm(b - a))
        s.u = (b - a) / s.length
        s.n = np.array([s.u[1], -s.u[0]]) * AP["side"].get(ref, 1)  # unit normal toward the terminal side

    def at(s, along, off=0.0):  # `along` metres past the threshold, `off` metres toward the terminal side
        return s.thr + s.u * along + s.n * off

    def cross(s, twy):  # metres from threshold where taxiway `twy` crosses
        for line in AP["twy"][twy]:
            for p, q in zip(line, line[1:]):
                t = _seg(s.thr, s.end, p, q)
                if t is not None:
                    return t * s.length
        raise KeyError(twy)


def use_airport(icao):
    global AP, RW
    AP = dict(AIRPORTS[icao])
    AP["mx"] = 111_320.0 * math.cos(math.radians(AP["center"][0]))
    rwy_pts, AP["twy"], AP["rwy_ways"] = {}, {}, {}
    for w in json.load(open(ROOT / "data" / AP["osm"]))["elements"]:
        pts = [to_xy(g["lat"], g["lon"]) for g in w["geometry"]]
        if w["tags"]["aeroway"] == "runway":
            rwy_pts.setdefault(w["tags"]["ref"], []).extend(pts)  # OSM splits runways into several ways
        elif w["tags"].get("ref"):
            AP["twy"].setdefault(w["tags"]["ref"], []).append(pts)
    for ref, pts in rwy_pts.items():  # a runway's ends are its two farthest-apart nodes
        AP["rwy_ways"][ref] = max(itertools.combinations(pts, 2), key=lambda ab: float(np.linalg.norm(ab[0] - ab[1])))
    RW = {r: Runway(r) for r in {AP["arr"], AP["dep"]}}

# ---------- phraseology ----------
AIRLINES = {"DAL": "Delta", "AAL": "American", "UAL": "United", "JBU": "JetBlue", "SWA": "Southwest", "ASA": "Alaska",
            "RPA": "Brickyard", "EDV": "Endeavor", "JZA": "Jazz", "FFT": "Frontier Flight", "SKW": "Skywest"}
DIGITS = "zero one two three four five six seven eight niner".split()
NATO = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet Kilo Lima Mike "
                "November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray Yankee Zulu".split()))
SIDE_WORD = {"L": "left", "R": "right", "C": "center"}


def say_num(n):
    return " ".join("point" if c == "." else DIGITS[int(c)] for c in str(n))


def rw(r):
    return " ".join([say_num(num(r))] + [SIDE_WORD[c] for c in r if c in SIDE_WORD])


def tw(t):
    return " ".join(NATO.get(c) or DIGITS[int(c)] for c in t)


def spoken_call(call):
    m = re.fullmatch(r"([A-Z]{3})(\d+)", call)
    return f"{AIRLINES[m[1]]} {say_num(m[2])}"


def twr():
    return f"{AP['name']} Tower"


def gnd():
    return f"{AP['name']} Ground"


def wind_words(w):
    if w["wind_kt"] < 3:
        return "wind calm"
    out = f"wind {say_num(f'{w['wind_dir']:03d}')} at {say_num(w['wind_kt'])}"
    return out + (f" gusts {say_num(w['gust_kt'])}" if w.get("gust_kt") else "")

# ---------- weather, shaped like the aviationweather.gov METAR API ----------


def fmt_vis(v):
    f = Fraction(v).limit_denominator(4)
    whole, frac = divmod(f, 1)
    return " ".join(x for x in (str(whole) if whole else "", str(frac) if frac else "") if x)


def metar_body(dt, w):
    wind = f"{w['wind_dir']:03d}{w['wind_kt']:02d}" + (f"G{w['gust_kt']:02d}" if w.get("gust_kt") else "") + "KT"
    sky = f"{'VV' if w['visibility_sm'] < 0.5 else 'OVC'}{w['ceiling_ft'] // 100:03d}" if w["ceiling_ft"] else "FEW050"
    return " ".join(x for x in (f"{AP['icao']} {dt:%d%H%M}Z", wind, f"{fmt_vis(w['visibility_sm'])}SM", w.get("wx"), sky,
                                f"{w['temp']:02d}/{w['dew']:02d}", f"A{w['altim']}", "RMK AO2") if x)


def parse_metar(m):
    w = re.search(r" (\d{3}|VRB)(\d{2})(?:G(\d{2}))?KT", m)
    v = re.search(r" (\d+ \d/\d|\d/\d|\d+)SM", m)
    c = re.findall(r"(BKN|OVC|VV)(\d{3})", m)
    td = re.search(r" (M?\d{2})/(M?\d{2}) ", m)
    alt = re.search(r" A(\d{4})", m)
    deg = lambda x: -int(x[1:]) if x.startswith("M") else int(x)
    return dict(wind_dir=0 if w[1] == "VRB" else int(w[1]), wind_kt=int(w[2]), gust_kt=int(w[3]) if w[3] else None,
                visibility_sm=float(sum(Fraction(x) for x in v[1].split())) if v else 10.0,
                ceiling_ft=int(c[0][1]) * 100 if c else None, temp=deg(td[1]), dew=deg(td[2]), altim=int(alt[1]), raw=m)


def flt_cat(w):
    c, v = w["ceiling_ft"] or 99999, w["visibility_sm"]
    return "LIFR" if c < 500 or v < 1 else "IFR" if c < 1000 or v < 3 else "MVFR" if c <= 3000 or v <= 5 else "VFR"


def api_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def api_report(dt, w, kind):
    """One observation exactly as https://aviationweather.gov/api/data/metar?format=json returns it."""
    raw = w["raw"] if w.get("raw") else f"{kind} {metar_body(dt, w)}"
    if not raw.startswith(("METAR", "SPECI")):
        raw = f"{kind} {raw}"
    rank = ["CLR", "FEW", "SCT", "BKN", "OVC", "OVX"]
    clouds = [{"cover": "OVX" if c == "VV" else c, "base": int(b) * 100} for c, b in re.findall(r"(FEW|SCT|BKN|OVC|VV)(\d{3})", raw)]
    rep = dt.replace(minute=0, second=0) + timedelta(hours=1) if kind == "METAR" and dt.minute >= 45 else dt
    o = {"icaoId": AP["icao"], "receiptTime": api_iso(dt + timedelta(seconds=METAR_DELAY if kind == "METAR" else SPECI_DELAY)),
         "obsTime": int(dt.timestamp()), "reportTime": api_iso(rep), "temp": float(w["temp"]), "dewp": float(w["dew"]),
         "wdir": w["wind_dir"], "wspd": w["wind_kt"]}
    if w.get("gust_kt"):
        o["wgst"] = w["gust_kt"]
    o |= {"visib": "10+" if w["visibility_sm"] >= 10 else w["visibility_sm"], "altim": round(w["altim"] / 100 * 33.8639, 1),
          "qcField": 4, "metarType": kind, "rawOb": raw, "lat": AP["center"][0], "lon": AP["center"][1], "elev": AP["elev"],
          "name": f"{AP['name']} Intl"}
    if wx := " ".join(re.findall(r" ([-+]?(?:RA|BR|FG|SN|DZ|TS\w*))(?= )", raw)):
        o["wxString"] = wx
    o |= {"cover": max((c["cover"] for c in clouds), key=rank.index, default="CLR"), "clouds": clouds, "fltCat": flt_cat(w)}
    return o

# ---------- speech: Windows SAPI (offline) or ElevenLabs, then a VHF radio effect ----------
PS1 = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
while ($null -ne ($line = [Console]::In.ReadLine())) {
  $p = $line.Split('|', 4)
  $s.SelectVoice($p[1]); $s.Rate = [int]$p[2]
  $s.SetOutputToWaveFile($p[0], $fmt); $s.Speak($p[3]); $s.SetOutputToNull()
  [Console]::Out.WriteLine('ok'); [Console]::Out.Flush()
}
"""


class Sapi:
    voices = ["Microsoft David Desktop", "Microsoft Zira Desktop"]

    def __init__(s, cache):
        s.cache, s.p = cache, None
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "tts.ps1").write_text(PS1)
        s.controller = {TWR: (s.voices[0], 2, 0.97, 0.006), GND: (s.voices[1], 2, 1.0, 0.006)}

    def pilot_voice(s, rng):  # (voice, rate, pitch shift, noise): two voices only, so pitch tells pilots apart
        return (str(rng.choice(s.voices)), int(rng.integers(1, 4)), round(float(rng.uniform(0.88, 1.12)), 2), round(float(rng.uniform(0.01, 0.04)), 3))

    def render(s, text, voice):
        name, rate, pitch, noise = voice
        key = hashlib.md5(f"{name}|{rate}|{text}".encode()).hexdigest()
        f = s.cache / f"{key}.wav"
        if not f.exists():
            if s.p is None:
                s.p = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(s.cache / "tts.ps1")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            s.p.stdin.write(f"{f}|{name}|{rate}|{text}\n")
            s.p.stdin.flush()
            assert s.p.stdout.readline().strip() == "ok", f"TTS failed: {text}"
        with wave.open(str(f)) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        return radio_fx(x, pitch, noise, int(key[:8], 16))


class ElevenLabs:
    """ElevenLabs text-to-speech. Every clip is cached on disk, so a rerun (and the offline demo) costs nothing."""
    API = "https://api.elevenlabs.io/v1"

    def __init__(s, cache, budget):
        s.cache, s.budget, s.spent = cache, budget, 0
        cache.mkdir(parents=True, exist_ok=True)
        s.key, env = os.environ.get("ELEVENLABS_API_KEY"), ROOT / ".env"
        if not s.key and env.exists():
            s.key = next((l.split("=", 1)[1].strip() for l in env.read_text().splitlines() if l.startswith("ELEVENLABS_API_KEY=")), None)
        if not s.key:
            raise SystemExit("--voices eleven needs ELEVENLABS_API_KEY in the environment or in .env next to simulate.py")
        vf = cache / "voices.json"
        if not vf.exists():
            vf.write_bytes(s._req("/voices"))
        vs = json.loads(vf.read_text())["voices"]
        s.voices = sorted(v["voice_id"] for v in vs if v.get("category") == "premade") or sorted(v["voice_id"] for v in vs)
        s.controller = {TWR: (s.voices[0], 1.1, 1.0, 0.006), GND: (s.voices[1], 1.1, 1.0, 0.006)}

    def _req(s, path, body=None):
        req = urllib.request.Request(s.API + path, data=json.dumps(body).encode() if body else None,
                                     headers={"xi-api-key": s.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:  # 401 bad key, 402/429 out of credits or rate limited
            raise SystemExit(f"ElevenLabs {path.split('?')[0]} failed: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")

    def pilot_voice(s, rng):  # (voice id, speed, pitch shift, noise)
        return (str(rng.choice(s.voices[2:])), round(float(rng.uniform(1.0, 1.15)), 2), 1.0, round(float(rng.uniform(0.01, 0.04)), 3))

    def render(s, text, voice):
        vid, speed, _, noise = voice
        key = hashlib.md5(f"eleven|{vid}|{speed}|{text}".encode()).hexdigest()
        f = s.cache / f"{key}.pcm"
        if not f.exists():
            s.spent += len(text)
            if s.spent > s.budget:
                raise SystemExit(f"Stopped: this run would spend more than {s.budget} ElevenLabs characters (--budget)")
            f.write_bytes(s._req(f"/text-to-speech/{vid}?output_format=pcm_16000",
                                 {"text": text, "model_id": "eleven_flash_v2_5",
                                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": speed}}))
        x = np.frombuffer(f.read_bytes(), "<i2").astype(np.float32) / 32768
        return radio_fx(x, 1.0, noise, int(key[:8], 16))


def radio_fx(x, pitch, noise, seed):
    rng = np.random.default_rng(seed)
    loud = np.flatnonzero(np.abs(x) > 0.01)
    x = x[loud[0]:loud[-1] + 1] if len(loud) else x
    if pitch != 1:  # resample: shifts pitch and tempo together, enough to tell speakers apart
        x = np.interp(np.arange(0, len(x) - 1, pitch), np.arange(len(x)), x)
    x = np.concatenate([np.zeros(int(0.08 * SR)), x, np.zeros(int(0.05 * SR))])
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X[(f < 300) | (f > 3000)] = 0  # VHF voice band
    x = np.fft.irfft(X, len(x))
    x = np.tanh(3 * x / (np.abs(x).max() + 1e-9)) * 0.6  # AGC and clipping
    x += noise * rng.standard_normal(len(x))
    tail = rng.standard_normal(int(0.12 * SR)) * 0.08 * np.linspace(1, 0, int(0.12 * SR))  # squelch tail
    return np.concatenate([x, tail]).astype(np.float32)

# ---------- movers ----------


class Track:
    def __init__(s, sim, kind, call, spoken, speaker):
        s.kind, s.call, s.sp, s.speaker = kind, call, spoken, speaker
        s.id = f"{sim.rng.integers(0xA00000, 0xAFFFFF):06x}" if kind == "aircraft" else f"veh-{sim.rng.integers(0, 0xFFFF):04x}"
        s.voice = sim.tts.pilot_voice(sim.rng)
        s.pts, s.recorded_until = [], -math.inf
        sim.tracks.append(s)

    @property
    def t(s):
        return s.pts[-1][0]

    def start(s, p, t, v_kt=0.0, ground=True):
        s.pts = [(t, np.asarray(p, float), v_kt * KT, ground)]
        return s

    def go(s, to, v_kt, ground=None):  # constant acceleration from the current speed to v_kt at `to`
        t, p, v0, g = s.pts[-1]
        g = g if ground is None else ground
        to, v1 = np.asarray(to, float), v_kt * KT
        d = float(np.linalg.norm(to - p))
        if d < 0.5:
            return s
        T = 2 * d / (v0 + v1)
        a, u = (v1 - v0) / T, (to - p) / d
        for k in np.arange(1.0, T, 1.0):
            s.pts.append((t + k, p + u * (v0 * k + a * k * k / 2), v0 + a * k, g))
        s.pts.append((t + T, to, v1, g))
        return s

    def taxi(s, to, v_kt=15):  # from stopped to stopped
        mid = (s.pts[-1][1] + np.asarray(to, float)) / 2
        return s.go(mid, v_kt).go(to, 0)

    def wait(s, until):
        t, p, v, g = s.pts[-1]
        if until > t:
            s.pts.append((until, p, 0.0, g))
        return s

    def time_at(s, p):
        return min(s.pts, key=lambda q: np.linalg.norm(q[1] - p))[0]

    def rows(s, sim, dur):
        t = np.array([q[0] for q in s.pts])
        xy = np.array([q[1] for q in s.pts])
        v = np.array([q[2] for q in s.pts])
        out, prev, hdg = [], None, 0
        for k in range(max(0, math.ceil(t[0])), min(int(dur), math.floor(t[-1])) + 1):
            p = np.array([np.interp(k, t, xy[:, 0]), np.interp(k, t, xy[:, 1])])
            if prev is not None and np.linalg.norm(p - prev) > 0.5:
                hdg = round(_hdg(prev, p))
            prev = p
            lat, lon = to_ll(p)
            out.append(dict(t=sim.iso(k), id=s.id, kind=s.kind, callsign=s.call if s.kind == "aircraft" else None,
                            lat=lat, lon=lon, speed_kt=round(float(np.interp(k, t, v)) / KT, 1), heading=hdg,
                            on_ground=bool(s.pts[max(0, np.searchsorted(t, k, "right") - 1)][3]),
                            source="recorded" if k <= s.recorded_until else "simulated"))
        return out

# ---------- scenario ----------


class Sim:
    def __init__(s, sid, kind, title, summary, base, seed, tts):
        s.sid, s.kind, s.title, s.summary, s.base, s.tts = sid, kind, title, summary, base, tts
        s.rng = np.random.default_rng(seed)
        s.tracks, s.tx, s.wx = [], {TWR: [], GND: []}, [(0, dict(AP["wx"]))]
        s.alerts, s.notes, s.event, s.used = [], [], None, set()
        s.reports, s.wx_source = None, "simulated"  # reports: [(t, "METAR"|"SPECI")]; None = routine + one SPECI per change
        s.A, s.D = AP["arr"], AP["dep"]
        s.T_INC = 170 + AP["gap"] * 3  # the incident arrival comes after three background arrivals

    def iso(s, t, frac=False):
        dt = s.base + timedelta(seconds=float(t))
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + (f".{dt.microsecond // 100000}" if frac else "") + "Z"

    def weather(s, t, **chg):
        if t <= 0:  # starting conditions, not a change: no SPECI
            s.wx[0] = (0, {**s.wx[0][1], **chg})
            return
        s.wx.append((t, {**s.wx_at(t), **chg}))
        s.wx.sort(key=lambda x: x[0])

    def wx_at(s, t):
        return next((w for t0, w in reversed(s.wx) if t0 <= t), s.wx[0][1])

    def aircraft(s, call=None):
        while call is None or call in s.used:
            call = f"{s.rng.choice(AP['airlines'])}{s.rng.integers(100, 5999)}"
        s.used.add(call)
        return Track(s, "aircraft", call, spoken_call(call), "pilot")

    def vehicle(s, call, spoken):
        return Track(s, "vehicle", call, spoken, "vehicle")

    # radio
    def slot(s, freq, t, dur):  # half duplex: first gap on this frequency that fits `dur`
        for a, b in sorted((x["start"], x["end"]) for x in s.tx[freq]):
            if t < b + 0.7 and t + dur + 0.7 > a:
                t = b + 0.7
        return t

    def say(s, freq, t, text, by=None, about=None, exact=False, **label):
        audio = s.tts.render(text, by.voice if by else s.tts.controller[freq])
        dur = len(audio) / SR
        t = t if exact else s.slot(freq, t, dur)
        who = by or about
        s.tx[freq].append(dict(start=t, end=t + dur, audio=audio, speaker=by.speaker if by else freq,
                               callsign=who.call if who else None, text=text, **label))
        return t + dur

    def instruct(s, freq, t, a, text, readback, action, runway=None, taxiway=None, exact=False):
        if not exact:  # reserve instruction + readback as one block so nobody talks in between
            t = s.slot(freq, t, (len(s.tts.render(text, s.tts.controller[freq])) + len(s.tts.render(readback, a.voice))) / SR + 0.4)
        end = s.say(freq, t, text, about=a, exact=True, action=action, runway=runway, taxiway=taxiway)
        rb = s.say(freq, end + 0.4, readback, by=a, exact=True, action="readback", readback_action=action, runway=runway, taxiway=taxiway)
        return end, rb

    def land(s, a, t, exact=False):
        r = a.rwy
        return s.instruct(TWR, t, a, f"{a.sp}, {wind_words(s.wx_at(t))}, runway {rw(r)}, cleared to land.",
                          f"Cleared to land runway {rw(r)}, {a.sp}.", "cleared_to_land", r, exact=exact)

    def go_around(s, a, t):
        return s.instruct(TWR, t, a, f"{a.sp}, go around.", f"Going around, {a.sp}.", "go_around", a.rwy)

    def cross_vehicle(s, freq, t, v, rwy, twy):
        return s.instruct(freq, t, v, f"{v.sp}, cross runway {rw(rwy)} at {tw(twy)}.", f"Crossing runway {rw(rwy)} at {tw(twy)}, {v.sp}.",
                          "cross_runway", rwy, twy)

    def hold_vehicle(s, v, t, rwy, twy, freq=GND):  # vehicle asks to cross, is told to hold short, drives to the line
        r, x = RW[rwy], RW[rwy].cross(twy)
        e = s.say(freq, t, f"{AP['name']} {freq.title()}, {v.sp}, request to cross runway {rw(rwy)} at {tw(twy)}.",
                  by=v, action="report", runway=rwy, taxiway=twy)
        _, rb = s.instruct(freq, e + 0.6, v, f"{v.sp}, hold short of runway {rw(rwy)} at {tw(twy)}.",
                           f"Hold short of runway {rw(rwy)} at {tw(twy)}, {v.sp}.", "hold_short", rwy, twy)
        v.start(r.at(x, 400), rb + 2).taxi(r.at(x, HOLD_M), 20)
        return r, x

    # traffic
    def arrival(s, T, call=None, go_around=False, pilot_go_around=False):
        """Arrival crossing the threshold at T. The landing clearance is issued separately (land)."""
        rwy, ex = AP["arr"], AP["exit"]
        a, r = s.aircraft(call), RW[rwy]
        a.rwy, a.T = rwy, T
        a.start(r.at(-6 * NM), T - 6 * NM / (140 * KT), 140, ground=False).go(r.at(0), 140)
        e = s.say(TWR, T - 155, f"{twr()}, {a.sp}, ILS runway {rw(rwy)}.", by=a, action="report", runway=rwy)
        s.say(TWR, e + 0.5, f"{a.sp}, {twr()}, continue.", about=a, action="other")
        if go_around or pilot_go_around:
            a.clear = a.go(r.at(r.length), 150).t
            a.go(r.at(r.length + 4000), 170)
            if pilot_go_around:
                e = s.say(TWR, T - 10, f"{twr()}, {a.sp}, going around.", by=a, action="go_around", runway=rwy)
                s.instruct(TWR, e + 0.5, a, f"{a.sp}, roger, fly runway heading, climb and maintain two thousand.",
                           f"Runway heading, two thousand, {a.sp}.", "other", rwy)
            return a
        x = r.cross(ex)
        a.go(r.at(300), 135).go(r.at(x), 25, ground=True).go(r.at(x, 90), 12)
        a.clear = a.t
        a.go(r.at(x, 250), 0)
        turn = "right" if AP["side"][rwy] > 0 else "left"
        _, e = s.instruct(TWR, T + 18, a, f"{a.sp}, turn {turn} {tw(ex)}, contact {AP['ground_short']}.",
                          f"{turn.title()} on {tw(ex)}, {AP['ground_short']}, {a.sp}.", "taxi", taxiway=ex)
        g = s.say(GND, max(e, a.clear) + 2, f"{gnd()}, {a.sp}, clear of runway {rw(rwy)} at {tw(ex)}.",
                  by=a, action="report", runway=rwy, taxiway=ex)
        s.instruct(GND, g + 0.6, a, f"{a.sp}, {gnd()}, taxi to the ramp via {tw(AP['ramp'])}.", f"Ramp via {tw(AP['ramp'])}, {a.sp}.",
                   "taxi", taxiway=AP["ramp"])
        return a

    def taxi_out(s, ready, call=None, rwy=None, cross="default"):
        """Departure taxis from the ramp to a hold-short line. With `cross` (runway, taxiway) it must then cross (cross_rwy)."""
        rwy = rwy or AP["dep"]
        cross = (AP["arr"], AP["dep_cross"]) if cross == "default" else cross
        d, r = s.aircraft(call), RW[rwy]
        d.rwy, d.cross_r = rwy, cross[0] if cross else None
        if cross:
            cr = RW[cross[0]]
            d.twy = cross[1]
            x = cr.cross(d.twy)
            hold, d.far, ramp = cr.at(x, HOLD_M), cr.at(x, -HOLD_M), cr.at(x, 350)
            d.x0 = 40
            d.hold = r.at(d.x0, np.sign((d.far - r.thr) @ r.n) * HOLD_M)
        else:
            d.twy = AP["single"]
            d.x0 = max(r.cross(d.twy), 20)
            hold = d.hold = r.at(d.x0, HOLD_M)
            ramp = r.at(d.x0, 400)
        d.lineup = r.at(d.x0)
        hr = d.cross_r or rwy
        via = tw(AP["ramp"]) if d.twy == AP["ramp"] else f"{tw(AP['ramp'])}, {tw(d.twy)}"
        hs = f"hold short runway {rw(hr)} at {tw(d.twy)}"
        e = s.say(GND, max(1, ready - 100), f"{gnd()}, {d.sp}, at the ramp, ready to taxi with information Kilo.", by=d, action="report")
        _, rb = s.instruct(GND, e + 0.6, d, f"{d.sp}, {gnd()}, runway {rw(rwy)}, taxi via {via}, {hs}.",
                           f"{via}, {hs}, {d.sp}.", "hold_short", hr, d.twy)
        d.start(ramp, rb + 2).taxi(hold, 16)
        d.at_hold = d.t
        if not cross:
            d.ready_twr = s.say(TWR, d.t + 1, f"{twr()}, {d.sp}, holding short runway {rw(rwy)}.", by=d, action="report", runway=rwy)
        return d

    def cross_rwy(s, d, t):
        cr = d.cross_r
        _, rb = s.instruct(GND, max(t, d.at_hold + 1), d, f"{d.sp}, cross runway {rw(cr)} at {tw(d.twy)}, then contact tower {AP['tower_freq']}.",
                           f"Crossing runway {rw(cr)} at {tw(d.twy)}, then tower, {d.sp}.", "cross_runway", cr, d.twy)
        d.wait(rb + 2).taxi(d.far, 16)
        d.cross_done = d.t
        d.taxi(d.hold, 16)
        d.ready_twr = s.say(TWR, d.t + 1, f"{twr()}, {d.sp}, holding short runway {rw(d.rwy)}.", by=d, action="report", runway=d.rwy)

    def take_off(s, d, after, reject_after=None, takeoff=True):
        r = RW[d.rwy]
        _, rb = s.instruct(TWR, max(after, d.ready_twr + 1), d, f"{d.sp}, runway {rw(d.rwy)}, line up and wait.",
                           f"Line up and wait runway {rw(d.rwy)}, {d.sp}.", "line_up_and_wait", d.rwy)
        d.wait(rb + 2).taxi(d.lineup, 15)
        if not takeoff:
            return None
        cue, rb = s.instruct(TWR, d.t + 3, d, f"{d.sp}, {wind_words(s.wx_at(d.t))}, runway {rw(d.rwy)}, cleared for takeoff.",
                             f"Cleared for takeoff runway {rw(d.rwy)}, {d.sp}.", "cleared_for_takeoff", d.rwy)
        d.wait(rb + 2)
        d.roll, acc = d.t, (150 * KT) ** 2 / 3000
        if reject_after:  # abort: accelerate for reject_after seconds, then brake at 3 m/s2
            v, x = acc * reject_after, d.x0 + acc * reject_after ** 2 / 2
            d.go(r.at(x), v / KT).go(r.at(x + v * v / 6), 0)
            return cue
        d.liftoff = d.go(r.at(d.x0 + 1500), 150).t
        d.go(r.at(d.x0 + 4500), 170, ground=False)
        s.instruct(TWR, d.liftoff + 10, d, f"{d.sp}, contact {AP['departure']}, good night.", f"{AP['departure']}, {d.sp}, good night.", "other")
        return cue

    def traffic(s, n, first=170, gap=None, dep_rwy=None, deps=True, pilot_go_arounds=()):
        """n arrivals and n-1 departures, sequenced so nothing conflicts."""
        gap = gap or AP["gap"]
        arrs = [s.arrival(first + gap * k, pilot_go_around=k in pilot_go_arounds) for k in range(n)]
        free = None
        for k, a in enumerate(arrs):
            t = a.T - 45 if free is None else max(a.T - 45, free + 2)
            assert t < a.T - 25, f"{s.sid}: landing clearance for {a.call} would be too late"
            s.land(a, t)
            if deps and k + 1 < n:
                if dep_rwy == AP["arr"]:  # single runway
                    d = s.taxi_out(a.clear + 5, rwy=dep_rwy, cross=None)
                    s.take_off(d, a.clear + 3)
                    free = d.liftoff
                else:
                    d = s.taxi_out(a.clear + 10)
                    s.cross_rwy(d, a.clear + 10)
                    free = d.cross_done
                    s.take_off(d, arrs[k + 1].clear + 3)
        return arrs

    def expect(s, rule, cue, runway, parties, why, slack=3):
        s.alerts.append(dict(rule=rule, runway=runway, parties=parties, cue=s.iso(cue, True), detect_by=s.iso(cue + slack, True), why=why))

    # output
    def weather_rows(s, dur):
        reports = s.reports
        if reports is None:  # routine METAR every hour at the airport's minute (from the one before start), plus a SPECI per change
            first = s.base.replace(minute=AP["metar_minute"], second=0, microsecond=0)
            first -= timedelta(hours=1) if first > s.base else timedelta(0)
            t0 = (first - s.base).total_seconds()
            reports = [(t0 + 3600 * k, "METAR") for k in range(int((dur - t0) // 3600) + 1)]
            reports += [(t, "SPECI") for t, _ in s.wx[1:] if 0 <= t < dur]
        rows = []
        for t, kind in sorted(reports):
            w, dt = s.wx_at(t), s.base + timedelta(seconds=t)
            o = api_report(dt, w, kind)
            rows.append(dict(t=s.iso(t), metar=o["rawOb"], wind_dir=w["wind_dir"], wind_kt=w["wind_kt"], gust_kt=w.get("gust_kt"),
                             visibility_sm=w["visibility_sm"], ceiling_ft=w["ceiling_ft"], source=s.wx_source, api=o))
        return rows

    def finish(s):
        dur = max([tr.pts[-1][0] for tr in s.tracks] + [x["end"] for ch in s.tx.values() for x in ch]) + 15
        d = OUT / s.sid
        d.mkdir(parents=True, exist_ok=True)
        tx_rows = []
        for f, ch in s.tx.items():
            spans = sorted((x["start"], x["end"]) for x in ch)
            assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:])), f"{s.sid}: overlapping transmissions on {f}"
            write_wav(d / f"{f}.wav", ch, dur, len(ch))
            tx_rows += [dict(t=s.iso(x["start"], True), t_end=s.iso(x["end"], True), freq=f, audio=f"{f}.wav",
                             offset_s=round(x["start"], 2), **{k: v for k, v in x.items() if k not in ("start", "end", "audio")}) for x in ch]
        tx_rows.sort(key=lambda r: r["t"])
        rows = sorted((r for tr in s.tracks for r in tr.rows(s, dur)), key=lambda r: r["t"])
        for name, data in (("transmissions", tx_rows), ("tracks", rows), ("weather", s.weather_rows(dur))):
            with open(d / f"{name}.jsonl", "w") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in data)
        (d / "airport.json").write_text(json.dumps(airport_json()))
        close = near_misses(rows)
        if s.kind == "nominal":
            assert not close, f"{s.sid}: nominal scenario has ground traffic too close: {close[:3]}"
        if s.event and s.alerts:
            for a in s.alerts:
                a["lead_time_s"] = round((datetime.fromisoformat(s.event["t"].replace("Z", "+00:00")) - datetime.fromisoformat(a["cue"].replace("Z", "+00:00"))).total_seconds(), 1)
        key = dict(id=s.sid, airport=AP["icao"], kind=s.kind, title=s.title, summary=s.summary, start=s.iso(0), end=s.iso(dur),
                   config=dict(arrivals=AP["arr"], departures=AP["dep"]), expected_alerts=s.alerts, event=s.event, limits_assumed=LIMITS,
                   vehicles={tr.id: tr.call for tr in s.tracks if tr.kind == "vehicle"},
                   closest_ground_approach=min(close, key=lambda c: c["m"]) if close else None, notes=s.notes)
        (d / "expected.json").write_text(json.dumps(key, indent=2))
        return dict(id=s.sid, airport=AP["icao"], kind=s.kind, title=s.title, minutes=round(dur / 60, 1), tower_calls=len(s.tx[TWR]),
                    ground_calls=len(s.tx[GND]), alerts=[(a["rule"], a["cue"]) for a in s.alerts])


def airport_json():  # runway and taxiway lines for the viewer
    ll = lambda pts: [to_ll(p) for p in pts]
    return dict(icao=AP["icao"], name=AP["name"], center=AP["center"],
                runways={k: ll(v) for k, v in AP["rwy_ways"].items()},
                taxiways={k: [ll(line) for line in v] for k, v in AP["twy"].items()})


def write_wav(path, ch, dur, seed):
    n = int(dur * SR)
    out = np.random.default_rng(seed).standard_normal(n).astype(np.float32) * 0.002  # idle hiss
    for x in ch:
        i = int(x["start"] * SR)
        a = x["audio"][: n - i]
        out[i:i + len(a)] += a
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())


def near_misses(rows, metres=60, fast_kt=30):  # 60 m: inside the 75 m hold line, where traffic legitimately waits
    """Ground movers closer than `metres` while either is faster than `fast_kt`. Physical sanity check."""
    by_t, hits = {}, []
    for r in rows:
        if r["on_ground"]:
            by_t.setdefault(r["t"], []).append(r)
    for t, rs in by_t.items():
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                m = float(np.linalg.norm(to_xy(a["lat"], a["lon"]) - to_xy(b["lat"], b["lon"])))
                if m < metres and max(a["speed_kt"], b["speed_kt"]) > fast_kt:
                    hits.append(dict(t=t, ids=[a["id"], b["id"]], m=round(m)))
    return hits

# ---------- the scenarios (they use roles: s.A arrival runway, s.D departure runway, AP[...] taxiways) ----------


def inc01(s):
    """Reconstruction of KLGA 2026-03-23 03:37Z: real Jazz 646 track and weather, simulated trucks, NTSB radio times."""
    inc = ROOT / "data" / "incident"
    rel = lambda iso: (datetime.fromisoformat(iso.replace("Z", "+00:00")) - s.base).total_seconds()
    at = lambda hms: rel(f"2026-03-23T{hms}Z")
    obs = [(rel(w["t"]), w["metar"]) for w in map(json.loads, open(inc / "weather.jsonl"))]
    s.wx = [(t, {**AP["wx"], **parse_metar(m)}) for t, m in obs]
    # Only routine and special reports reach the METAR API; the 5-minute AUTO (MADISHF) reports only set the tower wind.
    s.reports = [(t, "METAR" if " AUTO " not in m and m.split()[1].endswith("51Z") else "SPECI") for t, m in obs if "MADISHF" not in m and -3600 < t < 900]
    s.wx_source = "recorded"
    r, xd = RW["4"], RW["4"].cross("D")

    a = s.aircraft("JZA646")
    a.rwy, a.id, seen = "4", "c06800", set()  # real ICAO address of C-GNJZ
    for j in map(json.loads, open(inc / "jazz646_tracks.jsonl")):
        t = rel(j["t"])
        if t >= 60 and t not in seen:
            seen.add(t)
            a.pts.append((t, to_xy(j["lat"], j["lon"]), j["speed_kt"] * KT, t >= at("03:37:12")))  # NTSB touchdown 03:37:12
    a.recorded_until = a.t  # ADS-B trace ends 03:37:18; extrapolate the rollout to the collision
    t_hit = a.go(r.at(xd), 100, ground=True).t
    a.wait(t_hit + 15)

    t1 = at("03:37:07")  # NTSB: truck starts moving
    s0 = 23 + (t_hit - t1 - 20 / (26 * KT)) * 26 * KT / 2  # start distance so it reaches the centerline at t_hit
    truck = s.vehicle("TRUCK 1", "Truck One")
    truck.start(r.at(xd, s0 + 250), t1 - 120).taxi(r.at(xd, s0), 20).wait(t1).go(r.at(xd, 23), 26).go(r.at(xd, 3), 26).wait(t_hit + 15)
    truck2 = s.vehicle("TRUCK 2", "Truck Two")
    truck2.start(r.at(xd, s0 + 290), t1 - 115).taxi(r.at(xd, s0 + 40), 20).wait(t1 + 2).taxi(r.at(xd, HOLD_M + 3), 15).wait(t_hit + 15)

    # Real times from the NTSB preliminary report are placed exactly. Wording is standard phraseology, not a verbatim transcript.
    e = s.say(TWR, at("03:33:50"), "LaGuardia Tower, Jazz six four six, ILS runway four.", by=a, action="report", runway="4")
    s.say(TWR, e + 0.5, "Jazz six four six, LaGuardia Tower, continue.", about=a, action="other")
    s.land(a, at("03:35:07"), exact=True)
    s.say(TWR, at("03:36:58"), "LaGuardia Tower, Truck One, request to cross runway four at Delta.", by=truck, exact=True, action="report", runway="4", taxiway="D")
    cue, _ = s.instruct(TWR, at("03:37:04"), truck, "Truck One, cross runway four at Delta.", "Crossing runway four at Delta, Truck One.",
                        "cross_runway", "4", "D", exact=True)
    s.say(TWR, at("03:37:12"), "Stop, stop, stop.", exact=True, action="stop", runway="4")  # unaddressed, as reported
    s.say(TWR, at("03:37:20"), "Truck One, stop, stop, stop.", about=truck, exact=True, action="stop", runway="4", taxiway="D")
    s.traffic(2, first=150, gap=131, deps=False)

    s.event = dict(t=s.iso(t_hit, True), what="Jazz 646 and Truck One collide at runway 4 / taxiway Delta (time reconstructed from the trace)")
    s.expect("crossing_on_landing_runway", cue, "4", ["TRUCK 1", "JZA646"],
             "Tower cleared Truck One across runway 4 at Delta while Jazz 646, cleared to land runway 4 at 03:35:07Z, was on short final.")
    s.notes += ["Jazz 646 positions are real ADS-B (adsb.lol) until 03:37:18Z, extrapolated after. Weather is real (Iowa Environmental Mesonet).",
                "Trucks are simulated from the NTSB preliminary timeline: cleared 03:37:04, moving 03:37:07, entering the runway about 03:37:20 at 30 mph.",
                "Other traffic is simulated. Radio times for Jazz 646 and Truck One are NTSB times; the words are standard phraseology."]


def inc02(s):
    s.traffic(3)
    T, A, tx = s.T_INC, s.A, AP["veh_a"]
    a = s.arrival(T, go_around=True)
    v = s.vehicle("OPS 2", "Ops Two")
    r, x = s.hold_vehicle(v, T - 150, A, tx)
    s.land(a, T - 45)
    cue, rb = s.cross_vehicle(GND, T - 24, v, A, tx)
    v.wait(rb + 2).taxi(r.at(x, -HOLD_M), 20)
    s.go_around(a, T - 12)
    s.expect("crossing_on_landing_runway", cue, A, [v.call, a.call],
             f"Ground cleared Ops Two across runway {A} while Tower had {a.call} cleared to land. The two clearances are on different frequencies.")


def inc03(s):
    arrs = s.traffic(3)
    D, tx = s.D, AP["sweep"]
    d = s.taxi_out(arrs[-1].clear + 10)
    s.cross_rwy(d, arrs[-1].clear + 10)
    r, x = RW[D], RW[D].cross(tx)
    v = s.vehicle("SWEEPER 1", "Sweeper One")
    v.start(r.at(x, 400), d.ready_twr - 80).taxi(r.at(x, HOLD_M), 15)
    e = s.say(TWR, v.t + 2, f"{twr()}, Sweeper One, at {tw(tx)}, request to cross runway {rw(D)}.", by=v, action="report", runway=D, taxiway=tx)
    _, rb = s.cross_vehicle(TWR, e + 0.6, v, D, tx)
    v.wait(rb + 2).taxi(r.at(x, -HOLD_M), 10)
    cue = s.take_off(d, rb + 4, reject_after=9)
    s.instruct(TWR, d.roll + 7, d, f"{d.sp}, cancel takeoff clearance, I say again, cancel takeoff clearance.", f"Rejecting, {d.sp}.", "cancel_takeoff", D)
    s.expect("takeoff_on_occupied_runway", cue, D, [d.call, v.call], f"Tower cleared {d.call} for takeoff on runway {D} while Sweeper One was crossing it on Tower's own clearance.")


def inc04(s):
    A = s.A
    s.notes.append(f"Runway {s.D} closed (simulated NOTAM); runway {A} is used for arrivals and departures.")
    arrs = s.traffic(3, gap=AP["gap"] + 20, dep_rwy=A)  # one runway for both: arrivals need wider spacing
    T = arrs[-1].T + AP["gap"] + 20
    d = s.taxi_out(arrs[-1].clear + 5, rwy=A, cross=None)
    s.take_off(d, arrs[-1].clear + 3, takeoff=False)  # line up and wait, then forgotten
    a = s.arrival(T, go_around=True)
    cue, _ = s.land(a, T - 50)
    s.go_around(a, T - 18)
    s.expect("landing_on_occupied_runway", cue, A, [a.call, d.call], f"{d.call} is lined up and waiting on runway {A} when {a.call} is cleared to land on it.")


def inc05(s):
    s.traffic(3)
    T, A, tx = s.T_INC, s.A, AP["veh_a"]
    a = s.arrival(T, go_around=True)
    v = s.vehicle("TRUCK 3", "Truck Three")
    r, x = s.hold_vehicle(v, T - 150, A, tx)
    s.land(a, T - 45)
    v.wait(T - 16).taxi(r.at(x, -HOLD_M), 18)  # runs the hold-short line
    s.go_around(a, T - 9)
    s.say(GND, T - 8, "Truck Three, stop, stop, stop.", about=v, action="stop", runway=A, taxiway=tx)
    s.expect("runway_entry_without_clearance", T - 15, A, [v.call],
             f"Truck Three read back hold short at {tw(tx)}, then entered runway {A} with no clearance while {a.call} was on short final. Only the positions show it.")


def inc06(s):
    arrs = s.traffic(3)
    T, A, tx = s.T_INC, s.A, AP["veh_b"]
    d = s.taxi_out(arrs[-1].clear + 10, call="AAL1450", cross=(A, tx))
    a = s.arrival(T, go_around=True)
    s.land(a, T - 45)
    e = s.say(GND, T - 40, f"{gnd()}, {d.sp}, holding short runway {rw(A)} at {tw(tx)}.", by=d, action="report", runway=A, taxiway=tx)
    e = s.say(GND, e + 0.6, f"{d.sp}, continue holding short runway {rw(A)} at {tw(tx)}, traffic landing runway {rw(A)}.", about=d, action="hold_short", runway=A, taxiway=tx)
    rb = s.say(GND, e + 0.4, f"Cross runway {rw(A)} at {tw(tx)}, {d.sp}.", by=d, action="readback", readback_action="cross_runway", runway=A, taxiway=tx)
    d.wait(rb + 3).taxi(d.far, 12)
    s.go_around(a, T - 14)
    s.say(GND, T - 12, f"{d.sp}, hold position, hold position.", about=d, action="stop", runway=A, taxiway=tx)
    s.expect("readback_mismatch", rb, A, [d.call], "The pilot read back 'cross' after 'continue holding short'. Ground did not correct it (hearback error).")
    s.expect("runway_entry_without_clearance", rb + 4, A, [d.call, a.call], f"{d.call} enters runway {A} at {tw(tx)} with no crossing clearance while {a.call} is cleared to land.")


def inc07(s):
    arrs = s.traffic(3)
    T, A, tx1, tx2 = s.T_INC, s.A, AP["veh_b"], AP["dep_cross"]
    d1 = s.taxi_out(arrs[-1].clear + 10, call="DAL1234", cross=(A, tx1))
    d2 = s.taxi_out(arrs[-1].clear + 20, call="DAL1243", cross=(A, tx2))
    a = s.arrival(T, go_around=True)
    e = s.say(GND, T - 62, f"{d2.sp}, cross runway {rw(A)} at {tw(tx2)}, then contact tower {AP['tower_freq']}.", about=d2, action="cross_runway", runway=A, taxiway=tx2)
    rb = s.say(GND, e + 0.4, f"Crossing runway {rw(A)}, {d1.sp}.", by=d1, action="readback", readback_action="cross_runway", runway=A)
    d1.wait(rb + 3).taxi(d1.far, 8)
    cue, _ = s.land(a, T - 45)
    s.go_around(a, T - 12)
    s.expect("readback_wrong_callsign", rb, A, [d1.call, d2.call], f"Delta 1234 took a crossing clearance meant for Delta 1243 and crossed at {tw(tx1)}, not {tw(tx2)}. Ground missed the wrong callsign.")
    s.expect("landing_on_occupied_runway", cue, A, [a.call, d1.call], f"{a.call} is cleared to land while Delta 1234 is on runway {A} at {tw(tx1)}.")


def inc08(s):
    s.weather(0, visibility_sm=3.0, ceiling_ft=900, wx="BR", wind_kt=6, temp=9, dew=8, altim=2990)
    tb = (s.T_INC - 230) // 60 * 60  # reports carry whole minutes
    s.weather(tb - 240, visibility_sm=1.5, ceiling_ft=500)
    s.weather(tb - 120, visibility_sm=0.75, ceiling_ft=300, wx="FG")
    s.weather(tb, visibility_sm=0.25, ceiling_ft=100, wx="FG")
    s.traffic(3)
    a = s.arrival(s.T_INC)
    s.land(a, s.T_INC - 45)
    s.expect("weather_below_minimums", tb + SPECI_DELAY, s.A, [a.call], f"The SPECI with visibility 1/4 SM and ceiling 100 ft is below the assumed CAT I limits (1/2 SM, 200 ft), and arrivals keep landing runway {s.A}.", slack=60)


def inc09(s):
    tb = (s.T_INC - 230) // 60 * 60  # reports carry whole minutes
    behind = (num(s.A) * 10 + 180) % 360  # straight down the runway from behind
    s.weather(tb - 180, wind_dir=(behind + 90) % 360, wind_kt=4)
    s.weather(tb, wind_dir=behind, wind_kt=16, gust_kt=24)  # gust front
    s.traffic(3)
    a = s.arrival(s.T_INC)
    s.land(a, s.T_INC - 45)
    s.expect("tailwind_exceeds_limit", tb + SPECI_DELAY, s.A, [a.call], f"Wind {behind:03d} at 16 gusting 24 is a 16-24 kt tailwind on runway {s.A} (assumed limit 10 kt), and Tower still clears an arrival to land on it.", slack=60)


def inc10(s):
    arrs = s.traffic(3)
    D, tx = s.D, AP["sweep"]
    d = s.taxi_out(arrs[-1].clear + 10)
    s.cross_rwy(d, arrs[-1].clear + 10)
    r, x = RW[D], RW[D].cross(tx)
    v = s.vehicle("UNKNOWN", None)  # never talks on the radio
    t_in = d.ready_twr + 6
    v.start(r.at(x, 350), t_in - 70).taxi(r.at(x, HOLD_M + 5), 15).wait(t_in).taxi(r.at(x, 8), 10)
    cue = s.take_off(d, t_in + 8, reject_after=12)
    e = s.say(TWR, d.roll + 12.5, f"{d.sp}, rejecting, vehicle on the runway.", by=d, action="other", runway=D)
    s.say(TWR, e + 0.6, f"{d.sp}, roger, hold position.", about=d, action="stop", runway=D)
    s.expect("runway_entry_without_clearance", t_in + 2, D, [v.id], f"A vehicle with no radio contact drives onto runway {D} at {tw(tx)}. Only the positions show it.")
    s.expect("takeoff_on_occupied_runway", cue, D, [d.call, v.id], f"Takeoff clearance on runway {D} while the silent vehicle is on it.")


def nom01(s):
    s.traffic(4)


def nom02(s):
    s.traffic(3)
    T, A, tx = s.T_INC, s.A, AP["veh_b"]
    a = s.arrival(T)
    s.land(a, T - 45)
    v = s.vehicle("TRUCK 2", "Truck Two")
    r, x = s.hold_vehicle(v, T - 150, A, tx)
    _, rb = s.cross_vehicle(GND, a.time_at(r.at(x)) + 2, v, A, tx)
    v.wait(rb + 2).taxi(r.at(x, -HOLD_M), 20)
    s.notes.append(f"Ground clears Truck Two across {tw(tx)} once the arrival has passed it, while the arrival is still rolling out. This is legal. "
                   "A checker that alerts on any crossing while an arrival is still on the runway will false-alarm here.")


def nom03(s):
    crosswind = (num(s.A) * 10 + 90) % 360
    s.weather(0, visibility_sm=0.75, ceiling_ft=300, wx="BR", wind_dir=crosswind, wind_kt=12, temp=9, dew=8, altim=2988)
    s.traffic(4, pilot_go_arounds={1})
    s.notes.append("Low IFR but above the assumed limits, with a 12 kt crosswind. One arrival goes around by its own decision, a normal procedure.")


SCENARIOS = [  # id, kind, title, summary, fn, airports (None = all)
    ("inc01_lga_reconstruction", "incident", "LaGuardia, March 22 2026 (reconstruction)", "Truck cleared across runway 4 at Delta while Jazz 646 is on short final.", inc01, {"KLGA"}),
    ("inc02_cross_frequency_crossing", "incident", "Ground crosses a vehicle in front of Tower's arrival", "Landing clearance on Tower, crossing clearance on Ground. Only both streams together show the conflict.", inc02, None),
    ("inc03_takeoff_vehicle_crossing", "incident", "Takeoff clearance while a sweeper crosses", "Tower crosses Sweeper One over the departure runway, then clears a departure on it.", inc03, None),
    ("inc04_luaw_forgotten", "incident", "Forgotten line-up-and-wait", "Single-runway ops. A departure is left in position and an arrival is cleared to land.", inc04, None),
    ("inc05_vehicle_runs_hold_short", "incident", "Vehicle runs the hold-short line", "Correct readback, then the truck enters anyway. Positions only.", inc05, None),
    ("inc06_readback_error", "incident", "Readback error not caught", "'Continue holding short' read back as 'cross'. The aircraft crosses.", inc06, None),
    ("inc07_similar_callsigns", "incident", "Similar callsigns", "Delta 1234 takes Delta 1243's crossing clearance.", inc07, None),
    ("inc08_low_visibility", "incident", "Visibility drops below minimums", "Fog rolls in; arrivals keep getting landing clearances.", inc08, None),
    ("inc09_tailwind", "incident", "Tailwind on the arrival runway", "A gust front swings the wind to blow straight down the runway at 16 gusting 24.", inc09, None),
    ("inc10_silent_vehicle", "incident", "Vehicle on the runway with no radio", "An unknown vehicle drives onto the departure runway and a departure rolls.", inc10, None),
    ("nom01_normal_ops", "nominal", "Normal night operations", "Arrivals and departures; departures cross the arrival runway between arrivals.", nom01, None),
    ("nom02_crossing_behind_landing", "nominal", "Crossing behind a landing aircraft", "Legal crossing behind an arrival that is still rolling out.", nom02, None),
    ("nom03_marginal_weather_go_around", "nominal", "Low IFR with a go-around", "3/4 SM, 300 ft overcast, 12 kt crosswind, one pilot-initiated go-around.", nom03, None),
]


def main(args):
    airports, voices, budget = ["KLGA", "KSFO"], "sapi", 130_000  # a full LGA + SFO run is about 118k characters
    rest = []
    it = iter(args)
    for a in it:
        if a == "--airport":
            airports = next(it).upper().split(",")
        elif a == "--voices":
            voices = next(it)
        elif a == "--budget":
            budget = int(next(it))
        else:
            rest.append(a)
    tts = ElevenLabs(OUT / "_tts", budget) if voices == "eleven" else Sapi(OUT / "_tts")
    index = []
    for icao in airports:
        use_airport(icao)
        for i, (sid, kind, title, summary, fn, only_at) in enumerate(SCENARIOS):
            if (only_at and icao not in only_at) or (rest and not any(sid.startswith(o) for o in rest)):
                continue
            base = datetime(2026, 3, 23, 3, 30, tzinfo=timezone.utc) if sid.startswith("inc01") else \
                datetime(2026, 11, 1 + i, AP["utc_night"], 0, tzinfo=timezone.utc)
            s = Sim(f"{AP['short']}_{sid}", kind, title, summary, base, i, tts)
            fn(s)
            index.append(s.finish())
            print(json.dumps(index[-1]), flush=True)
    if isinstance(tts, ElevenLabs):
        print(f"ElevenLabs characters spent this run: {tts.spent}")
    if not rest:
        (OUT / "index.json").write_text(json.dumps(index, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
