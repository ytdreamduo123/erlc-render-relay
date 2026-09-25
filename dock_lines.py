"""Paced, cached Dock reverse lookups for dispatch mentions."""
import os
import threading
import time
from collections import deque
import requests

_cache = {}
_lock = threading.Lock()
_next_request = 0.0
_requests = deque()


def discord_ids(roblox_id: str, *, deadline: float | None = None) -> list[str]:
    global _next_request
    token = os.getenv("DOCK_API_KEY", "").strip()
    guild_id = os.getenv("DISCORD_GUILD_ID", "1515128511206002859").strip()
    if not token or not roblox_id.isdigit():
        return []
    key = (guild_id, roblox_id)
    remaining = max(0, deadline - time.monotonic()) if deadline is not None else 2
    if not _lock.acquire(timeout=remaining):
        return []
    try:
        now = time.monotonic()
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        while _requests and _requests[0] < now - 86400:
            _requests.popleft()
        # Reserve most Dock quota for the bot. No retry storms on relay events.
        if (deadline is not None and max(now, _next_request) >= deadline) or len(_requests) >= 500 or _next_request - now > 2:
            return []
        if _next_request > now:
            time.sleep(_next_request - now)
        _requests.append(time.monotonic())
        _next_request = time.monotonic() + 2
        try:
            response = requests.get(
                "https://api.docksys.xyz/api/v1/public/roblox-to-discord",
                headers={"Authorization": f"Bearer {token}"},
                params={"robloxId": roblox_id, "guildId": guild_id},
                timeout=max(0.05, min(2, (deadline - time.monotonic()) / 2)) if deadline is not None else 2,
            )
            if response.status_code == 429:
                try:
                    retry = max(2, float(response.headers.get("Retry-After", "60")))
                except (ValueError, TypeError):
                    retry = 60
                _next_request = time.monotonic() + retry
                return []
            if response.status_code != 200:
                _cache[key] = (time.monotonic() + 390, [])
                return []
            payload = response.json()
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            ids = data.get("discordIds", []) if isinstance(data, dict) and str(data.get("robloxId")) == roblox_id else []
            ids = [str(value) for value in ids if str(value).isdigit()] if isinstance(ids, list) else []
            _cache[key] = (time.monotonic() + (21600 if ids else 390), ids)
            return ids
        except (requests.RequestException, ValueError):
            _cache[key] = (time.monotonic() + 390, [])
            return []

    finally:
        _lock.release()


def member_label(player: dict, *, deadline: float | None = None) -> str:
    value = str(player.get("Player") or "Unknown")
    name, sep, rid = value.rpartition(":")
    if not sep or not rid.isdigit():
        return value.replace("@", "＠")
    ids = discord_ids(rid, deadline=deadline)
    # Multiple linked accounts are ambiguous: do not ping an arbitrary member.
    return f"<@{ids[0]}>" if len(ids) == 1 else name.replace("@", "＠")
