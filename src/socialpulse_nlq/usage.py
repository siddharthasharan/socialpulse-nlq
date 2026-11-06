"""Live usage tracking for the SocialPulse demo (Render).

Privacy-first by design; the full statement is docs/usage-tracking.md.
What is recorded, per event: a UTC timestamp, the event name, a country
code (e.g. "DE"), and a salted daily hash of the visitor IP. What is
NEVER recorded: the raw IP (full or partial), User-Agent, message text,
full URLs or query strings, or anything that identifies a person.

The visitor hash is sha256(salt | UTC date | IP), truncated to 16 hex
chars. The salt is generated once per server process (secrets.token_hex)
and is never logged, never committed, never returned by any endpoint.
The hash therefore changes every day and cannot be reversed to an IP.

Geo lookup uses the ip-api.com free endpoint (no key, no dependency).
At most one lookup per /24 subnet per day, 1-second timeout, and any
failure (timeout, error, non-success status) fails open to country
"unknown" — the response is never held up by the resolver. The lookup
is synchronous on a cache miss, so the worst case is a delay of at most
the 1-second timeout on the first request from a new subnet while the
resolver is down; cache hits cost nothing, and the demo's first data
scan is already far slower than that. Private/reserved IPs (localhost,
LAN, ...) are never sent to the resolver; they are "unknown" by
construction.

Note: resolving an IP necessarily sends that IP to ip-api.com as the
lookup key. We do not log or store it ourselves; see ip-api.com for
their handling of lookup requests.
"""
import hashlib
import ipaddress
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

GEO_URL = "http://ip-api.com/json/{ip}?fields=status,countryCode"
GEO_TIMEOUT_S = 1.0
GEO_CACHE_TTL_S = 24 * 3600  # one day: matches the daily visitor hash
GEO_NEGATIVE_TTL_S = 60  # cache a failed lookup briefly, so a down
# resolver is not retried on every new subnet
HASH_HEX_CHARS = 16  # 64 bits: far more than a demo's daily visitors

_SALT = secrets.token_hex(16)  # one salt per server process; never logged
_LOCK = threading.Lock()
_GEO_CACHE = {}  # /24 subnet -> (country_code, resolved_at_monotonic)
_COUNTERS = {
    "sessions": 0,
    "page_loads": 0,
    "sessions_by_country": Counter(),
    "visitors_today": set(),
    "visitors_date": None,  # UTC date the visitor set belongs to
}


def _utc_today():
    return datetime.now(timezone.utc).date()


def visitor_hash(ip, day=None, salt=None):
    """Salted daily hash of a visitor IP. Changes every day; the same
    IP + day + salt always gives the same hash, so distinct hashes in a
    day count unique visitors without ever storing the IP."""
    day = day or _utc_today()
    salt = salt if salt is not None else _SALT
    digest = hashlib.sha256(f"{salt}|{day.isoformat()}|{ip}".encode()).hexdigest()
    return digest[:HASH_HEX_CHARS]


def client_ip(headers, fallback=""):
    """Visitor IP: first entry of X-Forwarded-For (Render sits behind a
    proxy, so the client IP is the first entry), else the socket peer.
    X-Forwarded-For can be spoofed, but this only feeds an aggregate
    usage counter, never an access decision."""
    xff = headers.get("X-Forwarded-For") if headers is not None else None
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return fallback or "unknown"


