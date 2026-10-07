"use strict";
// VPXConfig UI. No build step, no dependencies. The step/field definitions come from /api/steps.

const $ = (sel) => document.querySelector(sel);
const HEADERS = { "Content-Type": "application/json", "X-VPX-Config": "1" };
const api = {
  get: (p) => fetch(p).then((r) => r.json()),
  send: (method, p, body) => fetch(p, { method, headers: HEADERS, body: JSON.stringify(body || {}) }).then((r) => r.json()),
};

const DISPLAY = "display";
const app = { steps: [], values: {}, displays: null, displayError: "", base: null, baseNotice: "", displayDiagnostics: null };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
}

// ---- state persistence -------------------------------------------------------------------
let saveTimer;
let pending = {};
function flush() {
  clearTimeout(saveTimer);
  const values = pending;
  pending = {};
  return Object.keys(values).length ? api.send("PUT", "/api/state", { values }) : Promise.resolve();
}
function setValue(id, value) {
  app.values[id] = value;
  pending[id] = value;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flush, 250);
}

// ---- monitors ----------------------------------------------------------------------------
async function loadDisplays() {
  const r = await api.get("/api/system/displays");
  app.displays = r.displays || [];
  app.displayError = r.ok ? "" : r.error;
  app.displayDiagnostics = r.diagnostics || null;
  app.displayCommand = r.command || "wayland-info -i output";
}
// A Display value is "<name> [x, y]" (what VPX writes and compares exactly). findMon needs that exact string; looseMon also
// recognises the same monitor written without the position (older files) or at another position (it was moved).
const findMon = (value) => (app.displays || []).find((m) => m.id === value);
const withoutPosition = (value) => value.replace(/\s*\[-?\d+,\s*-?\d+\]\s*$/, "");
const looseMon = (value) => findMon(value) || (app.displays || []).find((m) => m.description === withoutPosition(value));

// ---- sidebar -----------------------------------------------------------------------------

const expandedNavGroups = {};   // sidebar groups (e.g. "Plugins") the user has expanded; they start collapsed

function renderNav() {
  const cur = location.hash.slice(1) || app.steps[0]?.id;
  const curParent = app.steps.find((s) => s.id === cur)?.parent;
  const isOpen = (parent) => expandedNavGroups[parent] || curParent === parent;   // the current page's group is always open
  const items = [];
  let n = 0, lastParent = "";
  for (const s of app.steps) {
    if (s.parent) {
      if (s.parent !== lastParent) {
        n += 1;
        items.push(h("a", { href: "#", class: "nav-group", onclick: (e) => {
          e.preventDefault(); expandedNavGroups[s.parent] = !isOpen(s.parent); renderNav(); } },
          h("span", { class: "num" }, String(n)), s.parent, h("span", { class: "caret" }, isOpen(s.parent) ? "▾" : "▸")));
      }
      lastParent = s.parent;
      if (isOpen(s.parent)) items.push(h("a", { href: "#" + s.id, class: "sub" + (s.id === cur ? " active" : "") }, s.title));
    } else {
      lastParent = "";
      n += 1;
      items.push(h("a", { href: "#" + s.id, class: s.id === cur ? "active" : "" }, h("span", { class: "num" }, String(n)), s.title));
    }
  }
  $("#nav").replaceChildren(...items);
}

// ---- form fields -------------------------------------------------------------------------
function control(f, step) {
  const v = app.values[f.id] || "";
  const onchange = (e) => {
    setValue(f.id, e.target.value);
    e.target.closest(".field").classList.toggle("set", e.target.value.trim() !== "");
  };
  if (f.type === "bool" || f.type === "select") {
    return h("select", { id: f.id, onchange },
      h("option", { value: "" }, `Default (${f.default_label || "VPX decides"})`),
      f.options.map((o) => h("option", { value: o.value, selected: o.value === v }, o.label)));
  }
  if (f.type === "int" || f.type === "float") {
    return h("input", { id: f.id, type: "number", value: v, placeholder: f.placeholder, min: f.min, max: f.max,
      step: f.type === "int" ? "1" : "any", oninput: onchange });
  }
  if (f.type === DISPLAY) return displayControl(f, step, v);
  if (f.type === "mapping") return mappingControl(f, v);
  return h("input", { id: f.id, type: "text", value: v, placeholder: f.placeholder, oninput: onchange });
}

// "Plugins › PUP › Backglass Left Pad": sidebar group, page, card (when it adds something) and label
const where = (c) => [c.parent, c.step, c.group !== c.step ? c.group : "", c.label].filter(Boolean).join(" › ");

