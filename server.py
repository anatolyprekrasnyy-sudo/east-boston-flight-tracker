#!/usr/bin/env python3
"""
Flight Tracker – local proxy server
  GET /           → serves index.html
  GET /api/opensky?... → fetches from adsb.lol (free, no auth, reachable from Railway)
                         and translates response into OpenSky states array format
  GET /api/status      → auth status
  POST /api/save-credentials → save client_id + client_secret (no longer needed for
                               flight data, kept for UI compatibility)

Flight data source: api.adsb.lol — free ADS-B aggregator, no authentication required.
OpenSky Network's auth server (auth.opensky-network.org) is unreachable from Railway's
network, so adsb.lol is used as the primary data source instead.

Usage:
  python3 server.py
"""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT         = int(os.environ.get("PORT", 8765))
ADSBDB_ROUTE = "https://api.adsbdb.com/v0/callsign/"
ADSBDB_AC    = "https://api.adsbdb.com/v0/aircraft/"
ADSBDB_LOL   = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"
CACHE_SECS   = 10          # don't re-hit data source more often than this
HERE         = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE   = os.path.join(HERE, "credentials.txt")

# Long-lived caches (aircraft reg/type doesn't change, routes rarely change)
_ac_cache: dict    = {}   # icao24 -> metadata dict
_route_cache: dict = {}   # callsign -> route dict

# ── Response cache ────────────────────────────────────────────────────────────
_cache: dict = {}  # qs → {"ts": float, "status": int, "body": bytes}


