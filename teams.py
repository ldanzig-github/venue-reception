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
        "league_name": "American League",          # for the wild-card race
        "mlb_team_id": 140,                        # MLB Stats API id (standings of record)
        "wildcard_label": "AL Wild Card",
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
            # Ticketmaster JSON-LD supplies precise times / ticket links for
            # near-term concerts (wins dedup over the venue page's date-only
            # entries). Songkick dropped — it 406s the VPS and the venue page
            # already covers the same events.
            "sources": [
                "https://www.ticketmaster.com/globe-life-field-tickets-arlington/venue/99338",
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
    status = (comp.get("status") or {}).get("type") or {}
    state = status.get("state")            # pre / in / post
    # "post" alone is not proof a game was played: postponed, canceled and
    # suspended games all come back as post with 0-0 scores and no winner
    # flag. Only `completed` games may move the W-L (and therefore the GB) math.
    completed = bool(status.get("completed"))
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
    if state == "post" and completed and us_score is not None and them_score is not None:
        if us.get("winner"):
            won, wl = True, "W"
        elif them.get("winner"):
            won, wl = False, "L"
        else:
            won, wl = None, "—"        # tie — a game played, but not a W or an L
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
    played = [g for g in games if g["state"] == "post" and g.get("result")]
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


def _gb_between(me: tuple[int, int], other: tuple[int, int]) -> float:
    """Standard games-behind: how far (W, L) `me` trails (W, L) `other`.

    GB = ((otherW − meW) + (meL − otherL)) / 2 — negative when `me` is ahead.
    """
    return ((other[0] - me[0]) + (me[1] - other[1])) / 2


def _games_ahead(rows: list[tuple], me_abbr: str) -> Optional[float]:
    """Signed games ahead(+)/behind(−) for `me_abbr` in a [(abbr, W, L)] table.

    Measured off the leader, or off 2nd place when we *are* the leader — the
    same reference point the GB column in a published standings table uses.
    """
    ranked = sorted(rows, key=lambda r: (-(r[1] / max(r[1] + r[2], 1)), -(r[1] - r[2])))
    me = next((r for r in ranked if r[0] == me_abbr.upper()), None)
    if me is None or len(ranked) < 2:
        return None
    ref = ranked[1] if ranked[0][0] == me_abbr.upper() else ranked[0]
    return round(-_gb_between((me[1], me[2]), (ref[1], ref[2])), 1)


def _games_ahead_series(sport_path: str, division_abbrs: list[str], team_abbr: str,
                        team_games: list[dict]) -> list[dict]:
    """Signed games ahead/behind in the division after each of the team's games.

    Positive = leading the division (margin over 2nd); negative = games behind
    the leader. Rebuilt from every division team's game log, standings-by-date.
    """
    me_abbr = team_abbr.upper()
    logs = {me_abbr: [(g["date_iso"], g["won"]) for g in team_games
                      if g["state"] == "post" and g["won"] is not None]}
    for ab in division_abbrs:
        if ab.upper() == me_abbr:
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
    for date, _ in logs[me_abbr]:
        rows = [(ab, *wl_asof(ab, date)) for ab in logs]
        val = _games_ahead(rows, me_abbr)
        if val is not None:
            series.append({"d": date, "v": val})
    return series


def _current_games_ahead(table: list[dict]) -> Optional[float]:
    """Today's signed games ahead/behind, straight off the live standings table.

    The per-game series is reconstructed from schedules and can only be as
    current as the team's last game; this pins the headline number (and the
    chart's final point) to the same source as the standings block beside it,
    so the two can never disagree.
    """
    rows, me_abbr = [], None
    for r in table:
        try:
            W, L = int(r["wins"]), int(r["losses"])
        except (KeyError, TypeError, ValueError):
            return None
        ab = (r.get("abbr") or "").upper()
        rows.append((ab, W, L))
        if r.get("is_team"):
            me_abbr = ab
    return _games_ahead(rows, me_abbr) if me_abbr else None


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


# The whole standings blob is read three times per build (division table,
# wild-card race, opponent lookup). Memoise it briefly so one build makes one
# request, while a later build — 30 minutes on — still refetches.
_STANDINGS_TTL = 60.0
_standings_cache: dict[str, tuple[float, Optional[dict]]] = {}


def _standings_blob(sport_path: str) -> Optional[dict]:
    league = sport_path.split("/")[-1]  # 'mlb'
    hit = _standings_cache.get(league)
    now = datetime.now(timezone.utc).timestamp()
    if hit and now - hit[0] < _STANDINGS_TTL:
        return hit[1]
    d = _get(f"https://site.api.espn.com/apis/v2/sports/baseball/{league}/standings?level=3&season={_SEASON}")
    _standings_cache[league] = (now, d)
    return d


def _stat(entry: dict, name: str) -> dict:
    """One named stat dict out of a standings entry ({} when absent)."""
    for st in (entry.get("stats") or []):
        if st.get("name") == name:
            return st
    return {}


def _group_entries(group: Optional[dict]) -> list:
    """The team rows of a standings group, whichever shape ESPN used."""
    if not group:
        return []
    return (group.get("standings") or {}).get("entries") or group.get("entries") or []


def _find_group(root, name: str, *, with_entries: bool = True) -> Optional[dict]:
    """DFS a standings blob for the group node called `name`."""
    found = None

    def walk(node):
        nonlocal found
        if found is not None:
            return
        if isinstance(node, dict):
            if node.get("name") == name and (_group_entries(node) or not with_entries):
                found = node
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(root)
    return found


def _fetch_division_and_playoff(sport_path: str, division_name: str, abbr: str) -> dict:
    """AL West table (ordered) + ESPN model playoff % for our team."""
    d = _standings_blob(sport_path)
    if not d:
        return {"table": [], "playoff_pct": None}

    group = _find_group(d, division_name)

    table, playoff_pct, team_stats = [], None, {}
    for e in _group_entries(group):
        team = e.get("team") or {}
        row = {
            "name": team.get("displayName") or team.get("name"),
            "abbr": team.get("abbreviation"),
            "wins": _stat(e, "wins").get("displayValue"),
            "losses": _stat(e, "losses").get("displayValue"),
            "pct": _stat(e, "winPercent").get("displayValue"),
            "gb": _dash(_stat(e, "gamesBehind").get("displayValue")),
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


# Three wild cards per league since the 2022 expansion.
_WILDCARD_SPOTS = 3


def _fmt_gb(gb: float) -> str:
    """GB the way a standings table prints it: '—' level, '+2.0' up, '2.0' down."""
    if gb == 0:
        return "—"
    return f"+{abs(gb):.1f}" if gb < 0 else f"{gb:.1f}"


def _fetch_wildcard(sport_path: str, league_name: Optional[str], abbr: str,
                    spots: int = _WILDCARD_SPOTS, limit: int = 7) -> dict:
    """The league's wild-card race: everyone who isn't leading a division.

    Division leaders are already in, so they come out of the pool; the rest are
    ranked league-wide and every GB is measured off the team holding the last
    wild card — which is how the race is actually published (teams in the field
    show how far *ahead* of that cutline they sit, chasers how far behind).
    """
    d = _standings_blob(sport_path) if league_name else None
    league = _find_group(d, league_name, with_entries=False) if d else None
    if not league:
        return {"table": [], "spots": spots}

    def _row(entry):
        team = entry.get("team") or {}
        ab = (team.get("abbreviation") or "").upper()
        try:
            W = int(_stat(entry, "wins").get("displayValue"))
            L = int(_stat(entry, "losses").get("displayValue"))
        except (TypeError, ValueError):
            return None
        try:
            seed = int(_stat(entry, "playoffSeed").get("displayValue") or 0)
        except (TypeError, ValueError):
            seed = 0
        return {
            "name": team.get("shortDisplayName") or team.get("displayName") or team.get("name"),
            "abbr": ab, "wins": W, "losses": L, "seed": seed,
            "pct": _stat(entry, "winPercent").get("displayValue"),
            "is_team": ab == abbr.upper(),
        }

    # Each division contributes everyone but its leader — entries arrive in
    # standings order, so the leader is simply the first one.
    pool = []
    for division in _child_groups(league):
        rows = [r for r in (_row(e) for e in _group_entries(division)) if r]
        pool.extend(rows[1:])
    # ESPN's own playoff seeding already resolves head-to-head tiebreakers we
    # can't recompute; only fall back to win% when the feed omits it.
    if pool and all(r["seed"] for r in pool):
        pool.sort(key=lambda r: r["seed"])
    else:
        pool.sort(key=_standings_key)
    if len(pool) <= spots:
        return {"table": [], "spots": spots}

    cutline = pool[spots - 1]
    for i, r in enumerate(pool):
        gb = _gb_between((r["wins"], r["losses"]), (cutline["wins"], cutline["losses"]))
        r["gb"] = _fmt_gb(gb)
        r["in_field"] = i < spots

    # Show the field plus the nearest chasers — and our own team wherever it is.
    shown = pool[:limit]
    if not any(r["is_team"] for r in shown):
        mine = next((r for r in pool if r["is_team"]), None)
        if mine:
            shown = shown[:limit - 1] + [mine]
    return {"table": shown, "spots": spots}


def _standings_key(row: dict):
    return (-(row["wins"] / max(row["wins"] + row["losses"], 1)),
            -(row["wins"] - row["losses"]))


def _child_groups(node: dict) -> list:
    """The division groups under a league node, however deeply they're nested."""
    kids = [c for c in (node.get("children") or []) if _group_entries(c)]
    if kids:
        return kids
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n is not node and _group_entries(n):
                out.append(n)
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


# ── Standings of record: MLB's own Stats API ──────────────────────────────
# ESPN's standings feed stays the source for things only ESPN has (its model
# playoff %, its rate stats), but every number that has to be *right* — W-L,
# games back, and who holds a wild card — comes from MLB's free, no-auth Stats
# API instead. It publishes gamesBack / wildCardGamesBack / wildCardRank
# directly, so those figures are read from the league, never reconstructed.
_STATSAPI = "https://statsapi.mlb.com/api/v1/standings"
_MLB_LEAGUE_ID = {"American League": 103, "National League": 104}
_MLB_TTL = 60.0
_mlb_cache: dict[tuple, tuple[float, Optional[dict]]] = {}


def _mlb_standings(league_id: int, kind: str = "regularSeason") -> Optional[dict]:
    """One standings payload from MLB, memoised for the length of a build."""
    key = (league_id, kind)
    hit = _mlb_cache.get(key)
    now = datetime.now(timezone.utc).timestamp()
    if hit and now - hit[0] < _MLB_TTL:
        return hit[1]
    try:
        r = requests.get(_STATSAPI, timeout=_HTTP_TIMEOUT, headers={"User-Agent": "venue-reception/1.0"},
                         params={"leagueId": league_id, "season": _SEASON,
                                 "standingsTypes": kind, "hydrate": "team,division"})
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        logger.warning(f"teams: MLB standings ({league_id}/{kind}) failed: {e}")
        d = None
    _mlb_cache[key] = (now, d)
    return d


def _mlb_rows(payload: Optional[dict], division_name: Optional[str] = None) -> list[dict]:
    """Flatten a standings payload into team rows, optionally one division only."""
    rows = []
    for rec in (payload or {}).get("records") or []:
        if division_name and (rec.get("division") or {}).get("name") != division_name:
            continue
        for t in rec.get("teamRecords") or []:
            team = t.get("team") or {}
            try:
                W, L = int(t["wins"]), int(t["losses"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append({
                "id": team.get("id"),
                "name": team.get("name"),
                "short": team.get("teamName") or team.get("clubName") or team.get("name"),
                "abbr": (team.get("abbreviation") or "").upper(),
                "wins": W, "losses": L,
                "pct": t.get("winningPercentage"),
                "gb": _dash(t.get("gamesBack")),
                "wc_gb": _dash(t.get("wildCardGamesBack")),
                "wc_rank": t.get("wildCardRank"),
                "div_rank": t.get("divisionRank"),
                "div_leader": bool(t.get("divisionLeader")),
            })
    return rows


def _dash(v) -> str:
    """MLB writes a level/leading figure as '-'; print it as a real dash."""
    return "—" if str(v).strip() in ("-", "", "None") else str(v)


def _fetch_official_standings(cfg: dict, spots: int = _WILDCARD_SPOTS, limit: int = 7) -> dict:
    """Division table, current games ahead/behind, and the wild-card race — all
    straight from MLB. Returns empty pieces if the API is unreachable, and the
    ESPN-derived versions stand in."""
    league_id = _MLB_LEAGUE_ID.get(cfg.get("league_name") or "")
    out = {"table": [], "games_ahead": None, "wildcard": [], "spots": spots}
    if not league_id:
        return out
    me_id = cfg.get("mlb_team_id")

    # ── division table, ordered as MLB ranks it ──
    rows = _mlb_rows(_mlb_standings(league_id), cfg.get("division_name"))
    rows.sort(key=lambda r: r["div_rank"] or 99)
    out["table"] = [{
        "name": r["name"], "abbr": r["abbr"],
        "wins": str(r["wins"]), "losses": str(r["losses"]),
        "pct": r["pct"], "gb": r["gb"], "is_team": r["id"] == me_id,
    } for r in rows]

    # ── how far ahead/behind we are right now ──
    me = next((r for r in rows if r["id"] == me_id), None)
    if me:
        if me["div_leader"]:
            # MLB publishes no "games ahead" for a leader — derive the margin
            # over 2nd place from the same official records.
            second = next((r for r in rows if not r["div_leader"]), None)
            if second:
                out["games_ahead"] = round(
                    -_gb_between((me["wins"], me["losses"]), (second["wins"], second["losses"])), 1)
        else:
            try:
                out["games_ahead"] = -abs(float(me["gb"]))
            except ValueError:
                out["games_ahead"] = 0.0          # '—' → level with the leader

    # ── wild-card race: MLB's own wildCard standings, leaders already removed ──
    wc = [r for r in _mlb_rows(_mlb_standings(league_id, "wildCard")) if r["wc_rank"]]
    wc.sort(key=lambda r: int(r["wc_rank"]))
    shown = wc[:limit]
    if me_id and not any(r["id"] == me_id for r in shown):
        mine = next((r for r in wc if r["id"] == me_id), None)
        if mine:
            shown = shown[:limit - 1] + [mine]
    out["wildcard"] = [{
        "name": r["short"], "abbr": r["abbr"],
        "wins": str(r["wins"]), "losses": str(r["losses"]),
        "pct": r["pct"], "gb": r["wc_gb"],
        "in_field": int(r["wc_rank"]) <= spots, "is_team": r["id"] == me_id,
    } for r in shown]
    return out


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
    d = _standings_blob(sport_path)
    out = {}
    if not d:
        return out

    def _val(entry, name):
        return _stat(entry, name).get("displayValue")

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
                            "record": f"{_val(e, 'wins')}-{_val(e, 'losses')}",
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
    # MLB is the authority on W-L, games back and the wild-card race; the
    # ESPN-derived versions below only stand in if its API can't be reached.
    official = _fetch_official_standings(cfg)
    if official["table"]:
        div["table"] = official["table"]
    if official["wildcard"]:
        wildcard = {"table": official["wildcard"], "spots": official["spots"]}
    else:
        wildcard = _fetch_wildcard(cfg["sport_path"], cfg.get("league_name"), cfg["espn_abbr"])
    futures = _fetch_futures(cfg)
    news = _fetch_news(cfg["sport_path"], cfg["name"], cfg["espn_id"])

    winpct = _winpct_series(games)
    games_ahead = _games_ahead_series(
        cfg["sport_path"], cfg.get("division_abbrs") or [], cfg["espn_abbr"], games
    )
    # The per-game reconstruction stops at the team's last game; the live
    # standings table is the authority for where things stand right now.
    current_ahead = official["games_ahead"]
    if current_ahead is None:
        current_ahead = _current_games_ahead(div["table"])
    if current_ahead is not None:
        today = datetime.now(_DISPLAY_TZ).date().isoformat()
        if games_ahead and games_ahead[-1]["d"] >= today:
            games_ahead[-1]["v"] = current_ahead
        else:
            games_ahead.append({"d": today, "v": current_ahead})

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
        "wildcard_label": cfg.get("wildcard_label") or "Wild Card",
        "wildcard_spots": wildcard["spots"],
        "wildcard_table": wildcard["table"],
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
            "games_ahead_now": current_ahead,
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
