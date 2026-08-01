"""
Sports teams data source for the dashboard's Teams tab.

Pulls live team info from ESPN's public (unofficial, free, no-auth) API:
  • teams/{abbr}                     — record + division standing summary
  • teams/{abbr}/schedule            — full season; we slice prev-3 / next-3
  • standings?level=3                — division table + model playoff %
  • seasons/{yr}/futures             — WS / league / division betting odds
  • news                             — recent stories (filtered to the team)

Config-driven via TEAMS so adding another team is just a dict entry.
Betting futures come from ESPN's odds feed (ESPN BET preferred, else first
book that prices the team). "Make playoffs" has no betting market in the feed,
so we surface ESPN's model-based playoff probability (a %, not a moneyline).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# Ticketing sites' bot walls are picky: a bare "Mozilla/5.0" and, oddly, extra
# Sec-Fetch/Accept headers both draw a 406 from Songkick, while a plain realistic
# UA with NO extra headers passes (verified from the VPS). So: complete UA, and
# send *only* the User-Agent header — matching a working curl request.
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Times are shown in US Eastern — matches how MLB schedules are published.
_DISPLAY_TZ = ZoneInfo("America/New_York")
_HTTP_TIMEOUT = 15

# Which season's futures to read. Kept overridable so a rollover doesn't need
# a code change mid-crisis, but defaults to the current calendar year.
_SEASON = datetime.now(timezone.utc).year


TEAMS = [
    {
        "key": "texas_rangers",
        "name": "Texas Rangers",
        "league": "MLB",
        "sport_path": "baseball/mlb",   # ESPN URL segment
        "espn_abbr": "tex",
        "espn_id": "13",
        "division_name": "American League West",
        "division_abbrs": ["TEX", "HOU", "SEA", "ATH", "LAA"],  # for standings-by-date reconstruction
        "attendance_espn_name": "Texas",   # how the team appears in ESPN's attendance table
        # Avg home attendance/game for completed seasons (immutable; source: ESPN).
        # Seeded so historical bars never depend on scraping www.espn.com (bot-gated).
        # 2020 omitted — COVID season had no regular-season fans.
        "attendance_history": {
            "2016": 33461, "2017": 30960, "2018": 26013, "2019": 26333,
            "2021": 26052, "2022": 24831, "2023": 31272, "2024": 32735, "2025": 29593,
        },
        # Non-team events at the home venue, parsed from ticketing sites' JSON-LD.
        # Multiple redundant sources: any datacenter IP block (Songkick 403s the
        # VPS) is covered by another source, so all events still populate.
        "venue_events": {
            "venue_name": "Globe Life Field",
            # The venue's own events page is the fullest source (all event types,
            # far-future); it's parsed with _parse_venue_page_events, not JSON-LD.
            "venue_page": "https://globelifefield.com/stadium-events/",
            # JSON-LD sources supply precise times / ticket links for near-term
            # concerts (they win dedup over the venue page's date-only entries).
            "sources": [
                "https://www.ticketmaster.com/globe-life-field-tickets-arlington/venue/99338",
                "https://www.songkick.com/venues/4349753-globe-life-field",
            ],
        },
        # ESPN futures market names for THIS team's league/division.
        "pennant_future": "MLB - American League - Winner",
        "pennant_label": "Win American League",
        "division_future": "MLB - American League West",
        "worldseries_future": "MLB  - World Series - Winner",  # note: ESPN has 2 spaces
    },
]


def _get(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": "venue-reception/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"teams: GET failed {url}: {e}")
        return None


def _get_html(url: str) -> Optional[str]:
    # Only User-Agent — adding Accept/Sec-Fetch headers makes Songkick 406.
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": _BROWSER_UA})
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"teams: HTML GET failed {url}: {e}")
        return None


def _jsonld_events(html: str) -> list[dict]:
    """Extract schema.org Event entries from a page's JSON-LD blocks."""
    if not html:
        return []
    out = []
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except Exception:
            continue
        for it in (data if isinstance(data, list) else [data]):
            if not isinstance(it, dict) or "Event" not in str(it.get("@type", "")):
                continue
            start = it.get("startDate") or ""
            name = (it.get("name") or "").strip()
            if not start or not name:
                continue
            url = it.get("url") or ""
            if isinstance(url, dict):
                url = url.get("@id") or url.get("url") or ""
            out.append({"name": name, "start": start, "url": url})
    return out


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_VENUE_DATE_RE = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(20\d\d)")


