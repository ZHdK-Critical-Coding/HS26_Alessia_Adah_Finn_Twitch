#!/usr/bin/env python3
"""Lokaler Server: Vollbild-Seite plus zufällige Twitch-Streams mit 0 Zuschauer*innen."""

import http.client
import json
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import textwrap
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
# Pillow aus projekt-lokalem .venv laden. Wichtig: nur site-packages der
# laufenden Interpreter-Version (CLT python3 oft 3.9 ≠ .venv 3.13).
import sys as _sys

_venv_site = (
    ROOT
    / ".venv"
    / "lib"
    / f"python{_sys.version_info.major}.{_sys.version_info.minor}"
    / "site-packages"
)
if _venv_site.is_dir():
    _p = str(_venv_site)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
else:
    # Fallback: erst passende Version, sonst beliebige — fremde ABI kann scheitern.
    _fallback = None
    for _site in (ROOT / ".venv").glob("lib/python*/site-packages"):
        if _site.name == f"python{_sys.version_info.major}.{_sys.version_info.minor}":
            _fallback = _site
            break
        if _fallback is None:
            _fallback = _site
    if _fallback is not None:
        _p = str(_fallback)
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

PORT = 8787
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
POOL_TTL_SECONDS = 60
POOL_MIN = 12
SCREENSHOT_DIR = ROOT / "Screenshots"
SCREENSHOT_INTERVAL = 60
STRIP_FILE = SCREENSHOT_DIR / "endless_strip.jpg"
STRIP_META_FILE = SCREENSHOT_DIR / "endless_strip.json"
# Zusätzlicher, gut auffindbarer Ordner für den Endlosstreifen.
STREIFEN_DIR = ROOT / "Streifen"
STREIFEN_FILE = STREIFEN_DIR / "endless_strip.jpg"
# JPEG mjpeg-Encoder: max. ~65500 px je Achse — darunter rotieren wir den Streifen.
JPEG_MAX_DIM = 65000
ASSETS_DIR = ROOT / "assets"
# VORLAGE_DRUCK: seitliche Meta-Texte + grauer Titelbalken (nicht zentriertes Overlay).
STRIP_INNER_WIDTH = 1200
STRIP_SIDE_MARGIN = 52
STRIP_BAR_GRAY = (167, 154, 146)
STRIP_BG = (255, 255, 255)
STRIP_TITLE_SIZE = 28
STRIP_META_SIZE = 26
STRIP_BAR_PAD = 18
# Bevorzugt: assets/PRINT_FONT.ttf bzw. jede .ttf/.otf im assets-Ordner.
# Fallback: assets/PRINT_FONT.ttf.fallback (Arimo), dann Systemschriften.
SYSTEM_FONT_FALLBACKS = [
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    Path("/usr/share/fonts/truetype/croscore/Arimo-Regular.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
]
PREFERRED_RATIO = 7
GAMING_RATIO = 3
FILTERS_FILE = ROOT / "pool_filters.json"


def _asset_font_candidates():
    """Eigene Schriften in assets/ zuerst (nicht .fallback)."""
    preferred_names = (
        "PRINT_FONT.ttf",
        "PRINT_FONT.otf",
        "AkkuratStd-Mono.ttf",
    )
    found = []
    for name in preferred_names:
        candidate = ASSETS_DIR / name
        if candidate.is_file():
            found.append(candidate)
    if ASSETS_DIR.is_dir():
        for pattern in ("*.ttf", "*.otf"):
            for candidate in sorted(ASSETS_DIR.glob(pattern)):
                if candidate.name.endswith(".fallback"):
                    continue
                if candidate not in found:
                    found.append(candidate)
    fallback = ASSETS_DIR / "PRINT_FONT.ttf.fallback"
    if fallback.is_file():
        found.append(fallback)
    found.extend(SYSTEM_FONT_FALLBACKS)
    return found


def resolve_font_file():
    for candidate in _asset_font_candidates():
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "Keine Schrift gefunden. Lege eine .ttf/.otf in assets/ ab "
        "(z. B. assets/PRINT_FONT.ttf) oder installiere DejaVu/Arimo/Arial."
    )


FONT_FILE = None
try:
    FONT_FILE = resolve_font_file()
except FileNotFoundError:
    FONT_FILE = str(ASSETS_DIR / "PRINT_FONT.ttf")

# IRL, Just Chatting, ASMR, Musik, Crafts, Art und verwandte Kategorien.
# Von zehn Streams sollen sieben hieraus kommen, drei aus Gaming.
PREFERRED_CATEGORY_NAMES = [
    "Just Chatting",
    "ASMR",
    "Music",
    "Art",
    "Makers & Crafting",
    "Food & Drink",
    "Talk Shows & Podcasts",
    "Fitness & Health",
    "Science & Technology",
    "Software and Game Development",
    "Pools, Hot Tubs, and Beaches",
    "Animals, Aquariums, and Zoos",
    "Sports",
    "Special Events",
    "I'm Only Sleeping",
    "Always On",
    "Politics",
    "IRL",
    "Co-working & Studying",
    "Travel & Outdoors",
    "Beauty & Body Art",
]
CATEGORY_ALIASES = {
    "Music": ["musik"],
    "Art": ["kunst"],
    "Makers & Crafting": ["handwerk", "makers and crafting"],
    "Beauty & Body Art": ["beauty and body art"],
}
PREFERRED_CATEGORIES = {name.casefold() for name in PREFERRED_CATEGORY_NAMES}
for _aliases in CATEGORY_ALIASES.values():
    PREFERRED_CATEGORIES.update(a.casefold() for a in _aliases)

LANGUAGES = [
    "EN", "DE", "ES", "FR", "IT", "PT", "JA", "KO", "ZH", "RU", "PL", "TR",
    "NL", "SV", "TH", "VI", "AR", "CS", "HU", "FI", "DA", "NO", "EL", "RO",
    "BG", "SK", "ID", "HI", "OTHER", "UK", "CA", "ASL",
]

NODE_FIELDS = """
  id
  title
  viewersCount
  type
  averageFPS
  bitrate
  language
  broadcaster { login displayName }
  game { name }
"""

DIRECTORY_QUERY = f"""
query {{
  streams(first: 30, options: {{sort: VIEWER_COUNT_ASC}}) {{
    edges {{ node {{ {NODE_FIELDS} }} }}
  }}
}}
"""

LANGUAGE_QUERY = f"""
query ($lang: Language!) {{
  streams(first: 30, options: {{sort: VIEWER_COUNT_ASC, languages: [$lang]}}) {{
    edges {{ node {{ {NODE_FIELDS} }} }}
  }}
}}
"""

CATEGORY_QUERY = f"""
query ($name: String!) {{
  game(name: $name) {{
    name
    streams(first: 30, options: {{sort: VIEWER_COUNT_ASC}}) {{
      edges {{ node {{ {NODE_FIELDS} }} }}
    }}
  }}
}}
"""