// Button mappings ("Key;225"): a text box for the raw value, a readable name, and a button that captures a key press.
function mappingControl(f, v) {
  const shown = (value) => describeMapping(value) || (f.default ? `default: ${describeMapping(f.default)}` : "no key by default");
  const name = h("div", { class: "keyname" }, shown(v));
  let capturing = false;
  const input = h("input", { id: f.id, type: "text", value: v, placeholder: f.default, oninput: (e) => {
    setValue(f.id, e.target.value);
    e.target.closest(".field").classList.toggle("set", e.target.value.trim() !== "");
    name.textContent = shown(e.target.value);
  } });
  const button = h("button", { class: "small", type: "button" }, "Press a key");
  const stop = () => {
    capturing = false;
    document.removeEventListener("keydown", onKey, true);
    button.textContent = "Press a key";
    button.blur();
  };
  const onKey = (e) => {
    e.preventDefault(); e.stopPropagation();
    const n = KEY_BY_CODE.get(e.code);
    if (n === undefined) { name.textContent = `That key (${e.code}) has no SDL scancode here; type it instead.`; return stop(); }
    input.value = withKey(input.value, n);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    stop();
  };
  button.addEventListener("click", () => {
    if (capturing) return stop();
    capturing = true;
    button.textContent = "Now press the key… (click to cancel)";
    document.addEventListener("keydown", onKey, true);
  });
  button.addEventListener("blur", () => { if (capturing) stop(); });
  return h("div", {}, h("div", { class: "row" }, input, button), name);
}

function fieldRow(f, step) {
  const range = f.min != null ? ` · range ${f.min} – ${f.max}` : "";
  return h("div", { class: "field" + ((app.values[f.id] || "").trim() ? " set" : "") },
    h("label", { for: f.id }, f.label),
    control(f, step),
    h("div", { class: "hint" }, f.help || null, f.help ? " " : null,
      h("span", { class: "def" }, `(default: ${f.default_label || "none"}${range})`),
      f.detail.length ? h("div", {}, f.detail.join(" ")) : null));
}

// ---- monitor helper panel ----------------------------------------------------------------
function fillFields(ids, vals) {
  ids.forEach((id, i) => {
    setValue(id, String(vals[i]));
    const el = document.getElementById(id);
    if (el) { el.value = String(vals[i]); el.closest(".field").classList.add("set"); }
  });
}
// Display fields: a dropdown filled from the live monitor detection (never from a saved ini).
const shortDesc = (m) => m.description.replace(/\s*\([^)]*\)$/, "");
const monLabel = (m) => `${m.name} — ${shortDesc(m)}` + (m.width ? ` · ${m.width}×${m.height}` : "") + ` at [${m.x}, ${m.y}]`;

function displayInfo(f, step) {
  const value = (app.values[f.id] || "").trim();
  const box = h("div", { class: "mon-info row" });
  if (app.displayError) {
    box.append(h("span", { class: "err" }, `Could not detect monitors (${app.displayError}). Type the display name instead.`));
    if (app.displayDiagnostics)
      box.append(h("details", { class: "diag" }, h("summary", {}, "What was tried"),
        h("pre", {}, JSON.stringify(app.displayDiagnostics, null, 2))));
  }
  else if (value && findMon(value)) {
    const m = findMon(value);
    box.append(h("span", { class: "muted" }, `VPX names this monitor "${m.id}" (the name is SDL's: ${m.name_source}).`));
  } else if (value && !findMon(value)) {
    const same = looseMon(value);
    box.append(h("span", { class: "warn" }, same
      ? `This monitor is now "${same.id}" (VPX matches the whole text, including the position). Pick it from the list to update.`
      : "This display is not connected to this system right now."));
  }
  return box;
}

