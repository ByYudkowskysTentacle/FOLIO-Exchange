// FOLIO-Exchange page. Plain JavaScript, no build step. Every value from FOLIO or the files is set as text,
// never as HTML. Actions are POSTs with the page token; long ones return a job that is polled until done.
"use strict";

const TOKEN = document.querySelector('meta[name="fx-token"]').content;
const S = { site: null, view: null, feed: null, tab: "overview", runs: [], results: {}, job: null, nav: 0,
            bottom: null, logEl: null, timeEl: null };
const busy = () => !!S.job;
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------ helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-FX-Token": TOKEN },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) {
    const err = new Error(data.error || r.statusText);
    err.status = r.status;
    throw err;
  }
  return data;
}

const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 4000);
}

function when(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

const LABELS = { success: "OK", held: "Held", blocked: "Blocked", failed: "Fail", built: "Built", preview: "Prev",
                 running: "…", none: "New" };
function badge(status, text) {
  const s = status || "none";
  return h("span", { class: `badge badge-${s}`, text: text || LABELS[s] || s });
}

function bytes(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter(Boolean).map(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]));
}

function num(n) { return n === null || n === undefined ? "–" : Number(n).toLocaleString(); }

function announce(text) {
  // For screen readers: a quiet live region, cleared first so the same words are read again.
  const el = $("live");
  el.textContent = "";
  setTimeout(() => { el.textContent = text; }, 50);
}

// One tab set. Arrow keys, Home and End move between the tabs and select them; Tab moves on into the panel.
function tablist(cls, id, names, selected, ariaLabel, title, choose) {
  const select = (t) => {
    choose(t);
    const el = $(`${id}-tab-${t}`);
    if (el) el.focus();
  };
  return h("div", {
    class: cls, role: "tablist", "aria-label": ariaLabel,
    onkeydown: (e) => {
      const i = names.indexOf(selected);
      const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: names.length - 1 }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      select(names[(to + names.length) % names.length]);
    },
  }, names.map((t) => h("button", {
    class: "tab", type: "button", role: "tab", id: `${id}-tab-${t}`, "aria-controls": `${id}-panel`,
    "aria-selected": t === selected ? "true" : "false", tabindex: t === selected ? "0" : "-1",
    onclick: () => select(t),
  }, title(t))));
}

// ------------------------------------------------------------ the page's own words
// Some text comes from the command-line side (alerts, logs, safety checks) and can end with a command to
// type. Where the page has its own button for that step (run, preview, build, release, history, pause and
// resume, the connection check), the sentence is left out here. Commands the page cannot stand in for
// (setup, storing a secret, an upgrade, checking alerts or a delivery) stay, as they are the only remedy.
const PAGE_HAS_IT = /`?folio-exchange (?:run|preview|build|release|history|feeds|check(?! --))\b/;
function pageWords(text) {
  if (!text) return text;
  const kept = String(text).split(/(?<=\.)\s+/).filter((s) => !PAGE_HAS_IT.test(s));
  return kept.length ? kept.join(" ") : String(text);
}

function nextStep(status, feed) {
  if (status === "blocked") {
    return "Look at the files (Open folder). If they are right, use Review and send…; if not, fix the records " +
      "in FOLIO and Run again.";
  }
  if (status === "held" || (status === "built" && feed && feed.delivery.delivers)) {
    return "Look at the files (Open folder), then use Review and send… if they are right.";
  }
  if (status === "built") return "This feed has nowhere to send files yet, so they stay on this computer.";
  if (status === "failed") return "Fix the problem above, then Run again.";
  return null;
}

const LEVELS = { problem: ["failed", "Problem"], warning: ["held", "Check"], note: ["none", "Note"] };
function warningText(w, onFeedPage) {
  const f = ((S.site && S.site.feeds) || []).find((x) => x.name === w.feed);
  switch (w.code) {
    case "no-alerts":
      return "No alerts are set up, so nobody would hear about a failed scheduled run. Whoever looks after this " +
        "site can add an alert webhook or email address in site.yaml.";
    case "paused":
      return onFeedPage ? "This feed is paused: scheduled runs skip it. You can still preview and run it here; Resume starts the schedule again."
        : "Paused: scheduled runs skip it. You can still preview and run it on its page, and resume it there.";
    case "recipe-update":
      return `A newer version of its recipe${f && f.newer_recipe ? ` (${f.newer_recipe})` : ""} is available. ` +
        "It changes what the vendor receives, so the feed stays on its current version until someone reviews the change.";
    case "overdue":
      return `${String(w.text).split(";")[0].replace(/\.$/, "")}. Check that the task that starts scheduled runs on this computer is still set up and running.`;
    case "alerts-never":
      return "Alerts are switched off (alerts.on is never in site.yaml), so a failed run sends no alert.";
    case "ratio-off":
      return "The item count check is off, so a sudden drop in items would be sent without a hold for review.";
    case "host-key-unconfirmed":
      return "The vendor's SFTP server key is not confirmed yet, so nothing is sent there. Once the vendor " +
        "confirms the key, record it with the setup wizard.";
    default:
      return pageWords(w.text);
  }
}

function label(key) {
  const s = String(key).replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function readable(v) {
  if (v === true) return "yes";
  if (v === false) return "no";
  if (v === null || v === undefined || v === "") return "not set";
  if (Array.isArray(v)) {
    if (!v.length) return "none";
    if (v.some((x) => x && typeof x === "object")) return h("div", {}, v.map((x) => readable(x)));
    return v.join(", ");
  }
  if (typeof v === "object") return Object.keys(v).length ? settingsList(v) : "none";
  return String(v);
}

function settingsList(obj) {
  // Settings as a person reads them: a plain label, the value in words, and the key as it is in the file.
  return h("dl", { class: "kv" }, Object.entries(obj).map(([k, v]) => [
    h("dt", {}, label(k), k.includes("_") ? h("span", { class: "key", text: k }) : null),
    h("dd", {}, readable(v))]));
}

function detailText(detail) {
  return Object.entries(detail || {}).map(([k, v]) => {
    const val = Array.isArray(v) ? v.join(", ") : v && typeof v === "object" ? JSON.stringify(v) : String(v);
    return `${label(k)}: ${val}`;
  }).join(" · ");
}

function openFolderButton(run, id) {
  if (!run || !run.id || !run.folder) return null;
  return h("button", { class: "btn btn-small", type: "button", id, title: run.folder, onclick: () => openFolder(run.id) },
    "Open folder");
}

async function openFolder(id) {
  try {
    await api(`/api/runs/${id}/open-folder`, {});
    toast("The run's folder is open in a new window.");
  } catch (e) {
    toast(e.message);
  }
}

// ------------------------------------------------------------ routing
function go(view) {
  S.view = view;
  S.nav += 1;
  const hash = view.type === "feed" ? `#feed/${view.name}` : `#${view.type}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  renderSidebar();
  if (view.type === "feed") openFeed(view.name);
  else if (view.type === "connection") renderConnection();
  else if (view.type === "activity") renderActivity();
}

