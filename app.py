"""Caltrain planner: find trains from Millbrae to a SF station near a target arrival time."""
from __future__ import annotations

import csv
import io
import os
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

import requests
from flask import Flask, render_template, request

APP_ROOT = Path(__file__).parent
CACHE_DIR = APP_ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)
GTFS_ZIP = CACHE_DIR / "caltrain.zip"
GTFS_URL = "https://data.trilliumtransit.com/gtfs/caltrain-ca-us/caltrain-ca-us.zip"
ONE_DAY = 24 * 60 * 60

# Northbound (toward SF) stop_ids
MILLBRAE_NB = "70061"
SF_DESTINATIONS = [
    ("70011", "San Francisco (4th & King)"),
    ("70021", "22nd Street"),
    ("70031", "Bayshore"),
    ("70041", "South San Francisco"),
]
DEST_NAMES = {sid: name for sid, name in SF_DESTINATIONS}

_lock = Lock()
_cache: dict = {}


def _download_gtfs() -> None:
    """Fetch GTFS zip if older than 24h."""
    fresh = GTFS_ZIP.exists() and (time.time() - GTFS_ZIP.stat().st_mtime) < ONE_DAY
    if fresh:
        return
    r = requests.get(GTFS_URL, timeout=30, headers={"User-Agent": "caltrain-planner/1.0"})
    r.raise_for_status()
    GTFS_ZIP.write_bytes(r.content)


def _read_csv(zf: zipfile.ZipFile, name: str) -> list[dict]:
    with zf.open(name) as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
        return list(csv.DictReader(text))


def _hms_to_sec(t: str) -> int:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _sec_to_hm(sec: int) -> str:
    sec %= 24 * 3600
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return f"{h:02d}:{m:02d}"


def _load_gtfs() -> dict:
    """Parse zip into in-memory structures keyed on the zip mtime."""
    _download_gtfs()
    mtime = GTFS_ZIP.stat().st_mtime
    if _cache.get("mtime") == mtime:
        return _cache

    with zipfile.ZipFile(GTFS_ZIP) as zf:
        calendar = _read_csv(zf, "calendar.txt")
        cal_dates = _read_csv(zf, "calendar_dates.txt")
        trips = _read_csv(zf, "trips.txt")
        stop_times = _read_csv(zf, "stop_times.txt")
        routes = _read_csv(zf, "routes.txt")

    routes_by_id = {r["route_id"]: r for r in routes}
    trip_info = {
        t["trip_id"]: {
            "service_id": t["service_id"],
            "route_id": t["route_id"],
            "headsign": t.get("trip_headsign", ""),
            "short_name": t.get("trip_short_name", ""),
            "route_name": routes_by_id.get(t["route_id"], {}).get("route_long_name")
            or routes_by_id.get(t["route_id"], {}).get("route_short_name", ""),
        }
        for t in trips
    }

    # trip_id -> {stop_id: (arr_sec, dep_sec, seq)}
    trip_stops: dict[str, dict[str, tuple[int, int, int]]] = {}
    relevant = {MILLBRAE_NB, *DEST_NAMES.keys()}
    for st in stop_times:
        sid = st["stop_id"]
        if sid not in relevant:
            continue
        try:
            arr = _hms_to_sec(st["arrival_time"])
            dep = _hms_to_sec(st["departure_time"])
        except (ValueError, KeyError):
            continue
        seq = int(st["stop_sequence"])
        trip_stops.setdefault(st["trip_id"], {})[sid] = (arr, dep, seq)

    # service_id -> weekday bools and date range
    services = {}
    for c in calendar:
        services[c["service_id"]] = {
            "days": [int(c[d]) for d in
                     ("monday", "tuesday", "wednesday", "thursday",
                      "friday", "saturday", "sunday")],
            "start": c["start_date"],
            "end": c["end_date"],
            "added": set(),
            "removed": set(),
        }
    for cd in cal_dates:
        sid = cd["service_id"]
        s = services.setdefault(sid, {"days": [0] * 7, "start": "00000000",
                                      "end": "99999999", "added": set(), "removed": set()})
        if cd["exception_type"] == "1":
            s["added"].add(cd["date"])
        else:
            s["removed"].add(cd["date"])

    _cache.update({
        "mtime": mtime,
        "trip_info": trip_info,
        "trip_stops": trip_stops,
        "services": services,
        "fetched_at": datetime.fromtimestamp(mtime),
    })
    return _cache


