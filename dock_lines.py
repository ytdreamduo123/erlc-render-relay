"""Paced, cached Dock reverse lookups for dispatch mentions."""
import os
import os
import logging
import threading
import time
from collections import deque
import requests

_cache = {}
_lock = threading.Lock()
_next_request = 0.0
_requests = deque()
_requests = deque()
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s dock_links: %(message)s"))
    LOGGER.addHandler(handler)
LOGGER.propagate = False


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
        if cached and cached[0] > now:
            LOGGER.info("Dock cached lookup: Roblox %s in guild %s has %s linked account(s).", roblox_id, guild_id, len(cached[1]))
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
            if response.status_code != 200:
                LOGGER.warning("Dock lookup for Roblox %s in guild %s returned HTTP %s.", roblox_id, guild_id, response.status_code)
                _cache[key] = (time.monotonic() + (390 if response.status_code == 404 else 15), [])
                return []
            payload = response.json()
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            if not isinstance(data, dict) or str(data.get("robloxId")) != roblox_id or not isinstance(data.get("discordIds"), list):
                LOGGER.warning("Dock returned an unexpected response for Roblox %s; expected matching robloxId and a discordIds list.", roblox_id)
                _cache[key] = (time.monotonic() + 15, [])
                return []
            ids = data.get("discordIds", []) if isinstance(data, dict) and str(data.get("robloxId")) == roblox_id else []
            ids = [str(value) for value in ids if str(value).isdigit()] if isinstance(ids, list) else []
            ids = data["discordIds"]
            ids = [str(value) for value in ids if str(value).isdigit()] if isinstance(ids, list) else []
            ids = list(dict.fromkeys(ids))
            LOGGER.info("Dock lookup for Roblox %s in guild %s returned %s linked Discord account(s).", roblox_id, guild_id, len(ids))
            _cache[key] = (time.monotonic() + (21600 if ids else 390), ids)
            return ids
        except (requests.RequestException, ValueError):
            _cache[key] = (time.monotonic() + 390, [])
        except (requests.RequestException, ValueError):
            LOGGER.warning("Dock lookup for Roblox %s failed temporarily.", roblox_id)
            _cache[key] = (time.monotonic() + 15, [])
            return []

    finally:
        _lock.release()


def member_label(player: dict, *, deadline: float | None = None) -> str:
    value = str(player.get("Player") or "Unknown")
    name, sep, rid = value.rpartition(":")
    if not sep or not rid.isdigit():
        return value.replace("@", "＠")
    ids = discord_ids(rid, deadline=deadline)
    name, sep, rid = value.rpartition(":")
    name, rid = name.strip(), rid.strip()
    if not sep or not rid.isdigit():
        LOGGER.warning("Cannot resolve a dispatch mention: ER:LC player value has no numeric Roblox ID.")
        return value.replace("@", "＠")
    ids = discord_ids(rid, deadline=deadline)
    if len(ids) > 1:
        LOGGER.warning("Roblox %s has multiple linked Discord accounts; no unique mention can be selected.", rid)
    # Multiple linked accounts are ambiguous: do not ping an arbitrary member.
    return f"<@{ids[0]}>" if len(ids) == 1 else name.replace("@", "＠")