function fromHash() {
  const m = location.hash.match(/^#feed\/([a-z0-9-]+)$/);
  if (m) return { type: "feed", name: m[1] };
  if (location.hash === "#connection") return { type: "connection" };
  if (location.hash === "#activity") return { type: "activity" };
  return null;
}

// ------------------------------------------------------------ sidebar
async function loadSite() {
  applySite(await api("/api/site"));
}

function applySite(s) {
  S.site = s;
  $("site-chip").textContent = `${s.name} · ${s.folio.tenant}`;
  $("site-chip").title = `${s.root}\n${s.folio.base_url} as ${s.folio.username}`;
  $("nav-connection-sub").textContent = s.folio.base_url.replace(/^https?:\/\//, "");
  $("sidebar-foot").textContent = `folio-exchange ${s.version}`;
  renderSidebar();
  if (s.job && s.job.status === "running" && !S.job) follow(s.job);   // a reload or a second tab: reattach
}

function renderSidebar() {
  const list = $("feed-list");
  list.replaceChildren();
  if (!S.site) return;
  for (const f of S.site.feeds) {
    const current = S.view && S.view.type === "feed" && S.view.name === f.name;
    list.append(h("li", {}, h("button", {
      class: "nav-item", type: "button", "aria-current": current ? "true" : "false", "data-key": `nav-${f.name}`,
      onclick: () => go({ type: "feed", name: f.name }),
    }, S.job && S.job.feed === f.name ? badge("running", "Busy") : f.enabled ? badge(f.last_status) : badge("none", "Off"),
      h("span", { class: "nav-text" }, h("span", { class: "nav-name", text: f.name }),
        h("span", { class: "nav-sub", text: f.title })))));
  }
  for (const [name, problem] of Object.entries(S.site.problems || {})) {
    list.append(h("li", {}, h("button", {
      class: "nav-item nav-problem", type: "button", title: problem, "data-key": `nav-${name}`,
      onclick: () => showProblem(name, problem),
    }, badge("failed", "Error"), h("span", { class: "nav-text" }, h("span", { class: "nav-name", text: name }),
      h("span", { class: "nav-sub", text: "the feed file has a problem" })))));
  }
  if (!S.site.feeds.length && !Object.keys(S.site.problems || {}).length) {
    list.append(h("li", { class: "nav-sub" }, h("span", { class: "muted", text: "No feeds yet. Feeds are set up with folio-exchange init in a command window." })));
  }
  $("nav-connection").setAttribute("aria-current", S.view && S.view.type === "connection" ? "true" : "false");
  $("nav-activity").setAttribute("aria-current", S.view && S.view.type === "activity" ? "true" : "false");
}

function showProblem(name, problem) {
  S.view = { type: "problem", name };
  renderSidebar();
  $("main").replaceChildren(h("div", { class: "pane" },
    h("div", { class: "notice notice-failed" }, h("p", {}, h("b", { text: `feeds/${name}.yaml has a problem. ` }),
      "Fix the file in a text editor, then reload this page.")),
    h("pre", { class: "log", text: problem })));
}

// ------------------------------------------------------------ feed view
async function openFeed(name) {
  const nav = S.nav;
  let feed, runs;
  try {
    feed = await api(`/api/feeds/${name}`);
    runs = await api(`/api/feeds/${name}/runs?limit=50`);
  } catch (e) {
    if (nav !== S.nav) return;
    $("main").replaceChildren(h("div", { class: "empty" }, h("p", { class: "empty-title", text: "Could not open the feed" }),
      h("p", { class: "error-text", text: e.message })));
    return;
  }
  if (nav !== S.nav) return;                 // the person has moved on: do not draw over the newer view
  S.feed = feed;
  S.runs = runs;
  renderFeed();
}

function closeMenus() {
  for (const m of document.querySelectorAll(".menu")) m.hidden = true;
  for (const b of document.querySelectorAll("[aria-haspopup='menu']")) b.setAttribute("aria-expanded", "false");
}

// The caret beside Run: a menu button. Enter, Space or Down opens it on the first choice, Up on the last;
// arrows move, Escape closes it and puts the focus back on the caret, Tab closes it and moves on.
function runMenu() {
  const items = [
    h("button", { type: "button", role: "menuitem", tabindex: "-1", onclick: () => { closeMenus(); startRun("run"); } },
      "Run", h("small", { text: "Update from FOLIO, build, check, then send (or hold for review)" })),
    h("button", { type: "button", role: "menuitem", tabindex: "-1", onclick: () => { closeMenus(); startRun("build"); } },
      "Build only", h("small", { text: "Update and build the files; send nothing" }))];
  const menu = h("div", {
    class: "menu", id: "run-menu", hidden: true, role: "menu", "aria-label": "Run choices",
    onkeydown: (e) => {
      const i = items.indexOf(document.activeElement);
      const to = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
      if (to !== undefined) {
        e.preventDefault();
        items[(to + items.length) % items.length].focus();
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeMenus();
        caret.focus();
      } else if (e.key === "Tab") {
        closeMenus();
      }
    },
  }, items);
  const open = (at) => {
    closeMenus();
    menu.hidden = false;
    caret.setAttribute("aria-expanded", "true");
    items[at].focus();
  };
  const caret = h("button", {
    class: "btn btn-primary btn-caret", type: "button", id: "btn-run-more", "aria-label": "More run choices",
    "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "run-menu", disabled: busy(),
    onclick: () => (menu.hidden ? open(0) : closeMenus()),
    onkeydown: (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        open(e.key === "ArrowDown" ? 0 : items.length - 1);
      }
    },
  }, "▾");
  return [caret, menu];
}

function renderFeed() {
  const f = S.feed;
  const head = h("div", { class: "feed-head" },
    h("div", { class: "title-row" }, h("h1", { text: f.title }), h("span", { class: "sub", text: f.name }),
      h("div", { class: "spacer" }),
      h("button", { class: "btn btn-ghost", type: "button", id: "btn-pause", onclick: togglePause, disabled: busy() },
        f.enabled ? "Pause" : "Resume")),
    h("div", { class: "request-bar" },
      h("div", { class: "request-field", title: f.delivery.where },
        h("span", { class: "request-recipe", text: f.recipe.ref }),
        h("span", { class: "request-dest", text: `→ ${f.delivery.where}` })),
      h("button", { class: "btn", type: "button", id: "btn-preview", disabled: busy(),
        title: "Preview (Ctrl+Enter): builds sample files and sends nothing", onclick: () => startRun("preview") }, "Preview"),
      h("div", { class: "split" },
        h("button", { class: "btn btn-primary", type: "button", id: "btn-run", disabled: busy(), onclick: () => startRun("run") }, "Run"),
        runMenu())),
    tablist("tabs", "feed", ["overview", "layout", "settings", "history"], S.tab, "Feed sections",
      (t) => [label(t), t === "history" ? h("span", { class: "count", text: S.runs.length }) : null],
      (t) => { S.tab = t; renderFeed(); }));

  const top = h("div", { class: "pane pane-top", role: "tabpanel", id: "feed-panel", "aria-labelledby": `feed-tab-${S.tab}`,
    tabindex: "0", "data-scroll": "top" }, tabContent());
  S.bottom = h("div", { class: "pane-bottom" });
  $("main").replaceChildren(head, h("div", { class: "split-v" }, top, S.bottom));
  renderResult();
}

function renderResult() {
  if (!S.bottom || !S.bottom.isConnected || !S.feed) return;
  S.logEl = S.timeEl = null;
  S.bottom.replaceChildren(...[resultPanel()].flat(Infinity).filter(Boolean));
}

function tabContent() {
  const f = S.feed;
  if (S.tab === "layout") return layoutTab(f);
  if (S.tab === "settings") return settingsTab(f);
  if (S.tab === "history") return historyTab();
  return overviewTab(f);
}

function overviewTab(f) {
  const out = [];
  if (f.waiting) {
    const w = f.waiting;
    const why = pageWords(w.held || w.error) || (w.status === "built" ? "built without sending" : "");
    out.push(h("div", { class: "notice notice-held" }, badge(w.status),
      h("p", {}, h("b", { text: `The run of ${when(w.started)} is waiting for you. ` }), why),
      openFolderButton(w, "btn-folder-waiting"),
      h("button", { class: "btn btn-primary", type: "button", id: "btn-review", disabled: busy(), onclick: reviewRelease }, "Review and send…")));
  } else if (f.last_run && f.last_run.status === "failed") {
    out.push(h("div", { class: "notice notice-failed" }, badge("failed"),
      h("p", {}, h("b", { text: "The last run failed. " }), pageWords(f.last_run.error) || "")));
  }
  // The site's warnings about this feed (paused, overdue, a newer recipe, …), in the page's words.
  for (const w of ((S.site && S.site.warnings) || []).filter((x) => x.feed === f.name && x.code !== "feed-file")) {
    out.push(h("div", { class: `notice ${w.level === "problem" ? "notice-failed" : "notice-info"}` }, h("p", { text: warningText(w, true) })));
  }
  const last = f.last_run;
  out.push(kv([
    ["Recipe", `${f.recipe.title} (${f.recipe.vendor}), ${f.recipe.ref}`],
    ["Uses", h("code", { text: f.uses })],
    ["Schedule", f.enabled && f.schedule.next ? `${f.schedule.describe}; next ${when(f.schedule.next)}` : f.schedule.describe],
    ["Sends to", h("span", { class: "mono", text: f.delivery.where })],
    ["Last run", last ? h("span", {}, badge(last.status), ` ${when(last.started)} · ${num(last.items)} items, ${num(last.bibs)} bibs`) : "never"],
    last && nextStep(last.status, f) && !f.waiting ? ["Next step", nextStep(last.status, f)] : null,
    ["Feed file", h("span", { class: "mono", text: f.file })],
  ]));
  return out;
}

function fileName(f, o, ext) {
  return `${o.file_name.replace("{prefix}", f.file_prefix).replace("{date}", "YYYYMMDD")}.${ext}`;
}

function layoutTab(f) {
  return f.layout.map((o) => {
    if (o.format !== "delimited") {
      const subs = Object.entries(o.item_field.subfields || {}).map(([c, fld]) => `$${c} ${fld}`).join(", ");
      return [h("div", { class: "section-title" }, `${o.name} · MARC 21 · `, h("code", { text: fileName(f, o, "mrc") })),
        h("p", {}, "One record per instance; 001 = instance HRID; 999 ff $i = instance UUID",
          o.split_every ? `, a new file every ${num(o.split_every)} records.` : ".",
          o.embed_items ? ` Items embedded in ${o.item_field.tag}: ${subs}.` : ` No item fields; any ${o.item_field.tag} is removed.`)];
    }
    const delim = { "|": "pipe |", ",": "comma", "\t": "tab" }[o.delimiter] || o.delimiter;
    return [h("div", { class: "section-title" }, `${o.name} · `, h("code", { text: fileName(f, o, o.extension) })),
      h("p", { class: "muted", text: `${delim}-separated · header ${o.header ? "yes" : "no"} · empty values “${o.empty_value}” · dates ${o.date_format}` }),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", { class: "num", text: "#" }), h("th", { text: "Header" }),
        h("th", { text: "Field" }), h("th", { text: "What it holds" }))),
        h("tbody", {}, o.columns.map((c, i) => h("tr", {},
          h("td", { class: "num", text: i + 1 }),
          h("td", {}, c.header, c.required ? h("span", { class: "req", title: "required by the recipe", text: " *" }) : null),
          h("td", { class: "mono", text: c.field }), h("td", { text: c.description })))))];
  });
}