def services_active(d: date) -> set[str]:
    g = _load_gtfs()
    ds = d.strftime("%Y%m%d")
    weekday = d.weekday()
    out = set()
    for sid, s in g["services"].items():
        if ds in s["removed"]:
            continue
        if ds in s["added"]:
            out.add(sid)
            continue
        if s["start"] <= ds <= s["end"] and s["days"][weekday]:
            out.add(sid)
    return out


def find_trips(dest_stop: str, target_arrival: str,
               walk_minutes: int) -> list[dict]:
    g = _load_gtfs()
    target_sec = _hms_to_sec(target_arrival + ":00")
    # Service calendar: always today (date-independent calc, weekday/weekend aware).
    active = services_active(date.today())

    results = []
    for trip_id, stops in g["trip_stops"].items():
        if MILLBRAE_NB not in stops or dest_stop not in stops:
            continue
        info = g["trip_info"].get(trip_id)
        if not info or info["service_id"] not in active:
            continue
        m_arr, m_dep, m_seq = stops[MILLBRAE_NB]
        d_arr, d_dep, d_seq = stops[dest_stop]
        if d_seq <= m_seq:
            continue  # wrong direction
        leave_home_sec = m_dep - walk_minutes * 60
        results.append({
            "trip_id": trip_id,
            "short_name": info["short_name"],
            "route_name": info["route_name"],
            "headsign": info["headsign"],
            "leave_home": _sec_to_hm(leave_home_sec) if leave_home_sec >= 0 else _sec_to_hm(m_dep),
            "millbrae_dep": _sec_to_hm(m_dep),
            "dest_arr": _sec_to_hm(d_arr),
            "duration_min": round((d_arr - m_dep) / 60),
            "diff_sec": d_arr - target_sec,
        })

    results.sort(key=lambda r: (abs(r["diff_sec"]), r["diff_sec"]))
    # Keep nearest 8, prefer trains arriving on/before target first then after.
    nearest = results[:8]
    nearest.sort(key=lambda r: r["millbrae_dep"])
    for r in nearest:
        diff_min = round(r["diff_sec"] / 60)
        if diff_min == 0:
            r["delta"] = "on time"
        elif diff_min < 0:
            r["delta"] = f"{-diff_min} min early"
        else:
            r["delta"] = f"{diff_min} min late"
    return nearest


app = Flask(__name__)


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


