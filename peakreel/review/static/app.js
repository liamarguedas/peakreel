"use strict";
// Chat text is attacker-controlled: every value goes in through textContent, never innerHTML.
let currentState = "home";
const dirty = new Set();
const actions = new WeakMap(); // card node -> its controls, for keyboard shortcuts
const ON_PLATFORM = new Set(["processing", "published", "private_only"]); // see store.ON_PLATFORM_STATES
let selectedId = null;
let settings = { platforms: ["youtube", "tiktok", "instagram", "facebook"], enabled: [], mode: "export",
                 accounts: [], names: { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram",
                                        facebook: "Facebook" } };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (response.status === 401) {
    // The session expired or the password changed: log in again, then come back.
    location.assign("/login");
    throw new Error("login required");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || response.statusText);
  }
  return response.json();
}

function age(seconds) {
  if (seconds == null) return "no video";
  return seconds < 90 ? `${Math.round(seconds)}s` : `${Math.round(seconds / 60)}m`;
}

// Recording on or off: off frees the CPU and GPU; the queue keeps posting either way.
function recordingSwitch(status) {
  const on = status.running && !status.stopping;
  const button = el("button", { class: "switch", text: status.stopping ? "Stopping..." : on ? "Pause recording" : "Start recording",
    title: on ? "Stop recording and making new clips; posting the queue goes on" : "Record and clip the channels again" });
  button.disabled = Boolean(status.stopping);
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = on ? "Stopping..." : "Starting...";
    try {
      await api("/api/pipeline", { method: "POST", body: JSON.stringify({ on: !on }) });
    } catch (error) {
      button.textContent = error.message;
      return;
    }
    setTimeout(loadStatus, on ? 2000 : 15000);
  });
  return button;
}

// The AI model in use: the one that judged the latest clip, and on hover the whole
// order with each model's state. Amber when OpenRouter answers (Gemini is out), red when none can.
function shortModel(name) { return name.split("/").pop().replace(/:free$/, ""); }

function aiPill(status) {
  const models = status.models || [];
  const out = status.models_out || {};
  const last = status.model_last;
  const lines = models.map((m, i) => `${i + 1}. ${shortModel(m)}${out[m] ? ` · out of quota until ${brasilia(out[m]).slice(6, 11)}` : " · ready"}`);
  if (last) lines.push("", `Latest clip judged by ${shortModel(last.model)}, ${agoShort(last.at, Date.now())}`);
  const ready = models.filter((m) => !out[m]);
  if (models.length && !ready.length) {
    return el("span", { class: "pill bad", text: "AI: every model out of quota", title: lines.join("\n") });
  }
  const current = last && !out[last.model] ? last.model : ready[0];
  const fallback = Boolean(current && current.startsWith("openrouter/"));
  return el("span", { class: fallback ? "pill warn" : "pill", text: `AI: ${current ? shortModel(current) : "none"}`,
    title: lines.join("\n") });
}

async function loadStatus() {
  const box = document.getElementById("status");
  try {
    const status = await api("/api/status");
    box.replaceChildren(recordingSwitch(status), aiPill(status));
    if (!status.running) { box.append(el("span", { class: "pill bad", text: "recording off" })); return; }
    if (status.stopping) box.append(el("span", { class: "pill warn", text: "stopping: finishing clips in flight" }));
    // Healthy channels fold into one pill (details on hover); only problems get their own.
    const channels = Object.entries(status.channels || {});
    const line = ([name, ch]) => `${name} · ${ch.state} · video ${age(ch.video_age_s)} · ${ch.chat_per_min}/min` +
                                 (ch.restarts ? ` · ${ch.restarts} restarts` : "");
    const healthy = ([, ch]) => ch.state === "running" && ch.video_age_s != null && ch.video_age_s < 60;
    const good = channels.filter(healthy);
    box.append(el("span", { class: `pill ${good.length === channels.length ? "ok" : "bad"}`,
      text: `${good.length}/${channels.length} channels live`, title: good.map(line).join("\n") }));
    // A few problem channels get their own pill; many (a network problem) fold into one.
    const bad = channels.filter((c) => !healthy(c));
    if (bad.length > 3) box.append(el("span", { class: "pill bad", text: `${bad.length} behind or down`,
                                                title: bad.map(line).join("\n") }));
    else for (const entry of bad) box.append(el("span", { class: "pill bad", text: line(entry) }));
    for (const warning of status.warnings || []) box.append(el("span", { class: "pill warn", text: warning }));
    // The machine (peakreel.health.sample_machine): a missing reading is left out, never shown as 0.
    const m = status.machine || {};
    const health = status.health || {};
    const pill = (text, hot) => box.append(el("span", { class: hot ? "pill warn" : "pill", text }));
    if (m.cpu_pct != null) pill(`CPU ${Math.round(m.cpu_pct)}%`, m.cpu_pct > 85);
    if (m.ram_used_gb != null) pill(`RAM ${m.ram_used_gb}/${m.ram_total_gb} GB`, m.ram_used_gb > 0.9 * m.ram_total_gb);
    if (m.gpu_pct != null) pill(`GPU ${Math.round(m.gpu_pct)}% · ${m.vram_used_gb}/${m.vram_total_gb} GB`, m.gpu_pct > 90);
    if (m.download_mbps != null) pill(`↓ ${m.download_mbps} Mbit/s`, false);
    if (health.memory_mb != null) pill(`pipeline ${(health.memory_mb / 1024).toFixed(1)} GB`, false);
    if (health.free_disk_gb != null) pill(`${Math.round(health.free_disk_gb)} GB free`, health.free_disk_gb < 20);
    // Written every 30 s: older than 2 minutes means the pipeline stopped reporting.
    if (status.updated_utc) pill(`updated ${brasilia(status.updated_utc, false)}`, Date.now() - Date.parse(status.updated_utc) > 120000);
  } catch (error) {
    box.replaceChildren(el("span", { class: "pill bad", text: `status: ${error.message}` }));
  }
}

