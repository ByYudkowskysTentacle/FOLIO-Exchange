// FOLIO-Exchange page. Plain JavaScript, no build step. Every value from FOLIO or the files is set as text,
// never as HTML. The browser is signed in by a cookie it got from a one-time link; actions are POSTs that also
// carry the page token. Long actions return a job that is polled until done.
"use strict";

const TOKEN = document.querySelector('meta[name="fx-token"]').content;
const S = { site: null, view: null, feed: null, tab: "overview", runs: [], results: {}, job: null, nav: 0,
            bottom: null, logEl: null, timeEl: null,
            settings: {},    // per feed: what its forms show (GET /api/feeds/<name>/settings)
            forms: {},       // per feed: its open edit forms {settings, delivery}, kept so a redraw keeps what was typed
            deliv: {},       // per feed: the server key read on saving, the test result, the key just stored
            notes: {},       // per feed: "new" right after it was added here
            choices: null,   // the tenant's locations, item statuses and note types (read once)
            wizard: null, recipes: null, checks: {}, connForm: null, focus: null,
            signedOut: false };   // the server no longer knows this tab (restarted or closed)
const busy = () => !!S.job;
const $ = (id) => document.getElementById(id);

// ---- pure helpers: begin (no page access; tests run this block with Node)
const FEED_NAME = /^[a-z0-9][a-z0-9-]*$/;
const RESERVED_NAMES = ["folio", "smtp", "webhook", "all"];
const SCHEDULES = [["Sun 03:30", "Weekly, on Sunday at 03:30"], ["daily 03:30", "Daily at 03:30"],
                   ["monthly 1 03:30", "Monthly, on the 1st at 03:30"], ["manual", "Only when run by hand"]];
const FILTER_LISTS = ["exclude_location_codes", "exclude_item_statuses", "include_location_codes"];
const FILTER_FLAGS = ["include_suppressed_instances", "include_suppressed_items", "include_suppressed_holdings",
                      "include_items_without_barcode", "bibs_without_items"];
const LEGACY_KEYS = ["circ_count_note_type", "created_date_note_type", "last_circ_date_note_type"];
const SETTING_NAMES = {
  title: "Title", schedule: "Schedule", file_prefix: "File name prefix",
  exclude_location_codes: "Locations left out", exclude_item_statuses: "Item statuses left out",
  include_location_codes: "Only these locations",
  include_suppressed_instances: "Include instances suppressed from discovery",
  include_suppressed_items: "Include items suppressed from discovery",
  include_suppressed_holdings: "Include holdings suppressed from discovery",
  include_items_without_barcode: "Include items without a barcode",
  bibs_without_items: "Include bibs that have no items",
  min_item_ratio: "Block a delivery if the item count falls below",
  circ_count_note_type: "Loan count from the old system",
  created_date_note_type: "Date added in the old system",
  last_circ_date_note_type: "Last loan date in the old system",
};

// A feed name that is free: the recipe's own (chq), else chq-2, chq-3 and so on.
function freeName(base, taken) {
  const clean = String(base || "feed").toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "") || "feed";
  const used = new Set(taken || []);
  if (!used.has(clean) && !RESERVED_NAMES.includes(clean)) return clean;
  for (let i = 2; ; i += 1) if (!used.has(`${clean}-${i}`)) return `${clean}-${i}`;
}

function nameProblem(name, taken) {
  if (!name) return "Enter a name for the feed.";
  if (!FEED_NAME.test(name)) return "Use lowercase letters, digits and hyphens, starting with a letter or digit, e.g. chq.";
  if (RESERVED_NAMES.includes(name)) return `${name} is reserved; choose another name.`;
  if ((taken || []).includes(name)) return `There is already a feed called ${name}; choose another name.`;
  return null;
}

function schedulePreset(text) {
  const t = String(text || "").trim().replace(/\s+/g, " ").toLowerCase();
  const hit = SCHEDULES.find(([v]) => v.toLowerCase() === t);
  return hit ? hit[0] : null;
}

function urlProblem(url) {
  const u = String(url || "").trim();
  if (/^https:\/\/[^\s/]+/.test(u) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?(\/|$)/.test(u)) return null;
  return "Enter the full address, starting with https:// (on Eureka, the Kong gateway URL).";
}

function folderProblem(text) {
  const t = String(text || "").trim();
  if (!t) return "Enter the folder to copy the files into.";
  if (/^[A-Za-z]:[\\/]/.test(t) || /^\\\\[^\\]+\\[^\\]+/.test(t) || t.startsWith("/") || t.startsWith("~")) return null;
  return "Enter the folder's full path, such as D:\\Exports\\chq or \\\\server\\share\\chq.";
}

function fingerprintProblem(text) {
  const t = String(text || "").replace(/\s+/g, "");
  if (!t) return null;
  if (/^(?:MD5:)?(?:[0-9a-f]{2}:){15}[0-9a-f]{2}$/i.test(t)) return null;
  if (/^(?:SHA256:)?[A-Za-z0-9+/]{20,}={0,2}$/i.test(t)) return null;
  return "That does not look like a server key fingerprint. It starts with SHA256: (or is 16 pairs such as " +
    "3f:a2:…); copy it exactly as the vendor gave it.";
}

// Where a secret comes from, in words (the page is told where, never the secret itself).
function secretWhere(source) {
  const s = String(source || "");
  if (!s || s.startsWith("NOT FOUND")) return { stored: false, text: "Not stored yet" };
  if (s.startsWith("system keyring")) return { stored: true, text: `Stored in Windows Credential Manager${s.slice(14)}` };
  const env = s.match(/^environment \((.+)\)$/);
  if (env) return { stored: true, text: `From the environment variable ${env[1]}` };
  return { stored: true, text: s };
}

function sameList(a, b) {
  const x = [...(a || [])].sort();
  const y = [...(b || [])].sort();
  return x.length === y.length && x.every((v, i) => v === y[i]);
}
const listText = (v) => (v && v.length ? v.join(", ") : "none");

// How a chosen value missing from FOLIO's own list is labelled. Locations come from the tenant; item statuses are
// every status FOLIO defines, so a status outside that list is not one FOLIO knows at all.
const MISSING_LOCATION = "not a location in this FOLIO";
const MISSING_STATUS = "not a FOLIO item status";

// The three list filters as a recipe sets them by default (GET /api/recipes: each recipe's "selection").
function recipeLists(selection) {
  const s = selection || {};
  return { exclude_location_codes: [...(s.exclude_location_codes || [])], exclude_item_statuses: [...(s.exclude_item_statuses || [])],
           include_location_codes: [...(s.include_location_codes || [])] };
}

// A list for the wizard's summary, saying when it is simply the recipe's default.
function leftOutText(list, def) {
  return listText(list) + (def && def.length && sameList(list, def) ? " (the recipe's default)" : "");
}

// A settings form's values laid over settings: the person's changes, kept, on top of the file as it is now.
function overlaid(base, values) {
  const out = JSON.parse(JSON.stringify(base));
  for (const k of ["title", "schedule", "file_prefix"]) if (values && k in values) out[k] = values[k];
  for (const k of ["filters", "checks", "legacy"]) Object.assign(out[k], (values && values[k]) || {});
  return out;
}
const yesNo = (v) => (v ? "yes" : "no");
const pct = (r) => (r ? `${+(r * 100).toFixed(1)}% of the last one` : "off");

// What a settings form changes. `base` is the feed's settings; `form` has the same shape. Returns the values to
// send (only what changed) and each change as {what, before, after} for the person to review.
function settingsChanges(base, form) {
  const values = {};
  const changes = [];
  const note = (key, before, after) => changes.push({ what: SETTING_NAMES[key] || key, before, after });
  for (const k of ["title", "schedule", "file_prefix"]) {
    const a = base[k] || "";
    const b = form[k] || "";
    if (a !== b) {
      values[k] = b;
      note(k, a || "not set", b || "not set");
    }
  }
  const filters = {};
  for (const k of FILTER_LISTS) {
    if (!sameList(base.filters[k], form.filters[k])) {
      filters[k] = form.filters[k];
      note(k, listText(base.filters[k]), listText(form.filters[k]));
    }
  }
  for (const k of FILTER_FLAGS) {
    if (!!base.filters[k] !== !!form.filters[k]) {
      filters[k] = !!form.filters[k];
      note(k, yesNo(base.filters[k]), yesNo(form.filters[k]));
    }
  }
  if (Object.keys(filters).length) values.filters = filters;
  const ra = base.checks.min_item_ratio || 0;
  const rb = form.checks.min_item_ratio || 0;
  if (Math.abs(ra - rb) > 1e-9) {
    values.checks = { min_item_ratio: rb };
    note("min_item_ratio", pct(ra), pct(rb));
  }
  const legacy = {};
  for (const k of LEGACY_KEYS) {
    const a = base.legacy[k] || null;
    const b = form.legacy[k] || null;
    if (a !== b) {
      legacy[k] = b;
      note(k, a || "none", b || "none");
    }
  }
  if (Object.keys(legacy).length) values.legacy = legacy;
  return { values, changes };
}
// ---- pure helpers: end

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

// Replace an element's content. Like h(), it flattens lists and leaves out null, so optional parts can be
// written inline (replaceChildren on its own would show them as the text "null").
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-FX-Token": TOKEN },
    body: JSON.stringify(body),
  };
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new Error("Lost contact with folio-exchange. Check that its window is still open, then try again.");
  }
  const data = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) {
    if (r.status === 403 && data.signed_out) signedOut(data.error);
    const err = new Error(data.error || r.statusText);
    err.status = r.status;
    throw err;
  }
  return data;
}

const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

// FOLIO-Exchange was restarted or closed, so this tab's sign-in no longer works: say once how to get back in,
// and stop refreshing. Reloading would not help; only the launcher (or the link in its window) signs in again.
function signedOut(text) {
  if (S.signedOut) return;
  S.signedOut = true;
  if ($("modal").open) $("modal").close();
  $("main").replaceChildren(h("div", { class: "empty" },
    h("h1", { class: "empty-title", id: "signed-out-title", tabindex: "-1", text: "Open this page from FOLIO-Exchange again" }),
    h("p", { text }),
    h("p", { text: "If FOLIO-Exchange is still running, the launcher opens the page in your browser from there." })));
  $("signed-out-title").focus();
}

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

// Move the focus to an element drawn by the next render (a new step's heading, a saved form's button).
function takeFocus() {
  if (!S.focus) return;
  const el = $(S.focus);
  if (el) {
    S.focus = null;
    el.focus();
  }
}

function loading(text) {
  return h("p", { class: "muted loading" }, h("span", { class: "spinner", "aria-hidden": "true" }), ` ${text}`);
}

const short = (sha) => String(sha || "").slice(0, 7);

