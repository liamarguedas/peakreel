# M12: Instagram Reels and Facebook Reels publishing

Approved clips are posted as Reels to the Instagram account and the Facebook Page of their posting account (one of each per language, see M9), through one Meta app.
Videos are uploaded directly from this computer to Meta; no public hosting is needed.
Because the Pages and Instagram accounts are our own, Meta's **Standard Access** is enough: no App Review, no Business Verification (see `docs/platforms.md`).

Threads is not covered yet: its API only accepts a video at a public URL (decided: later).

## One-time setup

### 1. Accounts (per language)

1. A **Facebook Page** per language (en, es, pt, ja).
2. An Instagram **professional** account (Creator or Business) per language.
3. Link each Instagram account to its Page: Meta Accounts Center, or the Page's settings, "Linked accounts", Instagram.
   The person who will sign in must be an admin of the Pages.

### 2. Meta app

This is what worked on 2026-11-17 (the dashboard's names change often).

1. At https://developers.facebook.com, My Apps, **Create app**, with the use cases **Manage everything on your Page** and **Manage messaging & content on Instagram**.
   No business portfolio is needed.
2. **Use cases**, each one, **Customize**: **Add** these permissions until each shows "Ready for testing".
   For Instagram choose **API setup with Facebook login** (not Instagram login: that route cannot upload a video file, only fetch it from a public URL).
   Permissions: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`, `business_management`.
   A permission the app has not enabled is dropped silently at login; `meta login` names any that are missing.
3. **Facebook Login, Settings**: add **`http://localhost:53682/`** to *Valid OAuth Redirect URIs*.
4. **App settings, Basic**: privacy policy `https://liamarguedas.github.io/peakreel/privacy.html`, terms `https://liamarguedas.github.io/peakreel/terms.html`, user data deletion `https://liamarguedas.github.io/peakreel/data-deletion.html`; note the **App ID** and **App secret**.
5. Leave the *configuration id* empty in `meta setup`: the login then asks for the permissions above by name.
   A Facebook Login for Business configuration grants only the permissions ticked in it, and an incomplete one is easy to miss.
6. You are the app's admin, so your own Pages and Instagram accounts work at Standard Access while the app is in Development mode, without App Review.
   **To check with the first post:** that a post made while the app is in Development mode is visible to everyone (open it logged out).
   If it is not, switch the app to Live.

### 3. Connect Peakreel

`meta setup` asks for the secret, so run it in a terminal (not through `!` in Claude Code):

```
uv run peakreel meta setup                                   # app id, secret; configuration id empty
uv run peakreel meta login --account en --page "Sode.moments"  # Page name, any case
uv run peakreel meta status                                  # each account's Page and Instagram account
```

In the Facebook dialog allow the Pages **and** the Instagram accounts; if you logged in before, use *Edit access*.
If `meta login` reports a Page without Instagram, link the Instagram account to the Page (Page settings, Linked accounts, Instagram) and log in again.

The Page token Peakreel stores does not expire (it comes from a long-lived sign-in); it stops working only if you change your Facebook password, remove the app, or lose admin rights on the Page.
Then `meta login` again.

New Facebook accounts can only create a few Pages at a time; if Facebook refuses, try again a day later.

### 4. Turn it on

```yaml
publish:
  modes:
    instagram: public
    facebook: public        # or private: the Reel stays a draft on the Page
```

## Instagram without a Facebook Page (Instagram Login)

Facebook lets a person create only a few Pages a day (about one every 24 hours on 2026-11-19), and the route above needs one Page per Instagram account.
An Instagram account can instead sign in with **Instagram Login** (`peakreel/publish/instagram_login.py`): no Page, Instagram only (Facebook Reels still need a Page).
An account with a Page that has an Instagram account keeps using the Page route.

How it posts: this route cannot take an upload, so Instagram fetches the video from a link.
While a clip is posted, the review app serves it at `<public_url>/ig/v/<random>.mp4` through the Tailscale Funnel; the link is unguessable (an HMAC of the clip and account) and the file is removed once Instagram has processed the container (or failed), any left over after a day.
The login (a 60-day token) is renewed at least weekly. The limit is the same: 100 posts per account in 24 hours.

One-time setup:

1. Meta app: the use case **Manage messaging & content on Instagram**, **API setup with Instagram login**.
   Add the permissions `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_insights`.
   Under *Set up Instagram business login*, add the redirect URL `<public_url>/ig/callback` (`peakreel instagram setup` prints it), and note the **Instagram app ID** and **Instagram app secret** shown there (not the Facebook app's).
2. Funnel: forward `/ig` to the review app, next to the Kick webhook on the same port:
   ```
   tailscale funnel --bg --https=8443 --set-path=/ig http://127.0.0.1:8765/ig
   ```
   and set `publish.public_url` to the Funnel base (`https://<machine>.<tailnet>.ts.net:8443`).
3. `uv run peakreel instagram setup` (app id and secret, stored in the credential store).

Per Instagram account (no cooldown):

1. A professional (business or creator) Instagram account.
2. While the app is in development mode: Meta app, **App roles**, **Roles**, **Instagram testers**, add the account; accept the invite in Instagram (Settings, Apps and websites, Tester invites).
3. `uv run peakreel instagram login --account ja`: log in to that Instagram account in the browser that opens.
   The review page finishes the login (`/ig/callback`) and shows the result; the command waits for it and prints the username.
4. `uv run peakreel meta status` lists it as `@name (Instagram Login, no Facebook Page)`.

## What happens

- **Instagram:** a Reels container is created with the caption from M9, the video is uploaded directly, Meta processes it (usually under a minute), then it is published once.
  If the process dies right after publishing, the next check sees the container as published and never publishes it again.
  "Also share to feed" is on.
- **Facebook:** the Reel is uploaded to the Page and published with its description, or kept as a draft in private mode; the queue follows it until Facebook reports it published.
- **Permission:** a clip of a streamer whose `permission` is `unknown` never goes public: on Facebook it becomes a draft; Instagram has no private posts, so it gets an export package for you to decide.
- **Limits:** Instagram allows 100 API posts per account in a rolling 24 hours, Facebook 30 Reels per Page per day; Peakreel's own `daily_cap` (10 by default) stays below.
  Facebook also blocks bursts long before its cap (error 368, 2026-11-19: a Page blocked after 16 Reels in an hour); Peakreel pauses the Page 6 hours when it happens.
  A rate-limit answer waits an hour without using up the retries.
- **Removal:** Peakreel never deletes a post by itself; a takedown lists posts to remove by hand.

## Errors you may see

| Error | Fix |
| --- | --- |
| `no Facebook Page connected for account 'en'` | `peakreel meta login --account en` |
| `... has no Instagram account linked` | Link the Instagram account to the Page, then `meta login` again |
| `Meta authorization expired or was revoked` | `meta login` again (password change, app removed, admin rights lost) |
| `no instagram publisher for public mode` | `peakreel meta setup` |
| `Instagram could not process the video: ...` | Meta's reason (format or length); the clip stays in review |

## Views for the performance log (`peakreel stats`)

Peakreel reads each post's numbers 12, 24 and 72 hours after it went live (`peakreel/publish/stats.py`).
With the permissions above, Instagram and Facebook give likes and comments but not views.
For views:

1. In the Meta app, under **Use cases**, add `instagram_manage_insights` (Instagram) and `read_insights` (Facebook Pages).
2. Sign in again for each account with `--insights`: `uv run peakreel meta login --account <en|es|pt|ja> --page "<Page name>" --insights`.

Until then, views are stored as empty (never guessed); YouTube views work with the existing sign-in.