def _parse_venue_page_events(html: str, base_url: str) -> list[dict]:
    """Parse Globe Life Field's stadium-events listing (server-rendered HTML).

    Each event is a 'Mon D, YYYY' date followed by its title; multi-day events
    repeat the title on start/end dates (deduped, earliest kept). This is the
    fullest source — non-concert events and ones beyond ticketing-page windows.
    """
    if not html:
        return []
    lines = [re.sub(r"\s+", " ", l).strip() for l in unescape(re.sub(r"<[^>]+>", "\n", html)).split("\n")]
    lines = [l for l in lines if l]
    skip = {"-", "event details:", "texas rangers ticket information"}
    out, seen = [], set()
    for i, line in enumerate(lines):
        m = _VENUE_DATE_RE.match(line)
        if not m:
            continue
        name = next((c for c in lines[i + 1:i + 6]
                     if not _VENUE_DATE_RE.match(c) and c.lower() not in skip and len(c) > 2), None)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())   # dedup multi-day events by title (page is chronological)
        date_iso = f"{int(m.group(3))}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
        out.append({"name": name, "start": f"{date_iso}T00:00:00", "url": base_url})
    return out


def _clean_event_name(name: str) -> str:
    """Trim venue/location/date suffixes some aggregators append to event names."""
    name = re.sub(r"\s+", " ", name).strip()
    # e.g. "Guns N' Roses @ Globe Life Field" / "Noah Kahan - Arlington - Globe Life Field - Jul 30, 2026"
    name = re.split(r"\s+[-–—@]\s+(?:Arlington|Globe Life Field)\b", name, maxsplit=1)[0]
    return name.strip(" -–—@")


_TM_API = "https://app.ticketmaster.com/discovery/v2"


def _tm_api_venue_id(api_key: str, name: str) -> Optional[str]:
    """Resolve a venue's Ticketmaster Discovery id from its name."""
    d = _get(f"{_TM_API}/venues.json?keyword={quote(name)}&countryCode=US&size=10&apikey={api_key}")
    venues = ((d or {}).get("_embedded") or {}).get("venues") or []
    for v in venues:   # prefer an exact-name match
        if (v.get("name") or "").lower() == name.lower():
            return v.get("id")
    return venues[0].get("id") if venues else None


def _tm_api_events(api_key: str, venue_id: str) -> list[dict]:
    """Full upcoming event list for a venue via the Discovery API (JSON, no bot wall)."""
    d = _get(f"{_TM_API}/events.json?venueId={venue_id}&sort=date,asc&size=50&apikey={api_key}")
    out = []
    for e in ((d or {}).get("_embedded") or {}).get("events") or []:
        start = (e.get("dates") or {}).get("start") or {}
        date_iso = start.get("localDate")
        if not date_iso:
            continue
        out.append({
            "name": (e.get("name") or "").strip(),
            "start": f"{date_iso}T{start.get('localTime') or '00:00:00'}",
            "url": e.get("url") or "",
        })
    return out


def _fetch_venue_events(cfg: dict, team_name: str, limit: int = 4) -> list[dict]:
    """Upcoming NON-team events at the home venue. Prefers the Ticketmaster
    Discovery API when TICKETMASTER_API_KEY is set (a real JSON API — returns the
    full future calendar, no bot wall); otherwise scrapes ticketing-site JSON-LD.
    Scraped sources are kept as a fallback/supplement. The team's own home games
    are removed.
    """
    vcfg = cfg.get("venue_events") or {}
    raw = []

    api_key = os.getenv("TICKETMASTER_API_KEY", "").strip()
    if api_key:
        vid = vcfg.get("tm_api_venue_id") or _tm_api_venue_id(api_key, vcfg.get("venue_name", ""))
        if vid:
            raw += _tm_api_events(api_key, vid)
        else:
            logger.warning("teams: Ticketmaster API key set but venue id not resolved")

    # JSON-LD ticketing sources first — their times/URLs win dedup for concerts.
    sources = vcfg.get("sources") or [u for u in (vcfg.get("ticketmaster_url"), vcfg.get("songkick_url")) if u]
    for url in sources:
        raw += _jsonld_events(_get_html(url))

    # The venue's own events page — fullest coverage (all event types, far-future).
    if vcfg.get("venue_page"):
        raw += _parse_venue_page_events(_get_html(vcfg["venue_page"]), vcfg["venue_page"])

    # Optional researched seed events, if any configured (deduped below).
    for se in vcfg.get("seed_events") or []:
        raw.append({
            "name": se["name"],
            "start": f'{se["date"]}T{se.get("time", "00:00:00")}',
            "url": se.get("url", ""),
        })

    short = team_name.split()[-1].lower()  # 'rangers'
    today = datetime.now(timezone.utc).date().isoformat()

    def is_home_game(name: str) -> bool:
        n = name.lower()
        # Exclude actual team games ("<Team> vs ..." / "... at <Team>"), keep
        # non-game happenings even if team-branded (5Ks, etc.).
        return short in n and (" vs" in n or " at " in n or " v." in n)

    seen, events = set(), []
    for e in raw:
        date_iso = e["start"][:10]
        if date_iso < today or is_home_game(e["name"]):
            continue
        # Dedup the same event across sites: date + first two words of the name.
        norm = " ".join(re.sub(r"[^a-z0-9 ]", "", e["name"].lower()).split()[:2])
        key = (date_iso, norm)
        if key in seen:
            continue
        seen.add(key)
        try:
            dt = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
            when = dt.strftime("%a, %b %-d")
            time_txt = dt.strftime("%-I:%M %p") if (dt.hour or dt.minute) else ""
        except Exception:
            when, time_txt = date_iso, ""
        events.append({
            "date_iso": date_iso,
            "date": when,
            "time": time_txt,
            "name": _clean_event_name(e["name"]),
            "url": e["url"],
        })
    events.sort(key=lambda x: x["date_iso"])
    return events[:limit]


