"""Secure ER:LC event receiver for Render.

Set DISCORD_WEBHOOK_URL in Render, then configure this app's /erlc/events
address in the ER:LC private-server Event Webhook setting.
"""

import base64
import json
import os
from datetime import datetime, timezone

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from flask import Flask, Response, jsonify, request


app = Flask(__name__)

# ER:LC's official Ed25519 public key (SPKI DER, base64 encoded).
ERLC_PUBLIC_KEY = (
    "MCowBQYDK2VwAyEAjSICb9pp0kHizGQtdG8ySWsDChfGqi+gyFCttigBNOA="
)
PUBLIC_KEY = serialization.load_der_public_key(base64.b64decode(ERLC_PUBLIC_KEY))
ERLC_SERVER_URL = "https://api.erlc.gg/v2/server"


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


def event_embed(record: dict) -> dict:
    event_type = find_value(record, "event", "type", "eventType", "event_type", default="ER:LC Event")
    details = record.get("data") if isinstance(record.get("data"), dict) else record

    if event_type == "EmergencyCallStarted":
        description = find_value(details, "description", default="No description supplied.")
        location = find_value(details, "positionDescriptor", "location", default="Unknown location")
        team = find_value(details, "team", default="Unknown")
        call_number = find_value(details, "callNumber", default="Unknown")
        return {
            "title": "ER:LC Emergency Call",
            "color": 0xE67E22,
            "fields": [
                {"name": "Call", "value": f"#{call_number}", "inline": True},
                {"name": "Department", "value": team[:1024], "inline": True},
                {"name": "Location", "value": location[:1024], "inline": False},
                {"name": "Details", "value": description[:1024], "inline": False},
            ],
            "footer": {"text": "ER:LC Event Webhook"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

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
        }
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
    }


def discord_payload(data: dict) -> dict:
    return {
        "username": "Brisbane Roleplay - ER:LC",
        "embeds": [event_embed(record) for record in event_records(data)[:10]],
    }


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
        result = requests.post(webhook_url, json=discord_payload(event), timeout=10)
        result.raise_for_status()
    except requests.RequestException:
        app.logger.exception("Unable to post ER:LC event to Discord.")
        return jsonify(error="Unable to deliver the event to Discord."), 502

    return Response(status=204)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
