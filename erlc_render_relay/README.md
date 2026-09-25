# ER:LC Render Relay

This is a small, separate web service. It receives signed ER:LC event webhooks
and posts them to a Discord webhook. It does not need your Discord bot token.

## Deploy to Render

1. Upload the relay files: `app.py`, `dock_links.py`, `requirements.txt`,
   `erlc_map.png`, and `brisbane_logo.png`. Include `render.yaml` if using a Render Blueprint.
2. In Render, choose **New +** then **Web Service**, and connect that repository.
3. Use these values:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
4. In **Environment**, add `DISCORD_WEBHOOK_URL` with a Discord webhook URL
   created in the channel where ER:LC logs should go. Also add `ERLC_SERVER_KEY`
   with your ER:LC private-server API key; it is required to identify callers
   when the webhook does not contain their user ID. Add `DOCK_API_KEY` for mentions.
5. Deploy. When it says **Live**, copy the service URL and add `/erlc/events`.
   Example: `https://brisbane-erlc-relay.onrender.com/erlc/events`
6. In ER:LC private-server settings, find **Event Webhook** and paste that full
   URL. ER:LC will validate the signature endpoint before it saves.

## Create the Discord webhook

Open the target Discord channel: **Edit Channel** → **Integrations** →
**Webhooks** → **New Webhook** → **Copy Webhook URL**.

Keep the Discord webhook URL private, just like a bot token.

## Dock account links for 000 calls

Deploy `dock_links.py` alongside `app.py`. Set `DOCK_API_KEY` on the Render
service and `DISCORD_GUILD_ID` to the server ID (defaults to Brisbane).
Nearby units and callers with one Dock-linked Discord account render as Discord
mentions. Missing or ambiguous links retain the Roblox name. Mentions display
without sending notification pings.

Lookups are paced, successful links are cached for six hours, and missing links
are cached for 390 seconds. The relay observes Dock's Retry-After response and
caps background lookups at 500 per rolling day per process. Use one worker to
share this in-memory cache and budget. The bot and relay are separate services;
Dock's actual quota and responses remain authoritative across both hosts.

The bot checks ER:LC players every 30 seconds using one shared snapshot for
nickname and voice-location updates. Nickname changes use linked IDs and are
limited to one change per member per 390 seconds, with two seconds between
edits. Team changes apply on the next eligible check. The bot requires Manage
Nicknames and a role above the members it renames. Voice status retains its
existing 70-second edit cooldown and uses name matching only as a fallback.

## A 000 call did not arrive

GitHub stores the code. Runtime logs are in **Render dashboard → the relay web
service → Logs**. After uploading changes, use **Manual Deploy → Deploy latest
commit** if automatic deployment is not enabled.

Check the Render environment includes `DISCORD_WEBHOOK_URL` and
`ERLC_SERVER_KEY`. `DOCK_API_KEY` adds member mentions but is optional for sending
the call. A missing `dock_links.py` now falls back to Roblox names rather than
preventing the relay from starting.

The relay logs why a caller could not be identified and returns HTTP 503 rather
than silently acknowledging that undelivered call. Confirmed NPC calls are
still ignored. A known caller ID is sufficient to send a call even if the Roblox
username lookup fails. The first card uses cached Dock links only; a single background worker resolves
all listed units and updates the delivered message with confirmed mentions.
Temporary lookup failures are retried on later calls after a short cooldown,
rather than being treated as missing links for 390 seconds. Map rendering
failures fall back to a text card.

## Bundled Dock lookup update

Dock lookup code is now included directly in `app.py`; `dock_links.py` is an
optional compatibility wrapper. After deployment, startup logs must show
`000 relay build: bundled-dock-v1; Dock key configured=True` and your guild ID.
If they show False, set `DOCK_API_KEY` in this Render service's environment.
New calls print the Dock lookup result and whether the Discord message edit
succeeded. Keys and webhook URLs are never printed in these diagnostic lines.