// One clip: the video, who and when, what chat said, and the decision. Post text, platforms and
// language are left to auto review (Gemini writes the text; publish.platforms picks where).
function card(clip) {
  let video;
  if (clip.purged) {
    video = el("div", { class: "purged", text: "Video deleted after posting" });
  } else {
    video = el("video", { controls: "", preload: "none", playsinline: "" });
    if (clip.has_thumbnail) video.poster = `/media/${clip.id}/thumbnail`;
    video.src = `/media/${clip.id}/${clip.has_vertical ? "vertical" : "horizontal"}`;
  }

  const chat = el("div", { class: "chat", "aria-label": "What chat said most" });
  for (const [text, count] of clip.top_messages.slice(0, 8)) {
    chat.append(el("span", { title: text }, document.createTextNode(text), el("b", { text: `×${count}` })));
  }
  if (!clip.top_messages.length) chat.append(el("span", { class: "none", text: "No chat messages" }));

  // Safety flags stay: personal information or music on screen is worth a look before approving.
  const flags = clip.flags.filter((flag) => flag.kind !== "checks_incomplete");
  const warning = flags.length ? el("p", { class: "warning", text: `⚠ ${flags.map((f) => f.message).join(" · ")}` }) : null;

  // Only the Check tab says why the AI decided: that is what is being checked.
  const ai = currentState === "audit" && clip.auto ? el("p", { class: "ai", text: `AI: ${clip.auto.reason}` }) : null;

  const live = clip.publications.filter((pub) => ON_PLATFORM.has(pub.state));
  let links = null;
  if (live.length) {
    links = el("div", { class: "links" });
    for (const pub of live) {
      const label = `${settings.names[pub.platform] || pub.platform} ${pub.account.toUpperCase()}` +
        (pub.views != null ? ` · ${compact(pub.views)} views` : "");
      // The URL comes from the platform's API; only http(s) becomes a link.
      links.append(/^https?:\/\//.test(pub.url || "")
        ? el("a", { href: pub.url, target: "_blank", rel: "noopener noreferrer", text: `${label} ↗` })
        : el("span", { text: label }));
    }
  }

  const note = el("span", { class: "note", role: "status" });
  const save = async (state, reason) => {
    for (const button of box.querySelectorAll("button")) button.disabled = true;
    note.className = "note"; note.textContent = "Saving…";
    try {
      const updated = await api(`/api/clips/${clip.id}`, { method: "POST", body: JSON.stringify({ state, reason }) });
      dirty.delete(clip.id);
      const fresh = card(updated);
      const leaves = currentState && updated.state !== currentState;
      if (leaves && feedOn() && selectedId === clip.id) move(1); // on to the next clip, like a swipe
      swap(node, fresh);
      if (selectedId === clip.id) select(fresh, false);
      if (leaves) setTimeout(refresh, 400);
    } catch (error) {
      note.className = "note error"; note.textContent = error.message;
      for (const button of box.querySelectorAll("button")) button.disabled = false;
    }
  };

  const approve = el("button", { class: "approve", type: "button" }, el("kbd", { text: "A" }), "Approve");
  const reject = el("button", { class: "reject", type: "button" }, el("kbd", { text: "R" }), "Reject");
  const row = el("div", { class: "row" }, approve, reject);
  // Rejecting asks why (one tap): the reasons teach auto review (peakreel.review.auto.taste_examples).
  const reasons = el("div", { class: "reasons", hidden: "" }, el("p", { text: "Why reject it?" }));
  REJECT_REASONS.forEach(([code, label], i) => {
    const button = el("button", { type: "button" }, el("kbd", { text: String(i + 1) }), label);
    button.addEventListener("click", () => save("rejected", code));
    reasons.append(button);
  });
  const cancel = el("button", { class: "cancel", type: "button" }, el("kbd", { text: "Esc" }), "Cancel");
  reasons.append(cancel);
  const showReasons = (open) => {
    reasons.hidden = !open; row.hidden = open;
    if (open) dirty.add(clip.id); else dirty.delete(clip.id); // keep it open across the refresh
  };
  approve.addEventListener("click", () => save("approved"));
  reject.addEventListener("click", () => showReasons(true));
  cancel.addEventListener("click", () => showReasons(false));
  // An approved clip can still be stopped before it posts; a rejected one approved after all.
  approve.hidden = clip.state === "approved";
  reject.hidden = clip.state === "rejected";
  if (approve.hidden || reject.hidden) row.style.gridTemplateColumns = "1fr";
  if (clip.state === "blocked") { approve.disabled = true; reject.disabled = true; }
  const box = el("div", { class: "actions" }, row, reasons, note);

  const showState = currentState !== "new" && currentState !== "audit";
  const state = showState ? el("span", { class: `state ${clip.state}`, text:
    ({ new: "To review", approved: "Approved", rejected: "Rejected", blocked: "Taken down" }[clip.state] || clip.state) +
    (clip.review_reason ? ` · ${reasonLabel(clip.review_reason)}` : "") +
    (clip.reviewed_by === "auto" ? " · by AI" : clip.reviewed_by === "person" ? " · by you" : "") }) : null;

  const node = el("article", { class: "card", "data-id": String(clip.id) },
    video,
    el("div", { class: "info" },
      el("div", { class: "head" },
        el("div", {},
          el("div", { class: "channel", text: clip.channel }),
          el("div", { class: "when", text: `${brasilia(clip.start_utc).slice(0, 11)} · ${clip.duration_s}s` })),
        state),
      chat, warning, ai, links, box));
  actions.set(node, { approve, reject, reasons, showReasons, video: video instanceof HTMLVideoElement ? video : null });
  node.addEventListener("pointerdown", () => select(node, false));
  return node;
}

// Activity: what the pipeline and the publisher did, one block per kind, newest first
// (peakreel.review.activity keeps the latest events of each kind).
const ACTIVITY_BLOCKS = [
  ["upload", "Uploads", "Posts queued, posted and dropped"],
  ["detect", "Clips detected", "Chat spikes cut into clips"],
  ["duplicate", "Same moment on two channels", "Clips already cut from another streamer"],
  ["review", "Reviews", "Decisions by the AI and by you"],
  ["problem", "Problems", "Refused posts, retries and errors"],
  ["channel", "Channels", "Streams starting and stopping"],
  ["skip", "Moments skipped", "Spikes not worth a clip"],
];

function agoText(iso, now) {
  const minutes = Math.max(0, Math.round((now - Date.parse(iso)) / 60000));
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 6) / 10} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
}

async function refreshActivity(main) {
  const items = await api("/api/activity");
  const now = Date.now();
  const hourAgo = now - 3600000;
  const blocks = ACTIVITY_BLOCKS.map(([kind, title, about]) => {
    const own = items.filter((i) => i.kind === kind);
    const lastHour = own.filter((i) => Date.parse(i.time) >= hourAgo).length;
    const list = el("ol", { class: "qlist" });
    for (const item of own) {
      list.append(el("li", { class: "qrow" },
        el("span", { class: "qtime" }, el("b", { text: whenText(item.time, now).at }),
          el("small", { text: agoText(item.time, now) })),
        el("span", { class: "awhat" }, item.tag ? el("span", { class: "qwhere", text: item.tag }) : null,
          document.createTextNode(item.text))));
    }
    const heading = el("h2", { text: title }, el("small", { text: `${lastHour} in the last hour` }));
    const box = el("section", { class: `a-${kind}${kind === "problem" && lastHour ? " hot" : ""}` }, heading,
      el("p", { class: "note", text: own.length ? about : `${about}: nothing yet.` }), own.length ? list : null);
    return box;
  });
  const scrolled = [...main.querySelectorAll(".qlist")].map((list) => list.scrollTop); // kept across the refresh
  main.replaceChildren(el("div", { class: "stats acts" }, ...blocks));
  main.querySelectorAll(".qlist").forEach((list, i) => { list.scrollTop = scrolled[i] || 0; });
}

// Stats: clips, reviews, posts and views (peakreel.review.dashboard). Missing views show "–".
const HOUR = new Intl.DateTimeFormat("en-GB", { timeZone: "America/Sao_Paulo", day: "2-digit",
  month: "2-digit", hour: "2-digit", hourCycle: "h23" });