VERIFY_QUERY = """
query ($login: String!) {
  user(login: $login) {
    login
    displayName
    stream {
      title
      viewersCount
      type
      averageFPS
      bitrate
      language
      game { name }
    }
  }
}
"""


def default_filters():
    return {
        "preferredRatio": PREFERRED_RATIO,
        "gamingRatio": GAMING_RATIO,
        "gamingEnabled": True,
        "categories": {name: True for name in PREFERRED_CATEGORY_NAMES},
        "languages": {code: True for code in LANGUAGES},
    }


def _normalize_filters(data):
    base = default_filters()
    if not isinstance(data, dict):
        return base
    cats = dict(base["categories"])
    incoming = data.get("categories") or {}
    if isinstance(incoming, dict):
        for name in PREFERRED_CATEGORY_NAMES:
            if name in incoming:
                cats[name] = bool(incoming[name])
    langs = dict(base["languages"])
    incoming_l = data.get("languages") or {}
    if isinstance(incoming_l, dict):
        for code in LANGUAGES:
            if code in incoming_l:
                langs[code] = bool(incoming_l[code])
    try:
        pr = int(data.get("preferredRatio", base["preferredRatio"]))
    except (TypeError, ValueError):
        pr = base["preferredRatio"]
    try:
        gr = int(data.get("gamingRatio", base["gamingRatio"]))
    except (TypeError, ValueError):
        gr = base["gamingRatio"]
    return {
        "preferredRatio": max(0, min(20, pr)),
        "gamingRatio": max(0, min(20, gr)),
        "gamingEnabled": bool(data["gamingEnabled"])
        if "gamingEnabled" in data
        else True,
        "categories": cats,
        "languages": langs,
    }


FILTERS_LOCK = threading.Lock()
POOL_FILTERS = default_filters()


