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

1. At https://developers.facebook.com, My Apps, **Create app**.
   Choose the use cases for managing a Page's content and Instagram content publishing (names change; the products are **Facebook Login** and **Instagram**, API with Facebook Login).
2. Facebook Login settings: add **`http://localhost:53682/`** to *Valid OAuth Redirect URIs*.
3. If the app uses **Facebook Login for Business**, create a configuration with the permissions below and note its **configuration id**.
   Permissions: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`, `business_management`.
4. App settings, Basic: note the **App ID** and **App secret**.
5. You are the app's admin, so your own Pages and Instagram accounts work at Standard Access while the app is in Development mode.
   **To check with the first post:** that a post made while the app is in Development mode is visible to everyone (open it logged out).
   If it is not, switch the app to Live (it asks for a privacy policy URL; see M13).

### 3. Connect Peakreel

```
uv run peakreel meta setup                        # app id, secret, optional configuration id
uv run peakreel meta login --account en           # browser: sign in, allow the Pages; then pick the EN Page
uv run peakreel meta login --account es --page "Clips ES"
uv run peakreel meta status                       # each account's Page and Instagram account
```

The Page token Peakreel stores does not expire (it comes from a long-lived sign-in); it stops working only if you change your Facebook password, remove the app, or lose admin rights on the Page.
Then `meta login` again.

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
