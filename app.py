"""Secure ER:LC event receiver for Render.

Set DISCORD_WEBHOOK_URL in Render, then configure this app's /erlc/events
address in the ER:LC private-server Event Webhook setting.
"""

import base64
import io
import json
import math
import os
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
# The map's visible land area inside the supplied 1600px image.
MAP_BOUNDS = (48, 110, 1555, 1498)
# These can be refined in Render without changing code if ER:LC adjusts its map.
WORLD_X_MIN = float(os.getenv("ERLC_MAP_X_MIN", "0"))
WORLD_X_MAX = float(os.getenv("ERLC_MAP_X_MAX", "4096"))
WORLD_Z_MIN = float(os.getenv("ERLC_MAP_Z_MIN", "0"))
WORLD_Z_MAX = float(os.getenv("ERLC_MAP_Z_MAX", "4096"))


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


def location_coordinates(location: dict) -> tuple[float, float] | None:
    try:
        return float(location["LocationX"]), float(location["LocationZ"])
    except (KeyError, TypeError, ValueError):
        return None


def map_point(world_x: float, world_z: float) -> tuple[int, int]:
    """Convert ER:LC world coordinates into pixels on the supplied map."""
    left, top, right, bottom = MAP_BOUNDS
    x_ratio = (world_x - WORLD_X_MIN) / (WORLD_X_MAX - WORLD_X_MIN)
    z_ratio = (world_z - WORLD_Z_MIN) / (WORLD_Z_MAX - WORLD_Z_MIN)
    return (
        round(left + max(0.0, min(1.0, x_ratio)) * (right - left)),
        round(bottom - max(0.0, min(1.0, z_ratio)) * (bottom - top)),
    )


def nearby_police(players: list[dict], call_x: float, call_z: float) -> list[tuple[float, dict]]:
    units: list[tuple[float, dict]] = []
    for player in players:
        team = str(player.get("Team") or "").casefold()
        if not any(name in team for name in ("police", "sheriff", "state")):
            continue
        coordinates = location_coordinates(player.get("Location") or {})
        if coordinates is None:
            continue
        distance = math.hypot(coordinates[0] - call_x, coordinates[1] - call_z)
        units.append((distance, player))
    return sorted(units, key=lambda item: item[0])[:5]


def emergency_map(call_x: float, call_z: float, units: list[tuple[float, dict]]) -> io.BytesIO | None:
    """Create a cropped call map with a red caller pin and blue police pins."""
    if not MAP_FILE.is_file():
        return None
    with Image.open(MAP_FILE) as original:
        image = original.convert("RGB")
    call_point = map_point(call_x, call_z)
    crop_size = 620
    half = crop_size // 2
    # Keep the crop inside the actual ER:LC map, not the white transparent
    # border around the supplied map PNG.
    map_left, map_top, map_right, map_bottom = MAP_BOUNDS
    left = max(map_left, min(map_right - crop_size, call_point[0] - half))
    top = max(map_top, min(map_bottom - crop_size, call_point[1] - half))
    cropped = image.crop((left, top, left + crop_size, top + crop_size)).resize((900, 900), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(cropped)
    scale = 900 / crop_size

    def marker(point: tuple[int, int], colour: str, radius: int) -> None:
        x, y = (point[0] - left) * scale, (point[1] - top) * scale
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=colour, outline="white", width=4)

    for _, unit in units:
        coordinates = location_coordinates(unit.get("Location") or {})
        if coordinates:
            marker(map_point(*coordinates), "#2878F0", 12)
    marker(call_point, "#E53935", 18)
    draw.text((24, 24), "Emergency Call", fill="white", stroke_width=3, stroke_fill="black", font=ImageFont.load_default())

    output = io.BytesIO()
    cropped.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def event_embed(record: dict, players: list[dict]) -> tuple[dict, io.BytesIO | None]:
    event_type = find_value(record, "event", "type", "eventType", "event_type", default="ER:LC Event")
    details = record.get("data") if isinstance(record.get("data"), dict) else record

    if event_type == "EmergencyCallStarted":
        description = find_value(details, "description", default="No description supplied.")
        location = find_value(details, "positionDescriptor", "location", default="Unknown location")
        team = find_value(details, "team", default="Unknown")
        call_number = find_value(details, "callNumber", default="Unknown")
        try:
            call_x, call_z = (float(value) for value in details.get("position", [])[:2])
        except (TypeError, ValueError):
            call_x = call_z = 0.0
        units = nearby_police(players, call_x, call_z) if call_x or call_z else []
        nearby_text = "\n".join(
            f"• **{player_display_name(unit)}** — {unit.get('Callsign') or 'No callsign'} ({distance:.0f}m)"
            for distance, unit in units
        ) or "No nearby police units found."
        embed = {
            "title": "ER:LC Emergency Call",
            "color": 0xE67E22,
            "fields": [
                {"name": "Call", "value": f"#{call_number}", "inline": True},
                {"name": "Department", "value": team[:1024], "inline": True},
                {"name": "Location", "value": location[:1024], "inline": False},
                {"name": "Details", "value": description[:1024], "inline": False},
                {"name": "Nearby Police", "value": nearby_text[:1024], "inline": False},
            ],
            "footer": {"text": "ER:LC Event Webhook"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        map_image = emergency_map(call_x, call_z, units) if call_x or call_z else None
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


def discord_payload(data: dict) -> tuple[dict, io.BytesIO | None]:
    players = server_players()
    map_image: io.BytesIO | None = None
    embeds = []
    for record in event_records(data)[:10]:
        embed, rendered_map = event_embed(record, players)
        embeds.append(embed)
        map_image = map_image or rendered_map
    return {
        "username": "Brisbane Roleplay - ER:LC",
        "embeds": embeds,
    }, map_image


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
        app.logger.error("DISCORD_WEBHOOK_URL is not configured.")
        return jsonify(error="Discord webhook is not configured."), 503

    try:
        payload, map_image = discord_payload(event)
        if map_image:
            result = requests.post(
                webhook_url,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": ("erlc_emergency_map.png", map_image, "image/png")},
                timeout=15,
            )
        else:
            result = requests.post(webhook_url, json=payload, timeout=10)
        result.raise_for_status()
    except requests.RequestException:
        app.logger.exception("Unable to post ER:LC event to Discord.")
        return jsonify(error="Unable to deliver the event to Discord."), 502

    return Response(status=204)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