// A link out of the page (a vendor's repository): https only, in a new tab that cannot reach back to this page.
function outLink(href, text) {
  if (!/^https:\/\//.test(String(href || ""))) return text;
  return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text,
    h("span", { class: "sr-only", text: " (opens in a new tab)" }));
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

// ------------------------------------------------------------ form parts
// Every input has a <label for>; help and error texts are tied to it with aria-describedby. Errors are shown
// beside the field (or at the top of the form for the server's answer) and read out through the live region.
function describe(el, id, on) {
  const ids = new Set((el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
  if (on) ids.add(id);
  else ids.delete(id);
  if (ids.size) el.setAttribute("aria-describedby", [...ids].join(" "));
  else el.removeAttribute("aria-describedby");
}

function textInput(id, value, attrs) {
  return h("input", { class: "input", type: "text", id, value: value ?? "", spellcheck: "false", autocomplete: "off", ...attrs });
}

// The one place a secret field is made: a password field with autocomplete "new-password", which asks browsers
// not to fill in a saved password. (Some may still offer to save or suggest one; the page never keeps the value.)
function secretInput(id) {
  return h("input", { class: "input", type: "password", id, autocomplete: "new-password", spellcheck: "false" });
}

// Empty every secret field (passwords, passphrases, key files) under `root`. Called when the view or the feed
// tab changes, so a typed but unsent secret does not stay behind in a form the page keeps for later.
function clearSecrets(root) {
  for (const el of (root || document).querySelectorAll('input[type="password"], input[type="file"]')) el.value = "";
  for (const form of (root || document).querySelectorAll("form[data-dirty]")) delete form.dataset.dirty;
}

// A form's submit handler that runs once at a time: the guard is set (and the submit buttons disabled) before
// anything is awaited, so a second Enter or click cannot send the same thing twice.
function guarded(handler) {
  return async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    const focused = document.activeElement;
    const buttons = [...form.querySelectorAll('button[type="submit"]')].filter((b) => !b.disabled);
    for (const b of buttons) b.disabled = true;
    try {
      await handler(e, form);
    } finally {
      delete form.dataset.busy;
      for (const b of buttons) b.disabled = false;
      const a = document.activeElement;
      if (focused && focused.isConnected && (!a || a === document.body)) focused.focus();
    }
  };
}

function help(id, text) {
  return h("p", { class: "help", id: `${id}-help` }, text);
}

function errorSlot(id) {
  return h("p", { class: "field-error", id: `${id}-err`, hidden: true });
}

function field(input, text, helpText) {
  if (helpText) describe(input, `${input.id}-help`, true);
  return h("div", { class: "field" }, h("label", { for: input.id, text }), input,
    helpText ? help(input.id, helpText) : null, errorSlot(input.id));
}

function checkField(input, text, helpText) {
  if (helpText) describe(input, `${input.id}-help`, true);
  return h("div", { class: "check" }, input, h("div", {}, h("label", { for: input.id, text }),
    helpText ? help(input.id, helpText) : null));
}

function fieldError(input, message) {
  const err = $(`${input.id}-err`);
  if (err) {
    err.textContent = message;
    err.hidden = false;
    describe(input, err.id, true);
  }
  input.setAttribute("aria-invalid", "true");
  input.focus();
  announce(message);
}

function formErrorBox(formId) {
  return h("p", { class: "form-error", id: `${formId}-error`, hidden: true });
}

function formError(form, message, button) {
  const box = $(`${form.id}-error`);
  if (box) {
    box.textContent = message;
    box.hidden = false;
    if (button) describe(button, box.id, true);
    box.scrollIntoView({ block: "nearest" });
  }
  announce(message);
}

function clearErrors(form) {
  for (const el of form.querySelectorAll("[aria-invalid]")) {
    el.removeAttribute("aria-invalid");
    describe(el, `${el.id}-err`, false);
  }
  for (const p of form.querySelectorAll(".field-error")) {
    p.hidden = true;
    p.textContent = "";
  }
  const box = $(`${form.id}-error`);
  if (box) {
    box.hidden = true;
    box.textContent = "";
    for (const el of form.querySelectorAll("[aria-describedby]")) describe(el, box.id, false);
  }
}

// Run `work` with the button disabled and saying what is happening; the button comes back as it was.
async function working(button, text, work) {
  const kids = [...button.childNodes];
  const had = document.activeElement === button;
  button.disabled = true;
  button.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), ` ${text}`);
  announce(text);
  try {
    return await work();
  } finally {
    button.disabled = false;
    button.replaceChildren(...kids);
    if (had && button.isConnected && (document.activeElement === document.body || !document.activeElement)) button.focus();
  }
}

// A small form that is always on show (the SFTP password, the key) counts as being filled in once typed in,
// so the 45-second refresh leaves it alone.
function markDirty(e) {
  const form = e.currentTarget;
  form.dataset.dirty = "1";
}

function readText(file) {
  return new Promise((ok, bad) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result));
    r.onerror = () => bad(new Error("The file could not be read."));
    r.readAsText(file);
  });
}

function schedulePicker(prefix, current) {
  const preset = current === undefined ? SCHEDULES[0][0] : schedulePreset(current);
  const custom = textInput(`${prefix}-custom`, preset ? "" : current || "", { placeholder: "e.g. Mon,Thu 06:00" });
  const other = h("input", { type: "radio", name: prefix, id: `${prefix}-other`, value: "", checked: !preset });
  custom.addEventListener("input", () => { other.checked = true; });
  const el = h("fieldset", { class: "choices", id: `${prefix}-set` }, h("legend", { text: "When should it run?" }),
    SCHEDULES.map(([v, text], i) => checkField(h("input", { type: "radio", name: prefix, id: `${prefix}-${i}`, value: v,
      checked: v === preset }), text)),
    checkField(other, "Another time"),
    h("div", { class: "indent" }, field(custom, "Other schedule", "For example daily 06:00, Mon,Thu 06:00 or monthly 15 01:00 " +
      "(days 1 to 28). Times are in the site's own time zone.")));
  const value = () => {
    const c = el.querySelector(`input[name="${prefix}"]:checked`);
    return c && c.value ? c.value : custom.value.trim();
  };
  return { el, value, custom };
}

// A list of checkboxes (with a filter box when it is long). With no options (the tenant's list could not be
// read) it is a text field taking codes separated by commas. A chosen value missing from the options is still
// listed (and kept), labelled with `missing`, e.g. "not a location in this FOLIO".
function codePicker(id, legend, options, selected, helpText, missing) {
  const chosen = new Set(selected || []);
  if (!options) {
    const input = textInput(id, [...chosen].join(", "));
    return {
      el: field(input, legend, `${helpText ? `${helpText} ` : ""}Separate them with commas.`),
      value: () => input.value.split(",").map((s) => s.trim()).filter(Boolean),
    };
  }
  const all = [...options];
  for (const v of chosen) if (!all.some((o) => o.value === v)) all.push({ value: v, label: `${v} (${missing || "not in FOLIO's list"})` });
  const boxes = all.map((o, i) => {
    const cb = h("input", { type: "checkbox", id: `${id}-${i}`, value: o.value, checked: chosen.has(o.value) });
    return { o, cb, row: checkField(cb, o.label) };
  });
  const count = h("p", { class: "help", id: `${id}-count` });
  const update = () => {
    const n = boxes.filter((b) => b.cb.checked).length;
    count.textContent = n ? `${n} chosen` : "None chosen";
  };
  update();
  let filter = null;
  const none = h("p", { class: "muted", hidden: true, text: "Nothing matches." });
  if (all.length > 12) {
    filter = h("input", {
      class: "input", type: "search", id: `${id}-filter`, autocomplete: "off", spellcheck: "false", "aria-controls": `${id}-list`,
      onkeydown: (e) => { if (e.key === "Enter") e.preventDefault(); },        // Enter filters; it does not send the form
      oninput: () => {
        const q = filter.value.trim().toLowerCase();
        let shown = 0;
        for (const b of boxes) {
          const hit = !q || `${b.o.value} ${b.o.label}`.toLowerCase().includes(q);
          b.row.hidden = !hit;
          if (hit) shown += 1;
        }
        none.hidden = shown > 0;
      },
    });
  }
  const el = h("fieldset", { class: "choices", id },
    h("legend", { text: legend }), helpText ? help(id, helpText) : null,
    filter ? h("div", { class: "field" }, h("label", { for: filter.id, text: "Filter the list" }), filter) : null,
    h("div", { class: "pick-list", id: `${id}-list`, onchange: update }, boxes.map((b) => b.row), none), count);
  if (helpText) el.setAttribute("aria-describedby", `${id}-help ${id}-count`);
  else el.setAttribute("aria-describedby", `${id}-count`);
  return { el, value: () => boxes.filter((b) => b.cb.checked).map((b) => b.o.value) };
}

const LEGACY_HELP = {
  circ_count_note_type: "The item note type holding each item's loan count from the old system. It is added to FOLIO's own count.",
  created_date_note_type: "The item note type holding the date each item was added in the old system.",
  last_circ_date_note_type: "The item note type holding each item's last loan date in the old system.",
};

function legacyFields(prefix, legacy, types) {
  const inputs = {};
  const rows = LEGACY_KEYS.map((k) => {
    const id = `${prefix}-${k.replace(/_/g, "-")}`;
    const current = (legacy || {})[k] || "";
    let input;
    if (types) {
      const list = [...types];
      if (current && !list.includes(current)) list.push(current);
      input = h("select", { class: "input", id }, h("option", { value: "", text: "None" }),
        list.map((t) => h("option", { value: t, selected: t === current, text: t })));
    } else {
      input = textInput(id, current);
    }
    inputs[k] = input;
    return field(input, SETTING_NAMES[k], LEGACY_HELP[k]);
  });
  const el = h("details", { class: "more", open: LEGACY_KEYS.some((k) => (legacy || {})[k]) },
    h("summary", { text: "Items migrated from an older system" }),
    h("p", { class: "help", text: "If loan counts and dates from your old system were kept in item notes when you moved to FOLIO, " +
      "choose the note types that hold them, so the vendor gets the whole history. Otherwise leave these as None." }), rows);
  return { el, value: () => Object.fromEntries(LEGACY_KEYS.map((k) => [k, inputs[k].value.trim() || null])) };
}

async function getChoices(force) {
  if (S.choices && S.choices.data && !force) return S.choices;
  try {
    S.choices = { data: await api("/api/choices") };
  } catch (e) {
    S.choices = { error: e.message };
  }
  return S.choices;
}

function locationOptions(c) {
  return c ? c.locations.map((l) => ({ value: l.code, label: l.name ? `${l.code} – ${l.name}` : l.code })) : null;
}

function statusOptions(c) {
  return c ? c.statuses.map((s) => ({ value: s, label: s })) : null;
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
        "gives you the fingerprint, enter it under Change delivery on the feed's Delivery tab.";
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
    h("dt", {}, SETTING_NAMES[k] || label(k), k.includes("_") ? h("span", { class: "key", text: k }) : null),
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
  if (S.site && S.site.setup) return;              // nothing but the first-run card until FOLIO is connected
  if (!S.view || S.view.type !== view.type || S.view.name !== view.name) {
    clearSecrets(document);                       // a secret typed but not sent does not outlive the view
    if (S.connForm) clearSecrets(S.connForm);     // (the kept connection form, too, when it is not on show)
  }
  S.view = view;
  S.nav += 1;
  const hash = view.type === "feed" ? `#feed/${view.name}` : view.type === "home" ? "" : `#${view.type}`;
  if (location.hash !== hash) history.replaceState(null, "", hash || location.pathname);
  renderSidebar();
  if (view.type === "feed") {
    if (!openForms(view.name)) delete S.settings[view.name];  // fetched again on each visit
    openFeed(view.name);
  } else if (view.type === "connection") renderConnection();
  else if (view.type === "activity") renderActivity();
  else if (view.type === "recipes") renderRecipes();
  else if (view.type === "add") renderWizard();
  else renderHome();
}

function fromHash() {
  const m = location.hash.match(/^#feed\/([a-z0-9-]+)$/);
  if (m) return { type: "feed", name: m[1] };
  for (const t of ["connection", "activity", "recipes", "add"]) if (location.hash === `#${t}`) return { type: t };
  return null;
}

function renderHome() {
  const feeds = S.site ? S.site.feeds : [];
  $("main").replaceChildren(feeds.length
    ? h("div", { class: "empty" }, h("p", { class: "empty-title", text: "Choose a feed" }),
      h("p", { text: "Each feed sends one vendor its files on a schedule. Pick one on the left to see its layout, run it, or look at past runs." }))
    : h("div", { class: "empty" }, h("h1", { class: "empty-title", id: "home-title", tabindex: "-1", text: "No feeds yet" }),
      h("p", { text: "A feed sends one vendor a regular file of your library's records. Add one to start: it begins as a dry run that sends nothing." }),
      h("button", { class: "btn btn-primary", type: "button", id: "btn-add-empty", onclick: startWizard }, "Add a vendor feed")));
  takeFocus();
}

// ------------------------------------------------------------ sidebar
async function loadSite() {
  applySite(await api("/api/site"));
}

function applySite(s) {
  S.site = s;
  document.body.classList.toggle("first-run", !!s.setup);
  if (s.setup) {
    $("site-chip").textContent = "Not connected yet";
    $("site-chip").title = s.root;
    $("sidebar-foot").textContent = `folio-exchange ${s.version}`;
    renderSetup(s.setup);
    return;
  }
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
  if (!S.site || S.site.setup) return;
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
    list.append(h("li", { class: "nav-sub" }, h("span", { class: "muted", text: "No feeds yet." })));
  }
  const v = S.view ? S.view.type : null;
  $("nav-connection").setAttribute("aria-current", v === "connection" ? "true" : "false");
  $("nav-recipes").setAttribute("aria-current", v === "recipes" ? "true" : "false");
  $("nav-activity").setAttribute("aria-current", v === "activity" ? "true" : "false");
  $("btn-add-feed").setAttribute("aria-current", v === "add" ? "page" : "false");
}