function displayControl(f, step, v) {
  const onchange = (e) => {
    setValue(f.id, e.target.value);
    e.target.closest(".field").classList.toggle("set", e.target.value.trim() !== "");
    // Choosing a monitor sets the width/height to its full size (still editable afterwards).
    const m = findMon(e.target.value);
    if (m && m.width && step.size_keys) fillFields(step.size_keys, [m.width, m.height]);
    e.target.closest(".field").querySelector(".mon-info").replaceWith(displayInfo(f, step));
  };
  const detected = app.displays || [];
  let input;
  if (detected.length) {
    input = h("select", { id: f.id, onchange },
      h("option", { value: "" }, "Default (VPX chooses)"),
      detected.map((m) => h("option", { value: m.id, selected: m.id === v, title: `name from: ${m.name_source}` }, monLabel(m))),
      v && !findMon(v) ? h("option", { value: v, selected: true }, `${v} (${looseMon(v) ? "position or format differs" : "not connected"})`) : null);
  } else {
    input = h("input", { id: f.id, type: "text", value: v, placeholder: "Default display", oninput: onchange });
  }
  return h("div", {}, h("div", { class: "row" }, input,
    h("button", { class: "small", title: `Detect monitors again (${app.displayCommand})`,
      onclick: async () => { await loadDisplays(); render(); } }, "Re-detect")),
    displayInfo(f, step));
}

// ---- VPXConfig's own settings (Start page) ----------------------------------------------
function renderFoot() {
  $("#sidebar-foot").textContent = `Base: ${app.base.name} → ${app.output.name}`;
}

// Server-side file picker: browses the folders of the machine running this app, not the browser's.
function openPicker(startPath, onPick, opts = {}) {   // opts.save: choose a folder and a file name to write
  let hidden = true, all = false, selected = "", current = "";
  const crumbs = h("div", { class: "crumbs" });
  const list = h("div", { class: "filelist" });
  const status = h("p", { class: "muted" });
  const pickBtn = h("button", { class: "primary", disabled: !opts.save }, opts.save ? "Use this location" : "Use this file");
  const nameInput = opts.save ? h("input", { type: "text", value: opts.fileName || "VPinballX.ini", oninput: () => { chosen.textContent = target(); } }) : null;
  const target = () => (opts.save ? (current === "/" ? "" : current) + "/" + nameInput.value.trim() : selected);
  const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const choose = () => { if (opts.save && !nameInput.value.trim()) return; close(); onPick(target()); };
  pickBtn.addEventListener("click", choose);

  async function go(path) {
    const r = await api.get(`/api/fs?path=${encodeURIComponent(path || "")}&hidden=${hidden ? 1 : 0}&all=${all ? 1 : 0}&nearest=${opts.save ? 1 : 0}`);
    if (r.error) { status.className = "err"; status.textContent = r.error; return; }
    current = r.path;
    status.className = "muted";
    status.textContent = r.truncated ? "Only the first entries are shown." : (r.entries.length ? "" : "Nothing to show in this folder.");
    const join = (n) => (r.path === "/" ? "" : r.path) + "/" + n;
    selected = r.selected ? join(r.selected) : ""; pickBtn.disabled = opts.save ? false : !selected;
    if (opts.save && r.selected) nameInput.value = r.selected;
    crumbs.replaceChildren(
      h("button", { class: "small", onclick: () => go("") }, "Home"),
      ...r.crumbs.map((c) => h("button", { class: "small crumb", onclick: () => go(c.path) }, c.name)));
    list.replaceChildren(
      ...(r.parent ? [h("div", { class: "fentry dir", onclick: () => go(r.parent) }, h("span", {}, "↰"), "..")] : []),
      ...r.entries.map((e) => {
        const row = h("div", { class: "fentry " + (e.dir ? "dir" : "file") + (join(e.name) === selected ? " sel" : ""),
          onclick: () => {
            if (e.dir) return go(join(e.name));
            selected = join(e.name); pickBtn.disabled = false;
            if (opts.save) nameInput.value = e.name;
            list.querySelectorAll(".sel").forEach((x) => x.classList.remove("sel")); row.classList.add("sel");
            chosen.textContent = target();
          },
          ondblclick: () => { if (!e.dir) choose(); } }, h("span", {}, e.dir ? "📁" : "📄"), e.name);
        return row;
      }));
    chosen.textContent = opts.save ? target() : (selected || "No file selected");
  }

  const chosen = h("code", { class: "chosen" }, "No file selected");
  const toggles = h("div", { class: "row" },
    h("label", {}, h("input", { type: "checkbox", checked: true, onchange: (e) => { hidden = e.target.checked; go(current); } }), " Show hidden folders and files"),
    h("label", {}, h("input", { type: "checkbox", onchange: (e) => { all = e.target.checked; go(current); } }), " Show all files (not just .ini)"));
  const overlay = h("div", { class: "overlay", onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "modal" },
      h("div", { class: "row" }, h("h2", { style: "flex:1;margin:0" }, opts.title || "Choose a VPinballX.ini on this machine"),
        h("button", { class: "small", onclick: close }, "Close")),
      crumbs, toggles, list, status,
      opts.save ? h("div", { class: "row" }, h("label", {}, "File name:"), nameInput) : null,
      h("div", { class: "row" }, chosen, h("span", { style: "flex:1" }), h("button", { onclick: close }, "Cancel"), pickBtn)));
  document.body.append(overlay);
  document.addEventListener("keydown", onKey);
  go(startPath);
}