def _subnet24(ip):
    """The /24 of an IPv4 address (first three octets), or None when
    the address is not a globally routable IPv4 address (private,
    loopback, link-local, reserved, multicast, IPv6, unparsable).
    Only globally routable IPv4 addresses are ever sent to the
    resolver; everything else is "unknown" by construction."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address) or not addr.is_global:
        return None
    return str(addr).rsplit(".", 1)[0]


def _geo_lookup(ip):
    """One ip-api.com call for a single IP. Returns a 2-letter country
    code or "unknown". Never raises."""
    try:
        with urllib.request.urlopen(GEO_URL.format(ip=ip),
                                    timeout=GEO_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return "unknown"
    except Exception:  # noqa: BLE001 - any resolver failure fails open
        return "unknown"
    if payload.get("status") != "success":
        return "unknown"
    code = payload.get("countryCode") or ""
    return code if len(code) == 2 and code.isalpha() else "unknown"


def resolve_country(ip):
    """Country code for an IP, via the /24 cache. At most one resolver
    call per subnet per day; failures fail open to "unknown" and are
    cached briefly so a down resolver is not retried constantly."""
    subnet = _subnet24(ip)
    if subnet is None:
        return "unknown"
    now = time.monotonic()
    with _LOCK:
        cached = _GEO_CACHE.get(subnet)
        if cached is not None:
            country, at = cached
            ttl = GEO_CACHE_TTL_S if country != "unknown" else GEO_NEGATIVE_TTL_S
            if now - at < ttl:
                return country
    country = _geo_lookup(ip)
    with _LOCK:
        _GEO_CACHE[subnet] = (country, now)
    return country


def visitor_context(ip):
    """(country, visitor_hash) for an IP. The country comes from the
    /24 cache (one resolver call per subnet per day at most); the hash
    is computed from the full IP and the current UTC date."""
    return resolve_country(ip), visitor_hash(ip)


def log_event(event, country, visitor_hash_value):
    """One JSON line per event, printed to stdout so it lands in Render
    Logs (search for "event"). Deliberately NOT logged: the raw IP,
    User-Agent, message text, full URLs or query strings."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "event": event, "country": country,
           "visitor_hash": visitor_hash_value}
    print(json.dumps(rec), flush=True)


def record_event(event, ip):
    """Record a page_load or session_start: resolve the visitor
    context, update the in-memory counters, emit the log line.
    Returns (country, visitor_hash). Never raises: a failure anywhere
    in tracking must not break the request being served."""
    try:
        country, vhash = visitor_context(ip)
        today = _utc_today()
        with _LOCK:
            if _COUNTERS["visitors_date"] != today:
                _COUNTERS["visitors_date"] = today
                _COUNTERS["visitors_today"] = set()
            _COUNTERS["visitors_today"].add(vhash)
            if event == "session_start":
                _COUNTERS["sessions"] += 1
                _COUNTERS["sessions_by_country"][country] += 1
            elif event == "page_load":
                _COUNTERS["page_loads"] += 1
        log_event(event, country, vhash)
        return country, vhash
    except Exception:  # noqa: BLE001 - tracking must never break a request
        return "unknown", "unknown"


def usage_snapshot():
    """Counts for GET /api/usage. Counts only — no IPs, no hashes, no
    per-visitor rows. Everything is since the server process started;
    the free tier sleeps/restarts the instance, which resets them."""
    with _LOCK:
        today = _COUNTERS["visitors_date"] or _utc_today()
        return {
            "sessions": _COUNTERS["sessions"],
            "page_loads": _COUNTERS["page_loads"],
            "unique_visitors_today": len(_COUNTERS["visitors_today"]),
            "date": today.isoformat(),
            "sessions_by_country": dict(
                sorted(_COUNTERS["sessions_by_country"].items())),
            "since": "server start",
            "note": ("Counters are in-memory only and reset when the "
                     "instance restarts or sleeps (Render free tier). "
                     "Unique visitors are distinct salted daily hashes; "
                     "the hash changes every day and cannot be reversed "
                     "to an IP."),
        }


def reset_state():
    """Test helper: clear counters and the geo cache. The salt is kept,
    so hashes stay stable within a test process."""
    with _LOCK:
        _GEO_CACHE.clear()
        _COUNTERS["sessions"] = 0
        _COUNTERS["page_loads"] = 0
        _COUNTERS["sessions_by_country"] = Counter()
        _COUNTERS["visitors_today"] = set()
        _COUNTERS["visitors_date"] = None