function hourLabel(iso) {
  const p = Object.fromEntries(HOUR.formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  return { day: `${p.day}/${p.month}`, hour: `${p.hour}h` };
}

function num(n) {
  return n == null ? "–" : compact(n);
}

function tile(value, label, detail) {
  return el("div", { class: "tile" }, el("b", { text: value }), el("span", { text: detail ? `${label} · ${detail}` : label }));
}

function section(title, cls, ...children) {
  return el("section", cls ? { class: cls } : {}, el("h2", { text: title }), ...children);
}

function table(head, rows) {
  const t = el("table");
  t.append(el("thead", {}, el("tr", {}, ...head.map(([text, cls]) => el("th", cls ? { class: cls, text } : { text })))));
  const body = el("tbody");
  for (const cells of rows) body.append(el("tr", {}, ...cells));
  t.append(body);
  return el("div", { class: "scroll" }, t);
}

function td(content, cls) {
  const cell = el("td", cls ? { class: cls } : {});
  for (const part of [content].flat()) cell.append(typeof part === "string" ? document.createTextNode(part) : part);
  return cell;
}

function hourlyChart(hourly) {
  const max = Math.max(1, ...hourly.map(([, n]) => n));
  const chart = el("div", { class: "chart" });
  const bars = el("div", { class: "bars", role: "img",
    "aria-label": `Posts that went live per hour, last ${hourly.length} hours, at most ${max} in one hour` });
  const axis = el("div", { class: "axis", "aria-hidden": "true" });
  const tip = el("div", { class: "tip", hidden: "" });
  hourly.forEach(([iso, n], i) => {
    const { day, hour } = hourLabel(iso);
    const column = el("div", {}, el("i", { style: `height:${(n / max) * 100}%` }));
    column.addEventListener("pointerenter", () => {
      tip.textContent = `${day} ${hour} · ${n} post${n === 1 ? "" : "s"}`;
      tip.style.left = `${((i + 0.5) / hourly.length) * 100}%`;
      tip.hidden = false;
    });
    bars.append(column);
    const label = i % 6 === 0 ? (hour === "00h" || i === 0 ? `${day} ${hour}` : hour) : "";
    axis.append(el("span", i % 12 === 6 ? { class: "minor", text: label } : { text: label }));
  });
  bars.addEventListener("pointerleave", () => { tip.hidden = true; });
  chart.append(el("span", { class: "ymax", text: String(max) }), bars, axis, tip);
  return chart;
}

async function refreshStats(main) {
  const d = await api("/api/dashboard");
  const count = (state, by) => d.clips.counts.filter((c) => c.state === state && (by == null || c.by === by))
    .reduce((sum, c) => sum + c.n, 0);
  const sum = (key) => d.posts.reduce((total, g) => total + (g[key] ?? 0), 0);
  const anyViews = d.posts.some((g) => g.views != null);
  const clips = d.clips.counts.reduce((total, c) => total + c.n, 0);

  const tiles = el("div", { class: "tiles" },
    tile(compact(clips), "Clips made", `${count("new")} to review`),
    tile(compact(count("approved")), "Approved", `AI ${count("approved", "auto")} · you ${count("approved", "person")}`),
    tile(compact(count("rejected")), "Rejected", `AI ${count("rejected", "auto")} · you ${count("rejected", "person")}`),
    tile(compact(sum("live")), "Posts live", `${sum("waiting")} waiting · ${sum("failed")} failed`),
    tile(anyViews ? compact(sum("views")) : "–", "Views", d.posts.some((g) => g.platform !== "youtube" && g.live && g.views == null)
      ? "YouTube only so far" : "latest reading"),
    tile(compact(sum("likes")), "Likes"));

  const platforms = settings.platforms.filter((p) => d.posts.some((g) => g.platform === p));
  const accounts = [...new Set(d.posts.map((g) => g.account))];
  const byPlatform = table([["Language"], ...platforms.map((p) => [settings.names[p] || p, "num"])],
    accounts.map((a) => [td(accountLabel(a)), ...platforms.map((p) => {
      const g = d.posts.find((x) => x.account === a && x.platform === p);
      if (!g) return td("", "num");
      const extra = [g.waiting && `${g.waiting} waiting`, g.failed && `${g.failed} failed`].filter(Boolean).join(" · ");
      return td([`${g.live} live · ${num(g.views)} views`,
        extra ? el("small", { class: g.failed ? "bad" : "", text: extra }) : null].filter(Boolean), "num");
    })]));

  const [wrong, right] = d.check;
  const [rho, scored] = d.gemini;
  const review = el("dl", {},
    el("dt", { text: "Approved by AI" }), el("dd", { text: String(count("approved", "auto")) }),
    el("dt", { text: "Rejected by AI" }), el("dd", { text: String(count("rejected", "auto")) }),
    el("dt", {}, document.createTextNode("AI rejects you re-checked "), el("small", { text: "(Check tab)" })),
    el("dd", { text: wrong + right ? `${wrong} wrong of ${wrong + right}` : "none yet" }),
    el("dt", {}, document.createTextNode("Gemini score vs 12 h views "), el("small", { text: "(1 = predicts them)" })),
    el("dd", { text: rho == null ? `too few (${scored})` : `${rho.toFixed(2)} · ${scored} posts` }),
    ...d.clips.reasons.flatMap((r) => [el("dt", { text: `You rejected: ${reasonLabel(r.reason)}` }),
                                        el("dd", { text: String(r.n) })]));

  const top = d.top.length ? table([["Views", "num"], ["Likes", "num"], ["Post"], ["Where"]],
    d.top.map((p) => [td(num(p.views), "num"), td(num(p.likes), "num"),
      td([el("a", { href: p.url, target: "_blank", rel: "noopener", text: p.title || "(no title)" }),
          el("small", { text: p.channel })], "title"),
      td(`${p.account.toUpperCase()} · ${settings.names[p.platform] || p.platform}`, "where")]))
    : el("p", { class: "note", text: "No views read yet." });

  const channels = table([["Channel"], ["Approved", "num"], ["Views", "num"]],
    d.channels.map((c) => [td(c.channel), td(String(c.approved), "num"), td(num(c.views), "num")]));

  const metaNote = d.posts.some((g) => g.platform !== "youtube" && g.live && g.views == null)
    ? el("p", { class: "note", text: "Instagram and Facebook views need the insights permission: peakreel meta login --insights." })
    : null;

  main.replaceChildren(el("div", { class: "stats" }, tiles,
    section("Posts that went live per hour (Brasília time, last 48 h)", "wide", hourlyChart(d.hourly)),
    section("By language and platform", "wide", byPlatform, metaNote),
    section("Review", "", review),
    section("Most viewed posts", "", top),
    section("Channels with most approved clips", "", channels)));
}

// Schedule: where clips and posts are in the pipeline (peakreel.review.dashboard.schedule).
function whenText(iso, now) {
  const t = Date.parse(iso);
  const minutes = Math.round((t - now) / 60000);
  const [day, clock] = brasilia(iso).split(" ");
  const today = brasilia(new Date(now).toISOString()).split(" ")[0];
  const at = `${day === today ? "" : `${day} `}${clock.slice(0, 5)}`;
  if (minutes <= 0) return { at, rel: "due now" };
  if (minutes < 60) return { at, rel: `in ${minutes} min` };
  if (minutes < 48 * 60) return { at, rel: `in ${Math.round(minutes / 6) / 10} h` };
  return { at, rel: `in ${Math.round(minutes / 1440)} days` };
}

// Schedule as a board: a column per stage, a card per clip or post moving left to right.
// Tags under each card: where it posts, the streamer, the language, who approved it.
// Filters (kept across refreshes): platform for the post columns, language for all.
let schedulePlatform = null;
let scheduleLanguage = null;

async function openClipById(id) {
  try { openClip(await api(`/api/clips/${id}`)); } catch { /* gone: nothing to open */ }
}

function tag(text, cls = "") { return el("span", { class: `tag ${cls}`.trim(), text }); }

function boardCard({ id, thumbnail, title, url, time, tags, note, noteClass }) {
  const picture = thumbnail
    ? el("img", { src: `/media/${id}/thumbnail`, alt: "", loading: "lazy" })
    : el("span", { class: "pthumb-none", text: `#${id}` });
  const open = el("button", { class: "kthumb", type: "button", title: `Open clip #${id}` }, picture);
  open.addEventListener("click", () => openClipById(id));
  const heading = url && /^https?:\/\//.test(url)
    ? el("a", { class: "ktitle", href: url, target: "_blank", rel: "noopener noreferrer", text: title || "(no title)" })
    : el("span", { class: "ktitle", text: title || "(no title)" });
  return el("li", { class: "kcard" }, open,
    el("div", { class: "kbody" }, heading, el("small", { class: "ktime", text: time }),
      el("div", { class: "ktags" }, ...tags.filter(Boolean)),
      note ? el("small", { class: `knote ${noteClass || ""}`.trim(), text: note }) : null));
}

function approvedTag(by) {
  if (by === "auto") return tag("AI approved", "ai");
  if (by === "person") return tag("You approved", "you");
  return null;
}

function agoShort(iso, now) {
  const minutes = Math.max(0, Math.round((now - Date.parse(iso)) / 60000));
  return minutes < 60 ? `${minutes} min ago` : `${Math.round(minutes / 6) / 10} h ago`;
}

async function refreshSchedule(main) {
  const [d, checking, waiting] = await Promise.all([
    api("/api/schedule"), api("/api/clips?state=checking"), api("/api/clips?state=new")]);
  // Instagram only (2026-11-20): YouTube and Facebook are off.
  d.queued = d.queued.filter((p) => p.platform === "instagram");
  d.posted = d.posted.filter((p) => p.platform === "instagram");
  const now = Date.now();
  const posts = [...d.queued, ...d.posted];
  const platforms = PLATFORM_ORDER.filter((p) => posts.some((x) => x.platform === p));
  if (schedulePlatform && !platforms.includes(schedulePlatform)) schedulePlatform = null;
  const clipLanguage = (c) => c.route?.language || "";
  const languages = [...new Set([...posts.map((p) => p.language), ...checking.map(clipLanguage),
    ...waiting.map(clipLanguage)].filter(Boolean))].sort((a, b) =>
    (LANGUAGE_ORDER.indexOf(a) + 99) % 99 - (LANGUAGE_ORDER.indexOf(b) + 99) % 99);
  if (scheduleLanguage && !languages.includes(scheduleLanguage)) scheduleLanguage = null;

  const chips = (label, options, current, name, set) => el("div", { class: "pfilter", role: "group", "aria-label": label },
    ...[null, ...options].map((value) => {
      const button = el("button", { type: "button", text: value ? name(value) : `All ${label.toLowerCase()}s` });
      button.setAttribute("aria-pressed", String(value === current));
      button.addEventListener("click", () => { set(value); refreshSchedule(main); });
      return button;
    }));
  const filters = el("div", { class: "kfilters" },
    platforms.length > 1
      ? chips("Platform", platforms, schedulePlatform, (p) => settings.names[p] || p, (v) => { schedulePlatform = v; })
      : null,
    chips("Language", languages, scheduleLanguage, languageName, (v) => { scheduleLanguage = v; }));

  const langOk = (code) => !scheduleLanguage || code === scheduleLanguage;
  const postOk = (p) => langOk(p.language) && (!schedulePlatform || p.platform === schedulePlatform);
  const where = (p) => tag(`${settings.names[p.platform] || p.platform} ${p.account.toUpperCase()}`, `plat ${p.platform}`);
  const clipCard = (c, time) => boardCard({ id: c.id, thumbnail: c.has_thumbnail && !c.purged, title: c.title,
    time, tags: [tag(c.channel), tag(languageName(clipLanguage(c)))] });
  const postCard = (p, time, note, noteClass) => boardCard({ id: p.clip_id, thumbnail: p.thumbnail, title: p.title,
    url: p.url, time, note, noteClass,
    tags: [where(p), tag(p.channel), tag(languageName(p.language)), approvedTag(p.reviewed_by)] });

  const columns = [
    ["AI checking", "The AI is watching these", checking.filter((c) => langOk(clipLanguage(c)))
      .map((c) => clipCard(c, `made ${agoShort(c.created_utc, now)}`))],
    ["Waiting for you", "The AI was unsure: To review", waiting.filter((c) => langOk(clipLanguage(c)))
      .map((c) => clipCard(c, `made ${agoShort(c.created_utc, now)}`))],
    ["Queued", "Approved, waiting for their turn", d.queued.filter((p) => p.state === "approved" && postOk(p))
      .map((p) => { const w = whenText(p.due, now); return postCard(p, `${w.rel} · ${w.at}`,
        p.note && `Waiting: ${p.note.split(" (")[0]}`, "warn"); })],
    ["Uploading", "Sent; the platform is processing it", d.queued.filter((p) => p.state !== "approved" && postOk(p))
      .map((p) => postCard(p, p.state === "uploading" ? "uploading now" : "processing", null))],
    ["Posted", "Live in the last 24 h", d.posted.filter(postOk)
      .map((p) => postCard(p, `${agoShort(p.at, now)} · ${whenText(p.at, now).at}`,
        p.views != null ? `${compact(p.views)} views` : null, "ok"))],
  ];
  const scrolled = [...main.querySelectorAll(".kcol ol")].map((list) => list.scrollTop); // kept across the refresh
  const board = el("div", { class: "kanban" }, ...columns.map(([title, about, cards]) =>
    el("section", { class: "kcol" },
      el("h2", {}, document.createTextNode(title), el("small", { text: String(cards.length) })),
      el("p", { class: "note", text: about }),
      cards.length ? el("ol", {}, ...cards) : el("p", { class: "kempty", text: "Nothing here" }))));
  main.replaceChildren(el("div", { class: "stats" }, el("section", { class: "wide" }, filters),
    el("section", { class: "wide kwrap" }, board)));
  main.querySelectorAll(".kcol ol").forEach((list, i) => { list.scrollTop = scrolled[i] || 0; });
}

// Uploads: posts per account in the last 24 hours against the caps, and every day so far
// (peakreel.review.dashboard.uploads; the history is never deleted).
const PLATFORM_ORDER = ["instagram", "facebook", "youtube"];
const LANGUAGES = { en: "English", es: "Español", pt: "Português", ja: "日本語" };
const LANGUAGE_ORDER = Object.keys(LANGUAGES);

function languageName(code) { return LANGUAGES[code] || code || "No language"; }

// Groups items by language (English, Spanish, Portuguese first), as [language, items] pairs.
function byLanguage(items, languageOf) {
  const groups = new Map();
  for (const item of items) {
    const code = languageOf(item) || "";
    if (!groups.has(code)) groups.set(code, []);
    groups.get(code).push(item);
  }
  const rank = (code) => (LANGUAGE_ORDER.includes(code) ? LANGUAGE_ORDER.indexOf(code) : 99);
  return [...groups.entries()].sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]));
}