def _adsbdblol_to_opensky_states(ac_list: list) -> list:
    """Convert adsb.lol aircraft list to OpenSky states array format.

    OpenSky state vector indices (from index.html):
      0  icao24       hex transponder address
      1  callsign     flight number / tail
      2  origin_country
      3  time_position
      4  last_contact
      5  longitude
      6  latitude
      7  baro_altitude  (metres)
      8  on_ground
      9  velocity       (m/s)
      10 true_track     (degrees)
      11 vertical_rate  (m/s)
      12 sensors
      13 geo_altitude   (metres)
      14 squawk
      15 spi
      16 position_source
    """
    FT_TO_M  = 0.3048
    KT_TO_MS = 0.514444
    FPM_TO_MS = 0.00508

    states = []
    for ac in ac_list:
        # Skip aircraft with no position
        if ac.get("lat") is None or ac.get("lon") is None:
            continue

        icao24   = (ac.get("hex") or "").lower()
        callsign = (ac.get("flight") or "").strip()
        on_ground = ac.get("alt_baro") == "ground"

        # Altitude: adsb.lol gives feet, OpenSky expects metres
        alt_baro = ac.get("alt_baro")
        if alt_baro == "ground" or alt_baro is None:
            baro_alt = None
        else:
            try:
                baro_alt = float(alt_baro) * FT_TO_M
            except (TypeError, ValueError):
                baro_alt = None

        alt_geom = ac.get("alt_geom")
        if alt_geom is None:
            geo_alt = None
        else:
            try:
                geo_alt = float(alt_geom) * FT_TO_M
            except (TypeError, ValueError):
                geo_alt = None

        # Speed: adsb.lol gives knots, OpenSky expects m/s
        gs = ac.get("gs")
        velocity = float(gs) * KT_TO_MS if gs is not None else None

        # Vertical rate: adsb.lol gives ft/min, OpenSky expects m/s
        vr = ac.get("baro_rate") or ac.get("geom_rate")
        vert_rate = float(vr) * FPM_TO_MS if vr is not None else None

        heading = ac.get("track") or ac.get("true_heading")
        squawk  = ac.get("squawk") or ""

        now = int(time.time())
        states.append([
            icao24,           # 0
            callsign,         # 1
            "",               # 2  origin_country (not provided)
            now,              # 3  time_position
            now,              # 4  last_contact
            ac.get("lon"),    # 5
            ac.get("lat"),    # 6
            baro_alt,         # 7
            on_ground,        # 8
            velocity,         # 9
            heading,          # 10
            vert_rate,        # 11
            None,             # 12 sensors
            geo_alt,          # 13
            squawk,           # 14
            False,            # 15 spi
            0,                # 16 position_source (0=ADS-B)
        ])
    return states


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        print(f"  {self.command:6s} {self.path[:80]:80s} → {status}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    # ── Proxy flight data via adsb.lol ────────────────────────────────────────
    def proxy_opensky(self, qs):
        """Fetch from adsb.lol and return OpenSky-compatible states array."""
        global _cache
        now = time.time()

        if qs in _cache and (now - _cache[qs]["ts"]) < CACHE_SECS:
            c = _cache[qs]
            self.send_response(c["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Cache", "HIT")
            self.send_cors()
            self.end_headers()
            self.wfile.write(c["body"])
            return

        # Parse bounding box from query string (lamin, lomin, lamax, lomax)
        params = dict(urllib.parse.parse_qsl(qs))
        try:
            lamin = float(params["lamin"])
            lamax = float(params["lamax"])
            lomin = float(params["lomin"])
            lomax = float(params["lomax"])
            center_lat = (lamin + lamax) / 2
            center_lon = (lomin + lomax) / 2
            # Rough radius in nautical miles from bounding box
            lat_deg = (lamax - lamin) / 2
            lon_deg = (lomax - lomin) / 2
            dist_nm = int(math.ceil(max(lat_deg, lon_deg) * 60)) + 5
            dist_nm = max(10, min(dist_nm, 250))
        except (KeyError, ValueError):
            # No bbox — use East Boston defaults
            center_lat, center_lon, dist_nm = 42.37, -71.04, 25

        url = ADSBDB_LOL.format(lat=center_lat, lon=center_lon, dist=dist_nm)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "EastBostonFlightTracker/3.0", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data    = json.loads(resp.read())
                ac_list = data.get("ac") or []
                states  = _adsbdblol_to_opensky_states(ac_list)
                body    = json.dumps({"time": int(now), "states": states}).encode()
                status  = 200
                print(f"  adsb.lol: {len(states)} aircraft in view")
        except Exception as exc:
            body   = json.dumps({"error": str(exc)}).encode()
            status = 502

        _cache[qs] = {"ts": now, "status": status, "body": body}

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Cache", "MISS")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    # ── Serve index.html ──────────────────────────────────────────────────────
    def serve_html(self):
        path = os.path.join(HERE, "index.html")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"index.html not found")

    # ── Save credentials (kept for UI compatibility, no longer needed) ────────
    def save_credentials(self, body_bytes):
        global _cache
        try:
            payload   = json.loads(body_bytes)
            client_id = payload.get("client_id", "").strip()
        except Exception:
            self._json_response(400, {"error": "bad json"})
            return
        _cache.clear()
        print(f"  ✓  Credentials received for '{client_id}' (flight data via adsb.lol, no token needed)")
        self._json_response(200, {"ok": True, "client_id": client_id})

    # ── Aircraft metadata ──────────────────────────────────────────────────────
    def proxy_aircraft(self, icao24):
        """Fetch aircraft metadata from adsbdb (no auth needed)."""
        global _ac_cache
        icao24 = icao24.lower().strip()
        if icao24 in _ac_cache:
            self._json_response(200, _ac_cache[icao24])
            return

        result = {}
        try:
            req = urllib.request.Request(
                ADSBDB_AC + icao24.upper(),
                headers={"User-Agent": "EastBostonFlightTracker/3.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                d = json.loads(resp.read()).get("response", {}).get("aircraft", {})
                if d:
                    result = {
                        "type":         d.get("icao_type") or d.get("type") or "",
                        "model":        d.get("type") or "",
                        "registration": d.get("registration") or "",
                        "operator":     d.get("registered_owner") or "",
                        "operatorIcao": d.get("registered_owner_operator_flag_code") or "",
                    }
        except Exception:
            pass

        _ac_cache[icao24] = result
        self._json_response(200, result)

    # ── Flight route ───────────────────────────────────────────────────────────
    def proxy_route(self, callsign):
        """Fetch route (origin/destination airports) from adsbdb by callsign."""
        global _route_cache
        cs = callsign.upper().strip()
        if cs in _route_cache:
            self._json_response(200, _route_cache[cs])
            return

        try:
            req = urllib.request.Request(
                ADSBDB_ROUTE + cs,
                headers={"User-Agent": "EastBostonFlightTracker/3.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                d = json.loads(resp.read()).get("response", {}).get("flightroute", {})
                if not d:
                    _route_cache[cs] = {}
                    self._json_response(200, {})
                    return
                airline = d.get("airline", {})
                origin  = d.get("origin", {})
                dest    = d.get("destination", {})
                result  = {
                    "origin_iata":      origin.get("iata_code", ""),
                    "origin_icao":      origin.get("icao_code", ""),
                    "origin_name":      origin.get("name", ""),
                    "origin_city":      origin.get("municipality", ""),
                    "dest_iata":        dest.get("iata_code", ""),
                    "dest_icao":        dest.get("icao_code", ""),
                    "dest_name":        dest.get("name", ""),
                    "dest_city":        dest.get("municipality", ""),
                    "airline_name":     airline.get("name", ""),
                    "airline_iata":     airline.get("iata", ""),
                    "airline_icao":     airline.get("icao", ""),
                }
                _route_cache[cs] = result
                self._json_response(200, result)
        except urllib.error.HTTPError:
            _route_cache[cs] = {}
            self._json_response(200, {})
        except Exception:
            self._json_response(502, {})

    # ── Server status ─────────────────────────────────────────────────────────
    def send_status(self):
        self._json_response(200, {
            "authenticated": True,
            "client_id": "adsb.lol",
            "token_valid": True,
            "source": "adsb.lol",
        })

    # ── Helper ────────────────────────────────────────────────────────────────
    def _json_response(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    # ── Router ────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        qs     = parsed.query
        if path in ("/", "/index.html"):    self.serve_html()
        elif path == "/api/opensky":        self.proxy_opensky(qs)
        elif path == "/api/status":         self.send_status()
        elif path.startswith("/api/aircraft/"):
            icao = path[len("/api/aircraft/"):]
            self.proxy_aircraft(icao)
        elif path.startswith("/api/route/"):
            cs = path[len("/api/route/"):]
            self.proxy_route(cs)
        else:
            self.send_response(404); self.end_headers()
            self.wfile.write(b"Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        if path == "/api/save-credentials": self.save_credentials(body)
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()


if __name__ == "__main__":
    os.chdir(HERE)

    print(f"""
  ╔══════════════════════════════════════════════════════╗
  ║        ✈  East Boston Flight Tracker Server          ║
  ╚══════════════════════════════════════════════════════╝

  Open in browser →  http://localhost:{PORT}  (local)
  Listening on     :  0.0.0.0:{PORT}

  Data source   :  adsb.lol (free ADS-B, no auth required)
  Cache TTL     :  {CACHE_SECS}s

  Press Ctrl+C to stop.
""")

    host = "0.0.0.0"
    server = HTTPServer((host, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        sys.exit(0)
