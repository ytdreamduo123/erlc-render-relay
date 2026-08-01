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


def first_value(data: dict, *names: str, default: str = "Unknown") -> str:
    """Read a useful value despite small event-payload field-name changes."""
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return str(value)
    return default


def discord_embed(data: dict) -> dict:
    event_type = first_value(data, "type", "eventType", "event_type", default="ER:LC Event")
    player = first_value(data, "playerName", "player", "username", "sender", "author")
    message = first_value(data, "message", "content", "text", "reason", default="No message supplied.")
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
                ],
                "footer": {"text": "ER:LC Event Webhook"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
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
        result = requests.post(webhook_url, json=discord_embed(event), timeout=10)
        result.raise_for_status()
    except requests.RequestException:
        app.logger.exception("Unable to post ER:LC event to Discord.")
        return jsonify(error="Unable to deliver the event to Discord."), 502

    return Response(status=204)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
