# Caltrain Planner — Millbrae → SF

A small Flask web app that finds the Caltrain trips from **Millbrae** to a San
Francisco station closest to a target arrival time. Tron-styled dark UI with an
animated p5.js train-themed background.

## Features

- Pick a destination (4th & King, 22nd St, Bayshore, So. SF, San Bruno).
- Pick a target arrival time on an inline analog clock (timepicker-ui).
- Pick how far you live from Millbrae (default 5 minutes) — drives a
  "Leave home by" column.
- Returns the 8 trains closest to your target time, marked early / on time / late.
- GTFS schedule fetched from
  [Trillium Transit](https://data.trilliumtransit.com/gtfs/caltrain-ca-us/caltrain-ca-us.zip)
  and cached on disk for 24h.
- Animated p5.js background: receding rails, twinkling signal lights, a
  wireframe Caltrain crossing the horizon.

## Run

```bash
pip install flask
python app.py
```

Then open http://127.0.0.1:5000/.

The first request downloads and caches the GTFS feed in `cache/`; subsequent
requests in the next 24h hit the cache.

## Layout

- `app.py` — Flask app, GTFS parsing, schedule logic, embedded Jinja2 template
  with inline CSS and the p5.js background sketch.
- `cache/` — GTFS zip cache (gitignored).

## Stack

- Python 3 / Flask
- [timepicker-ui](https://github.com/musafir11/timepicker-ui) v4 (analog clock)
- [p5.js](https://p5js.org/) 1.11 (background animation)
- JetBrains Mono via Google Fonts