def load_filters():
    global POOL_FILTERS
    if not FILTERS_FILE.is_file():
        POOL_FILTERS = default_filters()
        return POOL_FILTERS
    try:
        raw = json.loads(FILTERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Filter-Datei unlesbar, Defaults: {exc}", flush=True)
        POOL_FILTERS = default_filters()
        return POOL_FILTERS
    with FILTERS_LOCK:
        POOL_FILTERS = _normalize_filters(raw)
        return dict(POOL_FILTERS)


def save_filters(filters):
    global POOL_FILTERS
    normalized = _normalize_filters(filters)
    FILTERS_FILE.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with FILTERS_LOCK:
        POOL_FILTERS = normalized
        snapshot = dict(POOL_FILTERS)
    return snapshot


def get_filters():
    with FILTERS_LOCK:
        return json.loads(json.dumps(POOL_FILTERS))


def reset_filters():
    snapshot = save_filters(default_filters())
    CATALOG.invalidate()
    threading.Thread(target=lambda: CATALOG.refresh(force=True), daemon=True).start()
    print("Pool-Filter auf Standard zurückgesetzt", flush=True)
    return snapshot


def apply_filters(data):
    snapshot = save_filters(data)
    CATALOG.invalidate()
    threading.Thread(target=lambda: CATALOG.refresh(force=True), daemon=True).start()
    print("Pool-Filter gespeichert → pool_filters.json", flush=True)
    return snapshot


def enabled_languages():
    filters = get_filters()
    return [code for code in LANGUAGES if filters["languages"].get(code, True)]


def enabled_category_names():
    filters = get_filters()
    return [name for name in PREFERRED_CATEGORY_NAMES if filters["categories"].get(name, True)]


def enabled_preferred_folds():
    folds = set()
    enabled = set(enabled_category_names())
    for name in enabled:
        folds.add(name.casefold())
        for alias in CATEGORY_ALIASES.get(name, []):
            folds.add(alias.casefold())
    return folds


def is_preferred_family(game):
    return (game or "").strip().casefold() in PREFERRED_CATEGORIES


def is_preferred_category(game):
    """True = IRL/Preferred-Bucket laut aktuell aktivierten Kategorien."""
    return (game or "").strip().casefold() in enabled_preferred_folds()


def language_allowed(language):
    enabled = {code.casefold() for code in enabled_languages()}
    if not enabled:
        return False
    lang = (language or "").strip().casefold()
    if not lang:
        return True
    if lang in enabled:
        return True
    # Twitch manchmal "en", Filter "EN"
    return lang.upper() in {c.upper() for c in enabled_languages()}


def record_allowed(record):
    if not record:
        return False
    if not language_allowed(record.get("language")):
        return False
    game = record.get("game") or ""
    filters = get_filters()
    if is_preferred_family(game):
        return is_preferred_category(game)
    return bool(filters.get("gamingEnabled", True))

PLAYBACK_QUERY = """
query ($login: String!) {
  streamPlaybackAccessToken(
    channelName: $login
    params: {platform: "web", playerBackend: "mediaplayer", playerType: "site"}
  ) {
    value
    signature
  }
}
"""


class Catalog:
    def __init__(self):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.pool = {}
        self.fetched_at = 0.0
        self._refreshing = False
        self._deck = []

    def invalidate(self):
        with self._lock:
            self.fetched_at = 0.0
            self._deck = []

    def _is_fresh(self):
        preferred = sum(1 for record in self.pool.values() if record.get("preferred"))
        gaming = len(self.pool) - preferred
        filters = get_filters()
        want_pref = bool(enabled_category_names())
        want_game = bool(filters.get("gamingEnabled", True))
        ttl_ok = (time.time() - self.fetched_at) < POOL_TTL_SECONDS
        if not ttl_ok or not self.pool:
            return False
        if want_pref and want_game:
            return preferred >= 2 and gaming >= 1
        if want_pref:
            return preferred >= 2
        if want_game:
            return gaming >= 2
        return False

    def refresh(self, force=False):
        with self._cv:
            while True:
                if self._is_fresh() and not force:
                    return len(self.pool)
                if not self._refreshing:
                    self._refreshing = True
                    break
                self._cv.wait()
                force = False

        fetched = None
        try:
            fetched = fetch_directory_pool()
        except Exception as exc:
            print(f"Pool-Aktualisierung fehlgeschlagen: {exc}", flush=True)
        finally:
            with self._cv:
                if fetched is not None:
                    self.pool = fetched
                    self.fetched_at = time.time()
                    self._deck = []
                self._refreshing = False
                self._cv.notify_all()
        return len(self.pool)

    def _next_wants_preferred(self):
        filters = get_filters()
        want_pref = bool(enabled_category_names())
        want_game = bool(filters.get("gamingEnabled", True))
        pr = int(filters.get("preferredRatio") or PREFERRED_RATIO)
        gr = int(filters.get("gamingRatio") or GAMING_RATIO)
        if not self._deck:
            if want_pref and want_game:
                self._deck = [True] * max(1, pr) + [False] * max(1, gr)
            elif want_pref:
                self._deck = [True] * max(1, pr or 1)
            elif want_game:
                self._deck = [False] * max(1, gr or 1)
            else:
                self._deck = [True]
            random.shuffle(self._deck)
        return self._deck.pop()

    def pick(self, exclude=None):
        if isinstance(exclude, (set, list, tuple)):
            exclude_keys = {str(e).strip().lower() for e in exclude if e}
        else:
            key = (exclude or "").strip().lower()
            exclude_keys = {key} if key else set()
        self.refresh(force=False)
        filters = get_filters()
        want_pref = bool(enabled_category_names())
        want_game = bool(filters.get("gamingEnabled", True))
        if not want_pref and not want_game:
            return None
        wants_preferred = self._next_wants_preferred()
        if want_pref and not want_game:
            order = [True]
        elif want_game and not want_pref:
            order = [False]
        else:
            order = [wants_preferred, not wants_preferred]
        for want_preferred in order:
            for round_index in range(2):
                if round_index == 1:
                    self.refresh(force=True)
                winner = self._pick_from_bucket(exclude_keys, want_preferred)
                if winner:
                    kind = "IRL" if winner.get("preferred") else "Gaming"
                    print(
                        f"Auswahl {kind}: {winner['login']} / {winner.get('game') or 'ohne Kategorie'}",
                        flush=True,
                    )
                    return winner
        return None

    def _pick_from_bucket(self, exclude_keys, want_preferred):
        tried = set()
        for _ in range(4):
            with self._lock:
                choices = [
                    record
                    for key, record in self.pool.items()
                    if key not in exclude_keys
                    and key not in tried
                    and bool(record.get("preferred")) == want_preferred
                ]
            if not choices:
                return None
            random.shuffle(choices)
            batch = choices[:6]
            for record in batch:
                tried.add(record["login"].lower())
            checked = verify_logins([record["login"] for record in batch])
            winner = None
            with self._lock:
                for record in batch:
                    key = record["login"].lower()
                    status = checked.get(key)
                    if status == "error":
                        continue
                    if not status or status["login"].lower() in exclude_keys:
                        self.pool.pop(key, None)
                        continue
                    if not record_allowed(status):
                        self.pool.pop(key, None)
                        continue
                    if bool(status.get("preferred")) != want_preferred:
                        stored = status["login"].lower()
                        self.pool.pop(key, None)
                        self.pool[stored] = status
                        continue
                    stored = status["login"].lower()
                    self.pool.pop(key, None)
                    self.pool[stored] = status
                    if winner is None:
                        winner = status
            if winner:
                return winner
        return None


CATALOG = Catalog()
load_filters()
PREFERRED_IP = None
SSL_CONTEXT = ssl.create_default_context()
CURRENT_LOCK = threading.Lock()
CURRENT_STREAM = None
SHOT_LOCK = threading.Lock()
SHOTS_ENABLED = True
# Offener Stream-Block im Streifen: Titelbalken kommt erst beim Wechsel.
STRIP_PENDING = None
STRIP_PENDING_LOCK = threading.Lock()
# Vorab gewählte 0-Viewer-Kandidaten — /api/random ohne Preflight-Wartezeit.
PREFETCH_LOCK = threading.Lock()
PREFETCH_QUEUE = []
PREFETCH_TARGET = 3


def _env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in ("1", "true", "yes", "on", "an")


# Live-Druck: nur das jeweils neue Frame-/Balken-JPEG an CUPS (nicht den ganzen Streifen).
# PRINT_LIVE=0|1 (Standard: an), PRINTER=Campusprint, optional LP_OPTIONS (z. B. "media=A4 CNDuplex=None").
PRINT_LOCK = threading.Lock()
PRINT_LIVE_ENABLED = _env_bool("PRINT_LIVE", True)
PRINTER_NAME = (os.environ.get("PRINTER") or "Campusprint").strip() or "Campusprint"
LP_OPTIONS = [
    part
    for part in re.split(r"[\s,]+", (os.environ.get("LP_OPTIONS") or "media=A4,CNDuplex=None,fit-to-page").strip())
    if part
]
LAST_PRINT = {"jobId": None, "path": None, "at": None, "ok": None, "error": None}


def _ips_from_getaddrinfo():
    ips = []
    try:
        infos = socket.getaddrinfo("gql.twitch.tv", 443, type=socket.SOCK_STREAM)
    except OSError:
        return ips
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


_DIG_CACHE = {"ips": [], "at": 0.0}
_DIG_CACHE_TTL = 60.0


def _ips_from_dig():
    """dig liefert oft gesündere Fastly-Edges als getaddrinfo (sticky tote Anycast-IPs)."""
    now = time.time()
    if _DIG_CACHE["ips"] and (now - _DIG_CACHE["at"]) < _DIG_CACHE_TTL:
        return list(_DIG_CACHE["ips"])
    ips = []
    try:
        out = subprocess.check_output(
            ["dig", "+short", "A", "gql.twitch.tv"],
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return list(_DIG_CACHE["ips"]) if _DIG_CACHE["ips"] else ips
    for line in out.splitlines():
        line = line.strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", line) and line not in ips:
            ips.append(line)
    random.shuffle(ips)
    if ips:
        _DIG_CACHE["ips"] = list(ips)
        _DIG_CACHE["at"] = now
    return ips


def twitch_ips():
    ips = []
    if PREFERRED_IP:
        ips.append(PREFERRED_IP)
    # dig zuerst (gecacht): getaddrinfo liefert hier oft nur einen toten Edge.
    for ip in _ips_from_dig() + _ips_from_getaddrinfo():
        if ip not in ips:
            ips.append(ip)
    return ips


def post_gql(body):
    """POST an Twitch-GQL. Tote AnyCast-Adressen werden schnell übersprungen."""
    global PREFERRED_IP
    payload = json.dumps(body).encode("utf-8")
    errors = []
    targets = twitch_ips()
    if not targets:
        raise RuntimeError("Twitch DNS leer (keine IPs für gql.twitch.tv)")
    for ip in targets:
        raw = None
        try:
            raw = socket.create_connection((ip, 443), timeout=1.5)
            raw.settimeout(12)
            secure = SSL_CONTEXT.wrap_socket(raw, server_hostname="gql.twitch.tv")
            connection = http.client.HTTPSConnection("gql.twitch.tv", 443, timeout=20)
            connection.sock = secure
            connection.request(
                "POST",
                "/gql",
                body=payload,
                headers={
                    "Client-ID": CLIENT_ID,
                    "Content-Type": "application/json",
                    "User-Agent": "twitch-null/1.0",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            raw_body = response.read()
            status = response.status
            connection.close()
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            PREFERRED_IP = ip
            return json.loads(raw_body)
        except Exception as exc:
            errors.append(f"{ip}: {exc}")
            if PREFERRED_IP == ip:
                PREFERRED_IP = None
            try:
                if raw:
                    raw.close()
            except Exception:
                pass
    raise RuntimeError("Twitch nicht erreichbar (" + "; ".join(errors) + ")")


def gql(operations):
    results = []
    for start in range(0, len(operations), 8):
        payload = post_gql(operations[start : start + 8])
        if isinstance(payload, dict):
            results.append(payload)
        else:
            results.extend(payload)
    return results


def usable_stream(
    login, display_name, title, game, viewer_count, stream_type, fps, bitrate, language=None
):
    login = (login or "").strip()
    if not login or login.lower().startswith("ingest"):
        return None
    if viewer_count != 0:
        return None
    if stream_type is not None and stream_type != "live":
        return None
    if not isinstance(fps, (int, float)) or fps <= 0:
        return None
    if not isinstance(bitrate, (int, float)) or bitrate <= 0:
        return None
    game_name = game or ""
    lang = (language or "").strip().upper() or None
    record = {
        "login": login,
        "displayName": (display_name or login).strip() or login,
        "title": (title or "").replace("\n", " ").strip(),
        "game": game_name,
        "viewerCount": 0,
        "language": lang,
        "preferred": is_preferred_category(game_name),
    }
    if not record_allowed(record):
        return None
    return record


def normalize_directory_node(node):
    if not node:
        return None
    broadcaster = node.get("broadcaster") or {}
    game = node.get("game") or {}
    return usable_stream(
        broadcaster.get("login"),
        broadcaster.get("displayName"),
        node.get("title"),
        game.get("name"),
        node.get("viewersCount"),
        node.get("type"),
        node.get("averageFPS"),
        node.get("bitrate"),
        node.get("language"),
    )


def add_edges(pool, edges):
    for edge in edges or []:
        record = normalize_directory_node(edge.get("node") or {})
        if record:
            pool[record["login"].lower()] = record


def fetch_directory_pool():
    langs = enabled_languages()
    cats = enabled_category_names()
    filters = get_filters()
    operations = []
    # Directory nur wenn Gaming erlaubt (sonst nur Kategorie-Queries).
    if filters.get("gamingEnabled", True):
        operations.append({"query": DIRECTORY_QUERY})
    operations.extend(
        {"query": LANGUAGE_QUERY, "variables": {"lang": language}}
        for language in langs
    )
    operations.extend(
        {"query": CATEGORY_QUERY, "variables": {"name": name}} for name in cats
    )
    if not operations:
        print("Pool leer: keine Sprachen/Kategorien aktiv", flush=True)
        return {}
    payloads = gql(operations)
    pool = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        data = payload.get("data") or {}
        streams = data.get("streams") or {}
        add_edges(pool, streams.get("edges"))
        game = data.get("game") or {}
        add_edges(pool, ((game.get("streams") or {}).get("edges")))
    preferred = sum(1 for record in pool.values() if record.get("preferred"))
    print(
        f"Pool aktualisiert: {len(pool)} Live-Streams mit 0 Zuschauer*innen "
        f"({preferred} IRL, {len(pool) - preferred} Gaming; "
        f"{len(cats)} Kat., {len(langs)} Spr.)",
        flush=True,
    )
    return pool


def verify_logins(logins):
    if not logins:
        return {}
    operations = [
        {"query": VERIFY_QUERY, "variables": {"login": login}} for login in logins
    ]
    try:
        payloads = gql(operations)
    except (OSError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Prüfung fehlgeschlagen: {exc}", flush=True)
        return {login.lower(): "error" for login in logins}

    checked = {}
    for login, payload in zip(logins, payloads):
        key = login.lower()
        if not isinstance(payload, dict):
            checked[key] = "error"
            continue
        if payload.get("errors") and not (payload.get("data") or {}).get("user"):
            checked[key] = "error"
            continue
        user = (payload.get("data") or {}).get("user") or {}
        stream = user.get("stream")
        if not stream:
            checked[key] = None
            continue
        game = (stream.get("game") or {}).get("name")
        record = usable_stream(
            user.get("login") or login,
            user.get("displayName"),
            stream.get("title"),
            game,
            stream.get("viewersCount"),
            stream.get("type"),
            stream.get("averageFPS"),
            stream.get("bitrate"),
            stream.get("language"),
        )
        checked[key] = record
    return checked


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_root_file(self, name):
        """Serve a project file by absolute ROOT path (avoids cwd/404 Error response)."""
        try:
            root = ROOT.resolve()
            path = (root / name).resolve()
            if root not in path.parents and path != root:
                self.send_error(404, f"Datei fehlt: {name}")
                return
            if not path.is_file():
                self.send_error(404, f"Datei fehlt: {name}")
                return
            data = path.read_bytes()
            ctype = self.guess_type(str(path))
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            print(f"send_root_file({name}): {exc}", flush=True)
            self.send_error(500, "Interner Dateifehler")

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/random":
                self.handle_random()
                return
            if path == "/api/current":
                with CURRENT_LOCK:
                    record = dict(CURRENT_STREAM) if CURRENT_STREAM else None
                self.send_json(200, {"stream": record})
                return
            if path == "/api/screenshots":
                self.send_json(200, {"enabled": screenshots_enabled()})
                return
            if path == "/api/print-live":
                self.send_json(200, print_live_status())
                return
            if path == "/api/strip":
                self.send_json(200, strip_status())
                return
            if path == "/api/filters":
                filters = get_filters()
                self.send_json(
                    200,
                    {
                        "filters": filters,
                        "defaults": default_filters(),
                        "categoryNames": list(PREFERRED_CATEGORY_NAMES),
                        "languages": list(LANGUAGES),
                    },
                )
                return
            if path in ("/", "/live"):
                self.send_root_file("index.html")
                return
            if path in ("/controls", "/admin"):
                self.send_root_file("controls.html")
                return
            if path == "/print":
                self.send_root_file("print.html")
                return
            super().do_GET()
        except Exception as exc:
            print(f"GET-Fehler {self.path}: {exc}", flush=True)
            try:
                self.send_error(500, "Interner Serverfehler")
            except Exception:
                pass

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error": "Ungültige Anfrage."})
            return

        if path == "/api/current":
            # Live-Seite übernimmt sichtbaren Stream (nach Prefetch/Player-Swap).
            login = (data.get("login") or "").strip()
            if not login or data.get("viewerCount") not in (0, "0"):
                self.send_json(400, {"error": "Ungültiger Stream."})
                return
            record = {
                "login": login,
                "displayName": data.get("displayName") or login,
                "title": data.get("title") or "",
                "game": data.get("game"),
                "viewerCount": 0,
                "language": data.get("language"),
                "preferred": bool(data.get("preferred")),
            }
            remember_stream(record)
            self.send_json(200, {"stream": record})
            return

        if path == "/api/filters/reset":
            filters = reset_filters()
            self.send_json(
                200,
                {
                    "filters": filters,
                    "defaults": default_filters(),
                    "categoryNames": list(PREFERRED_CATEGORY_NAMES),
                    "languages": list(LANGUAGES),
                    "reset": True,
                },
            )
            return

        if path == "/api/filters":
            filters = apply_filters(data.get("filters") if "filters" in data else data)
            self.send_json(
                200,
                {
                    "filters": filters,
                    "defaults": default_filters(),
                    "categoryNames": list(PREFERRED_CATEGORY_NAMES),
                    "languages": list(LANGUAGES),
                },
            )
            return

        if path not in ("/api/screenshots", "/api/print-live"):
            self.send_json(404, {"error": "Nicht gefunden."})
            return
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            self.send_json(400, {"error": "enabled muss true oder false sein."})
            return
        if path == "/api/screenshots":
            set_screenshots_enabled(enabled)
            state = "an" if enabled else "aus"
            print(f"Screenshots {state}", flush=True)
            self.send_json(200, {"enabled": enabled})
            return
        set_print_live_enabled(enabled)
        state = "an" if enabled else "aus"
        print(f"Live-Druck {state} → {PRINTER_NAME}", flush=True)
        self.send_json(200, print_live_status())

    def handle_random(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            exclude = (params.get("exclude") or [""])[0]
            banned = set()
            if exclude.strip():
                banned.add(exclude.strip().lower())
            # Schnellweg: Prefetch-Queue (ohne HLS-Preflight — Client skippt bei Fail).
            record = take_prefetched(banned)
            if not record:
                record = CATALOG.pick(banned)
            if record and record.get("viewerCount") != 0:
                record = None
        except Exception as exc:
            print(f"API-Fehler: {exc}", flush=True)
            self.send_json(
                502,
                {"error": "Die Stream-Liste konnte nicht geladen werden."},
            )
            return
        if not record or record.get("viewerCount") != 0:
            self.send_json(
                404,
                {
                    "error": "Gerade ist kein anderer Livestream mit genau 0 Zuschauer*innen verfügbar."
                },
            )
            return
        prefetch_only = (params.get("prefetch") or ["0"])[0].strip().casefold() in (
            "1",
            "true",
            "yes",
            "on",
        )
        print(
            f"{'Prefetch' if prefetch_only else 'Ausgabe'}: {record['login']} "
            f"({record['displayName']}) viewerCount={record['viewerCount']}",
            flush=True,
        )
        # Prefetch darf CURRENT_STREAM nicht überschreiben (Screenshots = sichtbarer Stream).
        if not prefetch_only:
            remember_stream(record)
        # Nächste Kandidaten im Hintergrund nachfüllen.
        threading.Thread(target=fill_prefetch, daemon=True).start()
        self.send_json(200, record)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Cache-Control kommt einmal in end_headers().
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def take_prefetched(banned):
    """Nimmt einen vorgewärmten Kandidaten, der nicht in banned liegt."""
    banned_keys = {str(x).strip().lower() for x in (banned or set()) if x}
    with PREFETCH_LOCK:
        for i, rec in enumerate(PREFETCH_QUEUE):
            login = (rec.get("login") or "").lower()
            if login and login not in banned_keys and rec.get("viewerCount") == 0:
                return PREFETCH_QUEUE.pop(i)
    return None


def fill_prefetch():
    """Füllt die Prefetch-Queue nach (ohne HLS-Curl — nur Katalog-Pick)."""
    try:
        while True:
            with PREFETCH_LOCK:
                need = PREFETCH_TARGET - len(PREFETCH_QUEUE)
                known = {(r.get("login") or "").lower() for r in PREFETCH_QUEUE}
            if need <= 0:
                return
            with CURRENT_LOCK:
                if CURRENT_STREAM and CURRENT_STREAM.get("login"):
                    known.add(CURRENT_STREAM["login"].lower())
            candidate = CATALOG.pick(known)
            if not candidate or candidate.get("viewerCount") != 0:
                return
            login = (candidate.get("login") or "").lower()
            with PREFETCH_LOCK:
                if login and login not in {(r.get("login") or "").lower() for r in PREFETCH_QUEUE}:
                    if len(PREFETCH_QUEUE) < PREFETCH_TARGET:
                        PREFETCH_QUEUE.append(dict(candidate))
                        print(f"Prefetch: {candidate['login']}", flush=True)
            # Kurz Pause, damit force-refresh im Catalog nicht stampft.
            time.sleep(0.05)
    except Exception as exc:
        print(f"Prefetch-Fehler: {exc}", flush=True)


def prefetch_loop():
    while True:
        try:
            with PREFETCH_LOCK:
                need = PREFETCH_TARGET - len(PREFETCH_QUEUE)
            if need > 0:
                fill_prefetch()
        except Exception:
            pass
        time.sleep(1.5)


def remember_stream(record):
    global CURRENT_STREAM
    with CURRENT_LOCK:
        previous = dict(CURRENT_STREAM) if CURRENT_STREAM else None
        CURRENT_STREAM = dict(record)
    # Beim Streamwechsel Titelbalken für den bisherigen Block abschließen.
    new_login = (record or {}).get("login")
    old_login = (previous or {}).get("login") if previous else None
    if old_login and new_login and old_login != new_login:
        try:
            flush_strip_title_bar(reason=f"Wechsel {old_login} → {new_login}")
        except Exception as exc:
            print(f"Titelbalken-Flush fehlgeschlagen: {exc}", flush=True)


def screenshots_enabled():
    return SHOTS_ENABLED


def set_screenshots_enabled(enabled):
    global SHOTS_ENABLED
    SHOTS_ENABLED = bool(enabled)


def print_live_enabled():
    return PRINT_LIVE_ENABLED


def set_print_live_enabled(enabled):
    global PRINT_LIVE_ENABLED
    PRINT_LIVE_ENABLED = bool(enabled)


def print_live_status():
    with PRINT_LOCK:
        last = dict(LAST_PRINT)
    return {
        "enabled": print_live_enabled(),
        "printer": PRINTER_NAME,
        "options": list(LP_OPTIONS),
        "last": last,
    }


def print_frame(image_path, job_title=None):
    """Sendet nur dieses JPEG an CUPS (Campusprint o. Ä.) — nicht den Endlosstreifen."""
    global LAST_PRINT
    if not print_live_enabled():
        return None
    image_path = Path(image_path)
    if not image_path.is_file():
        print(f"Live-Druck übersprungen (Datei fehlt): {image_path}", flush=True)
        return None
    title = job_title or image_path.stem
    cmd = ["lp", "-d", PRINTER_NAME, "-t", title]
    for opt in LP_OPTIONS:
        cmd.extend(["-o", opt])
    cmd.append(str(image_path))
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        msg = "lp/CUPS nicht gefunden"
        print(f"Live-Druck fehlgeschlagen: {msg}", flush=True)
        with PRINT_LOCK:
            LAST_PRINT = {
                "jobId": None,
                "path": str(image_path),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ok": False,
                "error": msg,
            }
        return None
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"Live-Druck fehlgeschlagen: {err}", flush=True)
        with PRINT_LOCK:
            LAST_PRINT = {
                "jobId": None,
                "path": str(image_path),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ok": False,
                "error": err,
            }
        return None
    except Exception as exc:
        print(f"Live-Druck fehlgeschlagen: {exc}", flush=True)
        with PRINT_LOCK:
            LAST_PRINT = {
                "jobId": None,
                "path": str(image_path),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ok": False,
                "error": str(exc),
            }
        return None

    out = (result.stdout or "").strip()
    # EN: "request id is Campusprint-42 (1 file(s))"
    # DE: "Anfrage-ID ist Campusprint–42 (1 Datei(en))"
    match = re.search(
        r"(?:request id is|Anfrage-ID ist)\s+(\S+)",
        out,
        re.IGNORECASE,
    )
    job_id = match.group(1) if match else (out or None)
    with PRINT_LOCK:
        LAST_PRINT = {
            "jobId": job_id,
            "path": str(image_path),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": True,
            "error": None,
        }
    print(f"Live-Druck → {PRINTER_NAME}: {job_id} ({image_path.name})", flush=True)
    return job_id


def playback_url(login):
    payload = post_gql({"query": PLAYBACK_QUERY, "variables": {"login": login}})
    token = ((payload or {}).get("data") or {}).get("streamPlaybackAccessToken")
    if not token or not token.get("value") or not token.get("signature"):
        raise RuntimeError("Kein Wiedergabe-Token")
    params = urlencode(
        {
            "allow_source": "true",
            "fast_bread": "true",
            "p": str(random.randint(100000, 999999)),
            "player_backend": "mediaplayer",
            "playlist_include_framerate": "true",
            "reassignments_supported": "true",
            "sig": token["signature"],
            "supported_codecs": "avc1",
            "token": token["value"],
            "cdm": "wv",
            "player_version": "1.31.0",
        }
    )
    return f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?{params}"


def stream_playable(login, timeout=1.5):
    """Optionaler Preflight (nicht mehr im /api/random-Hotpath).
    Curl-Timeouts (Code 28) gelten als OK."""
    try:
        url = playback_url(login)
    except Exception as exc:
        print(f"Preflight Token-Fail {login}: {exc}", flush=True)
        return False
    try:
        result = subprocess.run(
            [
                "curl",
                "-fsSL",
                "--max-time",
                str(timeout),
                "-A",
                "Mozilla/5.0",
                "-o",
                "-",
                url,
            ],
            check=False,
            capture_output=True,
            timeout=timeout + 1,
        )
    except Exception as exc:
        print(f"Preflight Netz-Fail {login}: {exc} → Token ok, erlauben", flush=True)
        return True
    body = (result.stdout or b"") + (result.stderr or b"")
    text = body.decode("utf-8", errors="replace")
    low = text.casefold()
    if result.returncode != 0:
        # 28 = operation timed out — nicht als „unspielbar“ werten.
        if result.returncode == 28:
            print(f"Preflight HLS-Timeout {login} → erlauben", flush=True)
            return True
        print(f"Preflight HTTP-Fail {login}: code={result.returncode}", flush=True)
        return False
    if "#extm3u" not in low:
        print(f"Preflight kein M3U8 {login}", flush=True)
        return False
    blocked_markers = (
        "unauthorized",
        "forbidden",
        "not available in your region",
        "unavailable in your region",
        "geoblock",
        "restricted",
        "subscriber",
        "error code",
        '"error"',
    )
    for marker in blocked_markers:
        if marker in low:
            print(f"Preflight geblockt {login}: {marker}", flush=True)
            return False
    return True


def shorten(text, limit):
    text = " ".join((text or "").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].strip()
    if len(cut) < limit // 2:
        cut = text[: limit - 1].strip()
    return cut + "…"


def load_strip_meta():
    if not STRIP_META_FILE.is_file():
        return {"frames": 0, "updatedAt": None, "width": None, "height": None}
    try:
        return json.loads(STRIP_META_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"frames": 0, "updatedAt": None, "width": None, "height": None}


def save_strip_meta(meta):
    STRIP_META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def probe_image(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    width_s, height_s = result.stdout.strip().split("x")
    return int(width_s), int(height_s)



def sync_streifen_copy():
    """Spiegelt den Streifen nach Streifen/endless_strip.jpg (Desktop-Projekt)."""
    STREIFEN_DIR.mkdir(exist_ok=True)
    if not STRIP_FILE.is_file():
        return
    shutil.copy2(STRIP_FILE, STREIFEN_FILE)


def strip_status():
    meta = load_strip_meta()
    exists = STRIP_FILE.is_file()
    if exists:
        try:
            width, height = probe_image(STRIP_FILE)
            meta["width"] = width
            meta["height"] = height
            if not meta.get("updatedAt"):
                meta["updatedAt"] = datetime.fromtimestamp(
                    STRIP_FILE.stat().st_mtime
                ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return {
        "exists": exists,
        "url": "/Screenshots/endless_strip.jpg" if exists else None,
        "streifenUrl": "/Streifen/endless_strip.jpg" if exists else None,
        "path": str(STRIP_FILE) if exists else None,
        "streifenPath": str(STREIFEN_FILE) if exists else None,
        "frames": meta.get("frames") or (1 if exists else 0),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "updatedAt": meta.get("updatedAt"),
        "font": FONT_FILE,
    }


def _ffmpeg_copy_jpg(src, dest):
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-q:v",
            "3",
            str(dest),
        ],
        check=True,
        timeout=30,
    )


def _archive_full_strip(reason=""):
    """Sichert den vollen Streifen und entfernt die aktive Datei (Neustart folgt)."""
    if not STRIP_FILE.is_file():
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    archive = SCREENSHOT_DIR / f"endless_strip_full_{stamp}.jpg"
    shutil.copy2(STRIP_FILE, archive)
    streifen_archive = STREIFEN_DIR / archive.name
    try:
        STREIFEN_DIR.mkdir(exist_ok=True)
        shutil.copy2(STRIP_FILE, streifen_archive)
    except Exception:
        pass
    note = f" ({reason})" if reason else ""
    print(f"Streifen archiviert → {archive}{note}", flush=True)
    try:
        STRIP_FILE.unlink()
    except OSError:
        pass
    return archive


def append_to_strip(frame_path):
    """Hängt ein gerahmtes Capture unten an den Endlosstreifen (ohne große Lücken).

    JPEG-Limit ~65500px: wenn der Stapel das sprengen würde, wird der volle Streifen
    archiviert und mit dem neuen Frame ein neuer Segment-Streifen gestartet.
    """
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    STREIFEN_DIR.mkdir(exist_ok=True)
    frame_path = Path(frame_path)
    if not frame_path.is_file():
        raise FileNotFoundError(frame_path)

    if not STRIP_FILE.is_file():
        _ffmpeg_copy_jpg(frame_path, STRIP_FILE)
        width, height = probe_image(STRIP_FILE)
        save_strip_meta(
            {
                "frames": 1,
                "width": width,
                "height": height,
                "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        print(f"Streifen gestartet: {STRIP_FILE}", flush=True)
        sync_streifen_copy()
        return STRIP_FILE

    strip_w, strip_h = probe_image(STRIP_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        scaled = folder / "scaled.jpg"
        stacked = folder / "stacked.jpg"
        # Gleiche Breite, keine Zwischenräume — nahtlos vertikal gestapelt.
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(frame_path),
                "-vf",
                f"scale={strip_w}:-1",
                "-q:v",
                "3",
                str(scaled),
            ],
            check=True,
            timeout=30,
        )
        scaled_w, scaled_h = probe_image(scaled)
        if strip_h + scaled_h > JPEG_MAX_DIM:
            _archive_full_strip(
                reason=f"JPEG-Höhe {strip_h}+{scaled_h}>{JPEG_MAX_DIM}"
            )
            _ffmpeg_copy_jpg(scaled, STRIP_FILE)
            width, height = probe_image(STRIP_FILE)
            save_strip_meta(
                {
                    "frames": 1,
                    "width": width,
                    "height": height,
                    "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "rotatedFromHeight": strip_h,
                }
            )
            print(
                f"Streifen neu gestartet nach Archiv (Frame {scaled_w}x{scaled_h})",
                flush=True,
            )
            sync_streifen_copy()
            return STRIP_FILE

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(STRIP_FILE),
                "-i",
                str(scaled),
                "-filter_complex",
                "vstack=inputs=2",
                "-q:v",
                "3",
                str(stacked),
            ],
            check=True,
            timeout=60,
        )
        # shutil.move: tempfile und Screenshots können auf anderen Devices liegen.
        shutil.move(str(stacked), str(STRIP_FILE))

    width, height = probe_image(STRIP_FILE)
    meta = load_strip_meta()
    frames = int(meta.get("frames") or 1) + 1
    save_strip_meta(
        {
            "frames": frames,
            "width": width,
            "height": height,
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    print(f"Streifen +1 → {frames} Frames: {STRIP_FILE}", flush=True)
    sync_streifen_copy()
    return STRIP_FILE


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "Pillow fehlt. Einmalig: python3 -m venv .venv && "
            ".venv/bin/pip install pillow"
        ) from exc
    return Image, ImageDraw, ImageFont


def _text_size(font, text):
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _vertical_label(text, font, bg, rotate_ccw=True):
    Image, ImageDraw, _ImageFont = _pil()
    tw, th = _text_size(font, text)
    pad = 4
    strip = Image.new("RGB", (max(1, tw + pad * 2), max(1, th + pad * 2)), bg)
    ImageDraw.Draw(strip).text((pad, pad), text, font=font, fill=(0, 0, 0))
    # Links: CCW 90° (unten→oben). Rechts: CW 90° (oben→unten).
    return strip.rotate(90 if rotate_ccw else -90, expand=True, fillcolor=bg)


def _title_lines(title):
    Image, ImageDraw, ImageFont = _pil()
    font_file = resolve_font_file()
    font_title = ImageFont.truetype(font_file, STRIP_TITLE_SIZE)
    title = (title or "").replace("\n", " ").strip()
    avg = _text_size(font_title, "M")[0] or 14
    max_title_w = STRIP_INNER_WIDTH + 2 * STRIP_SIDE_MARGIN - 2 * STRIP_BAR_PAD
    wrap_chars = max(12, max_title_w // avg)
    lines = textwrap.wrap(title, width=wrap_chars) if title else [""]
    line_h = max((_text_size(font_title, ln)[1] for ln in lines), default=STRIP_TITLE_SIZE) + 8
    bar_h = max(56, STRIP_BAR_PAD * 2 + line_h * len(lines))
    return font_title, lines, line_h, bar_h


def compose_title_bar(title, out_path, width=None):
    """Warmgrauer Titelbalken — schließt einen Stream-Block ab."""
    Image, ImageDraw, _ImageFont = _pil()
    font_title, lines, line_h, bar_h = _title_lines(title)
    canvas_w = int(width) if width else (STRIP_INNER_WIDTH + 2 * STRIP_SIDE_MARGIN)
    if STRIP_FILE.is_file():
        try:
            canvas_w = probe_image(STRIP_FILE)[0]
        except Exception:
            pass
    canvas = Image.new("RGB", (canvas_w, bar_h), STRIP_BAR_GRAY)
    draw = ImageDraw.Draw(canvas)
    y = (bar_h - line_h * len(lines)) // 2
    for ln in lines:
        tw, _th = _text_size(font_title, ln)
        draw.text(((canvas_w - tw) // 2, y), ln, font=font_title, fill=(0, 0, 0))
        y += line_h
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, quality=92)
    return out_path


def compose_vorlage_frame(raw_path, username, title, timestamp, out_path, with_title_bar=False):
    """Frame wie VORLAGE_DRUCK: Bild + seitliche Metas; Titelbalken optional (meist erst am Blockende)."""
    Image, ImageDraw, ImageFont = _pil()
    font_file = resolve_font_file()
    font_meta = ImageFont.truetype(font_file, STRIP_META_SIZE)

    img = Image.open(raw_path).convert("RGB")
    ratio = STRIP_INNER_WIDTH / img.width
    img = img.resize(
        (STRIP_INNER_WIDTH, max(1, int(img.height * ratio))),
        Image.Resampling.LANCZOS,
    )

    username = (username or "unknown").replace("\n", " ").strip() or "unknown"
    timestamp = (timestamp or "").replace("\n", " ").strip()

    canvas_w = STRIP_INNER_WIDTH + 2 * STRIP_SIDE_MARGIN
    bar_h = 0
    lines = []
    line_h = STRIP_TITLE_SIZE
    font_title = None
    if with_title_bar:
        font_title, lines, line_h, bar_h = _title_lines(title)

    canvas_h = img.height + bar_h
    canvas = Image.new("RGB", (canvas_w, canvas_h), STRIP_BG)
    canvas.paste(img, (STRIP_SIDE_MARGIN, 0))

    if with_title_bar and font_title is not None:
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([0, img.height, canvas_w, canvas_h], fill=STRIP_BAR_GRAY)
        y = img.height + (bar_h - line_h * len(lines)) // 2
        for ln in lines:
            tw, _th = _text_size(font_title, ln)
            draw.text(((canvas_w - tw) // 2, y), ln, font=font_title, fill=(0, 0, 0))
            y += line_h

    left = _vertical_label(username, font_meta, STRIP_BG, rotate_ccw=True)
    right = _vertical_label(timestamp, font_meta, STRIP_BG, rotate_ccw=False)
    lx = max(0, (STRIP_SIDE_MARGIN - left.width) // 2)
    ly = max(0, (img.height - left.height) // 2)
    rx = STRIP_SIDE_MARGIN + STRIP_INNER_WIDTH + max(
        0, (STRIP_SIDE_MARGIN - right.width) // 2
    )
    ry = max(0, (img.height - right.height) // 2)
    canvas.paste(left, (lx, ly))
    canvas.paste(right, (min(rx, canvas_w - right.width), ry))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, quality=92)
    return out_path


def _pending_from_meta():
    meta = load_strip_meta()
    pending = meta.get("pending")
    if isinstance(pending, dict) and pending.get("login"):
        return pending
    return None


def _store_pending(pending):
    global STRIP_PENDING
    meta = load_strip_meta()
    if pending:
        meta["pending"] = {
            "login": pending.get("login"),
            "title": pending.get("title") or "",
            "username": pending.get("username") or pending.get("login") or "",
        }
    else:
        meta.pop("pending", None)
    save_strip_meta(meta)
    with STRIP_PENDING_LOCK:
        STRIP_PENDING = dict(meta["pending"]) if pending else None


def flush_strip_title_bar(reason=""):
    """Hängt den Titelbalken für den offenen Stream-Block an (einmal pro Wechsel)."""
    with STRIP_PENDING_LOCK:
        pending = dict(STRIP_PENDING) if STRIP_PENDING else _pending_from_meta()
    if not pending or not pending.get("login"):
        return None
    title = (pending.get("title") or "").strip()
    if not title:
        # Auch ohne Titel den Block schließen (leerer/kurzer Balken mit Login).
        title = pending.get("username") or pending.get("login") or ""
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", pending["login"]).strip("_") or "stream"
    bar_path = SCREENSHOT_DIR / f"{stamp}_{safe}_titlebar.jpg"
    compose_title_bar(title, bar_path)
    append_to_strip(bar_path)
    try:
        print_frame(bar_path, job_title=f"titlebar-{pending.get('login')}")
    except Exception as exc:
        print(f"Live-Druck Titelbalken fehlgeschlagen: {exc}", flush=True)
    _store_pending(None)
    note = f" ({reason})" if reason else ""
    print(f"Titelbalken gesetzt für {pending.get('login')}{note}", flush=True)
    return bar_path


def save_screenshot(record):
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    STREIFEN_DIR.mkdir(exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    safe_login = re.sub(r"[^A-Za-z0-9_-]+", "_", record["login"]).strip("_") or "stream"
    target = SCREENSHOT_DIR / f"{stamp}_{safe_login}.jpg"
    username = (record.get("login") or safe_login).replace("\n", " ").strip()
    title = shorten(record.get("title") or "", 120)
    timestamp = now.strftime("%d.%m.%y %H:%M")

    # Falls Stream gewechselt hat, ohne dass remember_stream geflusht hat.
    with STRIP_PENDING_LOCK:
        pending = dict(STRIP_PENDING) if STRIP_PENDING else _pending_from_meta()
    if pending and pending.get("login") and pending["login"] != record["login"]:
        try:
            flush_strip_title_bar(reason="neuer Capture-Stream")
        except Exception as exc:
            print(f"Titelbalken-Flush fehlgeschlagen: {exc}", flush=True)

    playlist = playback_url(record["login"])
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-user_agent",
                "Mozilla/5.0",
                "-rw_timeout",
                "15000000",
                "-i",
                playlist,
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(raw),
            ],
            check=True,
            timeout=40,
        )
        # Während desselben Streams: kein Titelbalken — der kommt erst beim Wechsel.
        compose_vorlage_frame(
            raw, username, title, timestamp, target, with_title_bar=False
        )
    print(f"Screenshot: {target}", flush=True)
    try:
        append_to_strip(target)
        _store_pending(
            {"login": record["login"], "title": title, "username": username}
        )
        try:
            print_frame(target, job_title=f"frame-{safe_login}")
        except Exception as exc:
            print(f"Live-Druck Frame fehlgeschlagen: {exc}", flush=True)
    except Exception as exc:
        print(f"Streifen-Anhang fehlgeschlagen: {exc}", flush=True)
    return target



def screenshot_loop():
    while True:
        time.sleep(SCREENSHOT_INTERVAL)
        try:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            STREIFEN_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Screenshot-Ordner: {exc}", flush=True)
            continue
        if not screenshots_enabled():
            continue
        with CURRENT_LOCK:
            record = dict(CURRENT_STREAM) if CURRENT_STREAM else None
        if not record or not screenshots_enabled():
            continue
        if not SHOT_LOCK.acquire(blocking=False):
            continue
        try:
            save_screenshot(record)
        except Exception as exc:
            print(f"Screenshot fehlgeschlagen: {exc}", flush=True)
        finally:
            SHOT_LOCK.release()


def main():
    global STRIP_PENDING
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    STREIFEN_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    if STRIP_FILE.is_file():
        sync_streifen_copy()
    STRIP_PENDING = _pending_from_meta()
    if STRIP_PENDING:
        print(
            f"Offener Streifen-Block: {STRIP_PENDING.get('login')} "
            "(Titelbalken folgt beim nächsten Streamwechsel)",
            flush=True,
        )
    try:
        font = resolve_font_file()
        print(f"Schrift: {font}", flush=True)
    except FileNotFoundError as exc:
        print(f"Warnung: {exc}", flush=True)
    threading.Thread(target=lambda: CATALOG.refresh(force=True), daemon=True).start()
    threading.Thread(target=screenshot_loop, daemon=True).start()
    threading.Thread(target=prefetch_loop, daemon=True).start()
    # Dual-Stack: Browser „localhost“ oft via ::1 — sonst Connection refused.
    class _DualStackServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    server = _DualStackServer(("::", PORT), Handler)
    print(f"Live:      http://localhost:{PORT}/  (oder /live) — Enter = nächster Stream", flush=True)
    print(f"Bedienung: http://localhost:{PORT}/controls  (oder /admin)", flush=True)
    print(f"Streifen:  http://localhost:{PORT}/print", flush=True)
    print(f"Streifen-Ordner: {STREIFEN_DIR}", flush=True)
    live = "an" if print_live_enabled() else "aus"
    print(
        f"Live-Druck {live} → Drucker «{PRINTER_NAME}» "
        f"(PRINT_LIVE / PRINTER / /api/print-live)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
