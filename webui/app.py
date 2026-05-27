"""Local Flask control panel for the GeeLark Bumble auto-reply bot.

Run:
    pip install flask
    python3 -m webui.app          # or: python3 webui/app.py

Then open http://127.0.0.1:8765 .

The UI reuses Config / GeelarkOpenAPIClient from geelark_multimodal_bot.py for
phone status + start/stop, and reads diagnostics/ for past runs.

It can also launch a single-phone capture as a subprocess for ad-hoc testing.
For the long-running parallel monitor, manage it from the launcher script
shown in SESSION_NOTES.md.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Reuse the bot's GeeLark client and config so we share auth + endpoints.
from geelark_multimodal_bot import (  # noqa: E402
    Config,
    GeelarkOpenAPIClient,
    list_geelark_phones,
    load_env_file,
)

ENV_FILE = PROJECT_ROOT / ".env"
if ENV_FILE.exists():
    load_env_file(ENV_FILE, override=False)

CONFIG = Config()
API = GeelarkOpenAPIClient(CONFIG)
DIAG_ROOT = PROJECT_ROOT / "diagnostics"

# Default group from the project — overridable per request.
DEFAULT_GROUP_ID = os.getenv("WEBUI_GROUP_ID", "601002110892376134")

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)


# ---------- in-memory state for ad-hoc capture jobs ----------

# Map of profile_id -> dict(pid, started_at, log_path, status)
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

# Soft cache so the dashboard can keep rendering when GeeLark is slow / down.
PHONES_CACHE: dict[str, dict[str, Any]] = {}  # group_id -> {phones, ts, status_ts}
PHONES_CACHE_LOCK = threading.Lock()
PHONES_CACHE_TTL = 8.0  # seconds — refresh GeeLark at most this often
PHONES_API_BUDGET = 8.0  # seconds — give up the upstream call past this point
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="bbchat-webui")


def _call_with_timeout(fn, args=(), kwargs=None, timeout: float = 8.0):
    """Run fn(*args, **kwargs) but raise TimeoutError if it doesn't return in time.

    Note: this doesn't cancel the underlying call; it just stops blocking the
    HTTP request. The runaway upstream call still runs to completion in the
    background, but the API will return quickly.
    """
    fut = _EXECUTOR.submit(fn, *args, **(kwargs or {}))
    return fut.result(timeout=timeout)


def _register_job(profile_id: str, popen: subprocess.Popen, log_path: Path) -> None:
    with JOBS_LOCK:
        JOBS[profile_id] = {
            "pid": popen.pid,
            "started_at": time.time(),
            "log_path": str(log_path),
            "status": "running",
        }


def _refresh_jobs() -> None:
    """Mark finished jobs based on PID liveness."""
    with JOBS_LOCK:
        for pid_key, info in list(JOBS.items()):
            pid = info["pid"]
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                info["status"] = "finished"
            except PermissionError:
                pass


# ---------- GeeLark helpers ----------


def _safe_phone_status(ids: list[str], timeout: float = PHONES_API_BUDGET) -> dict[str, int]:
    """Return {profile_id: status_int}. Empty if API is unreachable / slow."""
    if not ids:
        return {}
    try:
        resp = _call_with_timeout(API.phone_status, args=(ids,), timeout=timeout)
    except concurrent.futures.TimeoutError:
        app.logger.warning("phone_status timed out after %ss", timeout)
        return {}
    except Exception as exc:
        app.logger.warning("phone_status failed: %s", exc)
        return {}
    data = (resp.get("data") or {}).get("successDetails") or []
    return {str(d.get("id")): int(d.get("status", -1)) for d in data}


def _list_group_phones(group_id: str) -> dict[str, Any]:
    """Return {phones, cache_age, upstream_ok} with soft caching."""
    with PHONES_CACHE_LOCK:
        cached = PHONES_CACHE.get(group_id)
    now = time.time()
    if cached and (now - cached.get("ts", 0)) < PHONES_CACHE_TTL:
        return {
            "phones": cached["phones"],
            "cache_age": round(now - cached["ts"], 2),
            "upstream_ok": cached.get("upstream_ok", True),
        }

    # Fetch the phone list (cheap if we have a cached list — we still refresh status).
    upstream_ok = True
    base_list: list[dict[str, Any]] = []
    if cached and (now - cached.get("list_ts", 0)) < 60.0:
        # Reuse the structural list within a minute, only re-poll status.
        base_list = cached["phones_raw"]
    else:
        try:
            all_phones = _call_with_timeout(list_geelark_phones, args=(CONFIG,), timeout=PHONES_API_BUDGET)
            base_list = [
                {
                    "id": str(p.get("id")),
                    "serialNo": p.get("serialNo"),
                    "serialName": p.get("serialName"),
                    "remark": p.get("remark"),
                    "group": p.get("group", {}),
                }
                for p in all_phones
                if str((p.get("group") or {}).get("id")) == str(group_id)
            ]
        except concurrent.futures.TimeoutError:
            app.logger.warning("list_geelark_phones timed out")
            upstream_ok = False
        except Exception as exc:
            app.logger.warning("list_geelark_phones failed: %s", exc)
            upstream_ok = False
        if not base_list and cached:
            base_list = cached.get("phones_raw", [])

    status_map = _safe_phone_status([p["id"] for p in base_list]) if base_list else {}
    if not status_map and cached:
        # Reuse last-known statuses if the API call failed.
        prior = {p["id"]: p.get("status", -1) for p in cached.get("phones", [])}
        status_map = prior
        upstream_ok = upstream_ok and False

    phones = []
    for p in base_list:
        phones.append({**p, "status": status_map.get(p["id"], -1)})
    phones.sort(key=lambda x: (x.get("serialNo") or "", x["id"]))

    with PHONES_CACHE_LOCK:
        PHONES_CACHE[group_id] = {
            "phones": phones,
            "phones_raw": base_list,
            "ts": now,
            "list_ts": now if base_list else (cached or {}).get("list_ts", 0),
            "upstream_ok": upstream_ok,
        }
    return {"phones": phones, "cache_age": 0, "upstream_ok": upstream_ok}


# ---------- diagnostics filesystem helpers ----------


def _iter_dirs(phone_dir: Path) -> list[Path]:
    if not phone_dir.exists():
        return []
    return sorted(
        [p for p in phone_dir.iterdir() if p.is_dir() and p.name.startswith("iter_")],
        key=lambda p: p.name,
        reverse=True,
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def _phone_recent_iters(profile_id: str, serial: str = "", name: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Find recent iter dirs across any long_run_parallel_* tree for this phone."""
    out: list[dict[str, Any]] = []
    if not DIAG_ROOT.exists():
        return out
    for parallel_root in sorted(DIAG_ROOT.glob("long_run_parallel_*"), reverse=True):
        # Each parallel_root contains directories like 5429__kimmarin__612641464983224616
        for phone_dir in parallel_root.iterdir():
            if not phone_dir.is_dir():
                continue
            if profile_id not in phone_dir.name:
                continue
            for iter_dir in _iter_dirs(phone_dir)[:limit]:
                summary = _load_json(iter_dir / "chat_capture_summary.json") or {}
                out.append(
                    {
                        "iter": iter_dir.name,
                        "path": str(iter_dir.relative_to(PROJECT_ROOT)),
                        "status": summary.get("status"),
                        "opened_chats": summary.get("opened_chats", 0),
                        "captured_at": summary.get("captured_at"),
                        "updated_at": summary.get("updated_at"),
                        "candidate_count": len(summary.get("chat_candidates") or []),
                        "candidates": [
                            c.get("label") for c in (summary.get("chat_candidates") or [])
                        ][:5],
                    }
                )
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    out.sort(key=lambda x: x.get("captured_at") or "", reverse=True)
    return out[:limit]


