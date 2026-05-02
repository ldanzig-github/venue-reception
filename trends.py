"""
Review-trend metrics, computed by comparing the current scrape to a rolling
history persisted at data/history.jsonl.

Each scrape cycle:
  1. After the scrape produces the dashboard data dict, call `enrich_with_trends(data)`.
     It reads recent history, computes deltas per venue, attaches them under
     each venue's "trends" key.
  2. Call `append_history(data)` to log the current snapshot.

History format (one JSON object per line):
  {"ts": "2026-05-01T20:00:00+00:00",
   "venues": {"poolhouse": {"rating": 4.8, "count": 97}, ...}}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).parent / "data" / "history.jsonl"
WINDOWS = [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))]


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _to_int(v) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _read_history(max_age=timedelta(days=45)) -> list[dict]:
    """Return parsed history entries newer than max_age, oldest first."""
    if not HISTORY_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - max_age
    out = []
    try:
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["ts"])
                if ts >= cutoff:
                    e["_ts"] = ts
                    out.append(e)
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
    except Exception as e:
        logger.warning(f"history read failed: {e}")
    out.sort(key=lambda e: e["_ts"])
    return out


def _find_snapshot_at_or_before(history: list[dict], target_time: datetime) -> Optional[dict]:
    """Closest snapshot at or before target_time (within ±10% of window)."""
    best = None
    for e in history:
        if e["_ts"] <= target_time:
            best = e
        else:
            break
    return best


def _venues_block(entry: dict) -> dict:
    """Pull the per-venue {rating, count} block out of a history entry."""
    return entry.get("venues") or {}


def _delta(prev: dict, cur_count, cur_rating) -> dict:
    prev_count = _to_int(prev.get("count"))
    prev_rating = _to_float(prev.get("rating"))
    entry = {}
    if cur_count is not None and prev_count is not None:
        entry["count_delta"] = cur_count - prev_count
    if cur_rating is not None and prev_rating is not None:
        entry["rating_delta"] = round(cur_rating - prev_rating, 2)
    return entry


def _format_elapsed(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 60:    return "just now"
    if secs < 3600:  return f"{secs // 60}m"
    if secs < 86400: return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _compute_venue_trends(venue_key: str, current_g: dict, history: list[dict]) -> dict:
    """
    Compute count + rating deltas. Tries standard windows (24h/7d/30d) first.
    If no standard window has matching history yet, falls back to the
    earliest available snapshot with a dynamic label like "since 45m".
    """
    cur_count = _to_int(current_g.get("count"))
    cur_rating = _to_float(current_g.get("rating"))
    if (cur_count is None and cur_rating is None) or not history:
        return {}

    now = datetime.now(timezone.utc)
    earliest = history[0]
    out = {}

    for label, delta in WINDOWS:
        target = now - delta
        # Don't fabricate a window we don't have data for.
        if earliest["_ts"] > target:
            continue
        snap = _find_snapshot_at_or_before(history, target)
        if not snap:
            continue
        entry = _delta(_venues_block(snap).get(venue_key) or {}, cur_count, cur_rating)
        if entry:
            out[label] = entry

    # Early-days fallback: if no standard window matched, show "since X"
    # using the earliest snapshot we have.
    if not out:
        elapsed = now - earliest["_ts"]
        if elapsed.total_seconds() >= 60:
            entry = _delta(_venues_block(earliest).get(venue_key) or {}, cur_count, cur_rating)
            if entry:
                out[_format_elapsed(elapsed)] = entry

    return out


def enrich_with_trends(data: dict) -> dict:
    """Mutate `data` in place: add a "trends" sub-dict to each venue."""
    history = _read_history()
    if not history:
        # First-ever run: no comparison possible. Still safe to call.
        for v in (data.get("venues") or {}).values():
            v["trends"] = {}
        return data
    venues = data.get("venues") or {}
    for key, v in venues.items():
        google_block = v.get("google") or {}
        v["trends"] = _compute_venue_trends(key, google_block, history)
    return data


def append_history(data: dict) -> None:
    """Append a compact snapshot to the history log."""
    venues = data.get("venues") or {}
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "venues": {
            key: {
                "rating": _to_float((v.get("google") or {}).get("rating")),
                "count":  _to_int((v.get("google") or {}).get("count")),
            }
            for key, v in venues.items()
        },
    }
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")
    except Exception as e:
        logger.warning(f"history append failed: {e}")