function settingsTab(f) {
  return [
    h("p", { class: "muted" }, "To change these, edit ", h("code", { text: f.file }), " in a text editor, reload this page, ",
      "then use Preview to try the change."),
    h("div", { class: "section-title", text: "Filters" }),
    Object.keys(f.filters).length ? settingsList(f.filters) : h("p", { class: "muted", text: "Every record the recipe selects." }),
    h("div", { class: "section-title", text: "Options" }),
    Object.keys(f.options).length ? settingsList(f.options) : h("p", { class: "muted", text: "The recipe's defaults." }),
    h("div", { class: "section-title", text: "Checks before sending" }), kv([
      ["Minimum item count", f.checks.min_item_ratio ? `${Math.round(f.checks.min_item_ratio * 100)}% of the last delivery` : "off"],
      ["Must be filled", (f.checks.must_be_filled || []).join(", ") || "nothing"]]),
    Object.keys(f.legacy).length ? [h("div", { class: "section-title", text: "Migrated values (item note types)" }), settingsList(f.legacy)] : null,
  ];
}

function historyTab() {
  if (!S.runs.length) return h("p", { class: "muted", text: "No runs yet." });
  return h("table", {}, h("thead", {}, h("tr", {}, ["Status", "Started", "By", "Items", "Bibs", "Folder", "Note"].map((t) =>
    h("th", { class: ["Items", "Bibs"].includes(t) ? "num" : null, text: t })))),
  // The row opens the run on a click; its first cell is a real button, so Tab reaches it and Enter or Space opens it.
  h("tbody", {}, S.runs.map((r) => h("tr", { class: "clickable", onclick: () => showRun(r.id) },
    h("td", {}, h("button", { class: "row-open", type: "button", "data-key": `run-${r.id}`,
      "aria-label": `Open the run of ${when(r.started)}: ${r.status}`,
      onclick: (e) => { e.stopPropagation(); showRun(r.id); } }, badge(r.status))),
    h("td", { text: when(r.started) }), h("td", { text: r.trigger || "" }),
    h("td", { class: "num", text: num(r.items) }), h("td", { class: "num", text: num(r.bibs) }),
    h("td", { class: "mono", text: r.folder || "" }),
    h("td", { class: "muted", text: r.released_by ? `released by ${r.released_by}` : (pageWords(r.error || r.held) || "").slice(0, 90) })))));
}

