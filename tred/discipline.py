"""UTC-day execution discipline shared by paper execution and replay."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile


def day_state(state: dict, now: datetime) -> dict:
    day = now.astimezone(timezone.utc).date().isoformat()
    if state.get("day") != day:
        state.clear()
        state.update(day=day, consecutive_losses=0, halted_reason=None)
    return state


def record_outcome(state: dict, pnl, now: datetime, maximum: int = 2) -> None:
    day_state(state, now)
    value = Decimal(str(pnl))
    if not value.is_finite() or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("Invalid realized outcome or consecutive-loss limit")
    # A break-even is not a win: it does not erase a developing loss streak.
    if value < 0:
        state["consecutive_losses"] += 1
    elif value > 0:
        state["consecutive_losses"] = 0
    if state["consecutive_losses"] >= maximum:
        state["halted_reason"] = state["halted_reason"] or "consecutive_loss_limit"


def halt_path(state_path) -> Path:
    return Path(str(state_path) + ".halt.json")


def request_halt(state_path, reason: str, now: datetime | None = None) -> dict:
    """Publish a manual mistake halt without modifying the runner-owned state."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("A mistake reason of 1–500 characters is required")
    now = now or datetime.now(timezone.utc)
    request = {"day": now.astimezone(timezone.utc).date().isoformat(),
               "reason": reason.strip(), "requested_at": now.isoformat()}
    path = halt_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".halt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(request, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return request


def apply_halt_request(state: dict, state_path, now: datetime) -> None:
    day_state(state, now)
    path = halt_path(state_path)
    if not path.exists():
        return
    if path.stat().st_size > 4096:
        raise ValueError("Invalid oversized halt request")
    request = json.loads(path.read_text())
    if request.get("day") == state["day"]:
        state["halted_reason"] = "execution_mistake"
        state["mistake_reason"] = request["reason"]