def _phone_recent_replies(profile_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """Pull successful AI reply records for this phone from any long_run dir."""
    out: list[dict[str, Any]] = []
    if not DIAG_ROOT.exists():
        return out
    for parallel_root in sorted(DIAG_ROOT.glob("long_run_parallel_*"), reverse=True):
        for phone_dir in parallel_root.iterdir():
            if not phone_dir.is_dir() or profile_id not in phone_dir.name:
                continue
            for iter_dir in _iter_dirs(phone_dir):
                summary = _load_json(iter_dir / "chat_capture_summary.json") or {}
                for chat in summary.get("chats") or []:
                    reply = (chat.get("reply") or {})
                    if reply.get("status") != "sent":
                        continue
                    ai = reply.get("ai") or {}
                    out.append(
                        {
                            "iter": iter_dir.name,
                            "chat_title": chat.get("chat_title"),
                            "reply": ai.get("reply"),
                            "heat_score": ai.get("heat_score"),
                            "reason": ai.get("reason"),
                            "captured_at": summary.get("captured_at"),
                        }
                    )
                    if len(out) >= limit:
                        return out
    return out


def _list_all_run_roots() -> list[dict[str, Any]]:
    """All known run roots under diagnostics/, with light metadata."""
    if not DIAG_ROOT.exists():
        return []
    runs: list[dict[str, Any]] = []
    for child in DIAG_ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            mtime = 0
        runs.append(
            {
                "name": child.name,
                "modified": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "_mtime": mtime,
            }
        )
    runs.sort(key=lambda r: r.get("_mtime", 0), reverse=True)
    for r in runs:
        r.pop("_mtime", None)
    return runs[:50]


# ---------- routes ----------


@app.route("/")
def index():
    return render_template("dashboard.html", default_group_id=DEFAULT_GROUP_ID)


@app.route("/phone/<profile_id>")
def phone_detail(profile_id: str):
    return render_template(
        "phone.html",
        profile_id=profile_id,
        default_group_id=DEFAULT_GROUP_ID,
    )


@app.route("/api/phones")
def api_phones():
    group_id = request.args.get("group_id", DEFAULT_GROUP_ID)
    info = _list_group_phones(group_id)
    _refresh_jobs()
    with JOBS_LOCK:
        jobs_snapshot = {k: dict(v) for k, v in JOBS.items()}
    return jsonify(
        {
            "group_id": group_id,
            "phones": info["phones"],
            "jobs": jobs_snapshot,
            "cache_age": info.get("cache_age", 0),
            "upstream_ok": info.get("upstream_ok", True),
        }
    )


@app.route("/api/phone/<profile_id>/start", methods=["POST"])
def api_start_phone(profile_id: str):
    try:
        resp = API.start_phone([profile_id])
        return jsonify({"ok": True, "resp": resp})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/phone/<profile_id>/stop", methods=["POST"])
def api_stop_phone(profile_id: str):
    try:
        resp = API.stop_phone([profile_id])
        return jsonify({"ok": True, "resp": resp})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/phone/<profile_id>/capture", methods=["POST"])
def api_run_capture(profile_id: str):
    """Launch one bumble-capture-chat run as a subprocess."""
    options = request.get_json(silent=True) or {}
    max_chats = int(options.get("max_chats", 3))
    send_ai = bool(options.get("send_ai_replies", True))
    keep_running = bool(options.get("keep_phone_running", True))
    out_root = DIAG_ROOT / "webui_ad_hoc" / profile_id
    out_root.mkdir(parents=True, exist_ok=True)
    iter_dir = out_root / f"iter_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    log_path = iter_dir / "run.log"

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "geelark_multimodal_bot.py"),
        "bumble-capture-chat",
        "--env-file",
        str(ENV_FILE),
        "--profile-id",
        profile_id,
        "--prepare-adb",
        "--api-shell",
        "--max-chats",
        str(max_chats),
        "--chat-scrolls",
        "0",
        "--profile-scrolls",
        "1",
        "--capture-self-profile-for-ai",
        "--economy-mode",
        "--output-dir",
        str(iter_dir.relative_to(PROJECT_ROOT)),
    ]
    if send_ai:
        cmd.append("--send-ai-replies")
    if keep_running:
        cmd.append("--keep-phone-running")

    log_file = open(log_path, "wb")
    popen = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    _register_job(profile_id, popen, log_path)
    return jsonify(
        {
            "ok": True,
            "pid": popen.pid,
            "log_path": str(log_path.relative_to(PROJECT_ROOT)),
            "iter_dir": str(iter_dir.relative_to(PROJECT_ROOT)),
        }
    )


