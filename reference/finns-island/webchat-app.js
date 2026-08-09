/* Double-O HQ client. Plain JS, no build step.
   Accessibility contract:
   - Every view change moves focus to that view's heading.
   - Agent replies are announced through one polite live region.
   - Messages are real DOM in document order — VoiceOver swipe just works. */

"use strict";

const $ = (id) => document.getElementById(id);
const views = { key: $("view-key"), list: $("view-list"), chat: $("view-chat") };

let KEY = localStorage.getItem("hq-key") || "";
let currentThread = null;
let pollTimer = null;
let lastCount = 0;      // messages rendered for the open thread
let localEcho = null;   // Kenny's just-sent text until the server confirms it
let openJump = false;   // next render is a thread-open: jump to oldest unread

// Accept a key passed once via #key=... (fragment never reaches server logs).
if (location.hash.startsWith("#key=")) {
  KEY = decodeURIComponent(location.hash.slice(5));
  localStorage.setItem("hq-key", KEY);
  history.replaceState(null, "", location.pathname);
}

// A tapped notification lands here as #open=<thread id>.
let pendingOpen = null;
if (location.hash.startsWith("#open=")) {
  pendingOpen = decodeURIComponent(location.hash.slice(6));
  history.replaceState(null, "", location.pathname);
}

function announce(text) {
  $("announce").textContent = "";
  // Second write in a tick so identical back-to-back messages still fire.
  setTimeout(() => { $("announce").textContent = text; }, 50);
}

function show(name) {
  Object.values(views).forEach((v) => { v.hidden = true; });
  views[name].hidden = false;
  const heading = views[name].querySelector("h1");
  if (heading) { heading.tabIndex = -1; heading.focus(); }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "X-Auth-Key": KEY, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (res.status === 401) { show("key"); throw new Error("bad key"); }
  return res.json();
}

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

/* ---------- agent list ---------- */

async function loadThreads() {
  const data = await api("/api/threads");
  // Group threads into collapsible sections — "Personal" plus one per
  // business. <details>/<summary> gives VoiceOver the expand/collapse
  // semantics for free; open state persists per group in localStorage.
  const groups = new Map();
  data.threads.forEach((t) => {
    const g = t.group || "Personal";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(t);
  });
  const box = $("thread-list");
  box.textContent = "";
  // Most recent conversation first, like any messaging app (Kenny's ask,
  // 2026-08-09) — a fresh message floats its agent to the top of its group.
  groups.forEach((threads) => {
    threads.sort((a, b) => ((b.last && b.last.ts) || 0) - ((a.last && a.last.ts) || 0));
  });
  groups.forEach((threads, name) => {
    const det = document.createElement("details");
    det.className = "group";
    det.open = localStorage.getItem(`hq-group-${name}`) !== "closed";
    det.addEventListener("toggle", () => {
      localStorage.setItem(`hq-group-${name}`, det.open ? "open" : "closed");
    });
    const sum = document.createElement("summary");
    sum.textContent = `${name} — ${threads.length} agent${threads.length === 1 ? "" : "s"}`;
    det.appendChild(sum);
    const ul = document.createElement("ul");
    ul.className = "threads";
    threads.forEach((t) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      const preview = t.last
        ? `${t.last.speaker}: ${t.last.text.slice(0, 90)}` : "No messages yet";
      btn.innerHTML =
        `<span class="name"></span><span class="tagline"></span><span class="preview"></span>`;
      btn.querySelector(".name").textContent = t.display;
      btn.querySelector(".tagline").textContent = t.tagline;
      btn.querySelector(".preview").textContent = preview;
      btn.addEventListener("click", () => openThread(t.id, t.display));
      li.appendChild(btn);
      ul.appendChild(li);
    });
    det.appendChild(ul);
    box.appendChild(det);
  });
  show("list");
  if (pendingOpen) {
    const t = data.threads.find((x) => x.id === pendingOpen);
    pendingOpen = null;
    if (t) openThread(t.id, t.display);
  }
}

/* ---------- chat ---------- */

/* Media in messages (2026-08-09): absolute paths to clips/photos become real
   players. Photos arrive as "[Attached photo: /path]" (folded in on send);
   clip paths show up in ARC's job-complete messages. Only paths the server's
   /api/media whitelist will actually serve get elements — anything else 403s
   harmlessly. */