def _fmt_game_datetime(iso_utc: str) -> tuple[str, str]:
    """(date, time) in US Eastern, e.g. ('Wed, Jul 29', '7:05 PM ET')."""
    if not iso_utc:
        return ("", "")
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(_DISPLAY_TZ)
        return (dt.strftime("%a, %b %-d"), dt.strftime("%-I:%M %p ET"))
    except Exception:
        return (iso_utc[:10], "")


def _parse_event(ev: dict, abbr: str) -> Optional[dict]:
    """Flatten one ESPN schedule event into our game shape."""
    comp = (ev.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    us = next((c for c in competitors if (c.get("team") or {}).get("abbreviation", "").upper() == abbr.upper()), None)
    them = next((c for c in competitors if c is not us), None)
    if not us or not them:
        return None
    state = ((comp.get("status") or {}).get("type") or {}).get("state")  # pre / in / post
    date_str, time_str = _fmt_game_datetime(ev.get("date", ""))
    home_away = us.get("homeAway")
    opp_name = (them.get("team") or {}).get("displayName") or (them.get("team") or {}).get("abbreviation") or "TBD"

    def _score(c):
        s = c.get("score")
        if isinstance(s, dict):
            return s.get("value")
        try:
            return float(s) if s not in (None, "") else None
        except (TypeError, ValueError):
            return None

    us_score, them_score = _score(us), _score(them)
    won = None
    result = None
    if state == "post" and us_score is not None and them_score is not None:
        won = bool(us.get("winner"))
        wl = "W" if us.get("winner") else ("L" if them.get("winner") else "—")
        result = f"{wl} {int(us_score)}–{int(them_score)}"

    return {
        "id": ev.get("id"),
        "date_iso": (ev.get("date") or "")[:10],   # UTC calendar date — stable join key
        "state": state,
        "date": date_str,
        "time": time_str,
        "opponent": opp_name,
        "opponent_abbr": (them.get("team") or {}).get("abbreviation"),
        "home_away": "vs" if home_away == "home" else "@",
        "home_away_raw": home_away,
        "venue": (comp.get("venue") or {}).get("fullName"),
        "attendance": comp.get("attendance"),
        "result": result,
        "won": won,
        "tv": None,   # filled in later from the scoreboard feed
    }


def _team_games(sport_path: str, abbr: str) -> list[dict]:
    """All parsed games for a team, in schedule order."""
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{abbr}/schedule")
    events = (d or {}).get("events") or []
    return [g for g in (_parse_event(e, abbr) for e in events) if g]


def _fetch_record_and_standing(sport_path: str, abbr: str) -> dict:
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{abbr}")
    team = (d or {}).get("team") or {}
    overall = ""
    for it in ((team.get("record") or {}).get("items") or []):
        if it.get("description") == "Overall Record" or it.get("type") == "total":
            overall = it.get("summary") or ""
            break
    return {
        "name": team.get("displayName"),
        "record": overall,
        "standing_summary": team.get("standingSummary"),
        "logo": (team.get("logos") or [{}])[0].get("href") if team.get("logos") else None,
    }


def _fetch_broadcasts(sport_path: str, abbr: str, games: list[dict]) -> dict:
    """{event_id: 'TV label'} for the given games, from the scoreboard feed.

    We join on ESPN event id (not date) because a night game's UTC calendar
    date can differ from its local date. Prefer national + the team's own feed.
    """
    if not games:
        return {}
    dates = sorted({g["date_iso"].replace("-", "") for g in games if g.get("date_iso")})
    if not dates:
        return {}
    rng = dates[0] if len(dates) == 1 else f"{dates[0]}-{dates[-1]}"
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard?dates={rng}&limit=300")
    out = {}
    for ev in (d or {}).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        us = next((c for c in comp.get("competitors") or []
                   if (c.get("team") or {}).get("abbreviation", "").upper() == abbr.upper()), None)
        if not us:
            continue
        ha = us.get("homeAway")
        names = []
        for b in comp.get("broadcasts") or []:
            if b.get("market") in ("national", ha):
                for n in b.get("names") or []:
                    # MLB.TV is the generic streaming package, not a TV channel —
                    # skip it so the row shows the actual broadcaster and fits one line.
                    if n and n != "MLB.TV" and n not in names:
                        names.append(n)
        if names:
            out[ev.get("id")] = " · ".join(names)
    return out


def _fetch_schedule(sport_path: str, abbr: str, games: list[dict]) -> dict:
    played = [g for g in games if g["state"] == "post"]
    upcoming = [g for g in games if g["state"] == "pre"]
    nxt = upcoming[:3]
    tv = _fetch_broadcasts(sport_path, abbr, nxt)
    for g in nxt:
        g["tv"] = tv.get(g["id"])
    return {
        "previous": played[-3:][::-1],   # most-recent first
        "next": nxt,
    }


def _winpct_series(games: list[dict]) -> list[dict]:
    """Cumulative win% after each completed game — the real season trajectory."""
    w = l = 0
    series = []
    for g in games:
        if g["state"] != "post" or g["won"] is None:
            continue
        if g["won"]:
            w += 1
        else:
            l += 1
        series.append({"d": g["date_iso"], "v": round(w / (w + l), 3)})
    return series


def _games_ahead_series(sport_path: str, division_abbrs: list[str], team_abbr: str,
                        team_games: list[dict]) -> list[dict]:
    """Signed games ahead/behind in the division after each of the team's games.

    Positive = leading the division (margin over 2nd); negative = games behind
    the leader. Rebuilt from every division team's game log, standings-by-date.
    """
    logs = {team_abbr.upper(): [(g["date_iso"], g["won"]) for g in team_games
                                if g["state"] == "post" and g["won"] is not None]}
    for ab in division_abbrs:
        if ab.upper() == team_abbr.upper():
            continue
        games = _team_games(sport_path, ab.lower())
        logs[ab.upper()] = [(g["date_iso"], g["won"]) for g in games
                            if g["state"] == "post" and g["won"] is not None]

    def wl_asof(ab, date):
        W = L = 0
        for d, won in logs.get(ab, []):
            if d <= date:
                W += 1 if won else 0
                L += 0 if won else 1
        return W, L

    series = []
    for date, _ in logs[team_abbr.upper()]:
        standings = []
        for ab in logs:
            W, L = wl_asof(ab, date)
            pct = W / max(W + L, 1)
            standings.append((ab, W, L, pct))
        standings.sort(key=lambda x: (-x[3], -x[1]))
        me = next(s for s in standings if s[0] == team_abbr.upper())
        if me[0] == standings[0][0]:                 # leading → ahead of 2nd
            second = standings[1]
            val = ((me[1] - second[1]) + (second[2] - me[2])) / 2
        else:                                        # trailing → behind leader
            lead = standings[0]
            val = -((lead[1] - me[1]) + (me[2] - lead[2])) / 2
        series.append({"d": date, "v": round(val, 1)})
    return series


def _attendance_series(games: list[dict]) -> list[dict]:
    """Home-game attendance over the season (the team's own ballpark)."""
    return [{"d": g["date_iso"], "v": int(g["attendance"])}
            for g in games
            if g.get("home_away_raw") == "home" and g.get("attendance")]


# Completed-season attendance never changes, so cache it on disk forever and
# only recompute the in-progress current year (from the live schedule).
_ATT_CACHE = Path(__file__).parent / "data" / "attendance_history.json"


def _load_att_cache() -> dict:
    try:
        return json.loads(_ATT_CACHE.read_text())
    except Exception:
        return {}


def _save_att_cache(cache: dict) -> None:
    _ATT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _ATT_CACHE.write_text(json.dumps(cache, indent=2))


def _espn_year_avg(espn_name: str, year: int) -> Optional[int]:
    """Avg home attendance/game for a team in a season, from ESPN's report."""
    html = _get_html(f"https://www.espn.com/mlb/attendance/_/year/{year}")
    if not html:
        return None
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    # Row layout: <rank> <name> <homeGms> <homeTotal> <homeAvg> ...
    m = re.search(rf"\b{re.escape(espn_name)}\s+\d+\s+[\d,]+\s+([\d,]+)", txt)
    return int(m.group(1).replace(",", "")) if m else None


def _attendance_by_year(cfg: dict, current_year: int, current_avg: Optional[int],
                        n_years: int = 10) -> list[dict]:
    """Avg attendance/game for the current year vs the prior `n_years`."""
    espn_name = cfg.get("attendance_espn_name") or cfg["name"].split()[-1]
    seed = {int(k): v for k, v in (cfg.get("attendance_history") or {}).items()}
    cache = _load_att_cache()
    team_cache = cache.get(cfg["espn_abbr"], {})
    out, dirty = [], False
    for yr in range(current_year - n_years, current_year + 1):
        if yr == current_year:
            avg = current_avg
        elif yr in seed:                       # immutable reference data — no fetch
            avg = seed[yr]
        elif str(yr) in team_cache:
            avg = team_cache[str(yr)]
        else:                                  # best-effort fallback for un-seeded years
            avg = _espn_year_avg(espn_name, yr)
            if avg is not None:
                team_cache[str(yr)] = avg
                dirty = True
        if avg:
            out.append({"year": yr, "avg": int(avg), "is_current": yr == current_year})
    if dirty:
        cache[cfg["espn_abbr"]] = team_cache
        _save_att_cache(cache)
    return out


def _fetch_live(sport_path: str, abbr: str) -> Optional[dict]:
    """Live score if the team has a game in progress right now, else None."""
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard")
    for ev in (d or {}).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        status = (comp.get("status") or {}).get("type") or {}
        if status.get("state") != "in":
            continue
        competitors = comp.get("competitors") or []
        us = next((c for c in competitors if (c.get("team") or {}).get("abbreviation", "").upper() == abbr.upper()), None)
        them = next((c for c in competitors if c is not us), None)
        if not us or not them:
            continue

        def _sc(c):
            s = c.get("score")
            return s.get("value") if isinstance(s, dict) else s

        sit = comp.get("situation") or {}
        return {
            "detail": status.get("shortDetail") or status.get("detail") or "In Progress",
            "home_away": "vs" if us.get("homeAway") == "home" else "@",
            "opponent": (them.get("team") or {}).get("displayName") or (them.get("team") or {}).get("abbreviation"),
            "us_score": _sc(us),
            "them_score": _sc(them),
            "outs": sit.get("outs"),
            "on_first": bool(sit.get("onFirst")),
            "on_second": bool(sit.get("onSecond")),
            "on_third": bool(sit.get("onThird")),
        }
    return None


def _walk_find_team(node, abbr):
    """DFS a standings blob for the entry dict whose team matches abbr."""
    if isinstance(node, dict):
        t = node.get("team")
        if isinstance(t, dict) and t.get("abbreviation", "").upper() == abbr.upper() and "stats" in node:
            return node
        for v in node.values():
            r = _walk_find_team(v, abbr)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _walk_find_team(v, abbr)
            if r:
                return r
    return None


def _fetch_division_and_playoff(sport_path: str, division_name: str, abbr: str) -> dict:
    """AL West table (ordered) + ESPN model playoff % for our team."""
    league = sport_path.split("/")[-1]  # 'mlb'
    d = _get(f"https://site.api.espn.com/apis/v2/sports/baseball/{league}/standings?level=3&season={_SEASON}")
    if not d:
        return {"table": [], "playoff_pct": None}

    # Find the division group by name.
    group = None

    def _find_group(node):
        nonlocal group
        if group is not None:
            return
        if isinstance(node, dict):
            if node.get("name") == division_name and (node.get("standings") or node.get("entries")):
                group = node
                return
            for v in node.values():
                _find_group(v)
        elif isinstance(node, list):
            for v in node:
                _find_group(v)

    _find_group(d)

    def _stat(entry, name):
        for s in (entry.get("stats") or []):
            if s.get("name") == name:
                return s
        return {}

    table, playoff_pct, team_stats = [], None, {}
    entries = []
    if group:
        entries = (group.get("standings") or {}).get("entries") or group.get("entries") or []
    for e in entries:
        team = e.get("team") or {}
        row = {
            "name": team.get("displayName") or team.get("name"),
            "abbr": team.get("abbreviation"),
            "wins": _stat(e, "wins").get("displayValue"),
            "losses": _stat(e, "losses").get("displayValue"),
            "pct": _stat(e, "winPercent").get("displayValue"),
            "gb": _stat(e, "gamesBehind").get("displayValue"),
            "is_team": team.get("abbreviation", "").upper() == abbr.upper(),
        }
        table.append(row)
        if row["is_team"]:
            pp = _stat(e, "playoffPercent")
            playoff_pct = pp.get("displayValue") or (f"{pp['value']:.1f}%" if pp.get("value") is not None else None)
            # Extra performance stats to surface elsewhere-unseen context.
            rd = _stat(e, "pointDifferential").get("value")
            team_stats = {
                "run_diff": (f"+{int(rd)}" if rd is not None and rd > 0 else (str(int(rd)) if rd is not None else None)),
                "last10": _stat(e, "Last Ten Games").get("displayValue"),
                "streak": _stat(e, "streak").get("displayValue"),
                "home": _stat(e, "Home").get("displayValue"),
                "road": _stat(e, "Road").get("displayValue"),
                "rpg_for": _stat(e, "avgPointsFor").get("displayValue"),
                "rpg_against": _stat(e, "avgPointsAgainst").get("displayValue"),
                "magic": _stat(e, "magicNumberDivision").get("displayValue"),
            }
    # Order the table by wins desc so it reads like a real standings block.
    table.sort(key=lambda r: float(r["pct"] or 0), reverse=True)
    return {"table": table, "playoff_pct": playoff_pct, "team_stats": team_stats}


# Book preference — DraftKings first: it prices every market (incl. divisions),
# so all lines come from one consistent book. Falls through if a book is absent.
_PROVIDER_PREFERENCE = ["DraftKings", "ESPN BET", "FanDuel", "Caesars Sportsbook"]


def _extract_team_odds(future: dict, espn_id: str) -> tuple[Optional[str], Optional[str]]:
    """(moneyline, book name) for the team, from the most-preferred book that prices it."""
    providers = future.get("futures") or []

    def rank(p):
        name = (p.get("provider") or {}).get("name") or ""
        return _PROVIDER_PREFERENCE.index(name) if name in _PROVIDER_PREFERENCE else len(_PROVIDER_PREFERENCE)

    for prov in sorted(providers, key=rank):
        for book in (prov.get("books") or []):
            ref = (book.get("team") or {}).get("$ref", "")
            tid = ref.split("/teams/")[-1].split("?")[0] if "/teams/" in ref else None
            if tid == espn_id:
                return book.get("value"), (prov.get("provider") or {}).get("name")
    return None, None


def _fetch_futures(cfg: dict) -> dict:
    d = _get(f"https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/seasons/{_SEASON}/futures?lang=en&region=us")
    items = (d or {}).get("items") or []
    by_name = {i.get("name"): i for i in items}
    espn_id = cfg["espn_id"]

    def odds(name):
        fut = by_name.get(name)
        return _extract_team_odds(fut, espn_id) if fut else (None, None)

    ws, pen, div = odds(cfg["worldseries_future"]), odds(cfg["pennant_future"]), odds(cfg["division_future"])
    return {
        "world_series": ws[0], "world_series_book": ws[1],
        "pennant": pen[0], "pennant_book": pen[1],
        "division": div[0], "division_book": div[1],
    }


def _fetch_news(sport_path: str, team_name: str, espn_id: str, limit: int = 4) -> list[dict]:
    """Recent stories, preferring ones about this team, falling back to league news."""
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/news?limit=50")
    articles = (d or {}).get("articles") or []
    short = team_name.split()[-1]  # 'Rangers'

    def _link(a):
        links = (a.get("links") or {}).get("web") or {}
        return links.get("href", "")

    def _is_team(a):
        blob = f"{a.get('headline','')} {a.get('description','')}".lower()
        if short.lower() in blob:
            return True
        for c in (a.get("categories") or []):
            if str(c.get("teamId")) == espn_id or (c.get("description") or "") == team_name:
                return True
        return False

    team_arts = [a for a in articles if _is_team(a)]
    # Team stories first, then top up with the latest league news to reach `limit`.
    other = [a for a in articles if a not in team_arts]
    chosen = (team_arts + other)[:limit]
    out = []
    for a in chosen:
        pub = a.get("published") or ""
        out.append({
            "headline": a.get("headline") or "",
            "url": _link(a),
            "published": pub[:10],
            "about_team": a in team_arts,
        })
    return out


def _pct_to_float(pct_str) -> Optional[float]:
    if not pct_str:
        return None
    try:
        return float(str(pct_str).replace("%", ""))
    except ValueError:
        return None


_DIV_SHORT = {
    "American League East": "AL East", "American League West": "AL West",
    "American League Central": "AL Central", "National League East": "NL East",
    "National League West": "NL West", "National League Central": "NL Central",
}

_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}