TEMPLATE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Millbrae → SF Caltrain Planner</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/timepicker-ui@4.3.0/dist/css/main.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/timepicker-ui@4.3.0/dist/css/themes/theme-dark.css">
<style>
  :root {
    --bg:        #001218;
    --bg-2:      #001a23;
    --panel:     rgba(0, 24, 32, 0.78);
    --panel-2:   rgba(0, 38, 50, 0.85);
    --line:      #0e3a4a;
    --line-2:    #15596f;
    --text:      #d4f5ff;
    --muted:     #5b8a99;
    --cyan:      #5ff7ff;
    --cyan-dim:  #2bb8c7;
    --amber:     #ffb454;
    --green:     #6fffb0;
    --red:       #ff7a7a;
  }
  * { box-sizing: border-box; }
  html { scrollbar-gutter: stable; color-scheme: dark; }
  @view-transition { navigation: auto; }
  html, body {
    color: var(--text); min-height: 100%;
  }
  /* No body bg — Vanta's canvas (z-index: -2) lives behind html/body and
     paints its own deep-black. Setting bg here would cover it. */
  body {
    margin: 0; padding: 1.2em 1.5em 2em;
    font-family: "JetBrains Mono", "IBM Plex Mono", "Fira Code", "Consolas",
                 ui-monospace, monospace;
    font-size: 14px; line-height: 1.4; letter-spacing: .2px;
    position: relative; overflow-x: hidden;
    /* Vertically center .layout in the viewport when there's room. */
    min-height: 100vh;
    display: flex; flex-direction: column; justify-content: center;
  }
  body::before {
    /* Soft cyan vignette only — no opaque base layer or it'll cover the
       animated mesh. Vanta's backgroundColor paints the deep black behind it. */
    content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background:
      radial-gradient(60vmax 50vmax at 50% -10%, rgba(0,54,68,.45) 0%, transparent 60%),
      radial-gradient(50vmax 40vmax at 50% 110%, rgba(0,42,54,.45) 0%, transparent 65%);
  }
  /* Animated Vanta NET mesh */
  #bg-fx {
    position: fixed; inset: 0; z-index: -2;
    pointer-events: none;
  }
  /* Faint horizontal scanlines */
  body::after {
    content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background: repeating-linear-gradient(
      to bottom,
      rgba(0,0,0,0) 0,
      rgba(0,0,0,0) 2px,
      rgba(0, 30, 40, .35) 3px,
      rgba(0,0,0,0) 4px
    );
    mix-blend-mode: multiply;
  }

  .layout {
    max-width: 1180px; margin: 0 auto;
    display: grid; grid-template-columns: minmax(320px, 380px) 1fr;
    gap: 1em; align-items: start;
  }
  @media (max-width: 880px) {
    .layout { grid-template-columns: 1fr; }
  }

  h1 {
    grid-column: 1 / -1;
    margin: 0 0 .4em;
    font-size: 1.25em; font-weight: 700;
    letter-spacing: 5px; text-transform: uppercase;
    color: var(--cyan);
    text-shadow: 0 0 8px rgba(95, 247, 255, .55),
                 0 0 22px rgba(95, 247, 255, .25);
  }
  h1::before { content: "▮ "; color: var(--cyan); }
  h1::after  { content: " ▮"; color: var(--cyan); }

  h2 {
    margin: 0 0 .7em;
    font-size: .82em; font-weight: 700;
    text-transform: uppercase; letter-spacing: 3px;
    color: var(--cyan-dim);
    border-bottom: 1px solid var(--line);
    padding: 0 0 .45em;
  }

  /* Panel = sharp framed box, translucent so the animated mesh shows through.
     Gradient fades the fill toward transparent at the bottom. */
  /* Panel = sharp framed box. Translucent tint + backdrop blur:
     the mesh is visible through it, but the blur and faint cyan wash
     anchor the content area so text stays readable. */
  .panel {
    background: rgba(0, 24, 32, 0.32);
    backdrop-filter: blur(6px) saturate(120%);
    -webkit-backdrop-filter: blur(6px) saturate(120%);
    border: 1px solid var(--line-2);
    border-radius: 0;
    padding: .9em 1.1em;
    box-shadow:
      0 0 0 1px rgba(95, 247, 255, .10),
      inset 0 0 24px rgba(0, 200, 230, .06),
      0 0 30px rgba(0, 50, 65, .35);
    position: relative;
  }
  /* Corner ticks like a HUD */
  .panel::before, .panel::after {
    content: ""; position: absolute; width: 12px; height: 12px;
    border: 1px solid var(--cyan);
    pointer-events: none;
  }
  .panel::before { top: -1px; left: -1px;
                   border-right: 0; border-bottom: 0; }
  .panel::after  { bottom: -1px; right: -1px;
                   border-left: 0; border-top: 0; }

  /* "// label" tag above the panel — translucent matching the panel top */
  .panel-label {
    position: absolute; top: -.7em; left: 14px;
    background: rgba(0, 18, 24, 0.85);
    padding: 0 .6em;
    color: var(--cyan-dim);
    font-size: .72em; letter-spacing: 3px; text-transform: uppercase;
  }

  form { display: grid; grid-template-columns: 1fr; gap: .6em; }
  .field { display: grid; gap: .25em; }
  label {
    color: var(--muted);
    font-size: .7em; font-weight: 600;
    text-transform: uppercase; letter-spacing: 2.5px;
  }
  label::before { content: "> "; color: var(--cyan-dim); }

  input, select, button {
    font: inherit; font-size: .9em;
    background: var(--bg-2); color: var(--text);
    border: 1px solid var(--line-2); border-radius: 0;
    padding: .4em .65em;
    transition: border-color .15s, box-shadow .15s;
  }
  input:hover, select:hover { border-color: var(--cyan-dim); }
  input:focus, select:focus {
    outline: none; border-color: var(--cyan);
    box-shadow: 0 0 0 1px var(--cyan),
                0 0 12px rgba(95, 247, 255, .35),
                inset 0 0 8px rgba(95, 247, 255, .15);
  }
  select {
    -webkit-appearance: none; appearance: none;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--cyan-dim) 50%),
      linear-gradient(135deg, var(--cyan-dim) 50%, transparent 50%);
    background-position:
      calc(100% - 18px) center, calc(100% - 13px) center;
    background-size: 5px 5px, 5px 5px;
    background-repeat: no-repeat;
    padding-right: 2em;
  }
  button {
    cursor: pointer; margin-top: .25em;
    background: transparent; color: var(--cyan);
    border: 1px solid var(--cyan);
    text-transform: uppercase; letter-spacing: 4px; font-weight: 700;
    padding: .55em 1.2em; font-size: .75em;
    box-shadow: 0 0 0 1px rgba(95,247,255,.18),
                inset 0 0 14px rgba(95,247,255,.08),
                0 0 16px rgba(95,247,255,.18);
    transition: background .15s, color .15s, box-shadow .15s, transform .1s;
  }
  button:hover {
    background: var(--cyan); color: #00131a;
    box-shadow: 0 0 0 1px var(--cyan),
                0 0 22px rgba(95,247,255,.55);
  }
  button:active { transform: translateY(1px); }

  table {
    border-collapse: collapse; width: 100%;
    background: transparent;
    font-variant-numeric: tabular-nums;
  }
  th, td {
    text-align: left; padding: .4em .7em;
    border-bottom: 1px solid var(--line);
  }
  th {
    color: var(--cyan-dim); font-weight: 600;
    font-size: .72em; letter-spacing: 2.5px;
    text-transform: uppercase;
    border-bottom: 1px solid var(--line-2);
  }
  tbody tr { transition: background .12s; }
  tbody tr:hover {
    background: rgba(95, 247, 255, .04);
    box-shadow: inset 2px 0 0 var(--cyan);
  }
  tbody tr:last-child td { border-bottom: 0; }
  td { font-size: 1em; }
  .target {
    color: var(--amber); font-weight: 700;
    text-shadow: 0 0 8px rgba(255, 180, 84, .55);
  }
  .early { color: var(--green); }
  .late  { color: var(--red); }
  small  { color: var(--muted); }
  p      { color: var(--muted); }
  ::selection { background: var(--cyan); color: #00131a; }

  .footer {
    grid-column: 1 / -1;
    text-align: center; margin-top: .4em;
    color: var(--muted); letter-spacing: 2px;
    text-transform: uppercase; font-size: .75em;
  }
  /* Footer panel inherits .panel frosted look and spans both columns. */
  .footer-panel {
    grid-column: 1 / -1;
    margin-top: .8em;
    text-align: center;
    padding: .7em 1.1em;
    color: var(--muted);
    letter-spacing: 1.5px;
    font-size: .72em;
    text-transform: uppercase;
  }

  /* Inline clock — let the library render at its native 256px geometry.
     Touching the inner sizes or transform-scaling breaks its hit-detection
     math (clicks land on the wrong minute). */
  .time-inline {
    background: var(--bg-2);
    border: 1px solid var(--line-2);
    border-radius: 0; padding: .4em;
    box-shadow: inset 0 0 14px rgba(95, 247, 255, .06);
    display: flex; align-items: center; justify-content: center;
  }
  .time-inline #time-host {
    /* native size — no transform, no negative margins */
  }
  .time-inline .tp-ui-modal,
  .time-inline .tp-ui-wrapper {
    position: static !important; box-shadow: none !important;
    background: transparent !important;
    min-height: 0 !important;
  }
  .time-inline .tp-ui-overlay { display: none !important; }
  .time-inline .tp-ui-select-time { display: none !important; }
  /* Compress the chunky digit-input header above the clock. */
  .time-inline .tp-ui-header { padding-bottom: 6px !important; }
  .time-inline .tp-ui-hour,
  .time-inline .tp-ui-minutes {
    height: 56px !important; font-size: 36px !important;
  }
  /* Hide the footer (cancel/ok buttons) — we drive value via callbacks. */
  .time-inline .tp-ui-footer { display: none !important; }

  /* Override timepicker-ui's lavender primary with our amber accent. */
  .time-inline [data-theme="dark"],
  .time-inline {
    --tp-primary: var(--amber) !important;
    --tp-on-primary: #1a0e00 !important;
    --tp-primary-container: var(--amber) !important;
    --tp-on-primary-container: #1a0e00 !important;
    --tp-tertiary-container: var(--amber) !important;
    --tp-on-tertiary-container: #1a0e00 !important;
    --tp-am-pm-active: var(--amber) !important;
    --tp-am-pm-text-selected: #1a0e00 !important;
    --tp-outline: var(--amber) !important;
    --tp-wheel-highlight-bg: rgba(255, 180, 84, .14) !important;
    --tp-wheel-selected-color: var(--amber) !important;
    --tp-bg: transparent !important;
    --tp-surface: rgba(0, 24, 32, .35) !important;
    --tp-on-surface: var(--text) !important;
    --tp-text: var(--text) !important;
    --tp-text-secondary: var(--muted) !important;
  }
  .time-inline .tp-ui-circle-hand {
    width: 26px !important; height: 26px !important;
    /* Library anchors circle's bottom edge to hand-top via translate(-50%,-100%);
       shrinking the size pulls the center off the number. Push it back up by
       half the size difference (46→26 = 10px) so the center sits on the digit. */
    top: -10px !important;
  }
  .time-inline .tp-ui-circle-hand.small-circle {
    width: 20px !important; height: 20px !important;
    top: -8px !important;   /* 36→20 = 8px half-diff */
  }
  /* The inner-ring (24h) circle uses translate(-50%,-50%) at top:4px and is
     already size-agnostic; restore that so our top override above doesn't
     break it. */
  .time-inline .tp-ui-circle-hand-24h {
    top: 4px !important;
  }

  .empty {
    text-align: center; color: var(--muted);
    padding: 1.4em 1em; font-size: .9em; letter-spacing: 1.5px;
  }
  .empty strong {
    color: var(--cyan); display: block; margin-bottom: .5em;
    font-size: 1.05em; letter-spacing: 3px; text-transform: uppercase;
    text-shadow: 0 0 10px rgba(95,247,255,.45);
  }

  /* Cursor blink for headings */
  .cursor::after {
    content: "_"; color: var(--cyan);
    animation: blink 1s steps(2, jump-none) infinite;
    margin-left: .15em;
  }
  @keyframes blink { 50% { opacity: 0; } }
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
</head><body>
<div id="bg-fx"></div>
<div class="layout">
<h1>Millbrae → San Francisco Caltrain</h1>