const MEDIA_RE = /\/[^\s"'\]]+\.(mp4|m4v|mov|jpg|jpeg|png|gif|webp|heic)\b/gi;

function mediaURL(p) {
  return `/api/media?path=${encodeURIComponent(p)}&key=${encodeURIComponent(KEY)}`;
}

function baseName(p) { return p.slice(p.lastIndexOf("/") + 1); }

function appendMedia(art, path) {
  const ext = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  if (["mp4", "m4v", "mov"].includes(ext)) {
    const v = document.createElement("video");
    v.controls = true;
    v.preload = "metadata";
    v.playsInline = true;
    v.src = mediaURL(path);
    v.setAttribute("aria-label", `Clip preview: ${baseName(path)}`);
    art.appendChild(v);
  } else {
    const img = document.createElement("img");
    img.src = mediaURL(path);
    img.alt = `Attached photo: ${baseName(path)}`;
    img.loading = "lazy";
    art.appendChild(img);
  }
}

function renderMessages(msgs, opts = {}) {
  const box = $("messages");
  box.textContent = "";
  const all = localEcho
    ? msgs.concat([{ speaker: "Kenny", mine: true, text: localEcho, ts: Date.now() / 1000 }])
    : msgs;
  all.forEach((m) => {
    const art = document.createElement("article");
    art.className = "msg" + (m.mine ? " mine" : "");
    art.tabIndex = -1;  // focusable so open-jump can land VoiceOver here
    if (m.ts) art.dataset.ts = m.ts;
    const meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = `${m.speaker} — ${fmtTime(m.ts)}`;
    const body = document.createElement("p");
    body.className = "body";
    // Long raw paths are VoiceOver noise — swap attachment markers for a
    // short label; the media element below carries the actual content.
    body.textContent = m.text.replace(/\[Attached photo: [^\]]+\]/g, "(photo attached)");
    art.append(meta, body);
    (m.text.match(MEDIA_RE) || []).forEach((p) => appendMedia(art, p));
    box.appendChild(art);
  });
  if (!opts.noScroll) window.scrollTo(0, document.body.scrollHeight);
}

/* Opening a chat lands where Kenny actually needs to be (his ask,
   2026-08-09): on the OLDEST message that arrived since his last visit —
   so he can swipe forward through the new ones in order — or at the very
   bottom when nothing is new. Read position is remembered per thread. */
function readKey(id) { return `hq-read-${id}`; }

function jumpOnOpen(msgs) {
  const lastRead = parseFloat(localStorage.getItem(readKey(currentThread)) || "0");
  const fresh = msgs.filter((m) => !m.mine && m.ts && m.ts > lastRead);
  const arts = Array.from($("messages").children);
  let target = arts[arts.length - 1];
  if (fresh.length) {
    target = arts.find((a) => parseFloat(a.dataset.ts || "0") >= fresh[0].ts) || target;
    announce(fresh.length === 1
      ? "One new message. Starting there."
      : `${fresh.length} new messages. Starting at the oldest — swipe down to move through them.`);
  }
  if (target) {
    target.scrollIntoView({ block: fresh.length ? "start" : "end" });
    target.focus();
  }
}

function markRead(msgs) {
  const newest = msgs.reduce((mx, m) => Math.max(mx, m.ts || 0), 0);
  if (newest) localStorage.setItem(readKey(currentThread), String(newest));
}

async function refreshThread(announceNew) {
  if (!currentThread) return;
  const data = await api(`/api/thread?id=${encodeURIComponent(currentThread)}`);
  const msgs = data.messages;
  // Server history now includes Kenny's message — drop the local echo.
  if (localEcho && msgs.some((m) => m.mine && m.text === localEcho)) localEcho = null;
  const thinking = $("thinking");
  const title = $("chat-title").textContent;
  thinking.hidden = !data.pending;
  thinking.textContent = data.pending ? `${title} is thinking…` : "";
  if (msgs.length !== lastCount || localEcho) {
    const fresh = msgs.slice(lastCount).filter((m) => !m.mine);
    if (openJump) {
      renderMessages(msgs, { noScroll: true });
      jumpOnOpen(msgs);
      openJump = false;
    } else {
      renderMessages(msgs);
    }
    markRead(msgs);
    if (announceNew && fresh.length) {
      announce(fresh.map((m) => `${m.speaker} says: ${m.text}`).join(" — "));
    }
    lastCount = msgs.length;
  }
  $("send-btn").disabled = data.pending;
  if (!data.pending && pollTimer && !localEcho) slowPoll();
}

function fastPoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => refreshThread(true).catch(() => {}), 2500);
}
function slowPoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => refreshThread(true).catch(() => {}), 10000);
}

async function openThread(id, display) {
  currentThread = id;
  lastCount = 0;
  localEcho = null;
  openJump = true;  // first render lands on the oldest unread (or the bottom)
  $("chat-title").textContent = display;
  $("box-label").textContent = `Your message to ${display}`;
  $("messages").textContent = "";
  show("chat");
  await refreshThread(false).catch(() => {});
  slowPoll();
}

