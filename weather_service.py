"""
weather_service.py – mini “national weather service” prototype

Features:
- CLI:
    * Fetch REAL BOM ACCESS-G forecast via Open-Meteo
    * OR use built-in artificial forecast (--fake-data)
    * Print summary for next few hours
    * Optional CSV logging
    * Optional PNG plot (temperature + rainfall)
    * Optional simple rainfall–runoff model

- Web API:
    * FastAPI server with /forecast JSON endpoint
    * /forecast?fake=true returns synthetic/artificial forecast
    * Mobile-friendly HTML frontend at /
    * Leaflet map with radar overlay tiles

This file is written to avoid f-string issues in HTML/JS (no f-strings there).

Usage examples:
    python weather_service.py
    python weather_service.py --fake-data
    python weather_service.py --plot
    python weather_service.py --runoff
    python weather_service.py --serve --port 8000
"""

import sys
import csv
import os
import argparse
import datetime as dt
from typing import List, Optional

import logging
import math
import random

import requests

import matplotlib.pyplot as plt

from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

# --------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------

LATITUDE = -37.81        # Melbourne CBD approx
LONGITUDE = 144.96
TIMEZONE = "Australia/Melbourne"
HOURS_TO_SHOW = 6        # how many future hours to show

CSV_LOG_FILE = "forecast_log.csv"
PLOT_OUTPUT_FILE = "forecast_plot.png"
LOG_FILE = "weather_service.log"

DEFAULT_CATCHMENT_KM2 = 100.0
DEFAULT_RUNOFF_COEFF = 0.6  # 60% runoff (toy)

BOM_API_URL = "https://api.open-meteo.com/v1/bom"

# --------------------------------------------------------------------
# LOGGING
# --------------------------------------------------------------------

logger = logging.getLogger("weather_service")
logger.setLevel(logging.INFO)

_fmt = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)
logger.addHandler(_stream_handler)


# --------------------------------------------------------------------
# CORE HELPERS
# --------------------------------------------------------------------

def nan_if_none(v):
    """Replace None with NaN for safe numeric formatting/plotting."""
    return float("nan") if v is None else v


def fetch_bom_forecast(lat: float, lon: float, tz: str) -> dict:
    """
    Call the BOM ACCESS-G API via Open-Meteo and return the JSON dict.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,wind_speed_10m",
        "timezone": tz,
        "forecast_days": 1,
    }

    logger.info("Requesting REAL forecast lat=%.3f lon=%.3f tz=%s", lat, lon, tz)
    resp = requests.get(BOM_API_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "Real forecast received: hourly points=%d",
        len(data.get("hourly", {}).get("time", [])),
    )
    return data


def build_fake_forecast(
    lat: float,
    lon: float,
    tz: str,
    start: Optional[dt.datetime] = None,
    hours: int = 24,
) -> dict:
    """
    Build a synthetic forecast with:
    - diurnal temperature cycle
    - artificial storm in the middle
    """
    if start is None:
        now = dt.datetime.now().astimezone()
        start = now.replace(minute=0, second=0, microsecond=0)

    times: List[str] = []
    temps: List[float] = []
    rain: List[float] = []
    wind: List[float] = []

    base_temp = 20.0
    diurnal_amp = 7.0
    storm_center = hours // 2

    random.seed(42)

    for i in range(hours):
        t = start + dt.timedelta(hours=i)
        times.append(t.strftime("%Y-%m-%dT%H:00"))

        hour_local = t.hour + t.minute / 60.0
        phase = (hour_local - 15.0) / 24.0 * 2.0 * math.pi
        temp = base_temp - diurnal_amp * math.cos(phase)
        temp += random.uniform(-0.5, 0.5)
        temps.append(temp)

        dist = abs(i - storm_center)
        if dist <= 1:
            p = random.uniform(20.0, 40.0)
            w = random.uniform(50.0, 80.0)
        elif dist == 2:
            p = random.uniform(5.0, 20.0)
            w = random.uniform(30.0, 50.0)
        elif dist == 3:
            p = random.uniform(1.0, 5.0)
            w = random.uniform(15.0, 30.0)
        else:
            p = random.uniform(0.0, 1.0)
            w = random.uniform(5.0, 18.0)

        rain.append(p)
        wind.append(w)

    logger.info("Using FAKE forecast: hours=%d", hours)

    return {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "precipitation": rain,
            "wind_speed_10m": wind,
        },
    }


def find_current_index(times: List[str], now: dt.datetime) -> int:
    """
    Given times (ISO strings) and current local datetime,
    find index of first time >= now, else last index.
    """
    now_str = now.strftime("%Y-%m-%dT%H:00")
    for i, t in enumerate(times):
        if t >= now_str:
            return i
    return len(times) - 1


def print_forecast_summary(data: dict, hours_to_show: int = HOURS_TO_SHOW) -> None:
    """Pretty-print a textual summary from the forecast data."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    rain = hourly.get("precipitation", [])
    wind = hourly.get("wind_speed_10m", [])

    if not times:
        print("No hourly data returned.")
        return

    now = dt.datetime.now().astimezone()
    idx0 = find_current_index(times, now)

    print()
    print("=== Forecast Summary ===")
    print(
        "Location: lat={:.2f}, lon={:.2f}".format(
            data.get("latitude"), data.get("longitude")
        )
    )
    print("Timezone: {}  (local now: {})".format(data.get("timezone"), now))
    print("---------------------------------------")

    max_idx = min(idx0 + hours_to_show, len(times))

    for i in range(idx0, max_idx):
        t = times[i]
        temp = nan_if_none(temps[i])
        pr = nan_if_none(rain[i])
        ws = nan_if_none(wind[i])

        try:
            t_obj = dt.datetime.fromisoformat(t)
            t_str = t_obj.strftime("%Y-%m-%d %H:%M")
        except Exception:
            t_str = t

        print(
            "{}  |  T = {:5.1f} °C  |  Rain = {:4.1f} mm  |  Wind = {:4.1f} km/h".format(
                t_str, temp, pr, ws
            )
        )


