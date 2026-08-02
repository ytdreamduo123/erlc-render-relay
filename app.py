"""Secure ER:LC event receiver for Render.

Set DISCORD_WEBHOOK_URL in Render, then configure this app's /erlc/events
address in the ER:LC private-server Event Webhook setting.
"""

import base64
import io
import json
import math
import os
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from PIL import Image, ImageDraw, ImageFont

from flask import Flask, Response, jsonify, request


app = Flask(__name__)

# ER:LC's official Ed25519 public key (SPKI DER, base64 encoded).
ERLC_PUBLIC_KEY = (
    "MCowBQYDK2VwAyEAjSICb9pp0kHizGQtdG8ySWsDChfGqi+gyFCttigBNOA="
)
PUBLIC_KEY = serialization.load_der_public_key(base64.b64decode(ERLC_PUBLIC_KEY))
ERLC_SERVER_URL = "https://api.erlc.gg/v2/server"
MAP_FILE = Path(__file__).with_name("erlc_map.png")
# ER:LC's API uses a world origin in the middle of the map, so locations can
# have negative values.  The old 0..4096 range clamped those calls to a corner.
# These values can be adjusted in Render if the game map changes.
WORLD_X_MIN = float(os.getenv("ERLC_MAP_X_MIN", "-3000"))
WORLD_X_MAX = float(os.getenv("ERLC_MAP_X_MAX", "3000"))
WORLD_Z_MIN = float(os.getenv("ERLC_MAP_Z_MIN", "-3000"))
WORLD_Z_MAX = float(os.getenv("ERLC_MAP_Z_MAX", "3000"))


def map_bounds(image: Image.Image) -> tuple[int, int, int, int]:
    """Find the real map area and exclude the white border in source images."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    left, top, right, bottom = width, height, -1, -1

    # The supplied ER:LC map has a white background.  Sample every fourth pixel
    # to quickly locate the non-white island without making webhook delivery slow.
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            red, green, blue = pixels[x, y]
            if min(red, green, blue) < 235:
                left, top = min(left, x), min(top, y)
                right, bottom = max(right, x), max(bottom, y)

    if right <= left or bottom <= top:
        return (0, 0, width, height)
    return (max(0, left - 8), max(0, top - 8), min(width, right + 8), min(height, bottom + 8))


def verified_request() -> tuple[bool, bytes]:
    """Validate ER:LC's signature before processing the JSON body."""
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    signature_hex = request.headers.get("X-Signature-Ed25519", "")
    body = request.get_data(cache=True, as_text=False)

    if not timestamp or not signature_hex:
        return False, body

    try:
        signature = bytes.fromhex(signature_hex)
        PUBLIC_KEY.verify(signature, timestamp.encode("utf-8") + body)
        return True, body
    except (ValueError, InvalidSignature):
        return False, body


def find_value(data: object, *names: str, default: str = "Unknown") -> str:
    """Find event values across nested and differently-capitalised payloads."""
    wanted = {name.casefold() for name in names}

    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).casefold() in wanted and value not in (None, ""):
                if isinstance(value, dict):
                    return find_value(value, "username", "name", "displayname", default=default)
                return str(value)

        for value in data.values():
            result = find_value(value, *names, default="")
            if result:
                return result
    elif isinstance(data, list):
        for value in data:
            result = find_value(value, *names, default="")
            if result:
                return result

    return default