// ------------------------------------------------------------ result panel
function otherJobNotice() {
  const j = S.job;
  if (!j || j.feed === S.feed.name) return null;
  const what = j.feed ? `${j.kind} of ${j.feed}` : "connection check";
  return h("div", { class: "notice notice-info" }, h("p", { text: `A ${what} is running; other actions wait until it finishes.` }),
    j.feed ? h("button", { class: "btn", type: "button", onclick: () => go({ type: "feed", name: j.feed }) }, "Open it") : null);
}

function resultPanel() {
  const R = S.results[S.feed.name];
  const notice = otherJobNotice();
  if (!R) {
    return [notice, h("div", { class: "empty" }, h("p", { class: "empty-title", text: "No result yet" }),
      h("p", { text: "Preview builds a sample from live data. Run updates from FOLIO, builds the files and sends them, or holds them for you to review." }))];
  }
  if (R.job && R.job.status === "running") {
    S.timeEl = h("b", { text: `${R.job.seconds} s` });
    S.logEl = h("pre", { class: "log", "data-job": R.job.id, text: R.job.log.join("\n") || "Starting…" });
    return [h("div", { class: "result-head" }, h("span", { class: "spinner" }),
      h("span", { class: "label", text: `${{ preview: "Previewing", run: "Running", build: "Building", release: "Sending", check: "Checking" }[R.job.kind] || "Working"}…` }),
      h("span", { class: "stat" }, "Time ", S.timeEl),
      h("span", { class: "stat", text: "Keep the folio-exchange window open until this finishes." })),
    h("div", { class: "result-body" }, S.logEl)];
  }
  if (R.lost) {
    return [notice, h("div", { class: "result-head" }, badge("none", "?"), h("span", { class: "label", text: "Lost contact" })),
      h("div", { class: "result-body" }, h("p", { text: "Lost contact with folio-exchange while it was working; the run may have finished or stopped. Reload the page to see." }))];
  }
  if (R.error) {
    return [notice, h("div", { class: "result-head" }, badge("failed"), h("span", { class: "label", text: "Did not run" })),
      h("div", { class: "result-body" }, h("p", { class: "error-text", text: pageWords(R.error) }),
        R.job && R.job.log.length ? h("pre", { class: "log", text: R.job.log.join("\n") }) : null)];
  }
  const run = R.run || {};
  const rep = R.report || {};
  const b = rep.build || {};
  const tabs = ["rows", "summary"].concat(R.job ? ["log"] : []);
  const head = h("div", { class: "result-head" }, badge(run.status || R.status),
    h("span", { class: "label", text: { success: "Delivered", held: "Held for review", blocked: "Blocked", failed: "Failed", built: "Built, not sent", preview: "Preview" }[run.status] || run.status }),
    R.job ? h("span", { class: "stat" }, "Time ", h("b", { text: `${R.job.seconds} s` })) : h("span", { class: "stat", text: when(run.started) }),
    h("span", { class: "stat" }, "Bibs ", h("b", { text: num(run.bibs ?? b.bibs_written) })),
    h("span", { class: "stat" }, "Items ", h("b", { text: num(run.items ?? b.items_written) })),
    h("span", { class: "stat" }, "Files ", h("b", { text: (run.files || []).length })),
    openFolderButton(run, "btn-folder-result"),
    S.feed && S.feed.waiting && S.feed.waiting.id === run.id
      ? h("button", { class: "btn btn-primary", type: "button", id: "btn-review-result", disabled: busy(), onclick: reviewRelease }, "Review and send…") : null,
    tablist("result-tabs", "result", tabs, tabs.includes(R.tab) ? R.tab : "rows", "Result views", label,
      (t) => { R.tab = t; renderResult(); if (t === "rows") loadRows(); }));
  const tab = tabs.includes(R.tab) ? R.tab : "rows";
  const panel = { role: "tabpanel", id: "result-panel", "aria-labelledby": `result-tab-${tab}`, tabindex: "0", "data-scroll": "result" };
  let body;
  if (tab === "summary") body = h("div", { class: "result-body", ...panel }, runSummary(run, rep));
  else if (tab === "log") body = h("div", { class: "result-body", ...panel }, h("pre", { class: "log", text: (R.job ? R.job.log : []).join("\n") }));
  else body = h("div", { class: "result-body flush", ...panel }, rowsView(R));
  return [notice, head, body];
}

