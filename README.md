# ER:LC Render Relay

This is a small, separate web service. It receives signed ER:LC event webhooks
and posts them to a Discord webhook. It does not need your Discord bot token.

## Deploy to Render

1. Make a GitHub repository containing these four files.
2. In Render, choose **New +** then **Web Service**, and connect that repository.
3. Use these values:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
4. In **Environment**, add `DISCORD_WEBHOOK_URL` with a Discord webhook URL
   created in the channel where ER:LC logs should go.
5. Deploy. When it says **Live**, copy the service URL and add `/erlc/events`.
   Example: `https://brisbane-erlc-relay.onrender.com/erlc/events`
6. In ER:LC private-server settings, find **Event Webhook** and paste that full
   URL. ER:LC will validate the signature endpoint before it saves.

## Create the Discord webhook

Open the target Discord channel: **Edit Channel** → **Integrations** →
**Webhooks** → **New Webhook** → **Copy Webhook URL**.

Keep the Discord webhook URL private, just like a bot token.
