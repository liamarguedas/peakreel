# Kick

Peakreel records and clips Kick streamers the same way as Twitch ones: chat spikes find the moments, the video buffer provides the clip, and clips go through review and publishing unchanged.
Credits and links say `kick.com/<channel>`.

| Part | How |
| --- | --- |
| Video | Streamlink's Kick plugin. Kick streams are Amazon IVS like Twitch's, with a broadcast timestamp on every 2 s segment, so clip timing is exact (checked: a 20.000 s cut, 1080p, no gaps). |
| Chat | Kick's **official** event webhooks: Kick calls a public HTTPS address with each chat message, signed. Peakreel verifies every call (RSA signature with Kick's published key, recent timestamp, no repeats). |
| Live status, discovery | Kick's official API (app token). |

## One-time setup

### 1. Kick developer app

1. kick.com, Settings, **Developer**: create an app. Note the **Client ID** and **Client Secret**.
2. In the app, **Enable Webhooks** and set the **Webhook URL** to your tunnel address followed by `/kick/webhook` (step 2).
3. Store it in Peakreel (the token is checked):

```
uv run peakreel kick setup
```

### 2. A stable public address (tunnel)

Kick needs to reach the endpoint Peakreel serves on `http://127.0.0.1:8770/kick/webhook`.
The webhook URL is typed into Kick's settings, so the address must not change between runs.

- **ngrok** (simplest): a free account includes one fixed domain.
  `ngrok http --url=<your-name>.ngrok-free.app 8770`, then the webhook URL is `https://<your-name>.ngrok-free.app/kick/webhook`.
- **Cloudflare Tunnel** with a domain you own: `cloudflared tunnel create peakreel`, route a hostname to it, and point it at `http://localhost:8770`.
  (Quick tunnels without a domain get a new address at every start, so they do not fit.)

Only signed calls from Kick are accepted, so the endpoint is safe to expose; nothing else is served on it.
Keep the tunnel running whenever Peakreel runs: if Kick cannot deliver for about a day, it cancels the subscriptions (Peakreel makes new ones at every start).

Record the public address in the config so `kick status` can show the exact URL to paste:

```yaml
kick:
  public_url: https://<your-name>.ngrok-free.app
```

### 3. Check

```
uv run peakreel kick status
```

## Channels

By hand, with `platform: kick` (the name is the channel's slug as in its URL):

```yaml
channels:
  - name: westcol
    platform: kick
    language: es
    permission: credit_only
```

Inside Peakreel the channel is `kick-westcol` (folders, chat logs, review page), so it never clashes with a Twitch login.

By discovery, alongside Twitch:

```yaml
discover:
  enabled: true
  platforms: [twitch, kick]
  category: "Grand Theft Auto VI"     # Twitch's name
  kick_category: "Grand Theft Auto VI" # Kick's exact name (check with discover preview)
```

`uv run peakreel discover preview` lists both platforms' top streams together, most viewers first.

## Notes

- One Peakreel run per Kick app: at start it removes the app's old event subscriptions (left by a crash) so chat is never delivered twice.
- Kick chat arrives through Kick's servers and the tunnel, a little later than Twitch IRC; spikes are aligned to video with each message's own timestamp, as on Twitch.
- Subscriptions, gifted subscriptions and gifted Kicks count as on-screen alerts for the safety flags, like Twitch subs and cheers.
- Kick's API changes faster than Twitch's (its first livestreams endpoint was replaced within a year); if discovery or status stops working, `kick status` and the log show the API error.