function runSummary(run, rep) {
  const b = rep.build || {};
  const skipped = Object.entries(b.items_skipped || {}).map(([k, v]) => `${k}: ${num(v)}`).join(", ");
  return [
    kv([
      ["Status", h("span", {}, badge(run.status), " ", run.status)],
      ["Started", when(run.started)],
      ["Recipe", run.recipe],
      rep.held ? ["Held because", pageWords(rep.held)] : null,
      run.error ? ["Problem", h("span", { class: "error-text", text: pageWords(run.error) })] : null,
      run.released_after ? ["Released despite", pageWords(run.released_after)] : null,
      run.released_by ? ["Released by", run.released_by] : null,
      nextStep(run.status, S.feed) ? ["Next step", nextStep(run.status, S.feed)] : null,
      ["Items left out", skipped || "none"],
      b.items_without_barcode ? ["Items without barcode", num(b.items_without_barcode)] : null,
      b.duplicate_barcodes ? ["Duplicate barcodes", num(b.duplicate_barcodes)] : null,
      b.bibs_written !== undefined ? ["Bibs", `${num(b.bibs_written)} (${num(b.bibs_from_srs)} from MARC, ${num(b.bibs_generated)} generated)`] : null,
      ["Folder", h("span", { class: "mono", text: run.folder || "" })],
    ]),
    h("div", { class: "section-title", text: "Files" }),
    (run.files || []).length ? h("ul", { class: "file-list" }, run.files.map((x) => h("li", { text: `${x.name}  ${bytes(x.bytes)}` }))) : h("p", { class: "muted", text: "None." }),
    (run.destinations || []).length ? [h("div", { class: "section-title", text: "Sent to" }),
      h("ul", { class: "file-list" }, run.destinations.map((d) => h("li", { text: d })))] : null,
  ];
}