# --------------------------------------------------------------------
# CSV LOGGING
# --------------------------------------------------------------------

def init_csv_log(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "logged_at",
                    "location_lat",
                    "location_lon",
                    "time",
                    "temperature_2m_C",
                    "precipitation_mm",
                    "wind_speed_10m_kmh",
                ]
            )
        logger.info("Created CSV log file %s", path)


def append_forecast_to_csv(path: str, data: dict) -> None:
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    rain = hourly.get("precipitation", [])
    wind = hourly.get("wind_speed_10m", [])

    if not times:
        logger.warning("No hourly data returned, nothing to log.")
        return

    logged_at = dt.datetime.now().astimezone().isoformat()
    lat = data.get("latitude")
    lon = data.get("longitude")

    init_csv_log(path)

    rows = 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        for t, temp, pr, ws in zip(times, temps, rain, wind):
            temp = nan_if_none(temp)
            pr = nan_if_none(pr)
            ws = nan_if_none(ws)
            w.writerow(
                [logged_at, "{:.4f}".format(lat), "{:.4f}".format(lon), t, temp, pr, ws]
            )
            rows += 1

    logger.info("Logged %d forecast rows to %s", rows, path)


# --------------------------------------------------------------------
# PLOTTING
# --------------------------------------------------------------------

def plot_forecast(data: dict, output_file: str = PLOT_OUTPUT_FILE) -> None:
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = [nan_if_none(v) for v in hourly.get("temperature_2m", [])]
    rain = [nan_if_none(v) for v in hourly.get("precipitation", [])]

    if not times:
        print("No hourly data returned, nothing to plot.")
        return

    t_objs = [dt.datetime.fromisoformat(t) for t in times]

    fig, ax1 = plt.subplots(figsize=(10, 5))

    ax1.plot(t_objs, temps, marker="o")
    ax1.set_xlabel("Time")
    ax1.set_ylabel("Temperature (°C)")
    ax1.grid(True, which="both", linestyle="--", linewidth=0.5)

    ax2 = ax1.twinx()
    ax2.bar(t_objs, rain, width=0.03)
    ax2.set_ylabel("Rain (mm)")

    fig.autofmt_xdate()
    fig.suptitle("Forecast (temperature & rainfall)")

    plt.tight_layout()
    plt.savefig(output_file, dpi=120)
    plt.close(fig)

    print("Saved plot to {}".format(output_file))
    logger.info("Saved plot to %s", output_file)