function untilText(iso, now) {
  const minutes = Math.max(1, Math.round((Date.parse(iso) - now) / 60000));
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

// One posting account on one platform: its daily cap, when it may post next, and its
// posts of the last 24 hours as small cards (newest first).
function uploadCard(a, now) {
  const full = a.last_24h >= a.cap;
  const bar = el("div", { class: `up-bar${full ? " full" : ""}${a.connected ? "" : " off"}`, role: "img",
    "aria-label": `${a.last_24h} of ${a.cap} posts in 24 hours` },
    el("i", { style: `width:${Math.min(100, (a.last_24h / a.cap) * 100)}%` }));
  const state = !a.connected
    ? el("div", { class: "up-state" }, el("span", { class: "off", text: "not connected" }),
        el("small", { text: "gets no clips" }))
    : a.next_at
    ? el("div", { class: "up-state" }, el("span", { class: "wait", text: full ? "at the cap" : "spacing" }),
        el("small", { text: `next in ${untilText(a.next_at, now)}` }))
    : el("div", { class: "up-state" }, el("span", { class: "room", text: "room now" }),
        el("small", { text: a.last_at ? `last ${brasilia(a.last_at).slice(6, 11)}` : "no posts yet" }));
  const head = el("div", { class: "up-head" },
    el("div", { class: "up-name" }, el("b", { text: a.label }),
      el("small", { text: `${settings.names[a.platform] || a.platform} · ${a.account.toUpperCase()}` })),
    state);
  const count = el("div", { class: "up-count" }, el("b", { text: String(a.last_24h) }),
    document.createTextNode(` of ${a.cap} in 24 h · platform limit ${a.limit}`));
  const posts = el("div", { class: "pcards" });
  for (const p of a.posts) {
    const picture = p.thumbnail
      ? el("img", { src: `/media/${p.clip_id}/thumbnail`, alt: "", loading: "lazy" })
      : el("span", { class: "pthumb-none", text: `#${p.clip_id}` });
    const label = `${p.title || "(no title)"} · ${p.channel}`;
    const link = p.url && /^https?:\/\//.test(p.url)
      ? el("a", { class: "pthumb", href: p.url, target: "_blank", rel: "noopener noreferrer", title: label }, picture)
      : el("span", { class: "pthumb", title: label }, picture);
    posts.append(el("div", { class: "pcard" }, link,
      el("b", { text: brasilia(p.at).slice(6, 11) }),
      el("small", { text: p.views != null ? `${compact(p.views)} views` : p.channel })));
  }
  return el("div", { class: `up-acct${a.connected ? "" : " off"}` }, head, bar, count,
    a.posts.length ? posts : el("p", { class: "note", text: "No posts in the last 24 h." }));
}

async function refreshUploads(main) {
  const d = await api("/api/uploads");
  d.accounts = d.accounts.filter((a) => a.platform === "instagram"); // Instagram only
  const now = Date.now();
  const sections = [];
  const languages = [...new Set(d.accounts.map((a) => a.language))];
  for (const language of languages) {
    const rows = d.accounts.filter((a) => a.language === language)
      .sort((x, y) => PLATFORM_ORDER.indexOf(x.platform) - PLATFORM_ORDER.indexOf(y.platform));
    const total = rows.reduce((n, a) => n + a.last_24h, 0);
    const cards = el("div", { class: "up-accts" });
    for (const a of rows) cards.append(uploadCard(a, now));
    sections.push(section(`${languageName(language)} · ${total} posts in the last 24 h`,
      "wide", cards));
  }

  // History: one row per Brasília day, one column per account and platform ever used.
  const keys = [...new Set(d.days.flatMap((day) => Object.keys(day.counts)))].filter((k) => k.endsWith("/instagram")).sort((x, y) => {
    const [ax, px] = x.split("/"), [ay, py] = y.split("/");
    return PLATFORM_ORDER.indexOf(px) - PLATFORM_ORDER.indexOf(py) || ax.localeCompare(ay);
  });
  const handle = (key) => d.labels[key] || key;
  const short = { instagram: "IG", facebook: "FB", youtube: "YT", tiktok: "TT" };
  const hist = table([["Day"], ...keys.map((k) => [`${short[k.split("/")[1]] || k} ${k.split("/")[0]}`, "num"]), ["Total", "num"]],
    d.days.map((day) => {
      const [y, m, dd] = day.day.split("-");
      const total = Object.values(day.counts).reduce((n, v) => n + v, 0);
      return [td(`${dd}/${m}/${y}`), ...keys.map((k) => td(String(day.counts[k] || 0), day.counts[k] ? "num" : "num zero")),
              td(String(total), "num")];
    }));
  hist.querySelectorAll("thead th").forEach((th, i) => {
    if (i > 0 && i <= keys.length) th.append(el("small", { text: handle(keys[i - 1]) }));
  });
  hist.classList.add("hist");
  main.replaceChildren(el("div", { class: "stats sched" }, ...sections,
    section(`Every day (Brasília time) · ${d.total_days} day${d.total_days === 1 ? "" : "s"} kept`, "wide", hist)));
}

// Posted, Approved, Rejected: by language, then a card per streamer with small thumbnails.
// A thumbnail opens the full clip (video, decision buttons) in a dialog, on phones too:
// Feed mode is only for the review tabs (FEED_STATES).
const GRID_STATES = ["published", "approved", "rejected"];
const SHORT = { instagram: "IG", facebook: "FB", youtube: "YT", tiktok: "TT" };

function clipNote(clip) {
  if (clip.state === "published") {
    const live = (clip.publications || []).filter((p) => p.state === "published");
    return live.map((p) => SHORT[p.platform] || p.platform).join(" ") || "posted";
  }
  if (clip.state === "rejected") {
    if (clip.review_reason) return reasonLabel(clip.review_reason);
    return clip.reviewed_by === "auto" ? "by the AI" : "rejected";
  }
  return clip.title || "(no title)";
}

function openClip(clip) {
  const dialog = document.getElementById("clip-view");
  dialog.querySelector(".clip-body").replaceChildren(card(clip));
  dialog.showModal();
}

function renderClipGrid(main, clips) {
  const now = Date.now();
  const blocks = byLanguage(clips, (c) => c.route?.language).map(([language, items]) => {
    const channels = new Map();
    for (const c of items) {
      if (!channels.has(c.channel)) channels.set(c.channel, []);
      channels.get(c.channel).push(c);
    }
    const box = el("div", { class: "up-accts" });
    for (const [channel, list] of [...channels.entries()].sort((a, b) => b[1].length - a[1].length)) {
      const thumbs = el("div", { class: "pcards" });
      for (const c of list) {
        const picture = c.has_thumbnail && !c.purged
          ? el("img", { src: `/media/${c.id}/thumbnail`, alt: "", loading: "lazy" })
          : el("span", { class: "pthumb-none", text: `#${c.id}` });
        const open = el("button", { class: "pthumb", type: "button", title: `#${c.id} · ${c.title || "(no title)"}` }, picture);
        open.addEventListener("click", () => openClip(c));
        thumbs.append(el("div", { class: "pcard" }, open,
          el("b", { text: whenText(c.reviewed_utc || c.created_utc, now).at }), el("small", { text: clipNote(c) })));
      }
      box.append(el("div", { class: "up-acct" },
        el("div", { class: "up-name" }, el("b", { text: channel }),
          el("small", { text: `${list.length} clip${list.length === 1 ? "" : "s"}` })), thumbs));
    }
    return section(`${languageName(language)} · ${items.length} clip${items.length === 1 ? "" : "s"}`, "wide", box);
  });
  main.replaceChildren(el("div", { class: "stats" }, ...blocks));
}

function igHandle(account) {
  const handle = (settings.accounts || []).find((a) => a.id === account)?.instagram;
  return handle ? `@${handle}` : account.toUpperCase();
}

// Posted: one box, Instagram only, newest post first, with the board's tags.
function renderPosted(main, clips) {
  const now = Date.now();
  const posts = clips.flatMap((c) => (c.publications || [])
    .filter((p) => p.platform === "instagram" && p.state === "published" && p.published_utc)
    .map((p) => ({ clip: c, post: p })))
    .sort((a, b) => Date.parse(b.post.published_utc) - Date.parse(a.post.published_utc));
  const cards = posts.map(({ clip: c, post: p }) => {
    const stats = [p.views != null ? `${compact(p.views)} views` : null,
      p.likes != null ? `${compact(p.likes)} likes` : null].filter(Boolean).join(" · ");
    return boardCard({ id: c.id, thumbnail: c.has_thumbnail && !c.purged, title: c.title, url: p.url,
      time: `${agoShort(p.published_utc, now)} · ${whenText(p.published_utc, now).at}`,
      note: stats || null, noteClass: "ok",
      tags: [tag(igHandle(p.account), "plat instagram"), tag(c.channel),
        tag(languageName(c.route?.language)), approvedTag(c.reviewed_by)] });
  });
  main.replaceChildren(el("div", { class: "stats" },
    section(`Posted on Instagram · ${posts.length}`, "wide",
      posts.length ? el("ol", { class: "posted-grid" }, ...cards) : el("p", { class: "note", text: "Nothing posted yet." }))));
}

// Home: what needs attention first, then today at a glance (Instagram only).
async function renderHome(main) {
  const [up, sched, waiting, feed] = await Promise.all([
    api("/api/uploads"), api("/api/schedule"), api("/api/clips?state=new"), api("/api/activity")]);
  const now = Date.now();
  setNewCount(waiting.length);
  const ig = up.accounts.filter((a) => a.platform === "instagram");
  const hourAgo = now - 3600000;

  const problems = [];
  for (const a of ig.filter((x) => !x.connected)) problems.push(`${a.label}: not connected, it gets no clips`);
  const seen = new Set();
  for (const item of feed.filter((i) => i.kind === "problem" && Date.parse(i.time) >= hourAgo)) {
    if (/youtube|facebook/i.test(item.tag)) continue; // Instagram only
    // One line per clip (a refusal is logged as "refused" and as "postponed").
    const clip = item.text.match(/#\d+/)?.[0];
    const key = clip || item.text.slice(0, 90);
    if (seen.has(key)) continue;
    seen.add(key);
    problems.push(`${item.tag ? `${item.tag}: ` : ""}${item.text}`);
  }
  const problemBox = problems.length
    ? el("section", { class: "wide home-problems" },
        el("h2", { text: `${problems.length} problem${problems.length === 1 ? "" : "s"} in the last hour` }),
        el("ul", {}, ...problems.slice(0, 5).map((t) => el("li", { text: t }))),
        (() => { const b = el("button", { type: "button", class: "linkish", text: "See all activity" });
                 b.addEventListener("click", () => selectState("activity")); return b; })())
    : el("section", { class: "wide home-ok" }, el("h2", { text: "All good: no problems in the last hour" }));

  const posted = ig.reduce((n, a) => n + a.last_24h, 0);
  const queued = sched.queued.filter((p) => p.platform === "instagram").length;
  const tile = (n, label, sub, state) => {
    const box = el("button", { type: "button", class: "home-tile" }, el("b", { text: compact(n) }),
      el("span", { text: label }), el("small", { text: sub }));
    box.addEventListener("click", () => selectState(state));
    return box;
  };
  const tiles = el("section", { class: "wide" }, el("div", { class: "home-tiles" },
    tile(posted, "Posted on Instagram", "last 24 h", "published"),
    tile(queued, "Waiting to post", "in the queue now", "schedule"),
    tile(waiting.length, "Waiting for you", "To review", "new"),
    tile(ig.filter((a) => a.connected).length, "Accounts posting", `of ${ig.length}`, "uploads")));

  const langs = byLanguage(ig, (a) => a.language).map(([language, accounts]) => {
    const done = accounts.reduce((n, a) => n + a.last_24h, 0);
    const cap = accounts.filter((a) => a.connected).reduce((n, a) => n + a.cap, 0);
    const rows = accounts.map((a) => {
      const full = a.last_24h >= a.cap;
      const state = !a.connected ? "not connected" : a.next_at ? `next in ${untilText(a.next_at, now)}` : "room now";
      return el("li", {},
        el("div", { class: "home-acct" }, el("b", { text: a.label }),
          el("small", { class: !a.connected ? "off" : a.next_at ? "wait" : "room", text: state })),
        el("div", { class: `up-bar${full ? " full" : ""}${a.connected ? "" : " off"}`, role: "img",
          "aria-label": `${a.last_24h} of ${a.cap} posts in 24 hours` },
          el("i", { style: `width:${Math.min(100, (a.last_24h / a.cap) * 100)}%` })),
        el("small", { class: "home-count", text: `${a.last_24h} of ${a.cap} today` }));
    });
    return el("section", { class: "home-lang" },
      el("h2", {}, document.createTextNode(languageName(language)), el("small", { text: `${done} / ${cap} in 24 h` })),
      el("ul", {}, ...rows));
  });

  const latest = ig.flatMap((a) => a.posts.map((post) => ({ ...post, label: a.label })))
    .sort((x, y) => Date.parse(y.at) - Date.parse(x.at)).slice(0, 12);
  const thumbs = el("div", { class: "pcards home-latest" }, ...latest.map((post) => {
    const picture = post.thumbnail
      ? el("img", { src: `/media/${post.clip_id}/thumbnail`, alt: "", loading: "lazy" })
      : el("span", { class: "pthumb-none", text: `#${post.clip_id}` });
    const link = post.url && /^https?:\/\//.test(post.url)
      ? el("a", { class: "pthumb", href: post.url, target: "_blank", rel: "noopener noreferrer", title: post.title || "" }, picture)
      : el("span", { class: "pthumb" }, picture);
    return el("div", { class: "pcard" }, link, el("b", { text: agoShort(post.at, now) }), el("small", { text: post.label }));
  }));

  const recent = el("ol", { class: "qlist home-activity" }, ...feed.filter((i) => i.kind !== "skip").slice(0, 10).map((item) =>
    el("li", { class: "qrow" },
      el("span", { class: "qtime" }, el("b", { text: whenText(item.time, now).at }), el("small", { text: agoShort(item.time, now) })),
      el("span", { class: `awhat k-${item.kind}` }, item.tag ? el("span", { class: "qwhere", text: item.tag }) : null,
        document.createTextNode(item.text)))));

  main.replaceChildren(el("div", { class: "stats home" }, problemBox, tiles, ...langs,
    section("Latest posts", "wide", latest.length ? thumbs : el("p", { class: "note", text: "No posts in the last 24 h." })),
    section("Recent activity", "wide", recent)));
}

async function refresh() {
  const main = document.getElementById("clips");
  if (currentState === "home") {
    try { await renderHome(main); } catch (error) {
      main.replaceChildren(el("p", { class: "empty", text: `Could not load the overview: ${error.message}` }));
    }
    return;
  }
  if (GRID_STATES.includes(currentState)) {
    try {
      const clips = await api(`/api/clips?state=${currentState}`);
      if (currentState === "published") renderPosted(main, clips);
      else if (clips.length) renderClipGrid(main, clips);
      else main.replaceChildren(empty("Nothing here yet", ""));
    } catch (error) {
      main.replaceChildren(el("p", { class: "empty", text: `Could not load clips: ${error.message}` }));
    }
    return;
  }
  if (currentState === "uploads") {
    try { await refreshUploads(main); } catch (error) {
      main.replaceChildren(el("p", { class: "empty", text: `Could not load uploads: ${error.message}` }));
    }
    return;
  }
  if (currentState === "schedule") {
    try { await refreshSchedule(main); } catch (error) {
      main.replaceChildren(el("p", { class: "empty", text: `Could not load the schedule: ${error.message}` }));
    }
    return;
  }
  if (currentState === "stats") {
    try { await refreshStats(main); } catch (error) {
      main.replaceChildren(el("p", { class: "empty", text: `Could not load stats: ${error.message}` }));
    }
    return;
  }
  if (currentState === "activity") {
    try { await refreshActivity(main); } catch (error) {
      main.replaceChildren(el("p", { class: "empty", text: `Could not load activity: ${error.message}` }));
    }
    return;
  }
  try {
    const clips = await api(`/api/clips${currentState ? `?state=${currentState}` : ""}`);
    const active = document.activeElement?.closest?.(".card")?.dataset.id;
    const keep = new Map();
    const before = cards();
    const selectedAt = before.findIndex((node) => Number(node.dataset.id) === selectedId);
    for (const node of before) {
      const id = Number(node.dataset.id);
      // In the feed the clip on screen stays as it is (it may be playing).
      if (dirty.has(id) || String(id) === active || (feedOn() && id === selectedId)) keep.set(id, node);
    }
    feedObserver.disconnect();
    main.replaceChildren(...clips.map((clip) => keep.get(clip.id) || card(clip)));
    if (currentState === "new") {
      setNewCount(clips.length);
      const checking = (await api("/api/clips?state=checking")).length;
      const busy = checking ? `The AI is checking ${checking} more clip${checking === 1 ? "" : "s"}.` : "";
      if (!clips.length) main.append(empty("Nothing to review", busy || "New clips show up here when the AI wants your opinion."));
      else if (busy) main.prepend(el("p", { class: "banner", text: busy }));
    } else if (currentState === "audit") {
      if (clips.length) main.prepend(el("p", { class: "banner", text: "The AI rejected these. Approve the ones it got wrong." }));
      else main.append(empty("Nothing to check", "Every AI reject in the sample has been looked at."));
    } else if (!clips.length) main.append(empty("Nothing here yet", ""));
    // Keep the highlight on the same clip; if it left this tab, on the one that took its place.
    const after = cards();
    const same = after.find((node) => Number(node.dataset.id) === selectedId);
    // New clips arrive at the top: keep the feed on the clip being watched.
    if (same && feedOn() && main.scrollTop !== same.offsetTop) main.scrollTop = same.offsetTop;
    for (const node of after) feedObserver.observe(node);
    if (same) select(same, false);
    else if (selectedAt >= 0 && after.length) select(after[Math.min(selectedAt, after.length - 1)], false);
    else selectedId = null;
  } catch (error) {
    main.replaceChildren(el("p", { class: "empty", text: `Could not load clips: ${error.message}` }));
  }
}

function empty(title, text) {
  return el("p", { class: "empty" }, el("b", { text: title }), document.createTextNode(text));
}

function setNewCount(n) {
  document.getElementById("new-count").textContent = n ? String(n) : "";
}

function cards() {
  return [...document.querySelectorAll("#clips .card")];
}

function select(node, scroll = true) {
  for (const other of document.querySelectorAll("#clips .card.selected")) {
    if (other !== node) other.classList.remove("selected");
  }
  node.classList.add("selected");
  selectedId = Number(node.dataset.id);
  if (scroll) node.scrollIntoView({ block: feedOn() ? "start" : "nearest", behavior: "smooth" });
}

function swap(old, fresh) {
  feedObserver.unobserve(old);
  old.replaceWith(fresh);
  feedObserver.observe(fresh);
}

// Feed view: remembered per browser; on by default on touch screens.
function feedOn() {
  return document.body.classList.contains("feed");
}

function autoplay(video) {
  for (const other of document.querySelectorAll("#clips video")) if (other !== video) other.pause();
  if (!video || !video.paused) return;
  video.loop = true;
  // Sound needs an earlier tap on the page; without one, browsers only allow muted autoplay.
  video.play().catch(() => { video.muted = true; video.play().catch(() => {}); });
}

const feedObserver = new IntersectionObserver((entries) => {
  if (!feedOn()) return;
  for (const entry of entries) {
    if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
      select(entry.target, false);
      autoplay(actions.get(entry.target)?.video);
    }
  }
}, { root: document.getElementById("clips"), threshold: 0.6 });

// Feed (one clip per screen) is for reviewing: To review and Check AI. The other tabs
// keep their own layout even when Feed is on (phones default to it).
const FEED_STATES = ["new", "audit"];
let feedWanted = false;

function applyFeed() {
  const on = feedWanted && FEED_STATES.includes(currentState);
  document.body.classList.toggle("feed", on);
  const button = document.getElementById("feed-toggle");
  button.setAttribute("aria-pressed", String(feedWanted));
  button.hidden = !FEED_STATES.includes(currentState);
  return on;
}

function setFeed(wanted) {
  feedWanted = wanted;
  try { localStorage.setItem("feed", wanted ? "1" : "0"); } catch { /* storage blocked: not remembered */ }
  const on = applyFeed();
  for (const video of document.querySelectorAll("#clips video")) { video.loop = on; if (!on) video.pause(); }
  if (!on) return;
  const node = cards().find((c) => Number(c.dataset.id) === selectedId) || cards()[0];
  if (!node) return;
  select(node, false);
  node.scrollIntoView({ block: "start" });
  autoplay(actions.get(node)?.video);
}

document.getElementById("feed-toggle").addEventListener("click", () => setFeed(!feedWanted));

// Light or dark: follows the system until chosen here; the choice is kept in this browser.
function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}
function showTheme() {
  const button = document.getElementById("theme-toggle");
  button.textContent = isDark() ? "Light mode" : "Dark mode";
}
document.getElementById("theme-toggle").addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch { /* storage blocked: not remembered */ }
  showTheme();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", showTheme);