function rowsView(R) {
  if (!R.rows) return h("div", { class: "result-body" }, h("p", { class: "muted", text: R.rowsError || "Loading rows…" }));
  const r = R.rows;
  if (!r.header.length) return h("div", { class: "result-body" }, h("p", { class: "muted", text: "This run has no item rows to show." }));
  return [h("div", { class: "result-head" }, h("span", { class: "stat" }, h("b", { text: r.file })),
      h("span", { class: "stat", text: r.total > r.rows.length ? `first ${num(r.rows.length)} of ${num(r.total)} rows` : `${num(r.total)} rows` }),
      (r.outputs || []).length > 1 ? h("select", { id: "rows-output", "aria-label": "Which file", onchange: (e) => loadRows(e.target.value) },
        r.outputs.map((o) => h("option", { value: o, selected: o === r.output }, o))) : null),
    h("div", { class: "table-wrap", "data-scroll": "rows" }, h("table", { class: "rows" },
      h("thead", {}, h("tr", {}, r.header.map((c) => h("th", { text: c })))),
      h("tbody", {}, r.rows.map((row) => h("tr", {}, row.map((c) => h("td", { text: c }))))))) ];
}

async function loadRows(output) {
  const name = S.feed && S.feed.name;
  const R = name && S.results[name];
  if (!R || !R.run || !R.run.id) return;
  try {
    R.rows = await api(`/api/runs/${R.run.id}/rows?limit=200${output ? `&output=${encodeURIComponent(output)}` : ""}`);
  } catch (e) {
    R.rowsError = e.message;
  }
  if (S.results[name] === R && S.feed && S.feed.name === name) renderResult();
}