@app.route("/api/phone/<profile_id>/job/stop", methods=["POST"])
def api_stop_job(profile_id: str):
    with JOBS_LOCK:
        info = JOBS.get(profile_id)
    if not info:
        return jsonify({"ok": False, "error": "no tracked job"}), 404
    try:
        os.kill(info["pid"], signal.SIGTERM)
        return jsonify({"ok": True})
    except ProcessLookupError:
        info["status"] = "finished"
        return jsonify({"ok": True, "note": "already gone"})


@app.route("/api/phone/<profile_id>/recent")
def api_phone_recent(profile_id: str):
    return jsonify(
        {
            "iters": _phone_recent_iters(profile_id),
            "replies": _phone_recent_replies(profile_id),
        }
    )


@app.route("/api/phone/<profile_id>/log")
def api_phone_log(profile_id: str):
    with JOBS_LOCK:
        info = JOBS.get(profile_id)
    if not info:
        return jsonify({"log": "", "status": "no job"})
    path = Path(info["log_path"])
    if not path.exists():
        return jsonify({"log": "", "status": info.get("status")})
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return jsonify({"log": "", "error": str(exc)}), 500
    tail = text[-8000:]
    return jsonify({"log": tail, "status": info.get("status")})


@app.route("/api/phone/<profile_id>/chat-list-latest")
def api_phone_chat_list_latest(profile_id: str):
    """Return the latest chat_list_initial.txt content for this phone."""
    if not DIAG_ROOT.exists():
        return jsonify({"text": ""})
    candidates: list[tuple[float, Path]] = []
    for parallel_root in DIAG_ROOT.glob("long_run_parallel_*"):
        for phone_dir in parallel_root.iterdir():
            if not phone_dir.is_dir() or profile_id not in phone_dir.name:
                continue
            for iter_dir in _iter_dirs(phone_dir):
                txt = iter_dir / "chat_list" / "chat_list_initial.txt"
                if txt.exists():
                    candidates.append((txt.stat().st_mtime, txt))
                    break
    for adhoc_root in (DIAG_ROOT / "webui_ad_hoc").glob(f"{profile_id}/*"):
        txt = adhoc_root / "chat_list" / "chat_list_initial.txt"
        if txt.exists():
            candidates.append((txt.stat().st_mtime, txt))
    if not candidates:
        return jsonify({"text": ""})
    candidates.sort(reverse=True)
    _, latest = candidates[0]
    return jsonify(
        {
            "text": latest.read_text(encoding="utf-8", errors="replace"),
            "source": str(latest.relative_to(PROJECT_ROOT)),
        }
    )


@app.route("/api/runs")
def api_runs():
    return jsonify({"runs": _list_all_run_roots()})


@app.route("/api/health")
def api_health():
    return jsonify(
        {
            "ok": True,
            "now": datetime.now(timezone.utc).isoformat(),
            "diag_root": str(DIAG_ROOT),
            "diag_exists": DIAG_ROOT.exists(),
        }
    )


if __name__ == "__main__":
    host = os.getenv("WEBUI_HOST", "127.0.0.1")
    port = int(os.getenv("WEBUI_PORT", "8765"))
    app.run(host=host, port=port, debug=False)