function closeThread() {
  clearInterval(pollTimer);
  pollTimer = null;
  currentThread = null;
  loadThreads().catch(() => show("key"));
}

/* ---------- photo attachment ---------- */

function clearAttachment() {
  $("photo-input").value = "";
  $("attach-clear").hidden = true;
  $("attach-status").textContent = "";
}

function attachmentChanged() {
  const file = $("photo-input").files[0];
  if (!file) { clearAttachment(); return; }
  $("attach-clear").hidden = false;
  $("attach-status").textContent = `Photo ready: ${file.name}. It uploads when you press Send.`;
  announce(`Photo attached: ${file.name}. It will upload when you send.`);
}

async function uploadAttachment(file) {
  const res = await fetch("/api/upload", {
    method: "POST",
    headers: {
      "X-Auth-Key": KEY,
      "X-Filename": file.name || "photo.jpg",
      "Content-Type": file.type || "application/octet-stream",
    },
    body: file,
  });
  const out = await res.json();
  if (!res.ok || !out.path) throw new Error(out.error || "upload failed");
  return out.path;
}

async function sendMessage(ev) {
  ev.preventDefault();
  const box = $("box");
  let text = box.value.trim();
  const file = $("photo-input").files[0];
  if ((!text && !file) || !currentThread) return;
  if (file) {
    $("attach-status").textContent = "Uploading photo…";
    try {
      const path = await uploadAttachment(file);
      text = (text ? text + "\n\n" : "") + `[Attached photo: ${path}]`;
      clearAttachment();
    } catch (e) {
      announce(`Photo upload failed: ${e.message}. Nothing was sent.`);
      $("attach-status").textContent = `Upload failed: ${e.message}. The photo is still attached — try Send again.`;
      return;
    }
  }
  box.value = "";
  localEcho = text;
  renderMessages([]);            // instant echo…
  refreshThread(false).catch(() => {});  // …then the real list
  try {
    const res = await api("/api/send", {
      method: "POST",
      body: JSON.stringify({ thread: currentThread, text }),
    });
    if (res.error) { announce(`Not sent: ${res.error}`); return; }
    announce("Sent. Waiting for the reply.");
    fastPoll();
  } catch {
    announce("Send failed. Check that the Mac is awake and try again.");
  }
  box.focus();
}

/* ---------- push notifications ---------- */

const PUSH_OK = "serviceWorker" in navigator && "PushManager" in window
  && "Notification" in window;
let swReg = null;

function urlB64ToU8(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function postSubscription(sub) {
  return api("/api/push/subscribe", { method: "POST", body: JSON.stringify(sub) });
}

async function initPush() {
  if (!PUSH_OK) return;  // on iOS this means: not installed to the Home Screen
  try { swReg = await navigator.serviceWorker.register("sw.js"); } catch { return; }
  // A tapped notification while HQ is already open arrives here instead of
  // through the #open= hash — jump straight to that agent's thread.
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.open) {
      pendingOpen = e.data.open;
      loadThreads().catch(() => {});
    }
  });
  if (Notification.permission === "granted") {
    const sub = await swReg.pushManager.getSubscription();
    if (sub) { postSubscription(sub).catch(() => {}); return; }
  }
  if (Notification.permission !== "denied") $("notify-btn").hidden = false;
}

async function enablePush() {
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      announce("Notifications stayed off. The button is still here if you change your mind.");
      return;
    }
    const res = await api("/api/push/vapid-key");
    const sub = await swReg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToU8(res.key),
    });
    await postSubscription(sub);
    $("notify-btn").hidden = true;
    announce("Notifications are on. Your agents can now reach you here.");
  } catch {
    announce("Could not turn notifications on. Make sure you opened HQ from the Home Screen icon and try again.");
  }
}

/* ---------- boot ---------- */

$("key-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  KEY = $("key-input").value.trim();
  localStorage.setItem("hq-key", KEY);
  loadThreads().catch(() => announce("That key didn't work. Try again."));
});
$("composer").addEventListener("submit", sendMessage);
$("photo-input").addEventListener("change", attachmentChanged);
$("attach-clear").addEventListener("click", () => {
  clearAttachment();
  announce("Photo removed.");
});
$("back-btn").addEventListener("click", closeThread);
$("back-btn-bottom").addEventListener("click", closeThread);
$("notify-btn").addEventListener("click", enablePush);

if (KEY) {
  loadThreads().catch(() => show("key"));
} else {
  show("key");
}
initPush();