showTheme();
{
  let saved = null;
  try { saved = localStorage.getItem("feed"); } catch { /* storage blocked */ }
  feedWanted = saved ? saved === "1" : matchMedia("(hover: none)").matches;
  applyFeed();
}

const REJECT_REASONS = [["boring", "Boring"], ["no_context", "Needs context"], ["not_gta", "Not GTA"],
                        ["bad_video", "Bad video"], ["repeat", "Repeat"], ["other", "Other"]]; // store.REJECT_REASONS

function reasonLabel(code) {
  return REJECT_REASONS.find(([c]) => c === code)?.[1] || code;
}

// The database keeps UTC; the page shows Brasília time (America/Sao_Paulo), as "19/11 09:13:29".
const BRASILIA = new Intl.DateTimeFormat("en-GB", { timeZone: "America/Sao_Paulo", day: "2-digit",
  month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

function brasilia(iso, withDate = true) {
  if (!iso) return "";
  const p = Object.fromEntries(BRASILIA.formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  const time = `${p.hour}:${p.minute}:${p.second}`;
  return withDate ? `${p.day}/${p.month} ${time}` : time;
}

function compact(n) {  // 1234 -> 1.2k
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
}

function accountLabel(id) {
  return settings.accounts.find((a) => a.id === id)?.label || id;
}

function move(step) {
  const all = cards();
  if (!all.length) return;
  const at = all.findIndex((node) => Number(node.dataset.id) === selectedId);
  const next = at < 0 ? all[0] : all[Math.max(0, Math.min(all.length - 1, at + step))];
  const playing = at >= 0 && actions.get(all[at])?.video;
  if (playing && !playing.paused) playing.pause();
  select(next);
}

function current() {
  const node = cards().find((card) => Number(card.dataset.id) === selectedId);
  return node ? actions.get(node) : undefined;
}

function togglePlay(video) {
  if (!video) return;
  if (video.paused) {
    for (const other of document.querySelectorAll("#clips video")) if (other !== video) other.pause();
    video.play().catch(() => {});
  } else {
    video.pause();
  }
}

const help = document.getElementById("help");
document.getElementById("help-button").addEventListener("click", () => help.showModal());

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey || help.open) return;
  const target = event.target;
  if (target instanceof HTMLElement && target.closest("input:not([type=checkbox]), textarea, select, [contenteditable]")) {
    if (event.key === "Escape") target.blur();
    return;
  }
  const controls = current();
  const press = (button) => { if (button && !button.disabled) button.click(); };
  const tabs = [...document.querySelectorAll("#tabs button[data-section]")];
  switch (event.key) {
    case "j": case "ArrowDown": move(1); break;
    case "k": case "ArrowUp": move(-1); break;
    case " ":
      // Without a highlighted clip, Space scrolls the page as usual.
      if (!controls) return;
      togglePlay(controls.video);
      break;
    case "ArrowRight": case "ArrowLeft":
      if (!controls?.video) return;
      controls.video.currentTime = Math.max(0, controls.video.currentTime + (event.key === "ArrowRight" ? 3 : -3));
      break;
    case "a": press(controls?.approve); break;
    case "r": press(controls?.reject); break;
    case "Escape": if (controls && !controls.reasons.hidden) controls.showReasons(false); else return; break;
    case "f": if (FEED_STATES.includes(currentState)) setFeed(!feedWanted); break;
    case "?": help.showModal(); break;
    default:
      // While the reasons are open, 1 to 6 picks one instead of switching tabs.
      if (controls && !controls.reasons.hidden && /^[1-6]$/.test(event.key)) {
        press(controls.reasons.querySelectorAll("button")[Number(event.key) - 1]);
        break;
      }
      if (/^[1-5]$/.test(event.key) && tabs[Number(event.key) - 1]) {
        selectTab(tabs[Number(event.key) - 1]);
        break;
      }
      return;
  }
  event.preventDefault();
});

