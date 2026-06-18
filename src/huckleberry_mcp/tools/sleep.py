"""Sleep tracking tools: timer + retroactive logging + history."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from google.cloud import firestore
from huckleberry_api.firebase_types import FirebaseSleepDetails

from ..auth import get_api
from ..utils import parse_dt, to_local_iso
from .children import validate_child_uid


async def _patch_live_timer(
    api: Any,
    child_uid: str,
    *,
    start_dt: datetime | None = None,
    notes: str | None = None,
) -> None:
    """Override fields on the active sleep timer doc after it's created.

    ``huckleberry-api``'s ``start_sleep`` hardcodes the start time to now and
    exposes no hook for notes, so we patch the firestore doc directly. We only
    touch fields the upstream timer structure already defines:
    ``timer.timerStartTime`` (ms; what ``complete_sleep`` reads for duration)
    and ``timer.details.notes`` (copied into the saved interval on completion).
    """
    updates: dict[str, Any] = {}
    if start_dt is not None:
        updates["timer.timerStartTime"] = start_dt.timestamp() * 1000
    if notes is not None:
        updates["timer.details.notes"] = notes
    if not updates:
        return
    client = await api._get_firestore_client()
    await client.collection("sleep").document(child_uid).update(updates)


async def log_sleep(
    child_uid: str | None = None,
    *,
    start_time: str,
    end_time: str | None = None,
    duration_minutes: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Retroactively log a completed sleep session.

    Provide EITHER end_time OR duration_minutes.
    Times are interpreted in America/New_York (EST/EDT) unless the input
    carries an explicit offset.

    notes: free-form text stored on the saved interval (e.g. a JSON blob
    describing how she was put down). Returned by get_sleep_history.
    """
    child_uid = await validate_child_uid(child_uid)
    api = await get_api()

    start_dt = parse_dt(start_time, default_now=False)
    if end_time and duration_minutes is not None:
        raise ValueError("Provide end_time OR duration_minutes, not both")
    if end_time:
        end_dt = parse_dt(end_time, default_now=False)
    elif duration_minutes is not None:
        end_dt = start_dt + timedelta(minutes=duration_minutes)
    else:
        raise ValueError("Provide end_time or duration_minutes")
    if end_dt <= start_dt:
        raise ValueError("end_time must be after start_time")

    details = FirebaseSleepDetails(notes=notes) if notes else None
    await api.log_sleep(child_uid, start_time=start_dt, end_time=end_dt, details=details)
    total = int((end_dt - start_dt).total_seconds() / 60)
    return {
        "success": True,
        "message": f"Logged {total} min sleep",
        "start_time": to_local_iso(start_dt),
        "end_time": to_local_iso(end_dt),
        "duration_minutes": total,
        "notes": notes,
    }