# --------------------------------------------------------------------
# RAINFALL–RUNOFF MODEL
# --------------------------------------------------------------------

def compute_runoff_series(
    times: List[str],
    precip_mm: List[float],
    catchment_km2: float = DEFAULT_CATCHMENT_KM2,
    runoff_coeff: float = DEFAULT_RUNOFF_COEFF,
) -> List[float]:
    """
    Toy rainfall–runoff model: Q (m3/s) = P(mm) * A(km2) * C / 3.6
    """
    area_factor = catchment_km2 / 3.6
    q_series: List[float] = []
    for p in precip_mm:
        p_val = 0.0 if p is None else p
        q = p_val * area_factor * runoff_coeff
        q_series.append(q)
    return q_series


def print_runoff_summary(
    data: dict,
    catchment_km2: float = DEFAULT_CATCHMENT_KM2,
    runoff_coeff: float = DEFAULT_RUNOFF_COEFF,
    hours_to_show: int = HOURS_TO_SHOW,
) -> None:
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    rain = hourly.get("precipitation", [])

    if not times:
        print("No hourly data for runoff model.")
        return

    now = dt.datetime.now().astimezone()
    idx0 = find_current_index(times, now)
    max_idx = min(idx0 + hours_to_show, len(times))

    sub_times = times[idx0:max_idx]
    sub_rain = rain[idx0:max_idx]

    q_series = compute_runoff_series(
        sub_times, sub_rain, catchment_km2, runoff_coeff
    )

    print()
    print("=== Simple Rainfall–Runoff Model (toy) ===")
    print(
        "Catchment area: {:.1f} km², Runoff coeff: {:.2f}".format(
            catchment_km2, runoff_coeff
        )
    )
    print("Time                 | Rain (mm) | Runoff (m³/s)")
    print("---------------------+-----------+--------------")

    for t, p, q in zip(sub_times, sub_rain, q_series):
        p_val = 0.0 if p is None else p
        try:
            t_obj = dt.datetime.fromisoformat(t)
            t_str = t_obj.strftime("%Y-%m-%d %H:%M")
        except Exception:
            t_str = t
        print("{:19s} | {:9.2f} | {:12.2f}".format(t_str, p_val, q))


# --------------------------------------------------------------------
# FASTAPI APP
# --------------------------------------------------------------------

app = FastAPI(title="Mini BOM Weather Service", version="0.4.0")


@app.get("/forecast")
def forecast_endpoint(
    lat: float = LATITUDE,
    lon: float = LONGITUDE,
    tz: str = TIMEZONE,
    hours: int = HOURS_TO_SHOW,
    fake: bool = False,
):
    """
    JSON forecast endpoint.
    - fake=true uses synthetic forecast instead of live BOM API.
    """
    try:
        if fake:
            data = build_fake_forecast(lat, lon, tz, hours=max(hours, HOURS_TO_SHOW))
        else:
            data = fetch_bom_forecast(lat, lon, tz)
    except requests.RequestException as e:
        logger.exception("Error fetching forecast in /forecast")
        return JSONResponse(
            status_code=502,
            content={"error": "Error fetching forecast: {}".format(e)},
        )

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if times and hours > 0:
        now = dt.datetime.now().astimezone()
        idx0 = find_current_index(times, now)
        max_idx = min(idx0 + hours, len(times))

        for key, arr in list(hourly.items()):
            hourly[key] = arr[idx0:max_idx]
        data["hourly"] = hourly

    return data


@app.get("/", response_class=HTMLResponse)
def root_page():
    """
    Mobile-friendly HTML frontend with:
    - toggle between real and artificial forecast
    - simple Leaflet map + radar overlay
    """
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Mini BOM Weather</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 0;
      background: #f5f5f5;
    }
    header {
      background: #1976d2;
      color: white;
      padding: 0.8rem 1rem;
      text-align: center;
    }
    main {
      padding: 0.8rem;
    }
    .card {
      background: white;
      border-radius: 8px;
      padding: 0.8rem;
      margin-bottom: 0.8rem;
      box-shadow: 0 1px 3px rgba(0,0,0,0.15);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }
    th, td {
      padding: 0.3rem;
      border-bottom: 1px solid #eee;
      text-align: right;
    }
    th:first-child, td:first-child {
      text-align: left;
    }
    #map {
      width: 100%;
      height: 260px;
      border-radius: 8px;
      overflow: hidden;
    }
    .small {
      font-size: 0.75rem;
      color: #555;
    }
    .toggle-row {
      margin-bottom: 0.5rem;
      font-size: 0.8rem;
    }
  </style>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    crossorigin=""
  />
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    crossorigin="">
  </script>