def discord_embed(data: dict) -> dict:
    event_type = find_value(data, "type", "eventType", "event_type", "event", default="ER:LC Event")
    player = find_value(
        data,
        "playerName",
        "player",
        "username",
        "sender",
        "author",
        "name",
        default="Unknown",
    )
    message = find_value(
        data,
        "message",
        "content",
        "text",
        "reason",
        "command",
        default="No message supplied.",
    )
    payload_preview = json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:950]
    return {
        "username": "Brisbane Roleplay • ER:LC",
        "embeds": [
            {
                "title": "ER:LC Event Received",
                "color": 0x2B6CB0,
                "fields": [
                    {"name": "Event", "value": event_type[:1024], "inline": True},
                    {"name": "Player", "value": player[:1024], "inline": True},
                    {"name": "Message", "value": message[:1024], "inline": False},
                    {
                        "name": "Event data (temporary)",
                        "value": f"```json\n{payload_preview}\n```",
                        "inline": False,
                    },
                ],
                "footer": {"text": "ER:LC Event Webhook"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def event_records(payload: dict) -> list[dict]:
    """ER:LC batches events in an `events` list; handle older single events too."""
    records = payload.get("events")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return [payload]


def is_emergency_call_event(record: dict) -> bool:
    """Return whether a webhook record is the start or end of an ER:LC call."""
    event_name = find_value(record, "event", "type", "eventType", "event_type", default="")
    return event_name in {"EmergencyCallStarted", "EmergencyCallEnded"}


def live_location(player: str) -> str:
    """Get the current in-game location for a named player, when configured."""
    server_key = os.getenv("ERLC_SERVER_KEY", "").strip()
    if not server_key or not player or player == "Unknown":
        return "Location unavailable."
    try:
        response = requests.get(
            ERLC_SERVER_URL,
            headers={"server-key": server_key},
            params={"Players": "true"},
            timeout=8,
        )
        response.raise_for_status()
        players = response.json().get("Players", [])
    except (requests.RequestException, ValueError, AttributeError):
        return "Location unavailable."

    wanted = player.casefold()
    for entry in players:
        if not isinstance(entry, dict):
            continue
        in_game_name = str(entry.get("Player") or "").rsplit(":", 1)[0]
        if in_game_name.casefold() != wanted:
            continue
        location = entry.get("Location") or {}
        postal = location.get("PostalCode") or "Unknown postal"
        street = " ".join(
            str(value) for value in (location.get("BuildingNumber"), location.get("StreetName")) if value
        ) or "Unknown street"
        return f"Postal: `{postal}`\nStreet: {street}"
    return "Player is not currently in the ER:LC server."


def server_players() -> list[dict]:
    """Fetch live ER:LC player data for location and nearby-unit details."""
    server_key = os.getenv("ERLC_SERVER_KEY", "").strip()
    if not server_key:
        return []
    try:
        response = requests.get(
            ERLC_SERVER_URL,
            headers={"server-key": server_key},
            params={"Players": "true"},
            timeout=8,
        )
        response.raise_for_status()
        players = response.json().get("Players", [])
        return [player for player in players if isinstance(player, dict)]
    except (requests.RequestException, ValueError, AttributeError):
        return []


def player_display_name(player: dict) -> str:
    return str(player.get("Player") or "Unknown").rsplit(":", 1)[0]


def emergency_caller_name(details: dict, players: list[dict]) -> str:
    """Resolve the caller ID from ER:LC's active-call data to a Roblox name."""
    supplied = find_value(details, "caller", "player", "playerName", "username", default="")
    if supplied and not supplied.isdigit():
        return supplied

    call_number = str(details.get("callNumber") or "")
    server_key = os.getenv("ERLC_SERVER_KEY", "").strip()
    if not server_key or not call_number:
        return "Anonymous caller"
    # The webhook can arrive a fraction of a second before ER:LC exposes the
    # same call through its server API. Retry briefly before treating it as an
    # automated call.
    for attempt in range(4):
        try:
            response = requests.get(
                ERLC_SERVER_URL,
                headers={"server-key": server_key},
                params={"EmergencyCalls": "true"},
                timeout=8,
            )
            response.raise_for_status()
            calls = response.json().get("EmergencyCalls", [])
        except (requests.RequestException, ValueError, AttributeError):
            calls = []

        for call in calls:
            if not isinstance(call, dict) or str(call.get("CallNumber") or "") != call_number:
                continue
            caller_id = str(call.get("Caller") or supplied or "")
            for player in players:
                if str(player.get("Player") or "").rsplit(":", 1)[-1] == caller_id:
                    return player_display_name(player)
            if caller_id.isdigit():
                try:
                    roblox_response = requests.get(
                        f"https://users.roblox.com/v1/users/{caller_id}", timeout=8
                    )
                    roblox_response.raise_for_status()
                    username = roblox_response.json().get("name")
                    if username:
                        return str(username)
                except (requests.RequestException, ValueError, AttributeError):
                    pass
        if attempt < 3:
            time.sleep(0.5)
    return "Anonymous caller"


def location_coordinates(location: dict) -> tuple[float, float] | None:
    try:
        return float(location["LocationX"]), float(location["LocationZ"])
    except (KeyError, TypeError, ValueError):
        return None


def map_point(world_x: float, world_z: float, bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    """Convert ER:LC world coordinates into pixels on the supplied map."""
    left, top, right, bottom = bounds
    x_ratio = (world_x - WORLD_X_MIN) / (WORLD_X_MAX - WORLD_X_MIN)
    z_ratio = (world_z - WORLD_Z_MIN) / (WORLD_Z_MAX - WORLD_Z_MIN)
    return (
        round(left + max(0.0, min(1.0, x_ratio)) * (right - left)),
        round(bottom - max(0.0, min(1.0, z_ratio)) * (bottom - top)),
    )


def dispatch_unit_filter(call_team: str) -> tuple[str, tuple[str, ...]]:
    """Choose the correct nearby responder group for an ER:LC call."""
    team = call_team.casefold()
    if "dot" in team:
        return "Nearby DOT", ("dot",)
    if any(word in team for word in ("fire", "medical", "ems", "rescue")):
        return "Nearby Fire & Rescue", ("fire", "rescue", "medical", "ems")
    if any(word in team for word in ("police", "sheriff", "state", "law")):
        return "Nearby Police", ("police", "sheriff", "state")
    return "Nearby Units", ("police", "sheriff", "state", "fire", "rescue", "medical", "ems", "dot")


def nearby_units(players: list[dict], call_x: float, call_z: float, call_team: str) -> list[tuple[float, dict]]:
    units: list[tuple[float, dict]] = []
    _, responder_keywords = dispatch_unit_filter(call_team)
    for player in players:
        team = str(player.get("Team") or "").casefold()
        if not any(name in team for name in responder_keywords):
            continue
        coordinates = location_coordinates(player.get("Location") or {})
        if coordinates is None:
            continue
        distance = math.hypot(coordinates[0] - call_x, coordinates[1] - call_z)
        units.append((distance, player))
    return sorted(units, key=lambda item: item[0])[:5]


def emergency_map(
    call_x: float, call_z: float, units: list[tuple[float, dict]], caller_name: str
) -> io.BytesIO | None:
    """Create a cropped call map with a labelled caller pin and unit pins."""
    if not MAP_FILE.is_file():
        return None
    with Image.open(MAP_FILE) as original:
        image = original.convert("RGB")
    visible_bounds = map_bounds(image)
    call_point = map_point(call_x, call_z, visible_bounds)
    crop_size = 620
    half = crop_size // 2
    # Keep the crop inside the actual ER:LC map, not its white border.  A map
    # can be smaller than the preferred crop, so never calculate a negative edge.
    map_left, map_top, map_right, map_bottom = visible_bounds
    crop_width = min(crop_size, map_right - map_left)
    crop_height = min(crop_size, map_bottom - map_top)
    left = max(map_left, min(map_right - crop_width, call_point[0] - crop_width // 2))
    top = max(map_top, min(map_bottom - crop_height, call_point[1] - crop_height // 2))
    cropped = image.crop((left, top, left + crop_width, top + crop_height)).resize((900, 900), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(cropped)
    scale_x = 900 / crop_width
    scale_y = 900 / crop_height

    def marker(point: tuple[int, int], colour: str, radius: int) -> None:
        x, y = (point[0] - left) * scale_x, (point[1] - top) * scale_y
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=colour, outline="white", width=4)

    for _, unit in units:
        coordinates = location_coordinates(unit.get("Location") or {})
        if coordinates:
            marker(map_point(*coordinates, visible_bounds), "#2878F0", 12)
    marker(call_point, "#E53935", 18)
    label = caller_name if caller_name and caller_name != "Anonymous caller" else "Emergency Call"
    label = label[:32]
    label_x = max(12, min(680, (call_point[0] - left) * scale_x + 24))
    label_y = max(12, min(860, (call_point[1] - top) * scale_y - 12))
    draw.text(
        (label_x, label_y),
        label,
        fill="white",
        stroke_width=3,
        stroke_fill="black",
        font=ImageFont.load_default(),
    )

    output = io.BytesIO()
    cropped.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def event_embed(record: dict, players: list[dict]) -> tuple[dict, io.BytesIO | None]:
    event_type = find_value(record, "event", "type", "eventType", "event_type", default="ER:LC Event")
    details = record.get("data") if isinstance(record.get("data"), dict) else record

    if is_emergency_call_event(record):
        description = find_value(details, "description", default="No description supplied.")
        location = find_value(details, "positionDescriptor", "location", default="Unknown location")
        team = find_value(details, "team", default="Unknown")
        call_number = find_value(details, "callNumber", default="Unknown")
        caller = emergency_caller_name(details, players)
        nearby_heading, _ = dispatch_unit_filter(team)
        try:
            call_x, call_z = (float(value) for value in details.get("position", [])[:2])
        except (TypeError, ValueError):
            call_x = call_z = 0.0
        units = nearby_units(players, call_x, call_z, team) if call_x or call_z else []
        nearby_text = "\n".join(
            f"• **{player_display_name(unit)}** — {unit.get('Callsign') or 'No callsign'} ({distance:.0f}m)"
            for distance, unit in units
        ) or f"No {nearby_heading.casefold()} found."
        embed = {
            "title": "ER:LC Emergency Call",
            "color": 0xE67E22,
            "fields": [
                {"name": "Call", "value": f"#{call_number}", "inline": True},
                {"name": "Department", "value": team[:1024], "inline": True},
                {"name": "Location", "value": location[:1024], "inline": False},
                {"name": "Details", "value": description[:1024], "inline": False},
                {"name": nearby_heading, "value": nearby_text[:1024], "inline": False},
            ],
            "footer": {"text": "ER:LC Event Webhook"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        map_image = emergency_map(call_x, call_z, units, caller) if call_x or call_z else None
        if map_image:
            embed["image"] = {"url": "attachment://erlc_emergency_map.png"}
        return embed, map_image

    player = find_value(
        details, "playerName", "player", "username", "sender", "author", "name", default="Unknown"
    )
    message = find_value(
        details, "message", "content", "text", "reason", "command", default="No message supplied."
    )
    if message.casefold().startswith(";000"):
        reason = message[4:].strip() or "No reason provided."
        return {
            "title": "ER:LC 000 Alert",
            "color": 0xD83C3E,
            "fields": [
                {"name": "Player", "value": player[:1024], "inline": True},
                {"name": "Reason", "value": reason[:1024], "inline": False},
                {"name": "Live Location", "value": live_location(player), "inline": False},
            ],
            "footer": {"text": "ER:LC Event Webhook"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, None
    if message == "No message supplied." and details:
        message = json.dumps(details, ensure_ascii=False, separators=(",", ":"))[:1000]
    return {
        "title": "ER:LC Event Received",
        "color": 0x2B6CB0,
        "fields": [
            {"name": "Event", "value": event_type[:1024], "inline": True},
            {"name": "Player", "value": player[:1024], "inline": True},
            {"name": "Message", "value": message[:1024], "inline": False},
        ],
        "footer": {"text": "ER:LC Event Webhook"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, None


def emergency_component_payload(record: dict, players: list[dict]) -> tuple[dict, io.BytesIO | None]:
    """Build a Components V2 dispatch card for a phone emergency call."""
    details = record.get("data") if isinstance(record.get("data"), dict) else {}
    event_name = find_value(record, "event", "type", "eventType", "event_type", default="")
    description = find_value(details, "description", default="No details provided.")
    location = find_value(details, "positionDescriptor", "location", default="Unknown location")
    team = find_value(details, "team", default="Emergency Services")
    call_number = find_value(details, "callNumber", default="Unknown")
    caller = emergency_caller_name(details, players)
    nearby_heading, _ = dispatch_unit_filter(team)
    try:
        call_x, call_z = (float(value) for value in details.get("position", [])[:2])
    except (TypeError, ValueError):
        call_x = call_z = 0.0
    units = nearby_units(players, call_x, call_z, team) if call_x or call_z else []
    unit_text = "\n".join(
        f"**{player_display_name(unit)}** — {unit.get('Callsign') or 'No callsign'} • {distance:.0f}m away"
        for distance, unit in units
    ) or f"*No {nearby_heading.casefold()} found.*"
    timestamp = int(record.get("timestamp") or datetime.now(timezone.utc).timestamp())
    map_image = emergency_map(call_x, call_z, units, caller) if call_x or call_z else None

    call_heading = "911 Call Closed" if event_name == "EmergencyCallEnded" else "911 Call Received"
    card_components = [
        {"type": 10, "content": f"## {call_heading}: {team}"},
        {
            "type": 10,
            "content": (
                f"**📞 Caller:** {caller}\n"
                f"**📋 Incident:** {description}\n"
                f"**📍 Location:** {location}\n"
                f"**🕒 Time:** <t:{timestamp}:F>"
            ),
        },
        {"type": 14, "spacing": 1, "divider": True},
        {"type": 10, "content": f"**🛡️ Closest Units**\n{unit_text}"},
        {"type": 14, "spacing": 1, "divider": True},
    ]
    if map_image:
        card_components.append(
            {
                "type": 12,
                "items": [{"media": {"url": "attachment://erlc_emergency_map.png"}}],
            }
        )
        card_components.append({"type": 14, "spacing": 1, "divider": True})
    card_components.append({"type": 10, "content": "-# Brisbane Roleplay • 911 Emergency Dispatch"})

    return {
        "username": "Brisbane Roleplay - ER:LC",
        "flags": 32768,
        "components": [{"type": 17, "components": card_components}],
    }, map_image


def discord_payload(data: dict) -> tuple[dict | None, io.BytesIO | None]:
    """Create a dispatch post only for a real player's newly opened 911 call."""
    players = server_players()
    records = event_records(data)[:10]
    for record in records:
        event_name = find_value(record, "event", "type", "eventType", "event_type", default="")
        if event_name != "EmergencyCallStarted":
            continue
        details = record.get("data") if isinstance(record.get("data"), dict) else {}
        caller = emergency_caller_name(details, players)
        if caller != "Anonymous caller":
            return emergency_component_payload(record, players)
        app.logger.info(
            "Ignored EmergencyCallStarted #%s because ER:LC did not provide a real caller ID.",
            details.get("callNumber", "unknown"),
        )
    return None, None


def post_to_discord(
    webhook_url: str,
    payload: dict,
    map_image: io.BytesIO | None,
) -> requests.Response:
    """Post Components V2 directly through the configured Discord webhook."""

    def send() -> requests.Response:
        if map_image:
            map_image.seek(0)
            return requests.post(
                webhook_url,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": ("erlc_emergency_map.png", map_image, "image/png")},
                timeout=15,
            )
        return requests.post(webhook_url, json=payload, timeout=10)

    last_response: requests.Response | None = None
    for attempt in range(3):
        response = send()
        last_response = response
        if response.ok or response.status_code < 500:
            return response
        app.logger.warning("Discord returned HTTP %s; retrying delivery (%s/3).", response.status_code, attempt + 1)
        time.sleep(attempt + 1)
    assert last_response is not None
    return last_response


@app.get("/")
def health_check():
    return jsonify(status="online", service="erlc-event-relay")


@app.post("/erlc/events")
def erlc_events():
    valid, body = verified_request()
    if not valid:
        return jsonify(error="Invalid ER:LC signature."), 401

    try:
        event = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify(error="Expected a JSON request body."), 400

    if not isinstance(event, dict):
        return jsonify(error="Expected a JSON object."), 400

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        app.logger.error("No Discord webhook configuration was found.")
        return jsonify(error="Discord delivery is not configured."), 503

    try:
        payload, map_image = discord_payload(event)
        if payload is None:
            # A webhook was valid but it was an automated ER:LC event, a closed
            # call, or a probe.  Accept it without posting anything to Discord.
            return Response(status=204)
        result = post_to_discord(webhook_url, payload, map_image)
        if not result.ok:
            app.logger.error(
                "Discord webhook rejected the payload (HTTP %s): %s",
                result.status_code,
                result.text[:2000],
            )
        result.raise_for_status()
    except requests.RequestException:
        app.logger.exception("Unable to post ER:LC event to Discord.")
        return jsonify(error="Unable to deliver the event to Discord."), 502

    return Response(status=204)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