async function showRun(id) {
  const name = S.feed.name;
  let run;
  try {
    run = await api(`/api/runs/${id}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  if (S.job && S.job.feed === name) {            // a running job owns the result panel
    toast("Wait for the running action to finish, then open the run again.");
    return;
  }
  S.results[name] = { run, report: run.report, tab: "rows" };
  if (S.feed && S.feed.name === name) {
    renderResult();
    loadRows();
    announce(`Showing the run of ${when(run.started)} below.`);
  }
}

// ------------------------------------------------------------ actions
function showProgress(j, fresh) {
  // Update the open result panel in place: no redraw, so scrolling, selection and focus stay put.
  if (S.timeEl && S.timeEl.isConnected) S.timeEl.textContent = `${j.seconds} s`;
  const pre = S.logEl;
  if (!pre || !pre.isConnected || pre.dataset.job !== j.id || !fresh.length) return;
  const box = pre.parentElement;
  const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  pre.textContent = pre.textContent === "Starting…" ? fresh.join("\n") : `${pre.textContent}\n${fresh.join("\n")}`;
  if (atEnd) box.scrollTop = box.scrollHeight;
}

async function watch(job) {
  let since = 0;
  let misses = 0;
  const lines = [...(job.log || [])];
  since = job.log_total || lines.length;
  while (true) {
    let j;
    try {
      j = await api(`/api/jobs/${job.id}?since=${since}`);
      misses = 0;
    } catch (e) {
      if (++misses > 3) throw e;          // a brief hiccup is retried; a dead server is reported
      await sleep(1000);
      continue;
    }
    const fresh = j.log;
    lines.push(...fresh);
    since = j.log_total;
    j.log = lines;
    if (j.feed && S.results[j.feed] && S.results[j.feed].job && S.results[j.feed].job.id === j.id) S.results[j.feed].job = j;
    if (j.status === "running") {
      S.job = j;
      showProgress(j, fresh);
      await sleep(700);
      continue;
    }
    return j;
  }
}

async function follow(job) {
  // Follow a job to its end (one started here, or found running after a reload) and show its result.
  S.job = job;
  if (job.feed) S.results[job.feed] = { job: { ...job, log: job.log || [] }, tab: "rows" };
  renderSidebar();
  announce(`${{ preview: "Previewing", run: "Running", build: "Building", release: "Sending", check: "Checking the connection" }[job.kind] || "Working"}${job.feed ? ` ${job.feed}` : ""}…`);
  if (S.view && S.view.type === "feed" && S.feed) renderFeed();
  let done = null;
  try {
    done = await watch(job);
  } catch (e) {
    if (job.feed) S.results[job.feed] = { lost: true };
  }
  S.job = null;
  if (done && job.feed) {
    if (done.status === "error") {
      S.results[job.feed] = { job: done, error: done.error };
    } else {
      S.results[job.feed] = { job: done, run: done.result.run, report: done.result.report, tab: "rows" };
      toast({ success: "Delivered.", held: "Held for your review.", blocked: "Blocked: see the summary.", failed: "The run failed: see the summary.",
              built: "Built. Nothing was sent.", preview: "Preview ready." }[done.result.status] || done.result.status);
    }
  }
  try {
    await loadSite();
  } catch (e) {
    toast(e.message);
  }
  if (S.view && S.view.type === "feed" && S.feed) {
    await openFeed(S.feed.name);
    const R = S.results[S.feed.name];
    if (R && R.run && !R.rows) loadRows();
  } else {
    renderSidebar();
  }
  return done;
}

async function startRun(kind, body) {
  if (busy()) {
    toast("Another action is running; wait for it to finish.");
    return;
  }
  const name = S.feed.name;
  let job;
  try {
    job = await api(`/api/feeds/${name}/${kind}`, body || {});
  } catch (e) {
    if (e.status === 409) {               // started elsewhere (another tab): show that one instead of a failure
      toast(e.message);
      await loadSite();
    } else {
      S.results[name] = { error: e.message };
      renderResult();
    }
    return;
  }
  await follow(job);
}

async function togglePause() {
  const f = S.feed;
  try {
    await api(`/api/feeds/${f.name}/${f.enabled ? "pause" : "resume"}`, {});
    toast(f.enabled ? `${f.name} paused.` : `${f.name} resumed.`);
  } catch (e) {
    toast(e.message);
  }
  await loadSite();
  await openFeed(f.name);
}

async function reviewRelease() {
  let s;
  try {
    s = await api(`/api/feeds/${S.feed.name}/release-preview`, {});
  } catch (e) {
    toast(e.message);
    return;
  }
  const dlg = $("modal");
  const last = s.last_delivered_items;
  $("modal-body").replaceChildren(...[
    h("h2", { text: "Send these files?" }),
    kv([
      ["Run", `${when(s.built)}, ${s.recipe}`],
      ["Waiting because", pageWords(s.reason) || "it was built without sending"],
      ["Items", last ? `${num(s.items)} (last delivery ${num(last)}, ${when(s.last_delivered)})` : `${num(s.items)} (nothing delivered there before)`],
      ["Bibs", num(s.bibs)],
      ["Sends to", h("span", { class: "mono", text: s.destination })],
    ]),
    h("ul", { class: "file-list" }, s.files.map(([n, size]) => h("li", { text: `${n}  ${bytes(size)}` }))),
    (s.newer_runs || []).length ? h("p", { class: "error-text", text: "A newer run was delivered since this one was built." }) : null,
    h("div", { class: "modal-actions" },
      h("button", { class: "btn", value: "cancel", type: "submit" }, "Cancel"),
      h("button", { class: "btn btn-primary", value: "send", type: "submit" }, "Send files")),
  ].filter(Boolean));
  dlg.returnValue = "";
  dlg.onclose = () => {
    if (dlg.returnValue !== "send") return;
    if (busy()) {
      toast("Another action is running; nothing was sent.");
      return;
    }
    startRun("release", { run_dir: s.run_dir });
  };
  dlg.showModal();
}

// ------------------------------------------------------------ connection and activity
let lastCheck = null;
function renderConnection() {
  const s = S.site;
  const run = h("button", { class: "btn btn-primary", type: "button", id: "btn-check-main", disabled: busy(), onclick: runCheck }, "Check connection");
  const parts = [h("div", { class: "feed-head" }, h("div", { class: "title-row" }, h("h1", { text: "Connection" }),
      h("span", { class: "sub", text: "FOLIO" }), h("div", { class: "spacer" }), run))];
  const body = [kv([
    ["Address", h("span", { class: "mono", text: s.folio.base_url })],
    ["Tenant", s.folio.tenant],
    ["Account", s.folio.username],
    ["Password", s.folio.password],
    ["Alerts", s.alerts.on === "never" || (!s.alerts.webhook && !s.alerts.email) ? "none set up" : [s.alerts.webhook && "webhook", s.alerts.email && "email"].filter(Boolean).join(" and ")],
    ["Site folder", h("span", { class: "mono", text: s.root })],
  ])];
  const warnings = s.warnings || [];
  body.push(h("div", { class: "section-title", text: "Before you leave it to run on its own" }),
    warnings.length ? h("ul", { class: "warnings" }, warnings.map((w) => {
      const [cls, word] = LEVELS[w.level] || LEVELS.note;
      const known = w.feed && s.feeds.some((f) => f.name === w.feed);
      return h("li", {}, badge(cls, word),
        h("p", {}, w.feed ? h("b", { text: `${w.feed}: ` }) : null, warningText(w, false)),
        known ? h("button", { class: "btn btn-small", type: "button", "data-key": `warn-${w.code}-${w.feed}`,
          onclick: () => go({ type: "feed", name: w.feed }) }, "Open feed") : null);
    })) : h("p", { class: "muted", text: "Nothing to look at: every feed is set up to run on its own." }));
  if (lastCheck && lastCheck.running) body.push(h("p", {}, h("span", { class: "spinner" }), " Checking…"));
  else if (lastCheck && lastCheck.error) body.push(h("div", { class: "notice notice-failed" }, h("p", { text: lastCheck.error })));
  else if (lastCheck && lastCheck.result) {
    const c = lastCheck.result;
    if (c.note) body.push(h("div", { class: "notice notice-info" }, h("p", { text: c.note })));
    body.push(h("div", { class: "section-title", text: `What this account can read (${lastCheck.seconds} s)` }),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "" }), h("th", { text: "Records" }), h("th", { text: "Path" }), h("th", { class: "num", text: "Count" }))),
        h("tbody", {}, c.endpoints.map((e) => h("tr", {},
          h("td", {}, badge(e.status === "ok" ? "success" : "failed", e.status === "ok" ? "OK" : e.status)),
          h("td", { text: e.name }), h("td", { class: "mono", text: e.path }),
          h("td", { class: "num", text: e.status === "ok" ? (e.count === null ? "–" : num(e.count)) : "" }))))));
    if (c.denied.length) {
      body.push(h("div", { class: "section-title", text: "Ask your FOLIO administrator for these permissions" }),
        h("ul", { class: "file-list" }, c.denied.map((p) => h("li", { text: p }))));
    }
  }
  parts.push(h("div", { class: "pane" }, body));
  $("main").replaceChildren(...parts);
}

async function runCheck() {
  if (busy()) {
    toast("Another action is running; wait for it to finish.");
    return;
  }
  let job;
  try {
    job = await api("/api/check", {});
  } catch (e) {
    if (e.status === 409) {
      toast(e.message);
      await loadSite();
      return;
    }
    lastCheck = { error: e.message };
    go({ type: "connection" });
    return;
  }
  lastCheck = { running: true };
  go({ type: "connection" });
  const done = await follow(job);
  lastCheck = !done ? { error: "Lost contact with folio-exchange during the check. Reload the page." }
    : done.status === "error" ? { error: done.error } : { result: done.result, seconds: done.seconds };
  if (S.view && S.view.type === "connection") renderConnection();
}

async function renderActivity() {
  const nav = S.nav;
  $("main").replaceChildren(h("div", { class: "pane" }, h("span", { class: "spinner" })));
  let rows = [];
  try {
    rows = await api("/api/audit?limit=100");
  } catch (e) {
    if (nav === S.nav) $("main").replaceChildren(h("div", { class: "pane" }, h("p", { class: "error-text", text: e.message })));
    return;
  }
  if (nav !== S.nav) return;
  $("main").replaceChildren(h("div", { class: "feed-head" }, h("div", { class: "title-row" }, h("h1", { text: "Activity" }),
      h("span", { class: "sub", text: "settings changes, releases, overrides and secrets stored" }))),
    h("div", { class: "pane" }, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, ["When", "Who", "Feed", "What", "Details"].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((a) => h("tr", {}, h("td", { text: when(a.ts) }), h("td", { text: a.actor || "" }),
        h("td", { text: a.feed || "" }), h("td", { text: a.action }),
        h("td", { text: detailText(a.detail).slice(0, 240) })))))
      : h("p", { class: "muted", text: "Nothing recorded yet." })));
}

// ------------------------------------------------------------ start
$("btn-check").addEventListener("click", runCheck);
$("nav-connection").addEventListener("click", () => go({ type: "connection" }));
$("nav-activity").addEventListener("click", () => go({ type: "activity" }));
// ------------------------------------------------------------ keeping the page current
// Scheduled runs happen while the page sits open. Every 45 seconds, and when the tab comes back into view,
// the sidebar and the open feed are fetched again and redrawn only if something changed, keeping the scroll
// position and the focus. Nothing is fetched while the tab is hidden, a job is running (it has its own
// polling), the dialog is open or the Run menu or a drop-down is in use.
const REFRESH_MS = 45000;
let lastRefresh = Date.now();

function quiet() {
  const a = document.activeElement;
  return document.hidden || busy() || $("modal").open || !!document.querySelector(".menu:not([hidden])")
    || (a && a.tagName === "SELECT");
}

function snapshot() {
  const a = document.activeElement;
  return {
    key: a && a !== document.body ? a.id || (a.dataset && a.dataset.key) || null : null,
    scrolls: [...document.querySelectorAll("#main [data-scroll]")].map((el) => [el.dataset.scroll, el.scrollTop, el.scrollLeft]),
    main: $("main").scrollTop, page: window.scrollY,
  };
}

function restore(snap) {
  for (const [k, top, left] of snap.scrolls) {
    const el = document.querySelector(`#main [data-scroll="${k}"]`);
    if (el) { el.scrollTop = top; el.scrollLeft = left; }
  }
  $("main").scrollTop = snap.main;
  window.scrollTo(window.scrollX, snap.page);
  if (!snap.key) return;
  const el = document.getElementById(snap.key) || document.querySelector(`[data-key="${CSS.escape(snap.key)}"]`);
  if (el && el !== document.activeElement) el.focus({ preventScroll: true });
}

function redraw(draw) {
  const snap = snapshot();
  draw();
  restore(snap);
}

async function refresh() {
  if (quiet()) return;
  lastRefresh = Date.now();
  const nav = S.nav;
  let site;
  try {
    site = await api("/api/site");
  } catch (e) {
    return;                                  // quiet: the next turn tries again
  }
  if (nav !== S.nav || quiet()) return;
  const siteChanged = JSON.stringify(site) !== JSON.stringify(S.site);
  if (siteChanged) redraw(() => applySite(site));
  if (busy()) return;                        // applySite found a job started elsewhere and follows it
  const v = S.view;
  if (v && v.type === "connection" && siteChanged && !(lastCheck && lastCheck.running)) {
    redraw(renderConnection);
  } else if (v && v.type === "feed" && S.feed && S.feed.name === v.name) {
    let feed, runs;
    try {
      feed = await api(`/api/feeds/${v.name}`);
      runs = await api(`/api/feeds/${v.name}/runs?limit=50`);
    } catch (e) {
      return;
    }
    if (nav !== S.nav || quiet() || !S.feed || S.feed.name !== v.name) return;
    if (!siteChanged && JSON.stringify(feed) === JSON.stringify(S.feed) && JSON.stringify(runs) === JSON.stringify(S.runs)) return;
    S.feed = feed;
    S.runs = runs;
    redraw(renderFeed);
  }
}

setInterval(refresh, REFRESH_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() - lastRefresh > 5000) refresh();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const inMenu = e.target.closest && e.target.closest(".split");
    closeMenus();
    if (inMenu && $("btn-run-more")) $("btn-run-more").focus();
  }
  if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
  // Ctrl+Enter previews (it never sends anything), and never while a dialog or a form field has the keys.
  if ($("modal").open || (e.target.closest && e.target.closest("dialog, input, select, textarea"))) return;
  if (S.view && S.view.type === "feed" && S.feed && !busy()) startRun("preview");
});
document.addEventListener("click", (e) => {
  if (!(e.target.closest && e.target.closest(".split"))) closeMenus();
});
window.addEventListener("hashchange", () => { const v = fromHash(); if (v) go(v); });

(async () => {
  try {
    await loadSite();
  } catch (e) {
    $("main").replaceChildren(h("div", { class: "empty" }, h("p", { class: "empty-title", text: "Could not load the site" }),
      h("p", { class: "error-text", text: e.message })));
    return;
  }
  const v = fromHash() || (S.site.feeds.length ? { type: "feed", name: S.site.feeds[0].name } : null);
  if (v) go(v);
})();