</head>
<body>
<header>
  <h1 style="margin:0;font-size:1.1rem;">Mini BOM Weather</h1>
  <div class="small">Melbourne demo – toggle real vs artificial forecast</div>
</header>
<main>
  <section class="card">
    <div class="toggle-row">
      <label>
        <input type="checkbox" id="fake-toggle">
        Use artificial forecast (offline/demo)
      </label>
    </div>
    <h2 style="margin-top:0;font-size:1rem;">Next HOURS_PLACEHOLDER hours (forecast)</h2>
    <div id="forecast-status" class="small">Loading…</div>
    <table id="forecast-table" style="display:none;">
      <thead>
        <tr>
          <th>Time</th>
          <th>T (°C)</th>
          <th>Rain (mm)</th>
          <th>Wind (km/h)</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </section>

  <section class="card">
    <h2 style="margin-top:0;font-size:1rem;">Radar overlay (demo)</h2>
    <div id="map"></div>
    <div class="small">
      Radar tiles via RainViewer (global). Swap URL in code for BOM/GA tiles.
    </div>
  </section>
</main>

<script>
var DEFAULT_LAT = LAT_PLACEHOLDER;
var DEFAULT_LON = LON_PLACEHOLDER;
var DEFAULT_TZ  = "TZ_PLACEHOLDER";
var DEFAULT_HOURS = HOURS_PLACEHOLDER;

function loadForecast() {
  var fake = document.getElementById('fake-toggle').checked;
  var url = '/forecast?lat=' + DEFAULT_LAT
          + '&lon='  + DEFAULT_LON
          + '&tz='   + encodeURIComponent(DEFAULT_TZ)
          + '&hours=' + DEFAULT_HOURS
          + '&fake=' + fake;

  var status = document.getElementById('forecast-status');
  var table = document.getElementById('forecast-table');
  var tbody = table.querySelector('tbody');
  status.textContent = 'Loading…';
  status.style.display = '';
  table.style.display = 'none';
  tbody.innerHTML = '';

  fetch(url)
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      var hourly = data.hourly || {};
      var times = hourly.time || [];
      var temps = hourly.temperature_2m || [];
      var rain = hourly.precipitation || [];
      var wind = hourly.wind_speed_10m || [];

      if (!times.length) {
        status.textContent = 'No data.';
        return;
      }

      status.style.display = 'none';
      table.style.display = '';

      for (var i = 0; i < times.length; i++) {
        var tr = document.createElement('tr');
        var t = new Date(times[i]);
        var tStr = t.toLocaleString([], {hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short'});

        var temp = (temps[i] != null) ? temps[i].toFixed(1) : '–';
        var pr   = (rain[i]  != null) ? rain[i].toFixed(1)  : '–';
        var ws   = (wind[i]  != null) ? wind[i].toFixed(1)  : '–';

        var rowHtml = ''
          + '<td>' + tStr + '</td>'
          + '<td>' + temp + '</td>'
          + '<td>' + pr   + '</td>'
          + '<td>' + ws   + '</td>';

        tr.innerHTML = rowHtml;
        tbody.appendChild(tr);
      }
    })
    .catch(function(err) {
      console.error(err);
      status.textContent = 'Error fetching forecast.';
    });
}