function basePanel() {
  const input = h("input", { id: "base-path", type: "text", value: app.base.path,
    placeholder: "Leave blank to use the project's VPinballX.ini" });
  const msg = h("p", { class: app.base.error ? "err" : "ok" }, app.base.error || app.baseNotice || "");
  app.baseNotice = "";
  const load = async () => {
    if (Object.values(app.values).some((v) => (v || "").trim()) &&
        !confirm("Loading a base file replaces the answers you have entered so far. Continue?")) return;
    clearTimeout(saveTimer); pending = {};
    const r = await api.send("POST", "/api/base", { path: input.value });
    if (r.error) { msg.className = "err"; msg.textContent = r.error; return; }
    app.steps = (await api.get("/api/steps")).steps;      // a loaded file can add fields (e.g. a connected controller's lines)
    app.values = r.values; app.base = r.base; app.output = r.output; renderFoot();
    const n = Object.keys(r.values).length;
    app.baseNotice = r.base.is_default ? "" : `${n} setting${n === 1 ? "" : "s"} filled in from the file.`;
    render();
  };
  const browse = () => openPicker(input.value.trim() || app.base.path, (path) => { input.value = path; load(); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
  return h("div", { class: "card" }, h("h2", {}, "Base ini file"),
    h("div", { class: "row" }, input, h("button", { class: "primary", onclick: browse }, "Browse…"),
      h("button", { onclick: load }, "Load")),
    msg,
    h("p", { class: "muted" }, app.base.is_default
      ? "Using the project's VPinballX.ini. Browse to an existing VPinballX.ini on this machine (or type its full path and click Load) to edit that instead."
      : `Using ${app.base.path}. The wizard fields below show its current values.` +
        (app.base.backup ? ` A backup copy is at ${app.base.backup}.` : "")));
}

// ---- quitting ---------------------------------------------------------------------------------
async function shutdown() {
  if (!confirm("Quit VPXConfig?\n\nYour answers are saved. The ini file is only written when you click Write on the Review page.")) return;
  try { await flush(); await api.send("POST", "/api/shutdown"); } catch (e) { /* the server may already be closing the connection */ }
  // When VPXConfig was started with --open-chrome, it closes this window itself once it sees the server
  // stop -- this page is only what's briefly visible (or, run plainly with no browser spawned, what stays
  // visible) in the meantime.
  document.body.replaceChildren(h("main", { class: "stopped" }, h("h1", {}, "VPXConfig has stopped"),
    h("p", { class: "muted" }, "You can close this tab. Start VPXConfig again to continue; your answers will still be there.")));
}

// ---- pages -------------------------------------------------------------------------------
function pager(i) {
  const prev = app.steps[i - 1], next = app.steps[i + 1];
  return h("div", { class: "pager" },
    prev ? h("button", { onclick: () => (location.hash = prev.id) }, "← " + prev.title) : h("span"),
    next ? h("button", { class: "primary", onclick: () => (location.hash = next.id) }, next.title + " →") : h("span"));
}

function formPage(step) {
  const panels = step.panels || (step.panel ? [step.panel] : []);
  return h("div", {}, panels.map((name) => (PANELS[name] ? PANELS[name]() : null)),
    step.groups.map((g) => g.collapsed
      ? h("details", { class: "card" }, h("summary", {}, `${g.title} (${g.fields.length})`), g.fields.map((f) => fieldRow(f, step)))
      : h("div", { class: "card" }, g.title === step.title ? null : h("h2", {}, g.title), g.fields.map((f) => fieldRow(f, step)))));
}

function targetPanel() {
  const o = app.output;
  const input = h("input", { id: "output-path", type: "text", value: o.override, placeholder: o.default_path });
  const msg = h("p", { class: "muted" });
  const apply = async () => {
    const r = await api.send("POST", "/api/output", { path: input.value });
    if (r.error) { msg.className = "err"; msg.textContent = r.error; return; }
    app.output = r.output; renderFoot(); render();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
  const browse = () => openPicker(input.value.trim() || o.path, (path) => { input.value = path; apply(); },
    { save: true, title: "Choose where to write the ini file", fileName: o.name });
  return h("div", { class: "card" }, h("h2", {}, "Output ini file"),
    h("div", { class: "row" }, input, h("button", { class: "primary", onclick: browse }, "Browse…"), h("button", { onclick: apply }, "Use")),
    msg,
    h("p", { class: "muted" }, o.is_default
      ? `By default the file is written where VPX keeps its settings: ${o.path}. Type another full path (or Browse) to write somewhere else.`
      : `Writing to ${o.path}. Clear the box and click Use to go back to ${o.default_path}.`),
    h("p", { class: "muted" }, o.exists
      ? `A file is already there. A backup copy named ${o.name}.vpconfigbackup is saved next to it before it is replaced.`
      : (o.folder_exists ? "There is no file there yet." : "That folder does not exist yet and will be created.")));
}

const PANELS = { base: basePanel, target: targetPanel };

function stubPage(step) {
  return h("div", { class: "card" }, h("p", {}, "This step isn't built yet."),
    h("p", { class: "muted" }, "See docs/PLAN.md for what it will cover."));
}

async function reviewPage() {
  const p = await api.get("/api/preview");
  const box = h("div", {});
  const result = h("p", {});
  box.append(
    ...(p.problems.length ? [h("div", { class: "card" }, h("h2", { class: "err" }, "Values that are not acceptable"),
      h("ul", {}, p.problems.map((x) => h("li", {}, h("b", {}, where(x)), `: "${x.value}" ${x.message}.`))),
      h("p", { class: "muted" }, "Fix these on their pages before writing the file."))] : []),
    h("div", { class: "card" }, h("h2", {}, `Changes from ${p.base.name} (${p.changes.length})`),
      p.changes.length ? h("table", {}, h("tr", {}, h("th", {}, "Setting"), h("th", {}, "Key"), h("th", {}, "New value"), h("th", {}, "Was")),
        p.changes.map((c) => h("tr", {}, h("td", {}, where(c)), h("td", {}, h("code", {}, `[${c.section}] ${c.key}`)),
          h("td", {}, c.value === "" ? h("i", { class: "muted" }, `VPX default (${c.default || "none"})`) : h("b", {}, c.value)),
          h("td", { class: "muted" }, c.was === "" ? `default (${c.default || "none"})` : c.was))))
        : h("p", { class: "muted" }, "No changes. The output would be identical to the base file."),
      h("p", { class: "muted" }, "The whole base file is written with these changes applied. A value equal to VPX's own default is left blank, because a blank value already means the default.")),
    h("div", { class: "card" }, h("div", { class: "row" },
      h("button", { class: "primary", onclick: async () => {
        const o = app.output;
        if (o.exists && !confirm(`This replaces ${o.path}.\nA backup copy is saved next to it first. Continue?`)) return;
        const r = await api.send("POST", "/api/save");
        result.className = r.error ? "err" : "ok";
        result.textContent = r.error || `Wrote ${r.path} (${r.count} setting${r.count === 1 ? "" : "s"} changed).` +
          (r.backup ? ` Backup of the previous file: ${r.backup}.` : "");
        if (!r.error) app.output = (await api.get("/api/state")).output;
      } }, `Write ${app.output.name}`)),
      h("p", { class: "muted" }, `Will write to ${app.output.path}` + (app.output.exists ? " (the existing file is backed up first)." : ".")),
      result,
      h("details", {}, h("summary", {}, "Preview the generated file"), h("pre", {}, p.text))));
  return box;
}

async function render() {
  await flush();
  const id = location.hash.slice(1) || app.steps[0].id;
  const i = Math.max(0, app.steps.findIndex((s) => s.id === id));
  const step = app.steps[i];
  renderNav();
  const body = step.review ? await reviewPage() : step.stub ? stubPage(step) : formPage(step);
  $("#main").replaceChildren(h("h1", {}, step.title), h("p", { class: "muted" }, step.description), body, pager(i));
  window.scrollTo(0, 0);
}

async function init() {
  const [s, st] = await Promise.all([api.get("/api/steps"), api.get("/api/state")]);
  app.steps = s.steps; app.output = st.output;
  $(".brand small").textContent = `Version: v${s.version}`;
  $("#shutdown").addEventListener("click", shutdown);
  app.values = st.values; app.base = st.base;
  renderFoot();
  await loadDisplays();
  window.addEventListener("hashchange", render);
  render();
}
init().catch((e) => { $("#main").textContent = "Failed to load: " + e; });
