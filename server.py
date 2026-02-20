#!/usr/bin/env python3
"""
Flight Tracker – local proxy server
  GET /           → serves index.html
  GET /api/opensky?... → proxies OpenSky Network API (OAuth2 Bearer token)
  GET /api/status      → auth status
  POST /api/save-credentials → save client_id + client_secret

OpenSky moved to OAuth2 client credentials for all accounts created after
mid-March 2025. Basic auth (username/password) is deprecated.

To get your client_id and client_secret:
  1. Log in at https://opensky-network.org
  2. Go to Account → API Clients → Create new client
  3. Copy the client_id and client_secret shown
  4. Enter them in the dashboard's "Fix credentials" modal

Usage:
  python3 server.py
  python3 server.py CLIENT_ID CLIENT_SECRET
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT        = int(os.environ.get("PORT", 8765))
OPENSKY_API = "https://opensky-network.org/api/states/all"
OPENSKY_AC  = "https://opensky-network.org/api/metadata/aircraft/icao/"
ADSBDB_ROUTE= "https://api.adsbdb.com/v0/callsign/"
ADSBDB_AC   = "https://api.adsbdb.com/v0/aircraft/"
TOKEN_URL   = ("https://auth.opensky-network.org/auth/realms/opensky-network"
               "/protocol/openid-connect/token")
CACHE_SECS  = 10          # don't re-hit OpenSky more often than this
TOKEN_GRACE = 60          # refresh token this many seconds before expiry
HERE        = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE  = os.path.join(HERE, "credentials.txt")

# Long-lived caches (aircraft reg/type doesn't change, routes rarely change)
_ac_cache: dict    = {}   # icao24 -> metadata dict
_route_cache: dict = {}   # callsign -> route dict

# ── Load credentials: CLI args → env vars → credentials.txt → empty ──────────
def _load_creds():
    if len(sys.argv) > 2:
        return sys.argv[1], sys.argv[2]
    # Cloud deployment: read from environment variables
    env_id  = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
    env_sec = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    if env_id and env_sec:
        return env_id, env_sec
    if os.path.exists(CREDS_FILE):
        try:
            line = open(CREDS_FILE).read().strip()
            if ":" in line:
                u, p = line.split(":", 1)
                return u.strip(), p.strip()
        except Exception:
            pass
    return "", ""

CLIENT_ID, CLIENT_SECRET = _load_creds()

# ── Token cache ───────────────────────────────────────────────────────────────
_token: str   = ""
_token_expiry: float = 0.0   # epoch seconds when token expires

# ── Response cache ────────────────────────────────────────────────────────────
_cache: dict = {}  # qs → {"ts": float, "status": int, "body": bytes}


def _fetch_token() -> str:
    """Exchange client credentials for a Bearer token. Returns token or ''."""
    global _token, _token_expiry
    if not CLIENT_ID or not CLIENT_SECRET:
        return ""

    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read())
            _token         = payload["access_token"]
            expires_in     = int(payload.get("expires_in", 300))
            _token_expiry  = time.time() + expires_in - TOKEN_GRACE
            print(f"  ✓  OAuth2 token obtained (expires in {expires_in}s)")
            return _token
    except Exception as exc:
        print(f"  ✗  Token fetch failed: {exc}")
        _token, _token_expiry = "", 0.0
        return ""


def _get_token() -> str:
    """Return a valid token, refreshing if necessary."""
    if _token and time.time() < _token_expiry:
        return _token
    return _fetch_token()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        print(f"  {self.command:6s} {self.path[:80]:80s} → {status}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    # ── Proxy OpenSky ─────────────────────────────────────────────────────────
    def proxy_opensky(self, qs):
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

        url = OPENSKY_API + ("?" + qs if qs else "")
        headers = {"User-Agent": "EastBostonFlightTracker/3.0", "Accept": "application/json"}

        token = _get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                body   = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            body   = e.read() or b'{"error":"upstream error"}'
            status = e.code
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

    # ── Save credentials ──────────────────────────────────────────────────────
    def save_credentials(self, body_bytes):
        global CLIENT_ID, CLIENT_SECRET, _token, _token_expiry, _cache
        try:
            payload   = json.loads(body_bytes)
            client_id = payload.get("client_id", "").strip()
            client_secret = payload.get("client_secret", "").strip()
        except Exception:
            self._json_response(400, {"error": "bad json"})
            return

        if not client_id or not client_secret:
            self._json_response(400, {"error": "client_id and client_secret required"})
            return

        with open(CREDS_FILE, "w") as f:
            f.write(f"{client_id}:{client_secret}\n")

        CLIENT_ID     = client_id
        CLIENT_SECRET = client_secret
        _token        = ""
        _token_expiry = 0.0
        _cache.clear()

        # Immediately test by fetching a token
        tok = _get_token()
        if not tok:
            self._json_response(401, {"error": "Credentials saved but token fetch failed — check client_id and client_secret"})
            return

        print(f"  ✓  Credentials saved for client '{client_id}'")
        self._json_response(200, {"ok": True, "client_id": client_id})

    # ── Aircraft metadata ──────────────────────────────────────────────────────
    def proxy_aircraft(self, icao24):
        """Try adsbdb first (richer data), fall back to OpenSky metadata."""
        global _ac_cache
        icao24 = icao24.lower().strip()
        if icao24 in _ac_cache:
            self._json_response(200, _ac_cache[icao24])
            return

        result = {}

        # 1. Try adsbdb (no auth needed, has owner/operator flag)
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

        # 2. Fall back to OpenSky metadata if adsbdb gave nothing
        if not result:
            try:
                url = OPENSKY_AC + icao24
                headers = {"User-Agent": "EastBostonFlightTracker/3.0", "Accept": "application/json"}
                token = _get_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    d = json.loads(resp.read())
                    result = {
                        "type":         d.get("typecode") or d.get("type") or "",
                        "model":        d.get("model") or "",
                        "registration": d.get("registration") or "",
                        "operator":     d.get("owner") or d.get("operator") or "",
                        "operatorIcao": d.get("operatorIcao") or "",
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
            "authenticated": bool(CLIENT_ID),
            "client_id": CLIENT_ID if CLIENT_ID else None,
            "token_valid": bool(_token and time.time() < _token_expiry),
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

    if CLIENT_ID:
        auth_line = f"✓  client_id loaded: '{CLIENT_ID}'"
        _fetch_token()   # warm up token on start
    else:
        auth_line = ("⚠  No credentials — anonymous mode\n"
                     "   Go to your OpenSky account → API Clients → Create client\n"
                     "   Then enter client_id + client_secret in the dashboard modal\n"
                     "   Or run:  python3 server.py CLIENT_ID CLIENT_SECRET")

    print(f"""
  ╔══════════════════════════════════════════════════════╗
  ║        ✈  East Boston Flight Tracker Server          ║
  ╚══════════════════════════════════════════════════════╝

  Open in browser →  http://localhost:{PORT}  (local)
  Listening on     :  0.0.0.0:{PORT}

  Auth          :  {auth_line}
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
