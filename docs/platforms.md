# Platforms: what posting through the APIs takes (M13)

Researched 2026-11-17 against the platforms' own documentation (sources at the end).
The milestone plan assumed all three platforms keep API posts private until an audit passes.
That is no longer true for two of the three, which changes the order of M10 to M13.

## Summary

| Platform | Public posting to our own accounts via API | Review needed | Limits |
| --- | --- | --- | --- |
| YouTube | **Yes, now.** Since 2026-11-02 uploads from unverified API projects are no longer forced private | None (an audit only raises quota) | 100 `videos.insert` calls per day per project by default (granular quota since 2026-06-01) |
| Instagram | **Yes, with Standard Access** for professional accounts we own, through "Instagram API with Instagram Login" | None for our own accounts (App Review and Business Verification are only for Advanced Access, i.e. other people's accounts) | 100 posts per account per rolling 24 h |
| TikTok | **No, not publicly.** Unaudited apps post `SELF_ONLY` to private accounts, at most 5 users per 24 h; the audit lists our use case as not acceptable | An audit we would very likely fail | Upload-to-inbox (drafts finished in the app) is the realistic route |

## YouTube

- The revision history entry of **2026-11-02**: "Videos uploaded through the videos.insert method from unverified API projects are no longer restricted", reversing the 2020-07-28 private-only policy.
  Some uploads can still default to private because of the channel's own settings.
- Quota: since 2026-06-01, `videos.insert` has its own bucket, 100 calls per day per project by default; an upload costs about 100 units since 2025-12-04.
  One project serves all our channels, so 100 uploads per day in total, which is far above our daily caps.
  The audit form only matters to raise quota.
- **OAuth trap:** a consent screen left in **Testing** issues refresh tokens that expire after **7 days** for `youtube.upload`, which silently stops unattended uploads.
  Publish the consent screen to **In production** (it can stay unverified: users see an "unverified app" screen once, with a cap of 100 users, far above our 4 channels), then authorize each channel again.
  Publishing asks for branding (app name, home page, privacy policy) and a verified domain for those pages.
- Shorts: there is no separate endpoint; a vertical video of 3 minutes or less is a Short.

## Instagram

- Use **Instagram API with Instagram Login** (graph.instagram.com), permissions `instagram_business_basic` and `instagram_business_content_publish`.
  No Facebook Page is needed.
- Meta: "If your app only serves your Instagram professional account or an account you manage, Standard Access is all your app needs."
  App Review and Business Verification are required only for Advanced Access (accounts we do not own).
- Standard Access works for people with a role on the Meta app (admin, developer, tester): each of our Instagram accounts gets a tester role.
- Accounts must be **professional** (Creator or Business); personal accounts cannot use the API.
- Reels: create a container (`media_type=REELS`, `video_url`, `caption`, optional `cover_url`, `collaborators`), wait until it is processed, then `media_publish`.
  The video must be reachable at a public URL, or uploaded with the resumable upload, which the documentation ties to Facebook Login; this needs checking when building M12.
- **Open question:** whether posts published while the Meta app is in Development mode are visible to everyone.
  Check with one real test post before relying on it; if not, the app must be switched to Live, which needs a privacy policy URL.

## TikTok

- Unaudited API clients: "can only post contents in SELF_ONLY viewership", "All user accounts using the API client to post must be set to private at the time of posting", and at most 5 users per 24 hours.
- The audit (to post publicly) lists as not acceptable:
  "A utility tool to help upload contents to the account(s) you or your team manages" and "An app that copies arbitrary contents from other platforms to TikTok"; clients "should be intended for a wide audience".
  Peakreel is both, so a public Direct Post audit would very likely be refused; applying anyway risks the app.
- Realistic route: **upload to inbox** (`video.upload`): the video lands in the account's TikTok drafts and is finished and posted by hand in the app, with TikTok's own privacy, interaction and disclosure settings.
  This saves moving files to the phone; whether it needs the audit should be confirmed in the developer portal when the app is created.
- Otherwise stay on export packages (M9), as now.
- If a direct-post UI is ever built, TikTok requires: the creator's nickname shown, privacy chosen by the user from a list with no default, interaction settings off by default, the commercial content disclosure, express consent before upload, and the "By posting, you agree to TikTok's Music Usage Confirmation" statement.

## Threads and Facebook

- **Threads:** the Threads API (graph.threads.net, its own sign-in in the same Meta app) publishes video posts to the Threads profile of an Instagram account; our own accounts work without App Review.
  Tokens last 60 days and must be refreshed; 250 posts per profile per day.
  It only accepts a video at a **public URL** (no direct upload), so it waits until hosting is decided (Cloudflare R2 or a tunnel).
- **Facebook:** Reels can only be posted to **Pages** (`/{page-id}/video_reels`), not personal profiles or groups; direct upload, 30 Reels per Page per day.
  Linking each Instagram account to its Page also unlocks direct (resumable) uploads for Instagram, which the Instagram-Login-only setup lacks.
  Built in M12.

## What this changes in the plan

1. **M10 YouTube adapter first:** public auto-posting works without any review, as soon as the channels exist.
2. **M12 Instagram adapter next:** our own accounts with Standard Access; first verify the development-mode visibility question with one manual API post.
3. **M11 TikTok:** inbox drafts only, or keep export packages.
4. **M13:** no audits to submit for YouTube and Instagram; a privacy policy and terms page are still needed (Google production consent screen, possibly Meta Live mode), hosted on a domain we can verify.

## Accounts to create tonight (and why)

Per language (en, es, pt, ja):

- **YouTube:** a channel per language. Brand accounts under one Google login keep authorization simple (one login, four channels).
- **Instagram:** a **professional** (Creator) account per language, 2FA on.
- **TikTok:** an account per language, 2FA on.

Developer side (can wait until the adapters are built):

- Google Cloud project with the YouTube Data API v3 enabled; OAuth client of type "Desktop app"; consent screen published to In production.
- Meta app with the Instagram product (Instagram Login); each Instagram account added as an Instagram tester and accepted from that account.
- TikTok developer app with Login Kit and the Content Posting API (for inbox uploads).

## Sources

- YouTube Data API revision history: https://developers.google.com/youtube/data/revision_history (entries of 2026-11-02, 2026-06-01, 2025-12-04)
- YouTube quota and compliance audits: https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits
- Instagram Platform overview (access levels): https://developers.facebook.com/docs/instagram-platform/overview/
- Instagram content publishing: https://developers.facebook.com/docs/instagram-platform/content-publishing/
- TikTok Content Sharing Guidelines: https://developers.tiktok.com/docs/en/content-sharing-guidelines
- Google OAuth testing-mode token expiry, discussed in: https://discuss.google.dev/t/oauth2-refresh-token-expiration-and-youtube-api-v3/160874/3

## Public pages (privacy, terms, data deletion)

Drafts are in `site/` (static HTML, no scripts, no tracking): `index.html`, `privacy.html`, `terms.html`, `data-deletion.html`.
They are needed by Google to publish the OAuth consent screen to production (home page and privacy policy links, on a domain verified in Search Console) and by Meta to switch the app to Live (privacy policy and data deletion instructions URLs).

Before publishing:

1. Replace `[CONTACT EMAIL]` (3 pages) with the address that should receive removal requests.
2. Host them, for example with GitHub Pages from the `site/` folder (`https://<user>.github.io/peakreel/`), or a custom domain.
3. Verify the domain in Google Search Console, then enter the URLs in the Google consent screen and the Meta app settings.

These pages state what the code does (retention periods, what is stored, the removal procedure); keep them in step when those change.
They are not legal advice.

