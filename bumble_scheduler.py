"""Adaptive multi-phone scheduler for the GeeLark Bumble bot.

Goals: never miss a customer message, never pay for idle cloud-phone time.

- Persona clock: each phone's active window (default 08:30-00:30 local, with
  a deterministic daily jitter) is computed in the timezone derived from the
  device's CURRENT GPS coordinates (the operator moves accounts around), with
  the GeeLark-reported timezone as fallback.
- Message-driven cadence: a check = start phone -> one capture/reply run ->
  stop phone. Finding a customer message resets the interval to the tier
  minimum and keeps the phone on in a "hot session" loop while the
  conversation is live; silence backs the interval off exponentially.
- Activity tiers (HOT/WARM/COLD/DORMANT) bound the backoff, and DORMANT
  phones get one daily right-swipe session to generate new matches.
- Expiry guard: "Conversation expires in N hours" rows create deadlines the
  scheduler will never back off past.

Usage:
  .venv/bin/python bumble_scheduler.py --env-file .env --serial-nos 6637,6629
  .venv/bin/python bumble_scheduler.py --env-file .env --serial-nos 6637 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import geelark_multimodal_bot as bot

try:
    from timezonefinder import TimezoneFinder

    _TZF = TimezoneFinder()
except ImportError:  # pragma: no cover - optional dependency
    _TZF = None

LOGGER = bot.LOGGER

STATE_PATH = Path(__file__).resolve().parent / "scheduler_state.json"
RUNS_ROOT = Path(__file__).resolve().parent / "diagnostics" / "scheduler_runs"

TIER_INTERVALS = {
    # seconds: (minimum interval, maximum backoff)
    "HOT": (12 * 60, 20 * 60),
    "WARM": (30 * 60, 60 * 60),
    "COLD": (2 * 3600, 4 * 3600),
    "DORMANT": (8 * 3600, 14 * 3600),
}
BACKOFF_FACTOR = 1.7
HOT_SESSION_RESCAN_SECONDS = (180, 300)
HOT_SESSION_EXTEND_SECONDS = 20 * 60
HOT_SESSION_MAX_SECONDS = 2 * 3600
HOT_SESSION_QUIET_ROUNDS = 3
ACTIVE_WINDOW_START = (8, 30)
ACTIVE_WINDOW_END = (0, 30)  # past midnight
WINDOW_JITTER_MINUTES = 40
EXPIRY_BUFFER_SECONDS = 90 * 60
DORMANT_SWIPE_COUNT = 12


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.warning("Scheduler state file was unreadable; starting fresh.")
    return {"phones": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _phone_state(state: dict, phone_id: str) -> dict:
    return state["phones"].setdefault(
        phone_id,
        {
            "serial_no": "",
            "serial_name": "",
            "tier": "WARM",
            "interval_seconds": TIER_INTERVALS["WARM"][0],
            "next_check_at": _utcnow().isoformat(),
            "last_message_at": None,
            "last_match_at": None,
            "last_check_at": None,
            "last_swipe_date": None,
            "expiry_deadline": None,
            "timezone": "",
            "coords": None,
        },
    )


# ---------------------------------------------------------------------------
# Timezone from live device coordinates
# ---------------------------------------------------------------------------

_LOCATION_RE = re.compile(r"Location\[\w+ (-?\d+\.\d+),(-?\d+\.\d+)")


def _read_device_coords(api: "bot.GeelarkOpenAPIClient", phone_id: str) -> tuple[float, float] | None:
    """Read the phone's current GPS fix over a throwaway adb connection."""

    try:
        data = api.post("/open/v1/adb/getData", {"ids": [phone_id]})
        item = ((data.get("data") or {}).get("items") or [{}])[0]
        serial = f"{item.get('ip')}:{item.get('port')}"
        adb = bot.Config().ADB_PATH
        subprocess.run([adb, "connect", serial], capture_output=True, timeout=20)
        subprocess.run(
            [adb, "-s", serial, "shell", "glogin", str(item.get("pwd") or "")],
            capture_output=True,
            timeout=20,
        )
        out = subprocess.run(
            [adb, "-s", serial, "shell", "dumpsys", "location"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        match = _LOCATION_RE.search(out)
        if match:
            return float(match.group(1)), float(match.group(2))
    except Exception as exc:
        LOGGER.debug("Could not read device coords for %s: %s", phone_id, exc)
    return None


def _refresh_timezone(
    api: "bot.GeelarkOpenAPIClient",
    phone_id: str,
    pstate: dict,
    *,
    phone_running: bool,
    api_timezone: str = "",
) -> None:
    """Update pstate['timezone'] from live GPS coords when possible."""

    if phone_running and _TZF is not None:
        coords = _read_device_coords(api, phone_id)
        if coords:
            tz_name = _TZF.timezone_at(lat=coords[0], lng=coords[1])
            if tz_name:
                if tz_name != pstate.get("timezone"):
                    LOGGER.info(
                        "Phone %s timezone -> %s (GPS %.4f,%.4f).",
                        pstate.get("serial_no") or phone_id,
                        tz_name,
                        coords[0],
                        coords[1],
                    )
                pstate["timezone"] = tz_name
                pstate["coords"] = list(coords)
                return
    if not pstate.get("timezone") and api_timezone:
        pstate["timezone"] = api_timezone


def _tzinfo(pstate: dict) -> ZoneInfo:
    try:
        return ZoneInfo(pstate.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Persona clock
# ---------------------------------------------------------------------------


def _daily_jitter_minutes(phone_id: str, local_date: dt.date, salt: str) -> int:
    seed = hashlib.sha256(f"{phone_id}:{local_date.isoformat()}:{salt}".encode()).digest()
    return int.from_bytes(seed[:2], "big") % (2 * WINDOW_JITTER_MINUTES + 1) - WINDOW_JITTER_MINUTES


def active_window(pstate: dict, phone_id: str, ref: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The [wake, sleep) window containing or following `ref` (UTC in/out)."""

    tz = _tzinfo(pstate)
    local = ref.astimezone(tz)
    for day_offset in (-1, 0, 1):
        day = (local + dt.timedelta(days=day_offset)).date()
        wake = dt.datetime.combine(
            day, dt.time(*ACTIVE_WINDOW_START), tzinfo=tz
        ) + dt.timedelta(minutes=_daily_jitter_minutes(phone_id, day, "wake"))
        sleep = dt.datetime.combine(
            day + dt.timedelta(days=1), dt.time(*ACTIVE_WINDOW_END), tzinfo=tz
        ) + dt.timedelta(minutes=_daily_jitter_minutes(phone_id, day, "sleep"))
        if ref < sleep.astimezone(dt.timezone.utc):
            return wake.astimezone(dt.timezone.utc), sleep.astimezone(dt.timezone.utc)
    raise RuntimeError("active window computation failed")


def clamp_into_window(pstate: dict, phone_id: str, when: dt.datetime) -> dt.datetime:
    wake, sleep = active_window(pstate, phone_id, when)
    if when < wake:
        return wake + dt.timedelta(minutes=random.uniform(0, 25))
    if when >= sleep:
        next_wake, _ = active_window(pstate, phone_id, sleep + dt.timedelta(hours=1))
        return next_wake + dt.timedelta(minutes=random.uniform(0, 25))
    return when


# ---------------------------------------------------------------------------
# Tiering & result parsing
# ---------------------------------------------------------------------------


def classify_tier(pstate: dict, now: dt.datetime) -> str:
    def age_hours(key: str) -> float:
        value = pstate.get(key)
        if not value:
            return float("inf")
        return (now - dt.datetime.fromisoformat(value)).total_seconds() / 3600

    msg_age = age_hours("last_message_at")
    match_age = age_hours("last_match_at")
    if msg_age <= 24:
        return "HOT"
    if msg_age <= 72 or match_age <= 72:
        return "WARM"
    if msg_age <= 7 * 24 or match_age <= 7 * 24:
        return "COLD"
    return "DORMANT"


_EXPIRES_RE = re.compile(r"expires? in (\d+) hour", re.IGNORECASE)


def parse_run_summary(summary_path: Path) -> dict:
    result = {
        "ok": False,
        "messages_replied": 0,
        "openers_sent": 0,
        "new_matches": 0,
        "nearest_expiry_hours": None,
    }
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    result["ok"] = data.get("status") not in (None, "failed")
    for chat in data.get("chats") or []:
        if (chat.get("reply") or {}).get("status") == "sent":
            result["messages_replied"] += 1
    for opener in data.get("match_openers") or []:
        if opener.get("status") == "opener_sent":
            result["openers_sent"] += 1
            result["new_matches"] += 1
    blob = json.dumps(data, ensure_ascii=False)
    hours = [int(h) for h in _EXPIRES_RE.findall(blob)]
    if hours:
        result["nearest_expiry_hours"] = min(hours)
    return result


# ---------------------------------------------------------------------------
# Run execution
# ---------------------------------------------------------------------------


def _run_bot(args: list[str], *, timeout: float = 1200) -> int:
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "geelark_multimodal_bot.py"), *args]
    LOGGER.info("Scheduler exec: %s", " ".join(shlex.quote(a) for a in cmd[2:]))
    try:
        proc = subprocess.run(cmd, timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        LOGGER.warning("Scheduler run timed out: %s", args[:3])
        return -1


def _reply_run(phone_id: str, env_file: str, label: str, *, keep_running: bool) -> dict:
    out_dir = RUNS_ROOT / phone_id / f"{label}_{bot._timestamp_for_path()}"
    args = [
        "bumble-capture-chat",
        "--env-file", env_file,
        "--profile-id", phone_id,
        "--prepare-adb",
        "--send-ai-replies",
        "--max-chats", "4",
        "--profile-scrolls", "2",
        "--economy-mode",
        "--output-dir", str(out_dir),
    ]
    if keep_running:
        args.append("--keep-phone-running")
    _run_bot(args)
    return parse_run_summary(out_dir / "chat_capture_summary.json")


def _swipe_run(phone_id: str, env_file: str) -> None:
    out_dir = RUNS_ROOT / phone_id / f"swipe_{bot._timestamp_for_path()}"
    _run_bot(
        [
            "bumble-right-swipe",
            "--env-file", env_file,
            "--profile-id", phone_id,
            "--prepare-adb",
            "--max-count", str(DORMANT_SWIPE_COUNT),
            "--output-dir", str(out_dir),
        ],
        timeout=2400,
    )


def _stop_phone(api: "bot.GeelarkOpenAPIClient", phone_id: str) -> None:
    try:
        api.stop_phone([phone_id])
        LOGGER.info("Scheduler stopped phone %s.", phone_id)
    except Exception as exc:
        LOGGER.warning("Scheduler could not stop phone %s: %s", phone_id, exc)


def process_phone(
    api: "bot.GeelarkOpenAPIClient",
    state: dict,
    phone_id: str,
    env_file: str,
) -> None:
    pstate = _phone_state(state, phone_id)
    now = _utcnow()
    label = pstate.get("serial_no") or phone_id

    # Daily swipe for dormant accounts rides in front of the check.
    tz = _tzinfo(pstate)
    local_today = now.astimezone(tz).date().isoformat()
    if (
        pstate.get("tier") == "DORMANT"
        and pstate.get("last_swipe_date") != local_today
    ):
        LOGGER.info("[%s] DORMANT revival swipe session.", label)
        _swipe_run(phone_id, env_file)
        pstate["last_swipe_date"] = local_today
        _save_state(state)

    # Hot-session loop: keep the phone on while the conversation is live.
    session_started = _utcnow()
    quiet_rounds = 0
    hot_until = session_started
    total_messages = 0
    round_idx = 0
    while True:
        round_idx += 1
        keep = True
        result = _reply_run(
            phone_id, env_file, f"check_r{round_idx}", keep_running=keep
        )
        now = _utcnow()
        pstate["last_check_at"] = now.isoformat()
        _refresh_timezone(api, phone_id, pstate, phone_running=True)
        if result["messages_replied"] > 0:
            total_messages += result["messages_replied"]
            pstate["last_message_at"] = now.isoformat()
            quiet_rounds = 0
            hot_until = min(
                now + dt.timedelta(seconds=HOT_SESSION_EXTEND_SECONDS),
                session_started + dt.timedelta(seconds=HOT_SESSION_MAX_SECONDS),
            )
            LOGGER.info(
                "[%s] replied to %s message(s); hot session until %s.",
                label,
                result["messages_replied"],
                hot_until.astimezone(tz).strftime("%H:%M"),
            )
        else:
            quiet_rounds += 1
        if result["new_matches"] > 0:
            pstate["last_match_at"] = now.isoformat()
        if result["nearest_expiry_hours"] is not None:
            deadline = now + dt.timedelta(
                hours=result["nearest_expiry_hours"]
            ) - dt.timedelta(seconds=EXPIRY_BUFFER_SECONDS)
            pstate["expiry_deadline"] = deadline.isoformat()
        _save_state(state)
        if total_messages == 0 or quiet_rounds >= HOT_SESSION_QUIET_ROUNDS or now >= hot_until:
            break
        time.sleep(random.uniform(*HOT_SESSION_RESCAN_SECONDS))

    _stop_phone(api, phone_id)

    # Schedule the next check.
    now = _utcnow()
    tier = classify_tier(pstate, now)
    min_iv, max_iv = TIER_INTERVALS[tier]
    if total_messages > 0:
        interval = min_iv
    else:
        interval = min(
            max(float(pstate.get("interval_seconds") or min_iv) * BACKOFF_FACTOR, min_iv),
            max_iv,
        )
    pstate["tier"] = tier
    pstate["interval_seconds"] = int(interval)
    next_check = now + dt.timedelta(seconds=interval)
    deadline_raw = pstate.get("expiry_deadline")
    if deadline_raw:
        deadline = dt.datetime.fromisoformat(deadline_raw)
        if deadline > now:
            next_check = min(next_check, deadline)
        else:
            pstate["expiry_deadline"] = None
    next_check = clamp_into_window(pstate, phone_id, next_check)
    pstate["next_check_at"] = next_check.isoformat()
    _save_state(state)
    LOGGER.info(
        "[%s] tier=%s replied=%s next check %s (%s local).",
        label,
        tier,
        total_messages,
        next_check.isoformat(timespec="minutes"),
        next_check.astimezone(tz).strftime("%m-%d %H:%M"),
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _resolve_phones(api: "bot.GeelarkOpenAPIClient", serial_nos: list[str], profile_ids: list[str]) -> list[dict]:
    wanted_serials = {s.strip() for s in serial_nos if s.strip()}
    wanted_ids = {p.strip() for p in profile_ids if p.strip()}
    found: list[dict] = []
    page = 1
    while True:
        data = api.phone_list(page=page, page_size=100)
        payload = data.get("data") or {}
        items = payload.get("items") or []
        for item in items:
            if str(item.get("serialNo")) in wanted_serials or str(item.get("id")) in wanted_ids:
                found.append(item)
        if page * 100 >= int(payload.get("total") or 0) or not items:
            break
        page += 1
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--serial-nos", default="", help="comma-separated GeeLark serialNo list, e.g. 6637,6629")
    parser.add_argument("--profile-ids", default="", help="comma-separated profile id list")
    parser.add_argument("--dry-run", action="store_true", help="print schedule decisions without starting phones")
    args = parser.parse_args()

    bot.load_env_file(Path(args.env_file)) if args.env_file else None
    api = bot.GeelarkOpenAPIClient(bot.Config())
    phones = _resolve_phones(api, args.serial_nos.split(","), args.profile_ids.split(","))
    if not phones:
        raise SystemExit("No phones matched --serial-nos/--profile-ids.")

    state = _load_state()
    for item in phones:
        pstate = _phone_state(state, str(item["id"]))
        pstate["serial_no"] = str(item.get("serialNo") or "")
        pstate["serial_name"] = str(item.get("serialName") or "")
        _refresh_timezone(
            api,
            str(item["id"]),
            pstate,
            phone_running=int(item.get("status") or 2) == 0,
            api_timezone=str(((item.get("equipmentInfo") or {}).get("timeZone")) or ""),
        )
    _save_state(state)

    if args.dry_run:
        now = _utcnow()
        for item in phones:
            phone_id = str(item["id"])
            pstate = _phone_state(state, phone_id)
            tz = _tzinfo(pstate)
            wake, sleep = active_window(pstate, phone_id, now)
            tier = classify_tier(pstate, now)
            print(
                f"{pstate['serial_no']} {pstate['serial_name']}: tz={pstate['timezone'] or 'UTC'} "
                f"tier={tier} window(local)={wake.astimezone(tz).strftime('%H:%M')}-"
                f"{sleep.astimezone(tz).strftime('%H:%M')} "
                f"next={dt.datetime.fromisoformat(pstate['next_check_at']).astimezone(tz).strftime('%m-%d %H:%M')} "
                f"interval={int(pstate['interval_seconds'])//60}min"
            )
        return

    LOGGER.info("Scheduler managing %s phone(s): %s", len(phones), [p.get("serialName") for p in phones])
    phone_ids = [str(p["id"]) for p in phones]
    while True:
        now = _utcnow()
        due = [
            pid
            for pid in phone_ids
            if dt.datetime.fromisoformat(_phone_state(state, pid)["next_check_at"]) <= now
        ]
        for pid in due:
            in_window_now = True
            pstate = _phone_state(state, pid)
            wake, sleep = active_window(pstate, pid, now)
            if not (wake <= now < sleep):
                pstate["next_check_at"] = clamp_into_window(pstate, pid, now).isoformat()
                _save_state(state)
                in_window_now = False
            if in_window_now:
                try:
                    process_phone(api, state, pid, args.env_file)
                except Exception:
                    LOGGER.exception("Scheduler check failed for %s.", pid)
                    pstate["next_check_at"] = (
                        _utcnow() + dt.timedelta(minutes=20)
                    ).isoformat()
                    _save_state(state)
        nexts = [
            dt.datetime.fromisoformat(_phone_state(state, pid)["next_check_at"])
            for pid in phone_ids
        ]
        wait = max(30.0, min((min(nexts) - _utcnow()).total_seconds(), 600.0))
        time.sleep(wait)


if __name__ == "__main__":
    main()