function showProblem(name, problem) {
  clearSecrets(document);
  if (S.connForm) clearSecrets(S.connForm);
  S.view = { type: "problem", name };
  renderSidebar();
  $("main").replaceChildren(h("div", { class: "pane" },
    h("div", { class: "notice notice-failed" }, h("p", {}, h("b", { text: `feeds/${name}.yaml has a problem. ` }),
      "Fix the file in a text editor, then reload this page.")),
    h("pre", { class: "log", text: problem })));
}

// ------------------------------------------------------------ first run: connect to FOLIO
function renderSetup(st) {
  if ($("cx-form") && $("cx-form").dataset.first) return;          // already showing: keep what was typed
  S.view = { type: "setup" };
  const card = connectionForm({
    folio: st.folio || {}, first: true, keyring: st.keyring, problem: st.problem, synced: st.synced_folder, root: st.root,
    done: async () => {
      await loadSite();
      toast("Connected to FOLIO. Next, add a vendor feed.");
      startWizard();
    },
  });
  $("main").replaceChildren(h("div", { class: "setup-wrap" }, card));
  S.focus = "cx-heading";
  takeFocus();
}

// The FOLIO connection form: the first-run card, and "Change connection or password" on the Connection view.
function connectionForm({ folio, first, keyring, problem, synced, root, done, cancel }) {
  const url = textInput("cx-url", folio.base_url || "", { type: "url", placeholder: "https://", inputmode: "url" });
  const tenant = textInput("cx-tenant", folio.tenant || "");
  const user = textInput("cx-user", folio.username || "");
  const pw = keyring ? secretInput("cx-pw") : null;
  const btn = h("button", { class: "btn btn-primary", type: "submit", id: "cx-save" }, "Test and save");
  const form = h("form", {
    class: first ? "setup-card" : "edit-box", id: "cx-form", "data-form": "open", "data-first": first ? "1" : null,
    novalidate: true, autocomplete: "off", "aria-labelledby": "cx-heading",
    onsubmit: guarded(async () => {
      clearErrors(form);
      const values = { base_url: url.value.trim(), tenant: tenant.value.trim(), username: user.value.trim() };
      const bad = urlProblem(values.base_url);
      if (bad) return fieldError(url, bad);
      if (!values.tenant) return fieldError(tenant, "Enter the tenant id.");
      if (!values.username) return fieldError(user, "Enter the user name.");
      let secret = pw ? pw.value : "";
      // A brand-new site needs the password; an earlier site.yaml may have left one stored (the server says if not).
      if (first && pw && !secret && !folio.username) return fieldError(pw, "Enter the password for this account.");
      if (pw) pw.value = "";
      let res;
      try {
        res = await working(btn, "Logging in…", () => api("/api/setup/connection", { ...values, password: secret }));
      } catch (err) {
        formError(form, err.message, btn);
        return undefined;
      } finally {
        secret = null;
      }
      await done(res);
      return undefined;
    }),
  },
  first ? h("h1", { id: "cx-heading", tabindex: "-1", text: "Connect to FOLIO" })
    : h("h2", { id: "cx-heading", tabindex: "-1", text: "Change connection or password" }),
  first ? h("p", { text: "FOLIO-Exchange reads your library's records from FOLIO with a read-only account and sends each vendor " +
    "the files it asks for. Start by connecting it to FOLIO: the details are tested with FOLIO before anything is saved." }) : null,
  problem ? h("div", { class: "notice notice-failed" }, h("p", {}, h("b", { text: "This folder's site.yaml could not be used. " }), problem)) : null,
  synced ? h("div", { class: "notice notice-held" }, h("p", { text: "This folder is inside OneDrive or Dropbox. Keep the site in a " +
    "folder that is not synced: syncing can copy half-written files and the run history between computers." })) : null,
  keyring ? null : h("div", { class: "notice notice-held" }, h("p", { text: "This computer has no Windows Credential Manager that " +
    "FOLIO-Exchange can use, so the password cannot be stored from this page. Whoever looks after this computer needs to set " +
    "the FOLIO_PASSWORD environment variable for this account; FOLIO-Exchange then uses it." })),
  field(url, "API address", "On Eureka, the Kong gateway URL, starting https://. Your FOLIO administrator can tell you it."),
  field(tenant, "Tenant id", "The FOLIO tenant this library uses."),
  field(user, "User name", "A read-only service account, not a person's own login."),
  pw ? field(pw, "Password", first ? "Checked with FOLIO, then kept in Windows Credential Manager on this computer. It is never written to a file." +
    (folio.username ? " Leave it empty to use a password stored here before." : "")
    : "Leave empty to keep the stored password. A new one is checked with FOLIO before it replaces the old one.") : null,
  formErrorBox("cx-form"),
  h("div", { class: "form-actions" }, btn,
    cancel ? h("button", { class: "btn", type: "button", id: "cx-cancel", onclick: cancel }, "Cancel") : null),
  first && root ? h("p", { class: "help" }, "Site folder: ", h("span", { class: "mono", text: root })) : null);
  return form;
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

const FEED_TABS = ["overview", "layout", "settings", "delivery", "history"];

function renderFeed() {
  const f = S.feed;
  if (!FEED_TABS.includes(S.tab)) S.tab = "overview";
  const head = h("div", { class: "feed-head" },
    h("div", { class: "title-row" }, h("h1", { id: "feed-title", tabindex: "-1", text: f.title }), h("span", { class: "sub", text: f.name }),
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
    tablist("tabs", "feed", FEED_TABS, S.tab, "Feed sections",
      (t) => [label(t), t === "history" ? h("span", { class: "count", text: S.runs.length }) : null],
      (t) => {
        if (t !== S.tab) clearSecrets($("feed-panel"));      // leaving the Delivery tab drops a typed, unsent password
        S.tab = t;
        renderFeed();
      }));

  const grow = S.tab === "settings" || S.tab === "delivery";
  const top = h("div", { class: `pane pane-top${grow ? " pane-grow" : ""}`, role: "tabpanel", id: "feed-panel",
    "aria-labelledby": `feed-tab-${S.tab}`, tabindex: "0", "data-scroll": "top" }, tabContent());
  S.bottom = h("div", { class: "pane-bottom" });
  $("main").replaceChildren(head, h("div", { class: "split-v" }, top, S.bottom));
  renderResult();
  takeFocus();
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
  if (S.tab === "delivery") return deliveryTab(f);
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
  if (f.delivery.transport === "none" || !f.delivery.transport) {
    out.push(h("div", { class: "notice notice-info" }, h("p", { text: "This feed has nowhere to send files yet: it is a dry run. " +
      "Preview and Run build its files on this computer only." }),
    h("button", { class: "btn btn-small", type: "button", id: "btn-goto-delivery", onclick: () => {
      S.tab = "delivery";
      S.focus = "dl-heading";
      renderFeed();
    } }, "Set up delivery")));
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

// ------------------------------------------------------------ feed settings (the Settings tab)
function openForms(name) {
  const F = S.forms[name];
  return !!(F && (F.settings || F.delivery));
}

async function loadSettings(name, force) {
  if (!force && S.settings[name] && !S.settings[name].error && !S.settings[name].loading) return S.settings[name];
  S.settings[name] = { loading: true };
  try {
    S.settings[name] = await api(`/api/feeds/${name}/settings`);
  } catch (e) {
    S.settings[name] = { error: e.message };
  }
  return S.settings[name];
}

function onFeedTab(name, ...tabs) {
  return S.view && S.view.type === "feed" && S.feed && S.feed.name === name && tabs.includes(S.tab);
}

function settingsTab(f) {
  const F = S.forms[f.name];
  if (F && F.settings) return F.settings;
  return [
    h("div", { class: "tab-toolbar" },
      h("button", { class: "btn", type: "button", id: "btn-edit-settings", onclick: (e) => editSettings(f.name, e.currentTarget) }, "Edit settings"),
      h("p", { class: "muted", text: "The layout (which columns and fields the vendor gets) comes from the recipe: see the Layout tab." })),
    kv([["Title", f.title], ["Schedule", f.schedule.describe], ["File name prefix", f.file_prefix || "not set"]]),
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

async function editSettings(name, btn) {
  let ok = false;
  await working(btn, "Opening…", async () => {
    const [st, ch] = await Promise.all([loadSettings(name, true), getChoices()]);
    if (st.error) {
      toast(st.error);
      announce(st.error);
      return;
    }
    (S.forms[name] || (S.forms[name] = {})).settings = settingsForm(name, st, ch);
    ok = true;
  });
  if (ok && onFeedTab(name, "settings")) {
    S.focus = "set-heading";
    renderFeed();
  }
}

function closeSettings(name, focus) {
  if (S.forms[name]) S.forms[name].settings = null;
  S.focus = focus || "btn-edit-settings";
  if (onFeedTab(name, "settings")) renderFeed();
}

// The settings form. Its inputs start from the feed's settings, or (opts.overlay) from the person's own changes
// laid over the file as it is now; changes are always worked out against the file the form holds the version of.
function settingsForm(name, st, ch, opts) {
  const { overlay = null, notice = null } = opts || {};
  const c = ch && ch.data;
  const base = settingsBase(st);
  const start = overlay ? overlaid(base, overlay) : base;
  const defaults = st.filter_defaults || {};
  const title = textInput("set-title", start.title, { maxlength: "200" });
  const sched = schedulePicker("set-sched", start.schedule);
  const prefix = textInput("set-prefix", start.file_prefix);
  const exLoc = codePicker("set-exloc", "Locations to leave out", locationOptions(c), start.filters.exclude_location_codes,
    "Items in these locations are not sent.", MISSING_LOCATION);
  const exSt = codePicker("set-exstatus", "Item statuses to leave out", statusOptions(c), start.filters.exclude_item_statuses,
    "Items with these statuses are not sent, for example withdrawn or lost items.", MISSING_STATUS);
  const inLoc = codePicker("set-inloc", "Only these locations", locationOptions(c), start.filters.include_location_codes,
    "When any are chosen, only items in those locations are sent. Choose none to send every location.", MISSING_LOCATION);
  const flags = Object.fromEntries(FILTER_FLAGS.map((k) => [k, h("input", { type: "checkbox", id: `set-${k.replace(/_/g, "-")}`,
    checked: !!start.filters[k] })]));
  const pctText = start.checks.min_item_ratio ? String(+(start.checks.min_item_ratio * 100).toFixed(1)) : "";
  const ratio = h("input", { class: "input input-narrow", type: "number", id: "set-ratio", min: "0", max: "100", step: "1",
    inputmode: "decimal", value: pctText, "aria-describedby": "set-ratio-unit set-ratio-help" });
  const legacy = legacyFields("set", start.legacy, c ? c.note_types : null);
  const fields = h("fieldset", { class: "plain", id: "set-fields" },
    h("legend", { class: "sr-only", text: "Settings" }),
    field(title, "Title", "How the feed is shown on this page and in alerts."),
    sched.el,
    field(prefix, "File name prefix", "The start of each file's name. Change it only if the vendor asks."),
    h("h3", { class: "section-title", text: "Which records" }),
    exLoc.el, exSt.el,
    h("details", { class: "more" }, h("summary", { text: "More options" }), inLoc.el,
      FILTER_FLAGS.map((k) => checkField(flags[k], SETTING_NAMES[k], `The recipe's default: ${yesNo(defaults[k])}.`))),
    h("h3", { class: "section-title", text: "Check before sending" }),
    h("div", { class: "field" },
      h("div", { class: "inline-field" }, h("label", { for: "set-ratio", text: "Block a delivery if the item count falls below" }),
        ratio, h("span", { id: "set-ratio-unit", text: "% of the last one" })),
      h("p", { class: "help", id: "set-ratio-help", text: "For example 90: a run with fewer than 90% of the items last delivered is " +
        "held for review instead of being sent. Leave it empty or 0 to switch the check off." }),
      errorSlot("set-ratio")),
    legacy.el);
  const review = h("div", { id: "set-review", class: "review", hidden: true });
  const reviewBtn = h("button", { class: "btn btn-primary", type: "submit", id: "set-review-btn" }, "Review changes");
  const cancel = h("button", { class: "btn", type: "button", id: "set-cancel", onclick: () => closeSettings(name) }, "Cancel");
  const actions = h("div", { class: "form-actions" }, reviewBtn, cancel);

  const read = () => {
    const t = ratio.value.trim();
    const r = t === pctText ? (start.checks.min_item_ratio || 0) : t === "" ? 0 : Number(t) / 100;
    return {
      title: title.value.trim(), schedule: sched.value(), file_prefix: prefix.value.trim(),
      filters: { exclude_location_codes: exLoc.value(), exclude_item_statuses: exSt.value(), include_location_codes: inLoc.value(),
                 ...Object.fromEntries(FILTER_FLAGS.map((k) => [k, flags[k].checked])) },
      checks: { min_item_ratio: r }, legacy: legacy.value(),
    };
  };
  const backToEditing = () => {
    fields.disabled = false;
    review.hidden = true;
    review.replaceChildren();
    actions.hidden = false;
  };

  const changeList = (changes) => h("ul", { class: "changes" }, changes.map((x) => h("li", {}, h("b", { text: `${x.what}: ` }),
    h("span", { class: "before", text: x.before }), " → ", h("span", { class: "after", text: x.after }))));
  const back = () => h("button", { class: "btn", type: "button", id: "set-back", onclick: () => {
    backToEditing();
    reviewBtn.focus();
  } }, "Back to editing");

  // Check the form and show what would change (the "Review changes" step).
  const showReview = () => {
    clearErrors(form);
    const t = ratio.value.trim();
    const n = Number(t);
    if (t && (!Number.isFinite(n) || n < 0 || n > 100)) {
      return fieldError(ratio, "Enter a percentage from 0 to 100, or leave it empty to switch the check off.");
    }
    const values = read();
    if (!values.schedule) return fieldError(sched.custom, "Enter a schedule, or choose one above.");
    const { values: send, changes } = settingsChanges(base, values);
    fields.disabled = true;
    actions.hidden = true;
    const save = h("button", { class: "btn btn-primary", type: "button", id: "set-save",
      onclick: () => saveSettings(name, form, send, save) }, "Save");
    put(review,
      h("h3", { id: "set-review-heading", tabindex: "-1", text: changes.length ? "Review changes" : "Nothing has changed" }),
      changes.length ? changeList(changes) : h("p", { class: "muted", text: "The form is the same as the saved settings." }),
      changes.length ? h("p", { class: "help", text: "Use Preview after saving to see the effect before the next delivery." }) : null,
      h("div", { class: "form-actions" }, changes.length ? save : null, back(),
        changes.length ? null : h("button", { class: "btn", type: "button", onclick: () => closeSettings(name) }, "Close")));
    review.hidden = false;
    $("set-review-heading").focus();
    return undefined;
  };

  const form = h("form", {
    class: "edit-box", id: "set-form", "data-form": "open", novalidate: true, "aria-labelledby": "set-heading",
    onsubmit: guarded(async () => showReview()),
  },
  h("h2", { class: "tab-heading", id: "set-heading", tabindex: "-1", text: "Edit settings" }),
  notice ? h("div", { class: "notice notice-held" }, h("p", { text: notice })) : null,
  fields, formErrorBox("set-form"), actions, review);
  form.version = st.version;            // moved on by the page's own writes to this feed (noteWrite)
  form.showReview = showReview;
  form.backToEditing = backToEditing;
  // The file changed underneath (by hand, or in another window): nothing was saved and everything typed stays.
  // Offer to lay the same changes over the file as it is now and review them again.
  form.conflict = (message, send) => {
    const apply = h("button", { class: "btn btn-primary", type: "button", id: "set-reapply",
      onclick: () => reapplySettings(name, send, apply) }, "Apply my changes to the file as it is now");
    put(review,
      h("h3", { id: "set-review-heading", tabindex: "-1", text: "Not saved: the file changed" }),
      h("p", { class: "form-error", id: "set-conflict", text: `${firstSentence(message)} Nothing was saved, and what you ` +
        "entered is still here." }),
      h("div", { class: "form-actions" }, apply, back()));
    review.hidden = false;
    $("set-review-heading").focus();
    announce(`Not saved: ${firstSentence(message)} What you entered is still here.`);
  };
  return form;
}

function settingsBase(st) {
  return { title: st.title || "", schedule: st.schedule || "", file_prefix: st.file_prefix || "",
           filters: { ...(st.filters || {}) }, checks: { ...(st.checks || {}) }, legacy: { ...(st.legacy || {}) } };
}

function firstSentence(text) {
  const s = String(text || "");
  const m = s.match(/^.*?\.(?=\s|$)/s);
  return m ? m[0] : s;
}

async function saveSettings(name, form, values, btn) {
  let res;
  try {
    res = await working(btn, "Saving…", () => api(`/api/feeds/${name}/settings`, { version: form.version, values }));
  } catch (e) {
    if (/changed since this form was opened/.test(e.message)) {
      form.conflict(e.message, values);
      return;
    }
    form.backToEditing();
    formError(form, e.message, $("set-review-btn"));
    $("set-review-btn").focus();
    return;
  }
  const { changed, ...settings } = res;
  noteWrite(name, settings);
  S.forms[name].settings = null;
  const words = changed.length ? "Settings saved. Use Preview to see the effect." : "Nothing needed saving.";
  toast(words);
  announce(words);
  S.focus = "btn-edit-settings";
  try {
    await loadSite();
  } catch (e) { /* the sidebar catches up on the next refresh */ }
  if (S.view && S.view.type === "feed" && S.view.name === name) await openFeed(name);
}

// Lay the person's changes over the feed file as it is now, show the form that way and review the result.
async function reapplySettings(name, values, btn) {
  let st, ch;
  await working(btn, "Reading the file…", async () => {
    st = await loadSettings(name, true);
    ch = await getChoices();
  });
  if (st.error) {
    formError($("set-form"), st.error, btn);
    return;
  }
  const F = S.forms[name] || (S.forms[name] = {});
  F.settings = settingsForm(name, st, ch, { overlay: values,
    notice: "The form now shows the file as it is now, with your changes laid over it. Check them below, then save." });
  if (S.view && S.view.type === "feed" && S.view.name === name) await openFeed(name);
  if (F.settings.isConnected) F.settings.showReview();
}

// After the page itself rewrote feeds/<name>.yaml (settings, delivery, a password or key, pause or resume): keep
// the new settings and move every open form of that feed on to the file's new version, so the page's own write
// is never mistaken for a change made underneath the form.
function noteWrite(name, settings) {
  if (!settings || !settings.version) return;
  S.settings[name] = settings;
  const F = S.forms[name];
  if (!F) return;
  for (const form of [F.settings, F.delivery]) if (form) form.version = settings.version;
}

// Pause and resume answer with the new state only: read the settings again for the file's new version.
async function rereadSettings(name) {
  try {
    noteWrite(name, await api(`/api/feeds/${name}/settings`));
  } catch (e) { /* a save then says the form is out of date, and offers to apply it again */ }
}

// ------------------------------------------------------------ delivery (the Delivery tab)
function deliv(name) {
  return S.deliv[name] || (S.deliv[name] = {});
}

function deliveryTab(f) {
  const name = f.name;
  const st = S.settings[name];
  if (!st || st.loading) {
    if (!st) {
      loadSettings(name).then(() => { if (onFeedTab(name, "delivery")) redraw(renderFeed); });
    }
    return loading("Loading the delivery settings…");
  }
  if (st.error) {
    return h("div", { class: "notice notice-failed" }, h("p", { text: st.error }),
      h("button", { class: "btn btn-small", type: "button", onclick: () => { delete S.settings[name]; renderFeed(); } }, "Try again"));
  }
  const d = st.delivery;
  const D = deliv(name);
  const F = S.forms[name] || {};
  const out = [];
  if (S.notes[name] && d.transport === "none") {
    out.push(h("div", { class: "notice notice-info" }, h("p", {}, h("b", { text: `${f.title} is added, as a paused dry run. ` }),
      "You can Preview and Run it now: its files stay on this computer. Nothing is sent to the vendor until a delivery is set up here.")));
  }
  if (st.problem) out.push(h("div", { class: "notice notice-failed" }, h("p", { text: st.problem })));
  out.push(h("h2", { class: "tab-heading", id: "dl-heading", tabindex: "-1", text: "Where the files go" }));
  const rows = [];
  if (d.transport === "sftp") {
    const pw = secretWhere(d.password);
    rows.push(["Sends to", "The vendor's SFTP server"],
      ["Server", h("span", { class: "mono", text: `${d.host}:${d.port || 22}` })],
      ["Folder on the server", h("span", { class: "mono", text: d.remote_dir || "." })],
      ["User name", h("span", { class: "mono", text: d.username || "" })],
      ["Server key", d.host_key_fingerprint ? h("span", {},
        d.host_key_confirmed ? badge("success", "OK") : badge("held", "Wait"), " ",
        h("span", { class: "mono fingerprint", text: d.host_key_fingerprint }),
        h("span", { class: "block muted", text: d.host_key_confirmed ? "Confirmed: it matches the fingerprint the vendor gave."
          : "Not confirmed yet: nothing logs in or sends until you enter the vendor's fingerprint under Change delivery." }))
        : "not read yet"],
      ["Login", d.auth === "key" ? "With a private key, kept in this site's keys folder (only this Windows user can read it)"
        : pw.stored ? `With a password. ${pw.text}.` : h("span", { class: "error-text", text: "No password or key is stored yet, so nothing can be sent." })]);
  } else if (d.transport === "local") {
    rows.push(["Sends to", "A folder on this computer or the network"], ["Folder", h("span", { class: "mono", text: d.directory || "" })]);
  } else {
    rows.push(["Sends to", "Nowhere yet. This feed is a dry run: Preview and Run build its files on this computer and send nothing."]);
  }
  out.push(kv(rows));
  if (D.serverKey) {
    out.push(h("div", { class: "notice notice-info" }, h("p", {}, h("b", { text: "The server's key, as read when you saved: " }),
      h("span", { class: "mono fingerprint", text: D.serverKey.sha256 }), " (older form: ",
      h("span", { class: "mono fingerprint", text: D.serverKey.md5 }), ")")));
  }
  if (F.delivery) {
    out.push(F.delivery);
  } else {
    const unconfirmed = d.transport === "sftp" && !d.host_key_confirmed;
    out.push(h("div", { class: "form-actions" },
      h("button", { class: "btn", type: "button", id: "btn-change-delivery", onclick: (e) => openDeliveryForm(name, e.currentTarget) },
        "Change delivery"),
      d.transport !== "none" ? h("button", { class: "btn", type: "button", id: "btn-test-delivery", disabled: busy() || unconfirmed,
        "aria-describedby": unconfirmed ? "dl-test-why" : null, onclick: (e) => testDelivery(name, e.currentTarget) }, "Test delivery") : null,
      unconfirmed ? h("span", { class: "help", id: "dl-test-why", text: "The test waits until the server key is confirmed." }) : null));
    out.push(testResult(D));
  }
  if (d.transport === "sftp" && !F.delivery) out.push(loginSection(f, st, D));
  if (!f.enabled && !F.delivery) out.push(resumeSection(f, st, D));
  return out;
}

function testResult(D) {
  const t = D.test;
  if (!t) return null;
  if (t.running) return h("div", { class: "notice notice-info", id: "dl-test" }, h("p", {}, h("span", { class: "spinner", "aria-hidden": "true" }),
    " Testing the delivery. It logs in and looks at the folder; it sends nothing."));
  if (t.error) return h("div", { class: "notice notice-failed", id: "dl-test" }, badge("failed"), h("p", {}, h("b", { text: "The test failed. " }), pageWords(t.error)));
  const r = t.result;
  const n = r.entries;
  return h("div", { class: "notice notice-ok", id: "dl-test" }, badge("success", "OK"),
    h("p", {}, h("b", { text: "It works. " }), `Reached ${r.where}. `,
      n === null || n === undefined ? "The server does not let this account list the folder, which is usual for upload-only accounts."
        : `The folder holds ${num(n)} ${n === 1 ? "entry" : "entries"}.`));
}

async function openDeliveryForm(name, btn) {
  let st;
  await working(btn, "Opening…", async () => { st = await loadSettings(name, true); });
  if (st.error) {
    toast(st.error);
    return;
  }
  (S.forms[name] || (S.forms[name] = {})).delivery = deliveryForm(name, st);
  S.focus = "dl-form-heading";
  if (onFeedTab(name, "delivery")) renderFeed();
}

function closeDeliveryForm(name, focus) {
  if (S.forms[name]) S.forms[name].delivery = null;
  S.focus = focus || "btn-change-delivery";
  if (onFeedTab(name, "delivery")) renderFeed();
}

function deliveryForm(name, st) {
  const d = st.delivery;
  const cur = { st };                   // the file the form is checked against; a re-apply moves it on
  const mode = ["sftp", "local", "none"].includes(d.transport) ? d.transport : "none";
  const choices = [["sftp", "The vendor's SFTP server"], ["local", "A folder on this computer or the network"],
                   ["none", "Not yet: keep it a dry run"]];
  const radios = choices.map(([v]) => h("input", { type: "radio", name: "dl-transport", id: `dl-t-${v}`, value: v, checked: v === mode }));
  const host = textInput("dl-host", d.host || "", { placeholder: "sftp.vendor.com" });
  const port = h("input", { class: "input input-narrow", type: "number", id: "dl-port", min: "1", max: "65535", inputmode: "numeric",
    value: String(d.port || 22) });
  const user = textInput("dl-user", d.username || "");
  const rdir = textInput("dl-rdir", d.remote_dir || ".");
  const confirmed = (x) => x.transport === "sftp" && x.host_key_confirmed && !!x.host_key_fingerprint;
  const known = confirmed(d);
  const fp = textInput("dl-fp", "", { placeholder: "SHA256:…" });
  const later = h("input", { type: "checkbox", id: "dl-later" });
  const dir = textInput("dl-dir", d.directory || "", { placeholder: "D:\\Exports\\vendor" });
  const boxes = {
    sftp: h("div", { id: "dl-box-sftp", class: "sub-fields" },
      field(host, "Server name", "The SFTP server the vendor gave you, such as sftp.vendor.com."),
      field(port, "Port", "Usually 22."),
      field(user, "User name", "The SFTP user name the vendor gave you."),
      field(rdir, "Folder on the server", "Where the files go on the vendor's server. A dot (.) is the folder you land in when you log in."),
      field(fp, "Server key fingerprint from the vendor", "Ask the vendor for it: it starts SHA256: (or is an older value such as " +
        "3f:a2:…:9c). On saving, FOLIO-Exchange reads the server's own key and saves only if they match, so files cannot go to a " +
        "server pretending to be the vendor's." + (known ? " This server's key is already confirmed: leave this empty to keep it." : "")),
      checkField(later, "I don't have it yet: save the server, but don't connect until I confirm it",
        "The feed is paused, and nothing logs in to the server, until you come back and enter the vendor's fingerprint.")),
    local: h("div", { id: "dl-box-local", class: "sub-fields" },
      field(dir, "Folder", "The folder's full path, on this computer or the network, such as D:\\Exports\\vendor or \\\\server\\share\\vendor. " +
        "It is made if it does not exist.")),
    none: h("div", { id: "dl-box-none", class: "sub-fields" },
      h("p", { class: "muted", text: "The feed keeps building its files on this computer only. Preview and Run still work." })),
  };
  const show = (v) => { for (const [k, el] of Object.entries(boxes)) el.hidden = k !== v; };
  show(mode);
  const btn = h("button", { class: "btn btn-primary", type: "submit", id: "dl-save" }, "Save delivery");
  const conflictBox = h("div", { class: "notice notice-held", id: "dl-conflict", hidden: true });
  const form = h("form", {
    class: "edit-box", id: "dl-form", "data-form": "open", novalidate: true, "aria-labelledby": "dl-form-heading",
    onchange: (e) => { if (e.target.name === "dl-transport") show(e.target.value); },
    onsubmit: guarded(async () => {
      clearErrors(form);
      conflictBox.hidden = true;
      describe(btn, conflictBox.id, false);
      const now = cur.st.delivery;
      const transport = (radios.find((r) => r.checked) || {}).value || "none";
      let body;
      if (transport === "sftp") {
        if (!host.value.trim()) return fieldError(host, "Enter the SFTP server name the vendor gave you.");
        const p = Number(port.value || 22);
        if (!Number.isInteger(p) || p < 1 || p > 65535) return fieldError(port, "Enter the port, usually 22.");
        if (!user.value.trim()) return fieldError(user, "Enter the SFTP user name the vendor gave you.");
        const bad = fingerprintProblem(fp.value);
        if (bad) return fieldError(fp, bad);
        const sameServer = confirmed(now) && host.value.trim().toLowerCase() === String(now.host || "").toLowerCase()
          && p === Number(now.port || 22);
        if (!fp.value.trim() && !later.checked && !sameServer) {
          return fieldError(fp, "Enter the fingerprint the vendor gave you, or tick the box below to confirm it later.");
        }
        body = { transport, host: host.value.trim(), port: p, username: user.value.trim(), remote_dir: rdir.value.trim() || ".",
                 vendor_fingerprint: fp.value.trim(), confirm_later: later.checked && !fp.value.trim() };
      } else if (transport === "local") {
        const bad = folderProblem(dir.value);
        if (bad) return fieldError(dir, bad);
        body = { transport, directory: dir.value.trim() };
      } else {
        body = { transport: "none" };
      }
      let res;
      try {
        res = await working(btn, transport === "sftp" ? "Reading the server's key…" : "Saving…",
          () => api(`/api/feeds/${name}/delivery`, { version: form.version, form: body }));
      } catch (err) {
        if (/changed since this form was opened/.test(err.message)) {
          // Changed underneath (by hand, or in another window): keep everything typed, and offer to save it again.
          const again = h("button", { class: "btn btn-small", type: "button", id: "dl-reapply", onclick: async () => {
            let fresh;
            await working(again, "Reading the file…", async () => { fresh = await loadSettings(name, true); });
            if (fresh.error) {
              formError(form, fresh.error, btn);
              return;
            }
            cur.st = fresh;
            form.version = fresh.version;
            form.requestSubmit(btn);
          } }, "Save my choices on the file as it is now");
          put(conflictBox, h("p", { text: `${firstSentence(err.message)} Nothing was saved, and what you entered is still here.` }), again);
          conflictBox.hidden = false;
          describe(btn, conflictBox.id, true);
          conflictBox.scrollIntoView({ block: "nearest" });
          announce(`Not saved: ${firstSentence(err.message)} What you entered is still here.`);
          return undefined;
        }
        formError(form, err.message, btn);
        return undefined;
      }
      const { server_key: serverKey, ...settings } = res;
      noteWrite(name, settings);
      const D = deliv(name);
      D.serverKey = serverKey || null;
      D.test = null;
      S.forms[name].delivery = null;
      const sd = settings.delivery;
      const words = sd.transport === "sftp" && !sd.host_key_confirmed
        ? "Delivery saved. The server key is not confirmed yet, so the feed stays paused and nothing logs in."
        : sd.transport === "none" ? "Saved: the feed stays a dry run." : "Delivery saved. Use Test delivery to try it.";
      toast(words);
      announce(words);
      S.focus = "dl-heading";
      try {
        await loadSite();
      } catch (e2) { /* caught up on the next refresh */ }
      if (S.view && S.view.type === "feed" && S.view.name === name) await openFeed(name);
      return undefined;
    }),
  },
  h("h3", { id: "dl-form-heading", tabindex: "-1", text: "Change delivery" }),
  h("fieldset", { class: "choices" }, h("legend", { text: "Where should the files go?" }),
    choices.map(([v, text], i) => checkField(radios[i], text))),
  boxes.sftp, boxes.local, boxes.none,
  formErrorBox("dl-form"), conflictBox,
  h("div", { class: "form-actions" }, btn,
    h("button", { class: "btn", type: "button", id: "dl-cancel", onclick: () => closeDeliveryForm(name) }, "Cancel")));
  form.version = st.version;            // moved on by the page's own writes to this feed (noteWrite)
  return form;
}

function loginSection(f, st, D) {
  const name = f.name;
  const d = st.delivery;
  const mode = D.loginMode || d.auth || "password";
  const pw = secretInput("dl-pw");
  const pwBtn = h("button", { class: "btn btn-primary", type: "submit", id: "dl-pw-save" }, "Save in Credential Manager");
  const pwForm = h("form", {
    class: "subform", id: "dl-pw-form", "data-form": "idle", novalidate: true, autocomplete: "off", hidden: mode !== "password",
    oninput: markDirty,
    onsubmit: guarded(async () => {
      clearErrors(pwForm);
      let secret = pw.value;
      pw.value = "";
      delete pwForm.dataset.dirty;
      if (!secret) return fieldError(pw, "Enter the password the vendor gave you.");
      let res;
      try {
        res = await working(pwBtn, "Saving…", () => api(`/api/feeds/${name}/password`, { password: secret }));
      } catch (err) {
        formError(pwForm, err.message, pwBtn);
        return undefined;
      } finally {
        secret = null;
      }
      noteWrite(name, res);
      D.loginMode = null;
      D.keyFp = null;
      toast("Password saved in Credential Manager.");
      announce("Password saved in Credential Manager. The feed logs in with it from now on.");
      S.focus = "dl-login-heading";
      if (onFeedTab(name, "delivery")) renderFeed();
      return undefined;
    }),
  },
  field(pw, "SFTP password", "Sent once to folio-exchange on this computer and kept in Windows Credential Manager. It is never shown again or written to a file."),
  formErrorBox("dl-pw-form"),
  h("div", { class: "form-actions" }, pwBtn));

  const keyFile = h("input", { class: "input", type: "file", id: "dl-keyfile" });
  const pass = secretInput("dl-pass");
  const keyBtn = h("button", { class: "btn btn-primary", type: "submit", id: "dl-key-save" }, "Save key");
  const keyForm = h("form", {
    class: "subform", id: "dl-key-form", "data-form": "idle", novalidate: true, autocomplete: "off", hidden: mode !== "key",
    oninput: markDirty, onchange: markDirty,
    // guarded(): the guard is set before the file is read, so a second click cannot send the key twice.
    onsubmit: guarded(async () => {
      clearErrors(keyForm);
      const file = keyFile.files && keyFile.files[0];
      let phrase = pass.value;
      pass.value = "";
      if (!file) {
        phrase = null;
        return fieldError(keyFile, "Choose the private key file.");
      }
      let text = null;
      try {
        if (file.size > 16384) throw new Error("That file is too large to be a private key.");
        text = await readText(file);
      } catch (err) {
        keyFile.value = "";
        phrase = null;
        delete keyForm.dataset.dirty;
        return fieldError(keyFile, err.message);
      }
      keyFile.value = "";
      delete keyForm.dataset.dirty;
      let res;
      try {
        res = await working(keyBtn, "Saving…", () => api(`/api/feeds/${name}/private-key`, { key: text, passphrase: phrase || "" }));
      } catch (err) {
        formError(keyForm, `${err.message} Choose the file again to retry.`, keyBtn);
        return undefined;
      } finally {
        text = null;
        phrase = null;
      }
      const { key, ...settings } = res;
      noteWrite(name, settings);
      D.loginMode = null;
      D.keyFp = key;
      toast("Key saved.");
      announce(`Key saved: ${key}. The feed logs in with it from now on.`);
      S.focus = "dl-login-heading";
      if (onFeedTab(name, "delivery")) renderFeed();
      return undefined;
    }),
  },
  field(keyFile, "Private key file", "The private key whose public half the vendor has (OpenSSH or PEM format). A PuTTY .ppk key needs " +
    "converting first; the message says how. It is kept in this site's keys folder, readable only by this Windows user."),
  field(pass, "Passphrase (if the key has one)", "Kept in Windows Credential Manager."),
  formErrorBox("dl-key-form"),
  h("div", { class: "form-actions" }, keyBtn));

  const pick = h("fieldset", { class: "choices", onchange: (e) => {
    if (e.target.name !== "dl-auth") return;
    D.loginMode = e.target.value;
    pwForm.hidden = D.loginMode !== "password";
    keyForm.hidden = D.loginMode !== "key";
  } }, h("legend", { text: "Log in with" }),
  checkField(h("input", { type: "radio", name: "dl-auth", id: "dl-auth-password", value: "password", checked: mode === "password" }), "A password"),
  checkField(h("input", { type: "radio", name: "dl-auth", id: "dl-auth-key", value: "key", checked: mode === "key" }), "A private key file"));

  return [
    h("h2", { class: "tab-heading", id: "dl-login-heading", tabindex: "-1", text: "Login to the vendor's server" }),
    S.site && S.site.keyring === false ? h("div", { class: "notice notice-held" }, h("p", { text: "This computer has no Windows " +
      "Credential Manager that FOLIO-Exchange can use, so passwords cannot be stored from this page." })) : null,
    D.keyFp ? h("div", { class: "notice notice-ok" }, badge("success", "OK"), h("p", {}, "Key saved. Its public fingerprint is ",
      h("span", { class: "mono fingerprint", text: D.keyFp }), ". The vendor's server must know the matching public key (the .pub file).")) : null,
    pick, pwForm, keyForm,
  ];
}

function resumeSection(f, st) {
  const d = st.delivery;
  const unconfirmed = d.transport === "sftp" && !d.host_key_confirmed;
  return [
    h("h2", { class: "tab-heading", text: "This feed is paused" }),
    h("p", { text: "Its schedule does not run it, but you can still Preview and Run it by hand." }),
    d.transport === "none"
      ? h("p", { text: "It has no delivery yet: resumed, its scheduled runs only build files on this computer." })
      : h("p", { text: "Resuming lets the schedule run it. The first delivery to a new destination is held for your review anyway: " +
        "nothing reaches the vendor until you have looked at the files and chosen Review and send…" }),
    unconfirmed ? h("p", { class: "error-text", text: "The server key is not confirmed yet, so nothing is sent even when it runs. " +
      "Enter the vendor's fingerprint under Change delivery first." }) : null,
    h("div", { class: "form-actions" },
      h("button", { class: "btn btn-primary", type: "button", id: "btn-resume", disabled: busy(),
        onclick: (e) => resumeFeed(f.name, e.currentTarget) }, "Resume feed")),
  ];
}

async function resumeFeed(name, btn) {
  if (btn) btn.disabled = true;                  // at once: a second click does nothing
  try {
    await api(`/api/feeds/${name}/resume`, {});
    await rereadSettings(name);
    toast(`${name} resumed.`);
    announce(`${name} resumed. Its schedule runs it from now on.`);
  } catch (e) {
    toast(e.message);
    announce(e.message);
  }
  S.focus = "dl-heading";
  await loadSite();
  if (S.view && S.view.type === "feed" && S.view.name === name) await openFeed(name);
}

async function testDelivery(name, btn) {
  if (busy() || testDelivery.starting) {
    toast("Another action is running; wait for it to finish.");
    return;
  }
  testDelivery.starting = true;                  // set before anything is awaited: one test per click
  if (btn) btn.disabled = true;
  let job;
  try {
    job = await api(`/api/feeds/${name}/test-delivery`, {});
  } catch (e) {
    deliv(name).test = { error: e.message };
    if (e.status === 409) await loadSite();
    if (onFeedTab(name, "delivery")) redraw(renderFeed);
    return;
  } finally {
    testDelivery.starting = false;
  }
  await follow(job);
}

// ------------------------------------------------------------ add a vendor feed
const STEPS = ["Recipe", "Name", "Records", "Schedule", "Add"];

function newWizard() {
  return { step: 0, recipe: null, name: "", title: "", perm: null, ...recipeLists(null), legacy: {}, schedule: SCHEDULES[0][0] };
}

function startWizard() {
  if (S.site && S.site.setup) return;
  if (!S.wizard) S.wizard = newWizard();
  go({ type: "add" });
}

function cancelWizard() {
  S.wizard = null;
  const feeds = S.site ? S.site.feeds : [];
  go(feeds.length ? { type: "feed", name: feeds[0].name } : { type: "home" });
}

const takenNames = () => [...(S.site ? S.site.feeds.map((f) => f.name) : []), ...Object.keys((S.site && S.site.problems) || {})];

function renderWizard() {
  const W = S.wizard || (S.wizard = newWizard());
  const head = h("div", { class: "feed-head" },
    h("div", { class: "title-row" }, h("h1", { text: "Add a vendor feed" }), h("div", { class: "spacer" }),
      h("button", { class: "btn btn-ghost", type: "button", id: "wz-cancel", onclick: cancelWizard }, "Cancel")),
    h("ol", { class: "steps", "aria-label": "Steps" }, STEPS.map((s, i) => h("li", {
      class: i < W.step ? "done" : null, "aria-current": i === W.step ? "step" : null },
    h("span", { class: "step-num", text: String(i + 1) }), h("span", { text: s }),
    i < W.step ? h("span", { class: "sr-only", text: " (done)" }) : null))));
  const body = h("div", { class: "pane wizard", id: "wizard" });
  $("main").replaceChildren(head, body);
  [stepRecipe, stepName, stepRecords, stepSchedule, stepAdd][W.step](body, W);
}

function stepForm(W, title, intro, nextText) {
  const nextBtn = nextText === null ? null : h("button", { class: "btn btn-primary", type: "submit", id: "wz-next" }, nextText || "Next");
  const content = h("div", { class: "step-body" });
  const form = h("form", {
    id: "wz-form", "data-form": "open", novalidate: true, "aria-labelledby": "wz-heading",
    onsubmit: guarded(async () => {
      clearErrors(form);
      if (form.next && nextBtn && !form.loading) await form.next(nextBtn);
    }),
  },
  h("h2", { id: "wz-heading", tabindex: "-1", text: `${W.step + 1}. ${title}` }),
  intro ? h("p", { class: "muted", text: intro }) : null,
  content, formErrorBox("wz-form"),
  h("div", { class: "form-actions" },
    W.step > 0 ? h("button", { class: "btn", type: "button", id: "wz-back", onclick: () => {
      if (form.keep) form.keep();
      W.step -= 1;
      renderWizard();
    } }, "Back") : null, nextBtn));
  form.content = content;
  form.nextBtn = nextBtn;
  return form;
}

function showStep(body, form) {
  body.replaceChildren(form);
  $("wz-heading").focus();
}

function moveTo(W, step) {
  W.step = step;
  if (S.view && S.view.type === "add" && S.wizard === W) renderWizard();
}

async function stepRecipe(body, W) {
  const form = stepForm(W, "Choose the vendor's recipe", "A recipe is the file layout a vendor asks for. Each feed uses one.", null);
  const area = h("div", {}, loading("Loading the recipes…"));
  form.content.append(area, h("p", {}, "Not listed? ", h("a", { href: "#recipes", id: "wz-get-recipes" }, "Get recipes from vendors"),
    ": download a vendor's recipes into this site."));
  showStep(body, form);
  let data;
  try {
    data = await api("/api/recipes");
  } catch (e) {
    if (form.isConnected) area.replaceChildren(h("p", { class: "error-text", text: e.message }));
    return;
  }
  if (!form.isConnected) return;
  S.recipes = data;
  if (!data.recipes.length) {
    area.replaceChildren(h("p", { class: "muted", text: "No recipes are installed yet. Get one from a vendor first." }));
    return;
  }
  area.replaceChildren(h("ul", { class: "cards" }, data.recipes.map((r) => {
    const latest = r.versions.length ? r.versions[r.versions.length - 1].version : "";
    const chosen = W.recipe && W.recipe.id === r.id;
    return h("li", {}, h("button", {
      class: `card${chosen ? " card-chosen" : ""}`, type: "button", id: `wz-recipe-${r.id}`, "aria-current": chosen ? "true" : null,
      onclick: () => chooseRecipe(W, r),
    }, h("span", { class: "card-title", text: r.title }),
    h("span", { class: "card-sub", text: `${r.vendor || "vendor not named"} · ${r.id} ${latest}` }),
    r.description ? h("span", { class: "card-text", text: r.description }) : null,
    h("span", { class: "card-sub", text: r.feeds.length ? `Already used by ${r.feeds.join(", ")}` : "Not used by any feed yet" }),
    chosen ? h("span", { class: "card-sub", text: "Chosen" }) : null));
  })));
}

function chooseRecipe(W, r) {
  if (!W.recipe || W.recipe.id !== r.id) {
    W.recipe = r;
    W.name = freeName(r.default_name, takenNames());
    W.title = r.title;
    W.perm = null;
    Object.assign(W, recipeLists(r.selection));      // the recipe's own defaults start ticked
  }
  moveTo(W, 1);
}

function stepName(body, W) {
  const form = stepForm(W, "Name the feed", `It uses ${W.recipe.title}${W.recipe.vendor ? ` (${W.recipe.vendor})` : ""}.`);
  const name = textInput("wz-name", W.name, { "aria-required": "true", maxlength: "60" });
  const title = textInput("wz-title", W.title, { maxlength: "200" });
  const perm = h("div", { id: "wz-perm" });
  form.content.append(
    field(name, "Feed name", "Lowercase letters, digits and hyphens. It names the feed's settings file and its folder of runs."),
    field(title, "Title", "How the feed is shown on this page and in alerts."),
    h("h3", { class: "section-title", text: "Can the FOLIO account read what this recipe needs?" }), perm);
  form.keep = () => {
    W.name = name.value.trim();
    W.title = title.value.trim();
  };
  form.next = async () => {
    form.keep();
    const bad = nameProblem(W.name, takenNames());
    if (bad) return fieldError(name, bad);
    moveTo(W, 2);
    return undefined;
  };
  showStep(body, form);
  drawPerm(W);
  if (!W.perm || W.perm.uses !== W.recipe.default_uses) checkPermissions(W);
}

async function checkPermissions(W) {
  const uses = W.recipe.default_uses;
  W.perm = { running: true, uses };
  drawPerm(W);
  let p;
  try {
    p = { result: await api(`/api/permissions?uses=${encodeURIComponent(uses)}`), uses };
  } catch (e) {
    p = { error: e.message, uses };
  }
  if (S.wizard !== W || !W.recipe || W.recipe.default_uses !== uses) return;
  W.perm = p;
  drawPerm(W);
  announce(p.error ? `The permission check could not run: ${p.error}` : p.result.ok
    ? "Permission check: the account can read everything this recipe uses."
    : "Permission check: the account is missing some permissions. They are listed below.");
}

function drawPerm(W) {
  const area = $("wz-perm");
  if (!area) return;
  const p = W.perm;
  if (!p || p.running) {
    area.replaceChildren(loading("Checking with FOLIO…"));
    return;
  }
  const again = h("button", { class: "btn btn-small", type: "button", id: "wz-perm-again", onclick: () => checkPermissions(W) }, "Check again");
  if (p.error) {
    area.replaceChildren(h("div", { class: "notice notice-failed" }, h("p", { text: `The check could not run: ${p.error}` }), again));
    return;
  }
  const r = p.result;
  if (r.ok) {
    area.replaceChildren(h("div", { class: "notice notice-ok" }, badge("success", "OK"),
      h("p", { text: "Yes: the account can read everything this recipe uses." }), again));
    return;
  }
  put(area,
    h("div", { class: "notice notice-held" }, badge("held", "Check"),
      h("p", { text: "Not yet. You can carry on: the feed is saved paused, but its runs fail until this is put right." }), again),
    r.denied.length ? [h("p", { text: "Ask your FOLIO administrator to give the account these permissions:" }),
      h("ul", { class: "file-list" }, r.denied.map((x) => h("li", { text: x })))] : null,
    r.failed.length ? [h("p", { text: "These could not be read for another reason:" }),
      h("ul", { class: "plain-list" }, r.failed.map((x) => h("li", {}, h("b", { text: x.name }), ` (${x.path}): ${x.error}`)))] : null);
}

// Help for a pick list in the wizard, naming the recipe's own default when it has one.
function withDefault(text, def, what) {
  return def && def.length ? `${text} The recipe ${what} ${def.join(", ")} by default, so ${def.length === 1 ? "it starts" : "they start"} ticked.`
    : text;
}

async function stepRecords(body, W) {
  const form = stepForm(W, "Choose which records to send", "Leave out what the vendor should not get. All of this can be changed later on the feed's Settings tab.");
  form.loading = true;
  form.nextBtn.disabled = true;
  const area = h("div", {}, loading("Reading locations and note types from FOLIO…"));
  form.content.append(area);
  showStep(body, form);
  const ch = await getChoices();
  if (!form.isConnected) return;
  const c = ch.data;
  const def = recipeLists(W.recipe.selection);
  const loc = codePicker("wz-loc", "Locations to leave out", locationOptions(c), W.exclude_location_codes,
    withDefault("Items in these locations are not sent.", def.exclude_location_codes, "leaves out"), MISSING_LOCATION);
  const st = codePicker("wz-status", "Item statuses to leave out", statusOptions(c), W.exclude_item_statuses,
    withDefault("Items with these statuses are not sent, for example withdrawn or lost items.", def.exclude_item_statuses, "leaves out"),
    MISSING_STATUS);
  const only = codePicker("wz-inloc", "Only these locations", locationOptions(c), W.include_location_codes,
    withDefault("When any are chosen, only items in those locations are sent. Choose none to send every location.",
      def.include_location_codes, "sends only"), MISSING_LOCATION);
  const legacy = legacyFields("wz", W.legacy, c ? c.note_types : null);
  put(area,
    ch.error ? h("div", { class: "notice notice-held" }, h("p", { text: `The lists could not be read from FOLIO (${ch.error}). ` +
      "You can type codes instead, or carry on and set this later." }),
    h("button", { class: "btn btn-small", type: "button", onclick: async () => {
      form.keep();
      await getChoices(true);
      renderWizard();
    } }, "Try again")) : null,
    loc.el, st.el,
    h("details", { class: "more", open: W.include_location_codes.length > 0 }, h("summary", { text: "More options" }), only.el),
    legacy.el);
  form.keep = () => {
    W.exclude_location_codes = loc.value();
    W.exclude_item_statuses = st.value();
    W.include_location_codes = only.value();
    W.legacy = legacy.value();
  };
  form.next = async () => {
    form.keep();
    moveTo(W, 3);
  };
  form.loading = false;
  form.nextBtn.disabled = false;
}

function stepSchedule(body, W) {
  const form = stepForm(W, "Choose when it runs", "The feed is saved paused, so it does not run on this schedule until you resume it.");
  const picker = schedulePicker("wz-sched", W.schedule);
  form.content.append(picker.el);
  form.keep = () => { W.schedule = picker.value(); };
  form.next = async () => {
    form.keep();
    if (!W.schedule) return fieldError(picker.custom, "Enter a schedule, or choose one above.");
    moveTo(W, 4);
    return undefined;
  };
  showStep(body, form);
}

function scheduleWords(text) {
  const hit = SCHEDULES.find(([v]) => v === text);
  return hit ? hit[1] : text;
}

function stepAdd(body, W) {
  const form = stepForm(W, "Check and add", null, "Add feed");
  const legacy = LEGACY_KEYS.filter((k) => W.legacy[k]).map((k) => `${SETTING_NAMES[k]}: ${W.legacy[k]}`);
  const def = recipeLists(W.recipe.selection);
  form.content.append(
    kv([["Recipe", `${W.recipe.title} (${W.recipe.default_uses})`], ["Name", h("span", { class: "mono", text: W.name })],
      ["Title", W.title || W.name],
      ["Locations left out", leftOutText(W.exclude_location_codes, def.exclude_location_codes)],
      ["Item statuses left out", leftOutText(W.exclude_item_statuses, def.exclude_item_statuses)],
      ["Locations sent", W.include_location_codes.length ? `only ${leftOutText(W.include_location_codes, def.include_location_codes)}`
        : "every location not left out"],
      ["Migrated values", legacy.length ? legacy.join("; ") : "none"],
      ["Schedule", scheduleWords(W.schedule)],
      ["Permissions", !W.perm || !W.perm.result ? "not checked" : W.perm.result.ok ? "the account can read everything it needs"
        : h("span", { class: "error-text", text: "missing some: runs fail until the FOLIO administrator adds them" })]]),
    h("div", { class: "notice notice-info" }, h("p", { text: "The feed is saved paused and with nowhere to send files: a dry run. " +
      "You can preview and run it straight away, and nothing is sent to the vendor until you set up its delivery." })));
  form.next = async (btn) => {
    // The full lists always go (the server drops a value equal to the recipe's default, so nothing extra is written).
    const filters = { exclude_location_codes: W.exclude_location_codes, exclude_item_statuses: W.exclude_item_statuses,
                      include_location_codes: W.include_location_codes };
    const legacyValues = Object.fromEntries(LEGACY_KEYS.filter((k) => W.legacy[k]).map((k) => [k, W.legacy[k]]));
    const values = { title: W.title, schedule: W.schedule, filters };
    if (Object.keys(legacyValues).length) values.legacy = legacyValues;
    let res;
    try {
      res = await working(btn, "Adding…", () => api("/api/feeds", { name: W.name, uses: W.recipe.default_uses, values }));
    } catch (e) {
      formError(form, e.message, btn);
      return;
    }
    S.wizard = null;
    S.settings[res.name] = res;
    S.notes[res.name] = "new";
    S.tab = "delivery";
    S.focus = "dl-heading";
    toast(`${res.title || res.name} added as a paused dry run.`);
    try {
      await loadSite();
    } catch (e) { /* the sidebar catches up on the next refresh */ }
    S.view = { type: "feed", name: res.name };
    S.nav += 1;
    history.replaceState(null, "", `#feed/${res.name}`);
    renderSidebar();
    await openFeed(res.name);
  };
  showStep(body, form);
}

// ------------------------------------------------------------ history
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
const KINDS = { preview: "preview", run: "run", build: "build", release: "send", "test-delivery": "delivery test", check: "connection check" };

function otherJobNotice() {
  const j = S.job;
  if (!j || (j.feed === S.feed.name && j.kind !== "test-delivery")) return null;
  const what = j.feed ? `${KINDS[j.kind] || j.kind} of ${j.feed}` : "connection check";
  return h("div", { class: "notice notice-info" }, h("p", { text: `A ${what} is running; other actions wait until it finishes.` }),
    j.feed && j.feed !== S.feed.name ? h("button", { class: "btn", type: "button", onclick: () => go({ type: "feed", name: j.feed }) }, "Open it") : null);
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
  if (S.job && S.job.feed === name && S.job.kind !== "test-delivery") {   // a running job owns the result panel
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
      if (++misses > 3 || S.signedOut) throw e;   // a brief hiccup is retried; a dead server is reported
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

// A delivery test: its result shows on the Delivery tab, not in the run result panel.
async function followTest(job) {
  const name = job.feed;
  const D = deliv(name);
  S.job = job;
  D.test = { running: true };
  renderSidebar();
  announce("Testing the delivery…");
  if (S.view && S.view.type === "feed" && S.feed) redraw(renderFeed);
  let done = null;
  try {
    done = await watch(job);
  } catch (e) {
    done = null;
  }
  S.job = null;
  D.test = !done ? { error: "Lost contact with folio-exchange during the test. Reload the page." }
    : done.status === "error" ? { error: done.error } : { result: done.result };
  const words = D.test.error ? `The delivery test failed: ${pageWords(D.test.error)}` : "The delivery test worked.";
  toast(D.test.error ? "The delivery test failed." : "The delivery test worked.");
  announce(words);
  renderSidebar();
  const a = document.activeElement;
  if (onFeedTab(name, "delivery") && (!a || a === document.body)) S.focus = "btn-test-delivery";
  if (S.view && S.view.type === "feed" && S.feed) redraw(renderFeed);
  return done;
}

async function follow(job) {
  // Follow a job to its end (one started here, or found running after a reload) and show its result.
  if (job.kind === "test-delivery") return followTest(job);
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
  if (S.signedOut) return done;              // the notice saying how to get back in stays on screen
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
  if ($("btn-pause")) $("btn-pause").disabled = true;   // at once: a second click does nothing
  try {
    await api(`/api/feeds/${f.name}/${f.enabled ? "pause" : "resume"}`, {});
    await rereadSettings(f.name);              // the file changed: open forms of this feed move on to its new version
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
  const change = S.connForm ? null : h("button", { class: "btn", type: "button", id: "btn-change-conn", onclick: openConnForm },
    "Change connection or password");
  const parts = [h("div", { class: "feed-head" }, h("div", { class: "title-row" }, h("h1", { text: "Connection" }),
      h("span", { class: "sub", text: "FOLIO" }), h("div", { class: "spacer" }), change, run))];
  const pw = secretWhere(s.folio.password);
  const body = [S.connForm, S.connNotice && !S.connForm ? h("div", { class: "notice notice-held", id: "conn-notice" },
    h("p", { text: S.connNotice })) : null, kv([
    ["Address", h("span", { class: "mono", text: s.folio.base_url })],
    ["Tenant", s.folio.tenant],
    ["Account", s.folio.username],
    ["Password", pw.stored ? pw.text : h("span", { class: "error-text", text: pw.text })],
    ["Alerts", s.alerts.on === "never" || (!s.alerts.webhook && !s.alerts.email) ? "none set up" : [s.alerts.webhook && "webhook", s.alerts.email && "email"].filter(Boolean).join(" and ")],
    ["Site folder", h("span", { class: "mono", text: s.root })],
  ])];
  const warnings = s.warnings || [];
  // The scheduled task's age, quietly, when all is well; a warning below says so when it is not.
  if (s.task && s.task.text && !warnings.some((w) => (w.code || "").startsWith("task-"))) {
    body.push(h("p", { class: "muted", id: "task-status", text: s.task.text }));
  }
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
  takeFocus();
}

function openConnForm() {
  S.connForm = connectionForm({
    folio: S.site.folio, first: false, keyring: S.site.keyring,
    cancel: () => {
      S.connForm = null;
      S.focus = "btn-change-conn";
      renderConnection();
    },
    done: async (res) => {
      S.connForm = null;
      lastCheck = null;
      S.connNotice = (res && res.notice) || null;      // pointed at another FOLIO: feeds hold their next run
      const where = res && res.folio && res.folio.password ? secretWhere(res.folio.password).text : "";
      toast("Connection saved.");
      announce(`Logged in and saved.${where ? ` Password: ${where}.` : ""}${S.connNotice ? ` ${S.connNotice}` : ""}`);
      S.focus = "btn-change-conn";
      try {
        await loadSite();
      } catch (e) { /* caught up on the next refresh */ }
      if (S.view && S.view.type === "connection") renderConnection();
    },
  });
  S.focus = "cx-heading";
  renderConnection();
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

// ------------------------------------------------------------ recipes
async function renderRecipes() {
  const nav = S.nav;
  $("main").replaceChildren(h("div", { class: "feed-head" }, h("div", { class: "title-row" }, h("h1", { id: "recipes-title", tabindex: "-1", text: "Recipes" }),
      h("span", { class: "sub", text: "the file layouts vendors ask for, and where to get new ones" }))),
    h("div", { class: "pane", id: "recipes-pane" }, loading("Loading the recipes…")));
  let data;
  try {
    data = await api("/api/recipes");
  } catch (e) {
    if (nav === S.nav) $("recipes-pane").replaceChildren(h("p", { class: "error-text", text: e.message }));
    return;
  }
  if (nav !== S.nav) return;
  S.recipes = data;
  drawRecipes();
}

function sourceTitle(id) {
  const s = ((S.recipes && S.recipes.sources) || []).find((x) => x.id === id);
  return s ? s.title : id;
}

function fromText(from) {
  if (from === "bundled") return "comes with FOLIO-Exchange";
  if (from === "this site") return "added to this site by hand";
  return `downloaded from ${sourceTitle(from)}`;
}

function drawRecipes() {
  const pane = $("recipes-pane");
  if (!pane || !S.recipes) return;
  const data = S.recipes;
  put(pane,
    h("h2", { class: "tab-heading", text: "Installed on this site" }),
    data.recipes.length ? data.recipes.map(recipeBox) : h("p", { class: "muted", text: "None yet. Get one from a vendor below." }),
    (data.problems || []).length ? [h("h3", { class: "section-title", text: "Recipe files with problems" }),
      h("ul", { class: "plain-list" }, data.problems.map((p) => h("li", { class: "error-text", text: p })))] : null,
    h("h2", { class: "tab-heading", text: "Get recipes from vendors" }),
    h("p", { class: "muted", text: "Vendors publish their recipes on GitHub. FOLIO-Exchange downloads only from the sources listed " +
      "here, shows you what would change, and installs only what you approve. An installed version never changes: a vendor " +
      "publishes a new version instead." }),
    data.sources.length ? data.sources.map(sourceBox) : h("p", { class: "muted", text: "No vendor sources are listed." }));
  takeFocus();
}

function recipeBox(r) {
  return h("section", { class: "box", "aria-labelledby": `rcp-${r.id}` },
    h("h3", { id: `rcp-${r.id}`, text: r.title }),
    h("p", { class: "muted", text: [r.vendor, r.id].filter(Boolean).join(" · ") }),
    r.description ? h("p", { text: r.description }) : null,
    kv([["Versions", h("ul", { class: "plain-list" }, r.versions.map((v) => h("li", {}, h("b", { text: v.version }), ` – ${fromText(v.from)}`)))],
      ["Used by", r.feeds.length ? r.feeds.join(", ") : "no feed yet"],
      ["Reads from FOLIO", (r.reads || []).join(", ") || "not stated"]]));
}

function sourceBox(s) {
  const C = S.checks[s.id];
  const repoUrl = `https://github.com/${s.repo}`;
  return h("section", { class: "box", "aria-labelledby": `src-${s.id}` },
    h("h3", { id: `src-${s.id}`, text: s.title }),
    s.description ? h("p", { text: s.description }) : null,
    kv([
      ["Vendor", s.vendor || ""],
      ["Repository", outLink(repoUrl, `github.com/${s.repo}`)],
      s.homepage && s.homepage !== repoUrl ? ["Home page", outLink(s.homepage, s.homepage)] : null,
      ["Provides", (s.recipes || []).join(", ")],
      ["Last downloaded", s.commit ? `${when(s.installed)}, commit ${short(s.commit)}` : "never"],
      s.origin === "site" ? ["Listed by", "this site's own settings"] : null,
    ]),
    h("div", { class: "form-actions" }, h("button", { class: "btn", type: "button", id: `chk-${s.id}`,
      onclick: (e) => checkSource(s, e.currentTarget) }, C ? "Check again" : "Check for updates")),
    C ? checkReview(s, C) : null);
}

async function checkSource(s, btn) {
  await working(btn, "Checking GitHub…", async () => {
    try {
      S.checks[s.id] = { result: await api("/api/recipes/check", { source: s.id }) };
    } catch (e) {
      S.checks[s.id] = { error: e.message };
    }
  });
  const C = S.checks[s.id];
  announce(C.error ? `The check failed: ${C.error}` : `Checked. ${C.result.new.length} new ${C.result.new.length === 1 ? "version" : "versions"}.`);
  S.focus = `rev-${s.id}`;
  if (S.view && S.view.type === "recipes") drawRecipes();
}

const REVIEW_BADGES = { new: ["built", "New"], installed: ["none", "Installed"], conflict: ["failed", "Refused"], invalid: ["failed", "Refused"] };

function changesView(changes) {
  if (Array.isArray(changes)) return changes.length ? h("ul", { class: "plain-list" }, changes.map((c) => h("li", { text: String(c) }))) : "not described";
  return changes ? String(changes) : "not described";
}

function reviewItem(item) {
  const [cls, word] = REVIEW_BADGES[item.status] || ["none", item.status];
  const status = { new: "not installed yet", installed: "already installed", conflict: "refused", invalid: "refused" }[item.status] || item.status;
  return h("li", {},
    h("div", { class: "review-head" }, badge(cls, word), h("b", { text: `${item.title || item.id} ${item.version}` }),
      h("span", { class: "muted", text: [item.vendor, status].filter(Boolean).join(" · ") })),
    item.problem ? h("p", { class: "error-text", text: item.problem }) : null,
    item.status === "new" ? kv([
      ["What changed", changesView(item.changes)],
      ["Reads from FOLIO", (item.reads || []).join(", ") || "not stated"],
      ["Patron data", item.patron_data ? "yes" : "none"],
      ["Feeds", (item.picked_up_by || []).length ? `From their next run, these feeds use it: ${item.picked_up_by.join(", ")}`
        : "No feed uses it until one is set to this version."],
    ]) : null);
}

function checkReview(s, C) {
  if (C.error) {
    return h("div", { class: "review" }, h("h4", { id: `rev-${s.id}`, tabindex: "-1", text: "The check failed" }),
      h("p", { class: "error-text", text: C.error }));
  }
  const r = C.result;
  const n = r.new.length;
  const installBtn = h("button", { class: "btn btn-primary", type: "button", id: `inst-${s.id}`,
    "aria-describedby": C.installError ? `inst-${s.id}-error` : null,
    onclick: (e) => installSource(s, r.commit, e.currentTarget) }, `Install ${n} new ${n === 1 ? "version" : "versions"}`);
  const was = r.installed_commit ? (r.installed_commit === r.commit ? "This is the commit last downloaded."
    : `Last downloaded: commit ${short(r.installed_commit)}.`) : "Nothing has been downloaded from here yet.";
  return h("div", { class: "review" },
    h("h4", { id: `rev-${s.id}`, tabindex: "-1", text: C.installed ? "Installed" : `What ${s.title} offers now` }),
    h("p", { class: "muted", text: `Commit ${short(r.commit)}, checked ${when(r.checked)}. ${was}` }),
    C.installed ? h("div", { class: "notice notice-ok" }, badge("success", "OK"), h("p", { text: C.installed.length
      ? `Installed ${C.installed.join(", ")}. Feeds whose version range allows it use the new version from their next run.`
      : "Nothing new was installed." })) : null,
    r.recipes.length ? h("ul", { class: "review-list" }, r.recipes.map(reviewItem)) : h("p", { class: "muted", text: "No recipe files were found there." }),
    (r.problems || []).length ? [h("p", { text: "Skipped:" }), h("ul", { class: "plain-list" }, r.problems.map((p) => h("li", { text: p })))] : null,
    C.installError ? h("p", { class: "form-error", id: `inst-${s.id}-error`, text: `Nothing was installed. ${C.installError}` }) : null,
    C.installed ? null : n ? h("div", { class: "form-actions" }, installBtn)
      : h("p", { class: "muted", text: "Nothing new to install." }));
}

async function installSource(s, commit, btn) {
  let res;
  try {
    res = await working(btn, "Installing…", () => api("/api/recipes/install", { source: s.id, commit }));
  } catch (e) {
    S.checks[s.id].installError = e.message;
    announce(e.message);
    S.focus = `inst-${s.id}`;
    drawRecipes();
    return;
  }
  S.checks[s.id] = { result: res, installed: res.installed };
  try {
    S.recipes = await api("/api/recipes");
    await loadSite();
  } catch (e) { /* the lists catch up on the next visit */ }
  announce(res.installed.length ? `Installed ${res.installed.join(", ")}.` : "Nothing new was installed.");
  S.focus = `rev-${s.id}`;
  if (S.view && S.view.type === "recipes") drawRecipes();
}

// ------------------------------------------------------------ start
$("btn-check").addEventListener("click", runCheck);
$("btn-add-feed").addEventListener("click", startWizard);
$("nav-connection").addEventListener("click", () => go({ type: "connection" }));
$("nav-recipes").addEventListener("click", () => go({ type: "recipes" }));
$("nav-activity").addEventListener("click", () => go({ type: "activity" }));
// ------------------------------------------------------------ keeping the page current
// Scheduled runs happen while the page sits open. Every 45 seconds, and when the tab comes back into view,
// the sidebar and the open feed are fetched again and redrawn only if something changed, keeping the scroll
// position and the focus. Nothing is fetched while the tab is hidden, a job is running (it has its own
// polling), the dialog is open, the Run menu or a drop-down is in use, or a setup form is open or typed in.
const REFRESH_MS = 45000;
let lastRefresh = Date.now();

function quiet() {
  const a = document.activeElement;
  return S.signedOut || document.hidden || busy() || $("modal").open || !!document.querySelector(".menu:not([hidden])")
    || (a && a.tagName === "SELECT") || !S.site || !!S.site.setup
    || !!document.querySelector('#main [data-form="open"], #main [data-dirty]');
}

function snapshot() {
  const a = document.activeElement;
  return {
    key: a && a !== document.body ? a.id || (a.dataset && a.dataset.key) || null : null,
    scrolls: [...document.querySelectorAll("#main [data-scroll]")].map((el) => [el.dataset.scroll, el.scrollTop, el.scrollLeft]),
    main: $("main").scrollTop, page: window.scrollY,
  };
}

function restore(snap, keepFocus) {
  for (const [k, top, left] of snap.scrolls) {
    const el = document.querySelector(`#main [data-scroll="${k}"]`);
    if (el) { el.scrollTop = top; el.scrollLeft = left; }
  }
  $("main").scrollTop = snap.main;
  window.scrollTo(window.scrollX, snap.page);
  if (!snap.key || keepFocus) return;
  const el = document.getElementById(snap.key) || document.querySelector(`[data-key="${CSS.escape(snap.key)}"]`);
  if (el && el !== document.activeElement) el.focus({ preventScroll: true });
}

function redraw(draw) {
  const snap = snapshot();
  const aimed = !!S.focus;                     // the draw moves the focus on purpose: leave it there
  draw();
  restore(snap, aimed);
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
  if (busy() || site.setup) return;          // applySite found a job started elsewhere and follows it
  const v = S.view;
  if (v && v.type === "connection" && siteChanged && !(lastCheck && lastCheck.running)) {
    redraw(renderConnection);
  } else if (v && v.type === "feed" && S.feed && S.feed.name === v.name) {
    let feed, runs, settings = null;
    try {
      feed = await api(`/api/feeds/${v.name}`);
      runs = await api(`/api/feeds/${v.name}/runs?limit=50`);
      if (S.tab === "delivery") settings = await api(`/api/feeds/${v.name}/settings`);
    } catch (e) {
      return;
    }
    if (nav !== S.nav || quiet() || !S.feed || S.feed.name !== v.name) return;
    const settingsChanged = settings && JSON.stringify(settings) !== JSON.stringify(S.settings[v.name]);
    if (!siteChanged && !settingsChanged && JSON.stringify(feed) === JSON.stringify(S.feed) && JSON.stringify(runs) === JSON.stringify(S.runs)) return;
    S.feed = feed;
    S.runs = runs;
    if (settings) S.settings[v.name] = settings;
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
  if ($("modal").open || (e.target.closest && e.target.closest("dialog, input, select, textarea, form"))) return;
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
  if (S.site.setup) return;
  const v = fromHash() || (S.site.feeds.length ? { type: "feed", name: S.site.feeds[0].name } : { type: "home" });
  if (v.type === "add") startWizard();
  else go(v);
})();
