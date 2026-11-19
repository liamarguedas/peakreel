# M10: YouTube Shorts publishing

Approved clips are uploaded to the YouTube channel of their posting account (one channel per language, see M9), as Shorts, through the YouTube Data API.
Since 2026-11-02 YouTube no longer forces uploads from unverified API projects to private, so no audit is needed (see `docs/platforms.md`).

## One-time setup

### 1. Google Cloud project

1. At https://console.cloud.google.com create a project (for example "Peakreel").
2. APIs and Services, Library: enable **YouTube Data API v3**.
3. APIs and Services, OAuth consent screen (Google Auth Platform):
   - User type **External**; app name "Peakreel"; your email as support and developer contact.
   - Scopes: `.../auth/youtube.upload` and `.../auth/youtube.readonly`.
   - **Publish the app (Audience, "In production").**
     In "Testing", Google expires the refresh tokens of these scopes after **7 days**, which would stop uploads silently after a week.
     Production can stay unverified for our own channels: the login shows "Google hasn't verified this app" once; continue with Advanced, then "Go to Peakreel".
     If publishing asks for a home page and privacy policy, those are the pages planned in M13.
4. Clients: **Create client**, type **Desktop app**, download the JSON (`client_secret_....json`).

### 2. Store the client

```
uv run peakreel youtube setup client_secret_XXXX.json
```

The client goes to the Windows Credential Manager; the JSON file can be deleted.

### 3. Connect each posting account to its channel

```
uv run peakreel youtube login --account en
uv run peakreel youtube login --account es
uv run peakreel youtube login --account pt
uv run peakreel youtube login --account ja
```

Each opens the browser: sign in, **pick that language's channel** (brand accounts are listed under the same login), and allow access.
The refresh token is stored per account in the Credential Manager.

```
uv run peakreel youtube status        # each account, its channel, and whether its token works
uv run peakreel youtube logout --account en
```

### 4. Turn it on

```yaml
publish:
  modes:
    youtube: public     # or private: uploaded, visible only to you, publish by hand in Studio
```

TikTok and Instagram stay on `export` until their adapters exist.

## What happens

- Approving a clip writes the export packages at once and queues the YouTube upload.
  The review page's background loop (every 30 s) uploads it newest clip first, within `publish.youtube.min_spacing_min` and `daily_cap` per account (`docs/m9-publisher.md`, Pacing), so approving never waits for an upload.
  Without the review page running, `peakreel publish run --watch` does the same.
- The upload uses the title, description and tags from M9, category Gaming, "not made for kids", no synthetic media.
  A vertical clip of 3 minutes or less becomes a Short automatically.
- After the upload the publication is `processing`; it is checked every minute until YouTube reports it processed: `published` if public, `private_only` if private.
  A rejected or failed video becomes `failed` with YouTube's reason.
- **Permission:** a clip of a streamer whose `permission` is `unknown` is uploaded **private** even in public mode, and the card says so.
- **Quota:** the default is 100 uploads per day per project. A quota refusal does not use up the retries: the upload waits an hour and tries again.
- **Resumable upload:** the video goes in 8 MiB chunks; a dropped connection resumes from the last byte YouTube confirmed.
- **Never twice:** a publication is marked `uploading` before the upload starts.
  If the process dies during an upload, it is marked `failed` with "check the platform before retrying" instead of being uploaded again; check the channel in YouTube Studio.
- **Removal:** Peakreel never deletes a YouTube video by itself.
  A takedown or a rejection after upload lists the video (`peakreel publish status`) for removal by hand in YouTube Studio.

## Errors you may see

| Error | Fix |
| --- | --- |
| `no YouTube channel connected for account 'en'` | `peakreel youtube login --account en` |
| `YouTube authorization expired or was revoked` | Log in again; if it happens every 7 days, the consent screen is still in Testing: publish it |
| `no youtube publisher for public mode` | `peakreel youtube setup client_secret.json` |
| `YouTube quota reached` | Nothing: it retries hourly; or request more quota from Google |
