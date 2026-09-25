"""Offline regression tests: python -m unittest test_delivery.py."""
import json
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import requests
import app as relay
import app as dock_links


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.client = relay.app.test_client()
        self.enterContext(patch.dict(relay.os.environ, {
            "DISCORD_WEBHOOK_URL": "https://example.com/webhook",
            "ERLC_SERVER_KEY": "", "DOCK_API_KEY": "",
        }))
        self.enterContext(patch.object(relay, "PUBLIC_KEY", self.key.public_key()))
        self.enterContext(patch.object(relay, "server_players", return_value=[]))

    def post(self, data):
        body = json.dumps({"events": [{"type": "EmergencyCallStarted",
            "timestamp": "2026-09-25T12:00:00Z", "data": data}]}).encode()
        timestamp = str(int(time.time()))
        signature = self.key.sign(timestamp.encode() + body).hex()
        return self.client.post("/erlc/events", data=body, headers={
            "X-Signature-Timestamp": timestamp, "X-Signature-Ed25519": signature,
        })

    def test_known_id_delivered_when_roblox_is_down(self):
        with patch.object(relay.requests, "get", side_effect=requests.Timeout), patch.object(relay.requests, "post", return_value=Mock(ok=True)) as post:
            response = self.post({"caller": 42, "callNumber": 7, "description": "Help"})
        self.assertEqual(response.status_code, 204)
        self.assertIn("Roblox user 42", str(post.call_args.kwargs["json"]))
        self.assertEqual(post.call_count, 1)

    def test_missing_server_key_is_not_silently_dropped(self):
        with patch.object(relay.requests, "post") as post:
            response = self.post({"callNumber": 7})
        self.assertEqual(response.status_code, 503)
        self.assertIn("ERLC_SERVER_KEY", response.json["error"])
        post.assert_not_called()

    def test_confirmed_npc_is_skipped(self):
        result = Mock(status_code=200)
        result.json.return_value = {"EmergencyCalls": [{"CallNumber": 7, "Caller": None}]}
        with patch.dict(relay.os.environ, {"ERLC_SERVER_KEY": "test"}), patch.object(relay.requests, "get", return_value=result), patch.object(relay.requests, "post") as post:
            response = self.post({"callNumber": 7})
        self.assertEqual(response.status_code, 204)
        post.assert_not_called()

    def test_map_failure_still_sends_text(self):
        with patch.object(relay, "visible_map_units", side_effect=OSError("bad map")), patch.object(relay, "emergency_map", side_effect=OSError("bad map")), patch.object(relay.requests, "post", return_value=Mock(ok=True)) as post:
            response = self.post({"caller": "Example", "position": [1, 2]})
        self.assertEqual(response.status_code, 204)
        self.assertIn("json", post.call_args.kwargs)

    def test_invalid_signature_does_not_send(self):
        with patch.object(relay.requests, "post") as post:
            response = self.client.post("/erlc/events", json={})
        self.assertEqual(response.status_code, 401)
        post.assert_not_called()

    def test_exhausted_dock_budget_does_not_block(self):
        with patch.dict(dock_links.os.environ, {"DOCK_API_KEY": "test"}), patch.object(dock_links.requests, "get") as get:
            self.assertEqual(dock_links.discord_ids("99999", deadline=time.monotonic()-1), [])
        get.assert_not_called()

    def test_incompatible_dock_helper_falls_back(self):
        with patch.object(relay, "member_label", side_effect=TypeError("older helper")):
            self.assertEqual(relay.dispatch_member_label({"Player": "Example:42"}, time.monotonic()+3), "Example")

    def test_background_update_resolves_every_unit_and_keeps_attachment(self):
        players = [{"Player": f"Unit{i}:{40+i}"} for i in range(5)]
        text = "**Nearby Units:**\n" + "\n".join(f"Unit{i} - Postal 305" for i in range(5))
        payload = {"components": [{"type": 17, "components": [{"type": 10, "content": text}]}]}
        message = {"id": "123", "attachments": [{"id": "456", "filename": "map.jpg"}]}
        with patch.dict(relay.os.environ, {"DOCK_API_KEY": "test"}), patch.object(relay, "dispatch_member_label", side_effect=[f"<@{100+i}>" for i in range(5)]) as label, patch.object(relay, "DOCK_UPDATE_SLOTS") as slots, patch.object(relay.requests, "patch", return_value=Mock(ok=True)) as edit:
            relay.update_dispatch_mentions("https://example.com/webhook", message, payload, players)
        self.assertEqual(label.call_count, 5)
        result = edit.call_args.kwargs["json"]
        self.assertIn("<@104> - Postal 305", str(result))
        self.assertEqual(result["attachments"], message["attachments"])
        self.assertIn("Unit4 - Postal 305", str(payload))
        slots.release.assert_called_once()

    def test_temporary_dock_error_is_not_cached_for_390_seconds(self):
        with patch.dict(dock_links.os.environ, {"DOCK_API_KEY": "test"}), patch.object(dock_links, "_cache", {}), patch.object(dock_links, "_next_request", 0), patch.object(dock_links.requests, "get", side_effect=requests.Timeout):
            self.assertEqual(dock_links.discord_ids("7654321"), [])
            expiry, ids = next(iter(dock_links._cache.values()))
            self.assertLessEqual(expiry - time.monotonic(), 15)

    def test_real_lookup_parser_updates_dreamduo_card(self):
        response = Mock(status_code=200)
        response.json.return_value = {"data": {"robloxId": "42", "discordIds": ["123456789012345678"]}}
        payload = {"components": [{"type": 17, "components": [{"type": 10,
            "content": "**Nearby Units:**\nDreamDuoAU - Postal 305"}]}]}
        with patch.dict(relay.os.environ, {"DOCK_API_KEY": "test"}), patch.object(relay, "_cache", {}), patch.object(relay, "_next_request", 0), patch.object(relay, "DOCK_UPDATE_SLOTS"), patch.object(relay.requests, "get", return_value=response) as lookup, patch.object(relay.requests, "patch", return_value=Mock(ok=True)) as edit:
            relay.update_dispatch_mentions("https://example.com/webhook", {"id": "789"}, payload, [{"Player": "DreamDuoAU:42"}])
        self.assertEqual(lookup.call_args.kwargs["params"]["robloxId"], "42")
        self.assertIn("<@123456789012345678> - Postal 305", str(edit.call_args.kwargs["json"]))


if __name__ == "__main__":
    unittest.main()
