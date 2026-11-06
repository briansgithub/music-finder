"""Global pause for audio clip downloads until seed cleanup is finalized."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import config

FLAG_PATH = config.DATA_DIR / "downloads_paused.json"


def is_paused() -> bool:
    if not FLAG_PATH.exists():
        return False
    try:
        data = json.loads(FLAG_PATH.read_text(encoding="utf-8"))
        return bool(data.get("paused"))
    except Exception:
        return False


def pause(reason: str = "seed cleanup not finalized") -> dict[str, Any]:
    FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "paused": True,
        "reason": reason,
        "paused_at": time.time(),
        "resume": "Unset via: ./run.sh downloads resume   OR delete data/downloads_paused.json",
    }
    FLAG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def resume() -> dict[str, Any]:
    payload = {
        "paused": False,
        "resumed_at": time.time(),
        "resume": "downloads enabled",
    }
    FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLAG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def status() -> dict[str, Any]:
    if not FLAG_PATH.exists():
        return {"paused": False, "flag": str(FLAG_PATH)}
    try:
        data = json.loads(FLAG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"paused": False, "error": str(e), "flag": str(FLAG_PATH)}
    data["flag"] = str(FLAG_PATH)
    return data


def blocked_result(op: str = "download") -> dict[str, Any]:
    st = status()
    return {
        "ok": False,
        "error": "downloads_paused",
        "op": op,
        "reason": st.get("reason") or "downloads paused",
        "resume": st.get("resume") or "./run.sh downloads resume",
        "retryable": False,
    }