<aside class="panel">
<span class="panel-label">// control</span>
<form method="get">
  <div class="field">
    <label for="dest">Destination</label>
    <select id="dest" name="dest">
      {% for sid, name in destinations %}
        <option value="{{sid}}" {% if sid==dest %}selected{% endif %}>{{name}}</option>
      {% endfor %}
    </select>
  </div>

  <div class="field">
    <label for="time">Arrive by (approx)</label>
    <div class="time-inline">
      <input id="time" type="text" name="time" value="{{target_time}}"
             autocomplete="off" style="display:none">
      <div id="time-host"></div>
    </div>
  </div>

  <div class="field">
    <label for="walk">Distance from Millbrae</label>
    <select id="walk" name="walk">
      {% for m in [0,2,5,10,15,20,30,45,60] %}
        <option value="{{m}}" {% if m==walk %}selected{% endif %}>{{m}} minutes</option>
      {% endfor %}
    </select>
  </div>

  <button type="submit">Find trains</button>
</form>
</aside>

<section class="panel">
<span class="panel-label">// departures</span>
{% if results is not none %}
  <h2>
    Trains to <span class="target">{{ dest_name }}</span>
    near <span class="target">{{ target_time }}</span>
  </h2>
  {% if results %}
    <table>
      <thead><tr>
        <th>Train</th><th>Leave home</th><th>Leaves Millbrae</th><th>Arrives</th><th>Duration</th><th>vs target</th>
      </tr></thead>
      <tbody>
      {% for r in results %}
        <tr>
          <td>#{{r.short_name}} <small>{{r.route_name}}</small></td>
          <td>{{r.leave_home}}</td>
          <td>{{r.millbrae_dep}}</td>
          <td>{{r.dest_arr}}</td>
          <td>{{r.duration_min}} min</td>
          <td class="{% if 'early' in r.delta %}early{% elif 'late' in r.delta %}late{% endif %}">{{r.delta}}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}
    <div class="empty">
      <strong>No matching trains.</strong>
      Try a later target time, or check weekday vs weekend service for this date.
    </div>
  {% endif %}
{% else %}
  <div class="empty">
    <strong>Pick a destination and time</strong>
    Results will appear here.
  </div>
{% endif %}
</section>