def _all_standings_lookup(sport_path: str) -> dict:
    """Map every team abbr → {record, div, rank} for opponent context."""
    league = sport_path.split("/")[-1]
    d = _get(f"https://site.api.espn.com/apis/v2/sports/baseball/{league}/standings?level=3&season={_SEASON}")
    out = {}
    if not d:
        return out

    def _stat(entry, name):
        for s in (entry.get("stats") or []):
            if s.get("name") == name:
                return s.get("displayValue")
        return None

    def walk(node):
        if isinstance(node, dict):
            nm = node.get("name")
            entries = (node.get("standings") or {}).get("entries") or node.get("entries")
            if nm and entries and any(k in str(nm) for k in ("East", "West", "Central")):
                # entries are already ranked in standings order
                for i, e in enumerate(entries):
                    ab = (e.get("team") or {}).get("abbreviation")
                    if ab:
                        out[ab] = {
                            "record": f"{_stat(e, 'wins')}-{_stat(e, 'losses')}",
                            "div": _DIV_SHORT.get(nm, nm),
                            "rank": _ORDINAL.get(i + 1, f"{i+1}th"),
                        }
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(d)
    return out


def _extract_statline(summary: dict, team_abbr: str) -> Optional[str]:
    """Shorthand box-score line for a team's standout performers, e.g.
    'Joc Pederson 2/4, 2HR, 4RBI; Kumar Rocker 6.2IP, 7K, 2ER'."""
    box = summary.get("boxscore") or {}
    for tp in box.get("players") or []:
        if (tp.get("team") or {}).get("abbreviation", "").upper() != team_abbr.upper():
            continue
        batters, pitcher = [], []
        for st in tp.get("statistics") or []:
            labels = st.get("labels") or []
            idx = {lab: i for i, lab in enumerate(labels)}
            typ = st.get("type")

            def _g(a_stats, lab):
                i = idx.get(lab)
                return a_stats[i] if i is not None and i < len(a_stats) else None

            for a in st.get("athletes") or []:
                s = a.get("stats") or []
                if not s:
                    continue
                name = (a.get("athlete") or {}).get("displayName") or (a.get("athlete") or {}).get("shortName")
                if typ == "batting":
                    try:
                        hr, rbi, h = int(_g(s, "HR") or 0), int(_g(s, "RBI") or 0), int(_g(s, "H") or 0)
                    except (TypeError, ValueError):
                        hr = rbi = h = 0
                    if hr > 0 or rbi >= 2 or h >= 3:   # only notable lines
                        parts = [(_g(s, "H-AB") or "").replace("-", "/")]
                        if hr > 0:
                            parts.append(f"{hr}HR")
                        if rbi > 0:
                            parts.append(f"{rbi}RBI")
                        batters.append((hr, rbi, h, f"{name} {', '.join(parts)}"))
                elif typ == "pitching" and not pitcher:   # starter is listed first
                    pitcher.append(f"{name} {_g(s,'IP')}IP, {_g(s,'K')}K, {_g(s,'ER')}ER")
        batters.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        line = "; ".join([b[3] for b in batters[:2]] + pitcher[:1])
        return line or None
    return None


