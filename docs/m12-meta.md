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

## What happens

- **Instagram:** a Reels container is created with the caption from M9, the video is uploaded directly, Meta processes it (usually under a minute), then it is published once.
  If the process dies right after publishing, the next check sees the container as published and never publishes it again.
  "Also share to feed" is on.
- **Facebook:** the Reel is uploaded to the Page and published with its description, or kept as a draft in private mode; the queue follows it until Facebook reports it published.
- **Permission:** a clip of a streamer whose `permission` is `unknown` never goes public: on Facebook it becomes a draft; Instagram has no private posts, so it gets an export package for you to decide.
- **Limits:** Instagram allows 100 API posts per account per day, Facebook 30 Reels per Page per day; Peakreel's own `daily_cap` (10) stays well below.
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