// Five sections; each holds one or more views (the old tabs), picked under it.
const SECTIONS = {
  home: [["home", "Overview"], ["activity", "Activity"]],
  review: [["new", "To review"], ["audit", "Check AI"]],
  pipeline: [["schedule", "Board"]],
  accounts: [["uploads", "Accounts"], ["stats", "Stats"]],
  library: [["published", "Posted"], ["approved", "Approved"], ["rejected", "Rejected"]],
};
const lastView = {}; // per section, the view last open in it

function sectionOf(state) {
  return Object.keys(SECTIONS).find((name) => SECTIONS[name].some(([view]) => view === state)) || "home";
}

function selectState(state) {
  selectedId = null;
  currentState = state;
  const section = sectionOf(state);
  lastView[section] = state;
  for (const tab of document.querySelectorAll("#tabs button[data-section]")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.section === section));
  }
  const sub = document.getElementById("subtabs");
  const views = SECTIONS[section];
  sub.hidden = views.length < 2;
  sub.replaceChildren(...views.map(([view, label]) => {
    const button = el("button", { type: "button", text: label });
    button.dataset.state = view;
    button.setAttribute("aria-pressed", String(view === state));
    return button;
  }));
  history.replaceState(null, "", `#${state}`);
  dirty.clear();
  applyFeed();
  refresh();
}

function selectTab(button) {
  const section = button.dataset.section;
  selectState(lastView[section] || SECTIONS[section][0][0]);
}

document.getElementById("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-section]");
  if (button) selectTab(button);
});
document.getElementById("subtabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-state]");
  if (button) selectState(button.dataset.state);
});

// Platforms and publish mode come from the server's config.
api("/api/settings").then((value) => {
  settings = value;
  document.getElementById("logout").hidden = !settings.login;
}).catch(() => {}).finally(start);

document.getElementById("logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  location.assign("/login");
});

function start() {
// A view can be linked: /#approved, /#schedule.
const linked = location.hash.slice(1);
const known = Object.values(SECTIONS).flat().some(([view]) => view === linked);
selectState(known ? linked : "home");
loadStatus();
}
setInterval(refresh, 20000);
setInterval(() => { if (currentState === "activity") refresh(); }, 5000); setInterval(loadStatus, 15000);
setInterval(() => { if (currentState !== "new") api("/api/clips?state=new").then((c) => setNewCount(c.length)).catch(() => {}); }, 30000);