async def start_sleep(
    child_uid: str | None = None,
    *,
    start_time: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Start a live sleep timer, optionally backdated.

    start_time: when sleep actually began. Use this when she fell asleep
        earlier and you're only starting the timer now (e.g. "started 40 min
        ago"). Naive times are America/New_York (EST/EDT). Defaults to now.
        The timer stays live — call complete_sleep when she wakes and the
        duration is measured from this start.
    notes: free-form text stored on the session (e.g. a JSON blob describing
        how she was put down). Carried into the saved interval on completion.
    """
    child_uid = await validate_child_uid(child_uid)
    api = await get_api()
    start_dt = parse_dt(start_time, default_now=False) if start_time else None
    if start_dt is not None and start_dt > datetime.now(start_dt.tzinfo):
        raise ValueError("start_time is in the future")
    await api.start_sleep(child_uid)
    await _patch_live_timer(api, child_uid, start_dt=start_dt, notes=notes)
    if start_dt is not None:
        elapsed = int((datetime.now(start_dt.tzinfo) - start_dt).total_seconds() / 60)
        message = f"Started sleep timer, backdated {elapsed} min to {to_local_iso(start_dt)}"
    else:
        message = "Started sleep timer"
    return {"success": True, "message": message, "notes": notes}


async def pause_sleep(child_uid: str | None = None) -> dict[str, Any]:
    child_uid = await validate_child_uid(child_uid)
    api = await get_api()
    await api.pause_sleep(child_uid)
    return {"success": True, "message": "Paused sleep timer"}


async def resume_sleep(child_uid: str | None = None) -> dict[str, Any]:
    child_uid = await validate_child_uid(child_uid)
    api = await get_api()
    await api.resume_sleep(child_uid)
    return {"success": True, "message": "Resumed sleep timer"}


async def complete_sleep(
    child_uid: str | None = None,
    *,
    notes: str | None = None,
) -> dict[str, Any]:
    """Complete and save the active sleep timer.

    notes: free-form text (e.g. a JSON blob of how she was put down) attached
        to the saved interval. Overrides any notes set at start_sleep time.
    """
    child_uid = await validate_child_uid(child_uid)
    api = await get_api()
    # Patch notes onto the live timer first; complete_sleep copies timer.details
    # into the saved interval.
    await _patch_live_timer(api, child_uid, notes=notes)
    await api.complete_sleep(child_uid)
    return {"success": True, "message": "Completed sleep", "notes": notes}


async def cancel_sleep(child_uid: str | None = None) -> dict[str, Any]:
    child_uid = await validate_child_uid(child_uid)
    api = await get_api()
    await api.cancel_sleep(child_uid)
    return {"success": True, "message": "Cancelled sleep timer"}


async def update_sleep(
    child_uid: str | None = None,
    *,
    match_start_time: str,
    new_start_time: str | None = None,
    new_end_time: str | None = None,
    new_duration_minutes: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Edit an existing saved sleep interval in place (no duplicate created).

    The underlying API has no edit method, so the interval is located by its
    current start time and the firestore doc is patched directly. Use this to
    attach notes to a past sleep, or correct its start/duration.

    match_start_time: the interval's CURRENT start — copy it verbatim from
        get_sleep_history so it resolves to the same instant. Matching is by
        absolute time, so an explicit offset (e.g. "...-04:00") is honored.
    new_start_time: corrected start. If given without a new duration/end, the
        original END is held fixed and duration shrinks/grows accordingly.
    new_end_time / new_duration_minutes: provide at most one.
    notes: free-form text (e.g. a JSON blob of how she was put down).
    """
    child_uid = await validate_child_uid(child_uid)
    if new_end_time is not None and new_duration_minutes is not None:
        raise ValueError("Provide new_end_time OR new_duration_minutes, not both")
    api = await get_api()

    match_dt = parse_dt(match_start_time, default_now=False)
    match_start_sec = int(match_dt.timestamp())

    client = await api._get_firestore_client()
    intervals_ref = client.collection("sleep").document(child_uid).collection("intervals")

    # Locate the individual interval doc by exact start second. Batched "multi"
    # containers are an older storage format we don't edit here.
    matches: list[tuple[str, dict[str, Any]]] = []
    docs = (
        intervals_ref.where(filter=firestore.FieldFilter("start", ">=", match_start_sec))
        .where(filter=firestore.FieldFilter("start", "<", match_start_sec + 1))
        .stream()
    )
    async for doc in docs:
        data = doc.to_dict() or {}
        if data.get("multi"):
            continue
        matches.append((doc.id, data))

    if not matches:
        raise ValueError(
            f"No sleep interval starts at {to_local_iso(match_dt)}. "
            "Copy match_start_time exactly from get_sleep_history."
        )
    if len(matches) > 1:
        raise ValueError(f"{len(matches)} intervals start at {to_local_iso(match_dt)}; cannot disambiguate.")

    doc_id, data = matches[0]
    old_start_sec = int(data.get("start", match_start_sec))
    old_duration = int(data.get("duration", 0) or 0)
    old_end_sec = old_start_sec + old_duration

    start_sec = old_start_sec
    if new_start_time is not None:
        start_sec = int(parse_dt(new_start_time, default_now=False).timestamp())

    if new_duration_minutes is not None:
        duration_sec = new_duration_minutes * 60
    elif new_end_time is not None:
        duration_sec = int(parse_dt(new_end_time, default_now=False).timestamp()) - start_sec
    elif new_start_time is not None:
        # Start moved, no new length given: keep the original end fixed.
        duration_sec = old_end_sec - start_sec
    else:
        duration_sec = old_duration
    if duration_sec <= 0:
        raise ValueError("Resulting duration must be positive")

    updates: dict[str, Any] = {}
    if start_sec != old_start_sec:
        updates["start"] = start_sec
    if duration_sec != old_duration:
        updates["duration"] = duration_sec
    if notes is not None:
        updates["details.notes"] = notes
    if not updates:
        return {"success": True, "message": "Nothing to update", "start_time": to_local_iso(old_start_sec)}

    updates["lastUpdated"] = time.time()
    await intervals_ref.document(doc_id).update(updates)

    return {
        "success": True,
        "message": "Updated sleep interval",
        "before": {
            "start_time": to_local_iso(old_start_sec),
            "duration_minutes": old_duration // 60,
        },
        "after": {
            "start_time": to_local_iso(start_sec),
            "end_time": to_local_iso(start_sec + duration_sec),
            "duration_minutes": duration_sec // 60,
            "notes": notes,
        },
    }


async def get_sleep_history(
    child_uid: str | None = None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch sleep history."""
    child_uid = await validate_child_uid(child_uid)
    api = await get_api()
    end_dt = parse_dt(end_date, end_of_day=True)
    start_dt = parse_dt(start_date) if start_date else (end_dt - timedelta(days=7))
    intervals = await api.list_sleep_intervals(child_uid, start_dt, end_dt)
    # Most recent first — item 0 is "the last sleep".
    intervals = sorted(intervals, key=lambda iv: getattr(iv, "start", 0), reverse=True)
    out: list[dict[str, Any]] = []
    for iv in intervals:
        start = getattr(iv, "start", None)
        duration = getattr(iv, "duration", 0) or 0
        end = getattr(iv, "end", None)
        details = getattr(iv, "details", None)
        out.append(
            {
                "start_time": to_local_iso(start) if start is not None else None,
                "end_time": to_local_iso(end) if end is not None else None,
                "duration_minutes": int(duration // 60) if duration else 0,
                "notes": getattr(details, "notes", None) if details else None,
            }
        )
    return out


def register_sleep_tools(mcp):
    mcp.tool()(log_sleep)
    mcp.tool()(start_sleep)
    mcp.tool()(pause_sleep)
    mcp.tool()(resume_sleep)
    mcp.tool()(complete_sleep)
    mcp.tool()(cancel_sleep)
    mcp.tool()(update_sleep)
    mcp.tool()(get_sleep_history)