<footer class="panel footer-panel">
  <span class="panel-label">// status</span>
  <small>GTFS feed last fetched {{ fetched_at }} · refreshed at most once per day.</small>
</footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/timepicker-ui@4.3.0/dist/index.umd.js"></script>
<script>
  (function () {
    const input = document.getElementById('time');
    if (!input || !window.TimepickerUI) return;

    function pad(n) { return String(n).padStart(2, '0'); }
    function sync(d) {
      if (!d || d.hour == null || d.minutes == null) return;
      input.value = pad(d.hour) + ':' + pad(d.minutes);
    }

    const picker = new window.TimepickerUI(input, {
      clock: { type: '24h' },
      labels: { time: '' },
      ui: {
        theme: 'dark',
        inline: { enabled: true, containerId: 'time-host' },
      },
      onUpdate:  sync,
      onConfirm: sync,
    });
    picker.create();

    // Final safety on submit: read live values straight off the clock inputs.
    if (input.form) {
      input.form.addEventListener('submit', function () {
        const host = document.getElementById('time-host');
        if (!host) return;
        const h  = host.querySelector('.tp-ui-hour');
        const mi = host.querySelector('.tp-ui-minutes');
        const hv = h && (h.value || h.textContent || '').trim();
        const mv = mi && (mi.value || mi.textContent || '').trim();
        if (hv && mv) input.value = pad(hv) + ':' + pad(mv);
      });
    }
  })();
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js"></script>
<script>
  // --- Custom Tron-Train Background ---------------------------------------
  // Wireframe rails with sleepers receding to a vanishing point, distant
  // signal lights twinkling above the horizon, and the occasional headlight
  // tracing the horizon. Cyan + amber, deep teal-black.
  // Mounts into #bg-fx so it sits behind everything (z-index: -2).
  p5.disableFriendlyErrors = true;

  const trainBgSketch = (p) => {
    // World / camera params (tuned to feel calm, not racing)
    const FOCAL       = 220;
    const CAM_HEIGHT  = 70;
    const RAIL_HALFW  = 28;
    const SLEEPER_SPACING = 14;
    const NUM_SLEEPERS = 60;
    const SPEED       = 0.45;

    let zOffset = 0;
    let signals = [];
    let trainPhase = -1;       // -1 means waiting; otherwise 0..1 progress
    let trainCooldown = 240;   // frames until next train

    // Color palette mirrors the Tron tokens
    const CYAN  = [95, 247, 255];
    const AMBER = [255, 180, 84];
    const BG    = [0, 8, 13];

    p.setup = function() {
      const c = p.createCanvas(p.windowWidth, p.windowHeight);
      c.parent("bg-fx");
      c.style("display", "block");
      p.frameRate(30);
      p.pixelDensity(1);
      seedSignals();
    };

    p.windowResized = function() {
      p.resizeCanvas(p.windowWidth, p.windowHeight);
      seedSignals();
    };

    function seedSignals() {
      signals = [];
      const horizonY = p.height * 0.55;
      const count = Math.max(18, Math.floor(p.width / 60));
      for (let i = 0; i < count; i++) {
        signals.push({
          x: p.random(p.width),
          y: p.random(horizonY - p.height * 0.30, horizonY - 4),
          vx: p.random(-0.25, 0.25),
          phase: p.random(p.TWO_PI),
          amber: p.random() < 0.18,
          size: p.random(1.2, 2.2)
        });
      }
    }

    p.draw = function() {
      const cx = p.width / 2;
      const horizonY = p.height * 0.55;

      // Deep teal-black wash with a tiny alpha so signal motion leaves no smear
      p.background(BG[0], BG[1], BG[2]);

      // --- Horizon glow (cyan halo, low and wide) ---
      p.noStroke();
      for (let r = 93; r > 0; r -= 3) {
        const a = p.map(r, 0, 93, 14, 0);
        p.fill(CYAN[0], CYAN[1], CYAN[2], a);
        p.ellipse(cx, horizonY, r * 2.4, r * 0.85);
      }

      // Subtle horizon line
      p.stroke(CYAN[0], CYAN[1], CYAN[2], 50);
      p.strokeWeight(1);
      p.line(0, horizonY, p.width, horizonY);

      // --- Distant signal lights (twinkling) ---
      for (const s of signals) {
        s.x += s.vx;
        if (s.x < -5) s.x = p.width + 5;
        if (s.x > p.width + 5) s.x = -5;
        const tw = 0.5 + 0.5 * Math.sin(p.frameCount * 0.05 + s.phase);
        const col = s.amber ? AMBER : CYAN;
        // soft halo
        p.noStroke();
        p.fill(col[0], col[1], col[2], 25 + 35 * tw);
        p.ellipse(s.x, s.y, s.size * 4, s.size * 4);
        // bright core
        p.fill(col[0], col[1], col[2], 180 + 60 * tw);
        p.ellipse(s.x, s.y, s.size, s.size);
      }

      // --- Rails (two converging lines from horizon to bottom edges) ---
      // Project a near-z ground point to find where rails meet screen edges.
      const nearZ = 1.2;
      const nearScale = FOCAL / nearZ;
      const nearY = horizonY + CAM_HEIGHT * nearScale;
      const nearXL = cx - RAIL_HALFW * nearScale;
      const nearXR = cx + RAIL_HALFW * nearScale;
      p.stroke(CYAN[0], CYAN[1], CYAN[2], 130);
      p.strokeWeight(1.2);
      p.line(cx, horizonY, nearXL, nearY);
      p.line(cx, horizonY, nearXR, nearY);

      // --- Sleepers (ties) flowing toward camera ---
      zOffset = (zOffset + SPEED) % SLEEPER_SPACING;
      for (let i = 1; i < NUM_SLEEPERS; i++) {
        const z = i * SLEEPER_SPACING - zOffset;
        if (z < 0.6) continue;
        const scale = FOCAL / z;
        const y = horizonY + CAM_HEIGHT * scale;
        if (y > p.height + 30) continue;
        const xl = cx - (RAIL_HALFW + 4) * scale;
        const xr = cx + (RAIL_HALFW + 4) * scale;
        // alpha ramps up as sleeper approaches; thin near horizon, bold near camera
        const t = 1 - z / (NUM_SLEEPERS * SLEEPER_SPACING);
        p.stroke(CYAN[0], CYAN[1], CYAN[2], 30 + 170 * t);
        p.strokeWeight(p.constrain(0.4 + scale * 0.04, 0.4, 2.2));
        p.line(xl, y, xr, y);
      }

      // --- Occasional wireframe train crossing the horizon ---
      if (trainPhase >= 0) {
        trainPhase += 1 / 480; // ~16s crossing at 30fps
        if (trainPhase >= 1) {
          trainPhase = -1;
          trainCooldown = Math.floor(p.random(420, 900)); // 14–30s gap
        } else {
          drawTrain(trainPhase, horizonY);
        }
      } else {
        trainCooldown -= 1;
        if (trainCooldown <= 0) trainPhase = 0;
      }
    };

    function drawTrain(t, horizonY) {
      // Travels right→left along a band just above the horizon
      const y = horizonY;
      const x = p.lerp(p.width + 90, -240, t);
      p.push();
      p.translate(x, y);
      p.noFill();
      p.stroke(CYAN[0], CYAN[1], CYAN[2], 200);
      p.strokeWeight(1);
      // Locomotive
      p.rect(-28, -7, 22, 7);
      p.line(-22, -7, -18, -10);  // little chimney/cab roof tick
      p.line(-12, -7, -8, -10);
      // Cars
      for (let i = 0; i < 3; i++) {
        const cx2 = -2 + i * 22;
        p.rect(cx2, -6, 20, 6);
        // window ticks
        p.stroke(CYAN[0], CYAN[1], CYAN[2], 110);
        p.line(cx2 + 4, -4, cx2 + 4, -2);
        p.line(cx2 + 10, -4, cx2 + 10, -2);
        p.line(cx2 + 16, -4, cx2 + 16, -2);
        p.stroke(CYAN[0], CYAN[1], CYAN[2], 200);
      }
      // Headlight (amber pinpoint at the front, leading direction = -x)
      p.noStroke();
      p.fill(AMBER[0], AMBER[1], AMBER[2], 220);
      p.ellipse(-30, -3.5, 2.2, 2.2);
      p.fill(AMBER[0], AMBER[1], AMBER[2], 60);
      p.ellipse(-30, -3.5, 8, 4);
      p.pop();
    }
  };

  new p5(trainBgSketch);
</script>
</body></html>
"""


@app.route("/")
def index():
    with _lock:
        g = _load_gtfs()

    today = date.today()

    target_time = request.args.get("time", "")
    walk = int(request.args.get("walk", 5))
    dest = request.args.get("dest", SF_DESTINATIONS[0][0])

    results = None
    if target_time:
        try:
            results = find_trips(dest, target_time, walk)
        except Exception as e:
            app.logger.exception("find_trips failed")
            results = []

    return render_template(
        "index.html",  # not used; we pass string below via render_template_string
    ) if False else _render(
        destinations=SF_DESTINATIONS,
        dest=dest,
        dest_name=DEST_NAMES.get(dest, ""),
        travel_date=today.isoformat(),
        target_time=target_time,
        walk=walk,
        results=results,
        fetched_at=g["fetched_at"].strftime("%Y-%m-%d %H:%M"),
    )


def _render(**ctx):
    from flask import render_template_string
    return render_template_string(TEMPLATE, **ctx)


if __name__ == "__main__":
    port = int(os.environ.get("WEBAPP_PORT", "5000"))
    app.run(host="127.0.0.1", port=port)