document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('fake-toggle').addEventListener('change', loadForecast);
  loadForecast();

  var map = L.map('map').setView([DEFAULT_LAT, DEFAULT_LON], 6);

  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap'
  }).addTo(map);

  var radar = L.tileLayer(
    'https://tilecache.rainviewer.com/v2/radar/now/256/{z}/{x}/{y}/2/1_1.png',
    {
      opacity: 0.6
    }
  );
  radar.addTo(map);
});
</script>
</body>
</html>"""

    html = html.replace("LAT_PLACEHOLDER", str(LATITUDE))
    html = html.replace("LON_PLACEHOLDER", str(LONGITUDE))
    html = html.replace("HOURS_PLACEHOLDER", str(HOURS_TO_SHOW))
    html = html.replace("TZ_PLACEHOLDER", TIMEZONE)

    return HTMLResponse(content=html)


# --------------------------------------------------------------------
# SIMPLE TESTS (optional)
# --------------------------------------------------------------------

def run_tests() -> int:
    import unittest

    class WeatherTests(unittest.TestCase):
        def test_build_fake_forecast_length(self):
            data = build_fake_forecast(LATITUDE, LONGITUDE, TIMEZONE, hours=12)
            hourly = data["hourly"]
            self.assertEqual(len(hourly["time"]), 12)
            self.assertEqual(len(hourly["temperature_2m"]), 12)
            self.assertEqual(len(hourly["precipitation"]), 12)
            self.assertEqual(len(hourly["wind_speed_10m"]), 12)

        def test_compute_runoff_zero(self):
            times = ["2025-01-01T00:00"]
            precip = [0.0]
            q = compute_runoff_series(times, precip, 100.0, 0.6)
            self.assertEqual(q, [0.0])

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WeatherTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


# --------------------------------------------------------------------
# CLI ENTRY POINT
# --------------------------------------------------------------------

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Mini BOM ACCESS-G weather prototype (real or artificial)"
    )
    parser.add_argument("--lat", type=float, default=LATITUDE, help="Latitude")
    parser.add_argument("--lon", type=float, default=LONGITUDE, help="Longitude")
    parser.add_argument("--tz", type=str, default=TIMEZONE, help="Timezone")
    parser.add_argument(
        "--hours", type=int, default=HOURS_TO_SHOW, help="Hours to show"
    )
    parser.add_argument(
        "--no-log", action="store_true", help="Do not log forecast to CSV"
    )
    parser.add_argument(
        "--plot", action="store_true", help="Generate PNG plot"
    )
    parser.add_argument(
        "--runoff", action="store_true", help="Print rainfall–runoff summary"
    )
    parser.add_argument(
        "--catchment-km2",
        type=float,
        default=DEFAULT_CATCHMENT_KM2,
        help="Catchment area for runoff model (km²)",
    )
    parser.add_argument(
        "--runoff-coeff",
        type=float,
        default=DEFAULT_RUNOFF_COEFF,
        help="Runoff coefficient (0–1)",
    )
    parser.add_argument(
        "--fake-data",
        action="store_true",
        help="Use built-in artificial forecast instead of live BOM API",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run FastAPI web server",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port for FastAPI server"
    )
    parser.add_argument(
        "--test", action="store_true", help="Run unit tests and exit"
    )

    args = parser.parse_args(argv)

    if args.test:
        return run_tests()

    if args.serve:
        import uvicorn

        logger.info("Starting FastAPI server on 0.0.0.0:%d", args.port)
        uvicorn.run(
            "weather_service:app",
            host="0.0.0.0",
            port=args.port,
            reload=False,
        )
        return 0

    print(
        "Requesting {} forecast for lat={}, lon={}, tz={} …".format(
            "FAKE" if args.fake_data else "REAL",
            args.lat,
            args.lon,
            args.tz,
        )
    )

    try:
        if args.fake_data:
            data = build_fake_forecast(
                args.lat, args.lon, args.tz, hours=max(args.hours, HOURS_TO_SHOW)
            )
        else:
            data = fetch_bom_forecast(args.lat, args.lon, args.tz)
    except requests.RequestException as e:
        logger.exception("Error fetching data in CLI")
        print("Error fetching data: {}".format(e))
        return 1

    print_forecast_summary(data, args.hours)

    if args.runoff:
        print_runoff_summary(
            data,
            catchment_km2=args.catchment_km2,
            runoff_coeff=args.runoff_coeff,
            hours_to_show=args.hours,
        )

    if not args.no_log:
        append_forecast_to_csv(CSV_LOG_FILE, data)

    if args.plot:
        plot_forecast(data, PLOT_OUTPUT_FILE)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