def _fetch_game_meta(sport_path: str, event_id: str, team_abbr: str = "") -> dict:
    """Weather forecast + shorthand stat line for one game, from its summary."""
    if not event_id:
        return {}
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/summary?event={event_id}")
    if not d:
        return {}
    meta = {}
    wx = (d.get("gameInfo") or {}).get("weather") or {}
    if wx.get("temperature") is not None:
        meta["weather"] = {
            "temp": wx.get("temperature"),
            "precip": wx.get("precipitation"),
        }
    if team_abbr:
        meta["statline"] = _extract_statline(d, team_abbr)
    return meta


def _weather_icon(precip) -> str:
    try:
        p = float(precip)
    except (TypeError, ValueError):
        return "🌡️"
    return "🌧️" if p >= 50 else ("⛅" if p >= 20 else "☀️")


def _build_team(cfg: dict) -> dict:
    core = _fetch_record_and_standing(cfg["sport_path"], cfg["espn_abbr"])
    games = _team_games(cfg["sport_path"], cfg["espn_abbr"])
    schedule = _fetch_schedule(cfg["sport_path"], cfg["espn_abbr"], games)
    div = _fetch_division_and_playoff(cfg["sport_path"], cfg["division_name"], cfg["espn_abbr"])
    futures = _fetch_futures(cfg)
    news = _fetch_news(cfg["sport_path"], cfg["name"], cfg["espn_id"])

    winpct = _winpct_series(games)
    games_ahead = _games_ahead_series(
        cfg["sport_path"], cfg.get("division_abbrs") or [], cfg["espn_abbr"], games
    )
    attendance = _attendance_series(games)
    current_avg = round(sum(p["v"] for p in attendance) / len(attendance)) if attendance else None
    last5_home = attendance[-5:][::-1] if attendance else []   # most recent first
    attendance_by_year = _attendance_by_year(cfg, _SEASON, current_avg)
    live = _fetch_live(cfg["sport_path"], cfg["espn_abbr"])
    venue_events = _fetch_venue_events(cfg, cfg["name"])

    # Enrich the schedule: opponent standing + weather for upcoming games,
    # recap highlights for completed games.
    sport_path = cfg["sport_path"]
    standings = _all_standings_lookup(sport_path)
    def _add_opp(g):
        opp = standings.get(g.get("opponent_abbr")) or {}
        g["opp_record"] = opp.get("record")
        g["opp_div"] = opp.get("div")
        g["opp_rank"] = opp.get("rank")

    for g in schedule["next"]:
        _add_opp(g)
        wx = _fetch_game_meta(sport_path, g.get("id")).get("weather")
        if wx:
            g["weather"] = {"icon": _weather_icon(wx.get("precip")), **wx}
    for g in schedule["previous"]:
        _add_opp(g)
        g["statline"] = _fetch_game_meta(sport_path, g.get("id"), cfg["espn_abbr"]).get("statline")

    return {
        "name": core.get("name") or cfg["name"],
        "league": cfg["league"],
        "logo": core.get("logo"),
        "record": core.get("record"),
        "standing_summary": core.get("standing_summary"),
        "division_name": cfg["division_name"],
        "division_stats": div.get("team_stats") or {},
        "venue_name": (cfg.get("venue_events") or {}).get("venue_name"),
        "venue_events": venue_events,
        "live": live,
        "previous_games": schedule["previous"],
        "next_games": schedule["next"],
        "division_table": div["table"],
        "odds": {
            "playoff_pct": div["playoff_pct"],       # model probability, not a line
            "pennant": futures["pennant"],
            "pennant_book": futures["pennant_book"],
            "pennant_label": cfg["pennant_label"],
            "world_series": futures["world_series"],
            "world_series_book": futures["world_series_book"],
            "division": futures["division"],
            "division_book": futures["division_book"],
        },
        "charts": {
            "playoff_pct": _pct_to_float(div["playoff_pct"]),
            "winpct": winpct,
            "games_ahead": games_ahead,
            "attendance_avg": current_avg,
            "attendance_by_year": attendance_by_year,
            "attendance_last5": last5_home,   # [{"d": iso, "v": count}] most-recent first
        },
        "news": news,
    }


def scrape_all_teams() -> dict:
    """Top-level entry, mirrors scrape_all_apps(). Returns {'teams': {key: {...}}}."""
    out = {}
    for cfg in TEAMS:
        try:
            out[cfg["key"]] = _build_team(cfg)
            logger.info(f"team {cfg['key']}: record={out[cfg['key']].get('record')} "
                        f"playoff%={out[cfg['key']]['odds'].get('playoff_pct')}")
        except Exception:
            logger.exception(f"team {cfg['key']}: build failed")
    return {
        "last_scrape": datetime.now(_DISPLAY_TZ).strftime("%b %-d, %Y · %-I:%M %p"),
        "teams": out,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(scrape_all_teams(), indent=2, default=str))
