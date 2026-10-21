"use strict";
// 所有資料一律用 textContent 寫入 DOM，避免把事項內容當 HTML 執行。

const $ = (sel) => document.querySelector(sel);
const VIEW_LABELS = { all: "全部", open: "未結", overdue: "逾期", disputed: "待確認／有爭議", accepted: "已驗收" };
const THEME_KEY = "sdt-theme";
const LAYOUT_KEY = "sdt-layout";
const SECTION_KEY = "sdt-sidebar-sections";
const ICONS_KEY = "sdt-sidebar-icons";
const SECTION_DEFAULTS = { view: true, project: false, status: false, priority: false, source: false };
const TEXTAREAS = new Set(["description", "source_excerpt", "acceptance_criteria"]);
const ACTION_LABELS = {
  create: "新增", update: "修改", import: "匯入", sample: "範例", "clear-sample": "清除範例",
  "confirm-draft": "確認入帳", "rename-project": "重新命名專案",
  "delete": "刪除", "detach-transcript": "解除逐字稿來源",
};

let META = null;
let state = {
  view: "all", editing: null, editingIssue: null, projects: [], importFile: null,
  layout: "board", expanded: null, selected: null, items: [], facetItems: [], counts: {},
  history: [], historyFor: null, sections: null, sidebarIcons: false,
};

async function api(path, opts = {}) {
  const headers = { "X-SDT": "1", ...(opts.headers || {}) };
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data.errors || ["操作失敗"]).join("\n"));
  return data;
}

function showMsg(text, kind = "") {
  const el = $("#msg");
  el.textContent = text;
  el.className = "msg " + kind;
  el.hidden = !text;
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function fillSelect(sel, values) {
  for (const v of values) sel.append(new Option(v, v));
}

function projectLabel(p) {
  return `${p.name || "（未命名）"}（${p.project_id}）`;
}

function query() {
  const p = new URLSearchParams({ view: state.view });
  for (const k of ["q", "issue_type", "status", "priority", "source_status", "owner", "sort", "order", "project"]) {
    const node = $("#f-" + k);
    if (!node) continue;
    const v = node.value.trim();
    if (v) p.set(k === "project" ? "project_id" : k, v);
  }
  return p.toString();
}

function fillProjectFilter() {
  const sel = $("#f-project");
  const current = sel.value;
  sel.replaceChildren(new Option("全部專案", ""));
  for (const p of state.projects) sel.append(new Option(projectLabel(p), p.project_id));
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

function currentProjectId() {
  return $("#f-project").value;
}

// 匯出與議程只跟著目前選取的專案；沒選專案時，按鈕明確標示「全部專案」。
function paintExportScope() {
  const id = currentProjectId();
  const project = state.projects.find((p) => p.project_id === id);
  const scope = id ? (project ? projectLabel(project) : id) : "全部專案";
  const link = $("#btn-export");
  link.href = "/api/export.csv" + (id ? "?" + new URLSearchParams({ project_id: id }) : "");
  link.textContent = `匯出 CSV（${scope}）`;
  $("#btn-agenda").textContent = `議程草稿（${scope}）`;
}

function fillProjectSelect(sel, { includeNew = false, selected = "" } = {}) {
  sel.replaceChildren();
  for (const p of state.projects) sel.append(new Option(projectLabel(p), p.project_id));
  if (includeNew) sel.append(new Option("＋ 新增專案…", "__new__"));
  if (selected && [...sel.options].some((o) => o.value === selected)) sel.value = selected;
  else if (!sel.options.length && includeNew) sel.value = "__new__";
}

function dueLabel(issue) {
  if (!issue.due_date) return "—";
  return issue.due_date.slice(5).replace("-", "/") + (issue.overdue ? " 逾期" : "");
}

function drawingLabel(issue) {
  return [issue.drawing_no, issue.drawing_rev].filter(Boolean).join(" ") || "—";
}

function modelLabel(issue) {
  return [issue.model_file, issue.model_element_id].filter(Boolean).join(" / ") || "—";
}

function tally(items, key) {
  const counts = new Map();
  for (const it of items) counts.set(it[key] || "", (counts.get(it[key] || "") || 0) + 1);
  return counts;
}

function setSelect(id, value, toggle) {
  const sel = $("#f-" + id);
  const next = toggle && sel.value === value ? "" : value;
  if (sel.value === next) return;
  sel.value = next;
  sel.dispatchEvent(new Event("change"));
}

function applyTheme(theme) {
  const dark = theme !== "light";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const btn = $("#btn-theme");
  btn.textContent = dark ? "☀ 切換為淺粉" : "☾ 切換為暗粉";
  btn.setAttribute("aria-pressed", dark ? "true" : "false");
  try { localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); } catch (e) { /* 隱私模式仍可切換，只是不記住 */ }
}

function applyStoredTheme() {
  let theme = "dark";
  try { theme = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) { theme = "dark"; }
  applyTheme(theme === "light" ? "light" : "dark");
}

function applyLayout(layout) {
  if (layout !== "board" && layout !== "split" && layout !== "segment") layout = "board";
  state.layout = layout;
  document.body.dataset.layout = layout;
  try { localStorage.setItem(LAYOUT_KEY, layout); } catch (e) { /* 同上 */ }
  $("#view-board").hidden = layout !== "board";
  $("#view-split").hidden = layout !== "split";
  $("#view-segment").hidden = layout !== "segment";
  $("#view-segments").hidden = layout !== "segment";
  const slot = layout === "segment" ? $("#search-slot-top") : $("#search-slot-side");
  slot.append($("#f-q"));
  for (const id of ["layout-board", "layout-split", "layout-segment"]) {
    const button = $("#" + id);
    button.setAttribute("aria-selected", button.dataset.layout === layout ? "true" : "false");
  }
  if (layout === "split" && !state.selected && state.items[0]) state.selected = state.items[0].issue_id;
}

function applyStoredLayout() {
  let layout = "board";
  try {
    const saved = localStorage.getItem(LAYOUT_KEY);
    if (saved === "split" || saved === "segment" || saved === "board") layout = saved;
  } catch (e) { layout = "board"; }
  applyLayout(layout);
  if (window.matchMedia("(max-width: 860px)").matches) {
    $("#sidebar").classList.add("is-collapsed");
    $("#btn-sidebar").setAttribute("aria-expanded", "false");
    $("#btn-sidebar").textContent = "展開篩選";
  }
}

async function pendingDraftCount(projects) {
  if (!projects.length) return 0;
  const counts = await Promise.all(projects.map((p) =>
    api("/api/drafts?" + new URLSearchParams({ project_id: p.project_id, status: "草稿" }))
      .then((data) => data.items.length)
      .catch(() => 0)));
  return counts.reduce((sum, n) => sum + n, 0);
}

async function refresh() {
  const facet = new URLSearchParams({ view: state.view });
  for (const k of ["q", "issue_type", "owner"]) {
    const v = $("#f-" + k).value.trim();
    if (v) facet.set(k, v);
  }
  const [data, projects, base] = await Promise.all([
    api("/api/issues?" + query()),
    api("/api/projects"),
    api("/api/issues?" + facet.toString()),
  ]);
  state.projects = projects.items;
  state.items = data.items;
  state.counts = data.counts;
  state.facetItems = base.items;
  fillProjectFilter();
  paintExportScope();
  if (state.selected && !data.items.some((it) => it.issue_id === state.selected)) state.selected = null;
  if (state.layout === "split" && !state.selected && data.items[0]) state.selected = data.items[0].issue_id;
  if (state.selected !== state.historyFor) state.history = [];
  paint();
  $("#btn-sample-clear").hidden = !data.has_sample;
  $("#btn-sample-load").hidden = data.has_sample;
  const pending = await pendingDraftCount(state.projects);
  const badge = $("#draft-count");
  badge.textContent = String(pending);
  if (state.layout === "split" && state.selected && state.historyFor !== state.selected) {
    loadSplitHistory(state.selected).catch((e) => showMsg(e.message, "error"));
  }
}

function loadSections() {
  const open = { ...SECTION_DEFAULTS };
  try {
    const saved = JSON.parse(localStorage.getItem(SECTION_KEY) || "{}");
    if (saved && typeof saved === "object") {
      for (const key of Object.keys(SECTION_DEFAULTS)) {
        if (typeof saved[key] === "boolean") open[key] = saved[key];
      }
    }
  } catch (e) { /* 用預設：只有檢視展開 */ }
  return open;
}

function saveSections() {
  try { localStorage.setItem(SECTION_KEY, JSON.stringify(state.sections)); } catch (e) { /* 仍可在這次工作階段開關 */ }
}

function sideIcon(paths) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("side-ico");
  for (const d of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.4");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
  }
  return svg;
}

const SECTION_ICONS = {
  view: ["M2.5 4h11", "M2.5 8h11", "M2.5 12h7"],
  project: ["M2 5.2h3.6l1.2 1.4H14V13H2z"],
  status: ["M8 2.6a5.4 5.4 0 1 0 0 10.8 5.4 5.4 0 0 0 0-10.8", "M8 5.2v3l1.8 1.2"],
  priority: ["M3.4 2.4v11.2", "M3.4 3.2h8.2L10 5.4l1.6 2.2H3.4"],
  source: ["M4 2.4h5.2L12.4 5.6V13.6H4z", "M9.2 2.6V5.8h3"],
};

function syncSection(wrap, id) {
  const open = Boolean(state.sections[id]);
  wrap.classList.toggle("is-open", open);
  const head = wrap.querySelector(".side-head");
  const panel = wrap.querySelector(".side-items");
  const hint = wrap.querySelector(".side-hint");
  head.setAttribute("aria-expanded", open ? "true" : "false");
  panel.inert = !open;
  if (hint) hint.hidden = open || !hint.dataset.label;
}

function sideGroup(id, name, options, onPick, hint) {
  if (!state.sections) state.sections = loadSections();
  const wrap = el("div", "", "side-group");
  wrap.dataset.section = id;
  const head = el("button", "", "side-head");
  head.type = "button";
  head.append(sideIcon(SECTION_ICONS[id]));
  head.append(el("span", name, "side-head-label"));
  const hintEl = el("span", hint || "", "side-hint");
  hintEl.dataset.label = hint || "";
  if (hint) hintEl.title = hint;
  const chev = el("span", "›", "side-chevron");
  chev.setAttribute("aria-hidden", "true");
  head.append(hintEl, chev);
  const panel = el("div", "", "side-items");
  panel.id = "side-sec-" + id;
  head.setAttribute("aria-controls", panel.id);
  const inner = el("div", "");
  for (const opt of options) {
    const button = el("button", "", "side-opt" + (opt.on ? " is-on" : ""));
    button.type = "button";
    button.setAttribute("aria-pressed", opt.on ? "true" : "false");
    button.append(el("span", opt.label, "side-opt-label"), el("span", String(opt.count), "side-count"));
    button.addEventListener("click", () => onPick(opt));
    inner.append(button);
  }
  panel.append(inner);
  head.addEventListener("click", () => {
    if (state.sidebarIcons) {
      applyIconMode(false);
      state.sections[id] = true;
    } else {
      state.sections[id] = !state.sections[id];
    }
    saveSections();
    syncSection(wrap, id);
  });
  wrap.append(head, panel);
  syncSection(wrap, id);
  return wrap;
}

function applyIconMode(on) {
  state.sidebarIcons = Boolean(on);
  const narrow = window.matchMedia("(max-width: 860px)").matches;
  $("#sidebar").classList.toggle("is-icons", state.sidebarIcons && !narrow);
  const btn = $("#btn-side-icons");
  if (btn) {
    btn.setAttribute("aria-pressed", state.sidebarIcons ? "true" : "false");
    btn.setAttribute("aria-label", state.sidebarIcons ? "展開側欄" : "收合為圖示");
  }
  try { localStorage.setItem(ICONS_KEY, state.sidebarIcons ? "1" : "0"); } catch (e) { /* 這次仍可收合 */ }
}

function renderSidebar() {
  const nav = $("#side-filters");
  nav.replaceChildren();
  const counts = state.counts || {};
  const viewOpts = META.views.map((v) => ({
    value: v, label: VIEW_LABELS[v], count: counts[v] ?? 0, on: state.view === v,
  }));
  nav.append(sideGroup("view", "檢視", viewOpts, (opt) => {
    if (state.view === opt.value) return;
    state.view = opt.value;
    refresh().catch((e) => showMsg(e.message, "error"));
  }, (viewOpts.find((o) => o.on && o.value !== "all") || {}).label || ""));
  const byProject = tally(state.facetItems, "project_id");
  const projectOpts = [];
  for (const p of state.projects) {
    projectOpts.push({
      value: p.project_id, label: p.name || "（未命名）", count: byProject.get(p.project_id) || 0,
      on: $("#f-project").value === p.project_id,
    });
  }
  nav.append(sideGroup("project", "專案", projectOpts, (opt) => setSelect("project", opt.value, opt.value !== ""),
    (projectOpts.find((o) => o.on) || {}).label || ""));
  const projectId = $("#f-project").value;
  const scoped = projectId ? state.facetItems.filter((it) => it.project_id === projectId) : state.facetItems;
  const byStatus = tally(scoped, "status");
  const statusOpts = META.statuses.map((s) => ({
    value: s, label: s, count: byStatus.get(s) || 0, on: $("#f-status").value === s,
  }));
  nav.append(sideGroup("status", "狀態", statusOpts, (opt) => setSelect("status", opt.value, true),
    (statusOpts.find((o) => o.on) || {}).label || ""));
  const byPriority = tally(scoped, "priority");
  const priOpts = META.priorities.map((s) => ({
    value: s, label: s, count: byPriority.get(s) || 0, on: $("#f-priority").value === s,
  }));
  nav.append(sideGroup("priority", "優先級", priOpts, (opt) => setSelect("priority", opt.value, true),
    (priOpts.find((o) => o.on) || {}).label || ""));
  const bySource = tally(scoped, "source_status");
  const srcOpts = META.source_statuses.map((s) => ({
    value: s, label: s, count: bySource.get(s) || 0, on: $("#f-source_status").value === s,
  }));
  nav.append(sideGroup("source", "來源狀態", srcOpts, (opt) => setSelect("source_status", opt.value, true),
    (srcOpts.find((o) => o.on) || {}).label || ""));
}

function renderViewSegments() {
  const box = $("#view-segments");
  box.replaceChildren();
  for (const v of META.views) {
    const button = el("button", `${VIEW_LABELS[v]} ${state.counts[v] ?? 0}`);
    button.type = "button";
    button.setAttribute("aria-pressed", state.view === v ? "true" : "false");
    if (v === "overdue") button.classList.add("is-overdue");
    button.addEventListener("click", () => {
      if (state.view === v) return;
      state.view = v;
      refresh().catch((e) => showMsg(e.message, "error"));
    });
    box.append(button);
  }
}

function statusChip(issue) {
  const chip = el("span", issue.status || "—", "chip");
  chip.dataset.status = issue.status || "";
  return chip;
}

function sourceMark(issue) {
  const src = el("span", "", "src");
  src.dataset.src = issue.source_status || "";
  const dot = document.createElement("i");
  dot.className = "src-dot";
  dot.setAttribute("aria-hidden", "true");
  src.append(dot, document.createTextNode(issue.source_status || "—"));
  return src;
}

function renderCard(issue, segment) {
  const open = state.expanded === issue.issue_id;
  const card = el("article", "", "kcard" + (open ? " is-open" : ""));
  card.dataset.issueId = issue.issue_id;
  const main = el("button", "", "kcard-main");
  main.type = "button";
  main.dataset.issueId = issue.issue_id;
  main.setAttribute("aria-expanded", open ? "true" : "false");
  if (segment) {
    main.draggable = true;
    main.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.setData("text/plain", issue.issue_id);
      ev.dataTransfer.effectAllowed = "move";
      card.classList.add("is-dragging");
    });
    main.addEventListener("dragend", () => {
      card.classList.remove("is-dragging");
      main.dataset.justDragged = "1";
      setTimeout(() => { delete main.dataset.justDragged; }, 0);
    });
  }
  const top = el("div", "", "kcard-top");
  const id = el("span", issue.issue_id, "mono");
  if (issue.is_sample) id.append(el("span", "範例", "tag sample"));
  const due = el("span", dueLabel(issue), issue.overdue ? "due overdue" : "due");
  if (issue.due_date) due.title = issue.due_date;
  top.append(id, due);
  main.append(top, el("div", issue.title, "kcard-title"));
  const meta = el("div", "", "kcard-meta");
  meta.append(statusChip(issue));
  if (segment) meta.append(el("span", drawingLabel(issue), "dwg"));
  else {
    meta.append(sourceMark(issue));
    if (issue.owner) meta.append(el("span", issue.owner, "owner"));
  }
  main.append(meta);
  main.addEventListener("click", () => {
    if (main.dataset.justDragged) return;
    state.expanded = open ? null : issue.issue_id;
    paint();
    const root = state.layout === "segment" ? "#view-segment" : "#view-board";
    const again = document.querySelector(`${root} [data-issue-id="${CSS.escape(issue.issue_id)}"] .kcard-main`);
    if (again) again.focus();
  });
  card.append(main);
  if (open) card.append(renderCardMore(issue, segment));
  return card;
}

function renderCardMore(issue, segment) {
  const more = el("div", "", "kcard-more");
  for (const [label, value] of [["圖號／版次", drawingLabel(issue)], ["模型／元素", modelLabel(issue)], ["來源", issue.source_file || "—"]]) {
    more.append(el("div", label, "meta-k"), el("div", value, "meta-v"));
  }
  if (segment && issue.status !== "處理中") {
    const actions = el("div", "", "kcard-actions");
    const move = el("button", "改為處理中");
    move.type = "button";
    move.addEventListener("click", () => changeStatus(issue.issue_id, "處理中"));
    actions.append(move);
    more.append(actions);
  }
  return more;
}

function groupColumns(items) {
  const columns = new Map();
  for (const issue of items) {
    const key = issue.project_id || "";
    if (!columns.has(key)) {
      columns.set(key, {
        project_id: key,
        name: issue.project || "（未指定專案）",
        items: [],
      });
    }
    columns.get(key).items.push(issue);
  }
  return [...columns.values()].sort((a, b) => a.project_id.localeCompare(b.project_id));
}

function renderBoard(root, segment) {
  root.replaceChildren();
  for (const column of groupColumns(state.items)) {
    const col = el("section", "", "kcol");
    const head = el("div", "", "kcol-head");
    head.append(el("div", column.name, "kcol-name"));
    const meta = el("div", "", "kcol-meta");
    meta.append(el("span", column.project_id || "—", "mono"), document.createTextNode(` · ${column.items.length}`));
    head.append(meta);
    col.append(head);
    for (const issue of column.items) col.append(renderCard(issue, segment));
    if (segment) col.append(dropZone());
    root.append(col);
  }
}

function dropZone() {
  const zone = el("div", "放開即改為「處理中」", "dropzone");
  zone.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    zone.classList.add("is-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
  zone.addEventListener("drop", (ev) => {
    ev.preventDefault();
    zone.classList.remove("is-over");
    const id = ev.dataTransfer.getData("text/plain");
    if (id) changeStatus(id, "處理中");
  });
  return zone;
}

function renderSplitItem(issue) {
  const on = state.selected === issue.issue_id;
  const button = el("button", "", "split-item" + (on ? " is-on" : ""));
  button.type = "button";
  button.setAttribute("aria-pressed", on ? "true" : "false");
  const top = el("div", "", "split-top");
  const id = el("span", issue.issue_id, "mono");
  if (issue.is_sample) id.append(el("span", "範例", "tag sample"));
  const due = el("span", dueLabel(issue), issue.overdue ? "due overdue" : "due");
  if (issue.due_date) due.title = issue.due_date;
  top.append(id, due);
  const meta = el("div", "", "split-meta");
  meta.append(statusChip(issue), sourceMark(issue));
  if (issue.owner) meta.append(el("span", issue.owner, "owner"));
  button.append(top, el("div", issue.title, "split-title"), meta);
  button.addEventListener("click", () => selectIssue(issue.issue_id));
  return button;
}

function renderSplit() {
  const list = $("#split-list");
  const detail = $("#split-detail");
  list.replaceChildren();
  detail.replaceChildren();
  for (const issue of state.items) list.append(renderSplitItem(issue));
  const issue = state.items.find((it) => it.issue_id === state.selected);
  if (!issue) {
    detail.append(el("p", "選取左側事項以查看證據與變更紀錄。", "hint"));
    return;
  }
  const head = el("div", "", "detail-head");
  head.append(el("span", `${issue.issue_id} · ${issue.project || "（未指定專案）"}`, "mono"));
  head.append(el("span", issue.issue_type || "—"));
  detail.append(head, el("h2", issue.title, "detail-title"));
  const grid = el("div", "", "detail-grid");
  const fields = [
    ["狀態", issue.status || "—", ""],
    ["來源狀態", issue.source_status || "—", ""],
    ["負責人", issue.owner || "—", ""],
    ["期限", dueLabel(issue), issue.overdue ? "overdue" : ""],
    ["圖號／版次", drawingLabel(issue), ""],
    ["模型／元素", modelLabel(issue), ""],
  ];
  for (const [label, value, cls] of fields) {
    grid.append(el("div", label, "meta-k"), el("div", value, cls));
  }
  detail.append(grid);
  const evidence = el("div", "");
  const bits = ["來源證據"];
  if (issue.source_file) bits.push(issue.source_file);
  if (issue.source_location) bits.push(issue.source_location);
  evidence.append(el("div", bits.join(" · "), "hint"));
  evidence.append(el("pre", issue.source_quote || issue.source_excerpt || "（沒有原文摘錄）", "quote"));
  detail.append(evidence);
  const histWrap = el("div", "");
  histWrap.append(el("div", "變更紀錄", "hint"));
  const hist = el("div", "");
  hist.id = "split-history";
  histWrap.append(hist);
  detail.append(histWrap);
  if (state.historyFor === issue.issue_id) fillHistory(hist, state.history);
  else hist.append(el("p", "載入變更紀錄…", "hint"));
  const actions = el("div", "", "detail-actions");
  const docs = el("button", "關聯工程文件");
  docs.type = "button";
  docs.addEventListener("click", () => openIssue(issue.issue_id).catch((e) => showMsg(e.message, "error")));
  const edit = el("button", "編輯");
  edit.type = "button";
  edit.addEventListener("click", () => openIssue(issue.issue_id).catch((e) => showMsg(e.message, "error")));
  const accept = el("button", "送驗收", "primary");
  accept.type = "button";
  accept.disabled = issue.status === "待驗收" || issue.status === "已驗收" || issue.status === "已關閉";
  accept.addEventListener("click", () => changeStatus(issue.issue_id, "待驗收"));
  actions.append(docs, edit, accept);
  detail.append(actions);
}

function fillHistory(box, items) {
  box.replaceChildren();
  if (!items.length) {
    box.append(el("p", "沒有變更紀錄。", "hint"));
    return;
  }
  for (const row of items.slice().reverse()) {
    const line = el("div", "", "hist-row");
    const fieldLabel = (META.fields.find(([key]) => key === row.field) || [row.field, row.field])[1];
    const change = row.old_value ? `${row.old_value} → ${row.new_value}` : (row.new_value || "");
    line.append(el("span", row.changed_at, "mono"), el("span", row.operator), el("span", fieldLabel), el("span", change));
    box.append(line);
  }
}

function selectIssue(id) {
  state.selected = id;
  state.history = [];
  state.historyFor = null;
  paint();
  loadSplitHistory(id).catch((e) => showMsg(e.message, "error"));
}

async function loadSplitHistory(id) {
  const hist = await api(`/api/issues/${encodeURIComponent(id)}/history`);
  if (state.selected !== id) return;
  state.history = hist.items;
  state.historyFor = id;
  const box = $("#split-history");
  if (box) fillHistory(box, hist.items);
}

async function changeStatus(id, status) {
  const current = state.items.find((it) => it.issue_id === id);
  if (!current || current.status === status) return;
  try {
    const res = await api("/api/issues/" + encodeURIComponent(id), { method: "PUT", json: { status } });
    const note = res.warnings && res.warnings.length ? `${id} 已更新，注意：\n` + res.warnings.join("\n") : `${id} 狀態已改為「${status}」`;
    showMsg(note, res.warnings && res.warnings.length ? "warn" : "");
    if (state.selected === id) state.historyFor = null;
    await refresh();
  } catch (e) {
    showMsg(e.message, "error");
  }
}

function paint() {
  const remember = ["#view-board", "#view-segment", "#split-list", "#side-body"];
  const saved = new Map();
  for (const sel of remember) {
    const node = $(sel);
    saved.set(sel, {
      top: node.scrollTop,
      left: node.scrollLeft,
      cols: [...node.querySelectorAll(".kcol")].map((col) => col.scrollTop),
    });
  }
  renderSidebar();
  renderViewSegments();
  renderBoard($("#view-board"), false);
  renderBoard($("#view-segment"), true);
  renderSplit();
  $("#empty").hidden = state.items.length > 0;
  for (const [sel, pos] of saved) {
    const node = $(sel);
    node.scrollTop = pos.top;
    node.scrollLeft = pos.left;
    node.querySelectorAll(".kcol").forEach((col, i) => {
      if (pos.cols[i] != null) col.scrollTop = pos.cols[i];
    });
  }
}

function buildForm(issue) {
  const wrap = $("#form-fields");
  wrap.replaceChildren();
  const choices = {
    issue_type: META.issue_types, source_status: META.source_statuses, priority: META.priorities,
    status: META.statuses, acceptance_result: META.acceptance_results,
  };
  for (const [key, label] of META.fields) {
    if (["issue_id", "created_at", "updated_at"].includes(key)) continue;
    if (key === "project") {
      const locked = Boolean(issue && issue.project_locked);
      const lab = el("label", "專案（ID 不可變）");
      const sel = document.createElement("select");
      sel.name = "project_id";
      fillProjectSelect(sel, { includeNew: !locked, selected: issue ? issue.project_id : "" });
      const nameInput = document.createElement("input");
      nameInput.name = "new_project_name";
      nameInput.placeholder = "新專案名稱";
      nameInput.hidden = locked || sel.value !== "__new__";
      sel.addEventListener("change", () => { nameInput.hidden = locked || sel.value !== "__new__"; });
      const hint = el("span", issue && issue.project_id ? `目前 ID：${issue.project_id}` : "選擇既有專案，或建立新的 ID");
      lab.append(sel, nameInput, hint);
      if (locked) {
        sel.disabled = true;
        nameInput.disabled = true;
        lab.append(el("span", "此事項已有逐字稿來源或文件關聯，不能直接改掛到別的專案，以免追溯資料對不上。請另建事項。", "lock-note"));
      }
      wrap.append(lab);
      continue;
    }
    const lab = el("label", label);
    let input;
    if (choices[key]) {
      input = document.createElement("select");
      for (const v of choices[key]) input.append(new Option(v || "（未填）", v));
    } else if (TEXTAREAS.has(key)) {
      input = document.createElement("textarea");
      lab.classList.add("wide");
    } else {
      input = document.createElement("input");
      input.type = key === "due_date" ? "date" : "text";
    }
    input.name = key;
    input.value = issue ? issue[key] ?? "" : defaultValue(key);
    if (key === "title") input.required = true;
    lab.append(input);
    wrap.append(lab);
  }
}

function defaultValue(key) {
  return { issue_type: "待確認", source_status: "缺乏證據", priority: "中", status: "待處理" }[key] || "";
}

function openNew() {
  state.editing = null;
  state.editingIssue = null;
  $("#dlg-title").textContent = "新增事項";
  buildForm(null);
  $("#form-errors").hidden = true;
  $("#history-wrap").hidden = true;
  $("#origin-wrap").hidden = true;
  $("#doc-wrap").hidden = true;
  $("#btn-delete-issue").hidden = true;
  $("#dlg-issue").showModal();
}

function showOrigin(issue) {
  const wrap = $("#origin-wrap");
  const has = Boolean(issue.transcript_id && issue.segment_ref);
  wrap.hidden = !has && !issue.source_quote;
  if (wrap.hidden) return;
  const bits = [];
  if (issue.extraction_method) bits.push(`抽取方式：${issue.extraction_method}`);
  if (issue.transcript_id) bits.push(`逐字稿 ${issue.transcript_id}`);
  if (issue.segment_ref) bits.push(`段落 ${issue.segment_ref}`);
  $("#origin-meta").textContent = bits.join(" · ") || "原始引用不會因修改標題或描述而覆寫。";
  $("#origin-quote").textContent = issue.source_quote || issue.source_excerpt || "";
  $("#origin-full").hidden = true;
  $("#origin-full").textContent = "";
}

async function openIssue(id) {
  const [issue, hist] = await Promise.all([
    api("/api/issues/" + encodeURIComponent(id)),
    api(`/api/issues/${encodeURIComponent(id)}/history`),
  ]);
  state.projects = (await api("/api/projects")).items;
  state.editing = id;
  state.editingIssue = issue;
  $("#dlg-title").textContent = `編輯 ${id}（建立 ${issue.created_at}，更新 ${issue.updated_at}）`;
  buildForm(issue);
  $("#form-errors").hidden = true;
  showOrigin(issue);
  const rows = $("#history-rows");
  rows.replaceChildren();
  for (const h of hist.items.slice().reverse()) {
    const tr = document.createElement("tr");
    const fieldLabel = (META.fields.find(([k]) => k === h.field) || [h.field, h.field])[1];
    for (const v of [h.changed_at, h.operator, ACTION_LABELS[h.action] || h.action, fieldLabel, h.old_value, h.new_value]) {
      tr.append(el("td", v));
    }
    rows.append(tr);
  }
  $("#history-wrap").hidden = false;
  $("#doc-wrap").hidden = false;
  $("#btn-delete-issue").hidden = false;
  $("#doc-results").replaceChildren();
  $("#doc-msg").textContent = "";
  await loadDocLinks(id);
  loadNotebooks().catch((e) => { $("#doc-msg").textContent = e.message; });
  $("#dlg-issue").showModal();
}

async function saveIssue(ev) {
  ev.preventDefault();
  const form = $("#form-issue");
  const data = {};
  for (const input of form.querySelectorAll("[name]")) data[input.name] = input.value;
  try {
    if (data.project_id === "__new__") {
      const name = (data.new_project_name || "").trim();
      if (!name) throw new Error("請輸入新專案名稱");
      const created = await api("/api/projects", { method: "POST", json: { name } });
      data.project_id = created.project.project_id;
    }
    delete data.new_project_name;
    const res = state.editing
      ? await api("/api/issues/" + encodeURIComponent(state.editing), { method: "PUT", json: data })
      : await api("/api/issues", { method: "POST", json: data });
    $("#dlg-issue").close();
    state.historyFor = null;
    const id = res.issue.issue_id;
    showMsg(res.warnings.length ? `${id} 已儲存，注意：\n` + res.warnings.join("\n") : `${id} 已儲存`,
      res.warnings.length ? "warn" : "");
    await refresh();
  } catch (e) {
    const box = $("#form-errors");
    box.textContent = e.message;
    box.hidden = false;
  }
}

async function showSourceSegment() {
  const issue = state.editingIssue;
  if (!issue || !issue.transcript_id || !issue.segment_ref) return;
  const seg = await api(`/api/transcripts/${encodeURIComponent(issue.transcript_id)}/segments/${encodeURIComponent(issue.segment_ref)}`);
  const lines = [
    `時間：${seg.timestamp_label || "無時間資訊"}`,
    seg.line_start ? `行號：${seg.line_start}` : "此段沒有行號（JSON 以段落 id 定位）",
    seg.speaker ? `講者：${seg.speaker}` : "",
    "",
    "顯示文字：",
    seg.text || "",
    "",
    "原始文字：",
    seg.raw_text || "",
  ].filter((v, i, arr) => v !== "" || arr[i - 1] !== "");
  const box = $("#origin-full");
  box.textContent = lines.join("\n");
  box.hidden = false;
}

function locationText(hit) {
  const parts = [];
  if (hit.page) parts.push(`第 ${hit.page} 頁`);
  if (hit.line_start) {
    parts.push(hit.line_end && hit.line_end !== hit.line_start
      ? `第 ${hit.line_start}–${hit.line_end} 行` : `第 ${hit.line_start} 行`);
  }
  if (hit.timestamp) parts.push(hit.timestamp);
  return parts.join("、") || "（此結果沒有頁碼、行號或時間戳）";
}

async function loadNotebooks() {
  const status = await api("/api/notebook/status");
  const sel = $("#doc-notebook");
  sel.replaceChildren(new Option("notebook…", ""));
  if (!status.reachable) {
    $("#doc-msg").textContent = status.message || "搜尋服務未啟動。事項資料仍在。";
    return;
  }
  const data = await api("/api/notebook/notebooks");
  for (const book of data.items) sel.append(new Option(book.name || "", book.name || ""));
  $("#doc-msg").textContent = status.db_configured
    ? "已連上搜尋。建立關聯後可用「定位」檢查複製檔是否還在。"
    : "已連上搜尋。未設定 local_notebook_db 時只能保存出處，無法從此處開啟檔案。";
}

async function searchDocs() {
  const notebook = $("#doc-notebook").value;
  const q = $("#doc-q").value.trim();
  const box = $("#doc-results");
  box.replaceChildren();
  try {
    const data = await api("/api/notebook/search?" + new URLSearchParams({ notebook, q }));
    if (!data.results.length) {
      box.append(el("p", "沒有符合的段落", "hint"));
      return;
    }
    for (const hit of data.results) {
      const card = el("article", "", "card");
      card.append(el("h3", hit.filename));
      card.append(el("p", locationText(hit), "hint"));
      card.append(el("p", hit.snippet || "（沒有摘錄）"));
      card.append(el("p", "未主張支持。選取只建立關聯。", "hint"));
      const btn = el("button", "關聯此段落");
      btn.type = "button";
      btn.addEventListener("click", () => linkDoc(hit).catch((e) => { $("#doc-msg").textContent = e.message; }));
      card.append(btn);
      box.append(card);
    }
  } catch (e) {
    $("#doc-msg").textContent = e.message + "\n事項台帳沒有被修改。";
  }
}

async function linkDoc(hit) {
  const res = await api(`/api/issues/${encodeURIComponent(state.editing)}/links`, { method: "POST", json: { hit } });
  $("#doc-msg").textContent = res.message;
  await loadDocLinks(state.editing);
}

async function loadDocLinks(issueId) {
  const data = await api(`/api/issues/${encodeURIComponent(issueId)}/links`);
  const box = $("#doc-links");
  box.replaceChildren();
  if (!data.items.length) {
    box.append(el("p", "尚未關聯文件。", "hint"));
    return;
  }
  for (const link of data.items) {
    const card = el("article", "", "card");
    card.append(el("h3", link.filename));
    card.append(el("p", `${link.notebook} · chunk ${link.chunk_id} · source ${link.source_id}`, "hint"));
    card.append(el("p", locationText(link)));
    card.append(el("p", link.snippet || ""));
    card.append(el("p", "狀態：未主張支持（關聯≠文件支持此決議）", "hint"));
    const btn = el("button", "定位來源");
    btn.type = "button";
    btn.addEventListener("click", () => locateLink(link.link_id, card).catch((e) => { $("#doc-msg").textContent = e.message; }));
    card.append(btn);
    box.append(card);
  }
}

async function locateLink(linkId, card) {
  const res = await api(`/api/links/${encodeURIComponent(linkId)}/locate`, { method: "POST", json: {} });
  const note = el("p", `${res.status}：${res.message}`);
  if (res.original_path) note.append(document.createTextNode(" 路徑：" + res.original_path));
  card.append(note);
}

function fileToBase64(file) {
  return file.arrayBuffer().then((buf) => {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  });
}

async function ensureImportProject() {
  const typed = $("#import-new-project").value.trim();
  if (typed) {
    const created = await api("/api/projects", { method: "POST", json: { name: typed } });
    state.projects = (await api("/api/projects")).items;
    fillProjectSelect($("#import-project"), { selected: created.project.project_id });
    $("#import-new-project").value = "";
    return created.project.project_id;
  }
  if (!$("#import-project").value) throw new Error("請選擇專案，或輸入新專案名稱");
  return $("#import-project").value;
}

function importBody() {
  if (!state.importFile) throw new Error("請選擇檔案");
  return ensureImportProject().then((projectId) => fileToBase64(state.importFile).then((content_base64) => ({
    project_id: projectId,
    filename: state.importFile.name,
    content_base64,
  })));
}

async function loadReparseTranscripts() {
  const sel = $("#reparse-transcript");
  const list = $("#transcript-list");
  const projectId = $("#import-project").value;
  sel.replaceChildren(new Option("選擇已匯入的逐字稿", ""));
  list.replaceChildren();
  if (!projectId) return;
  const data = await api("/api/transcripts?" + new URLSearchParams({ project_id: projectId }));
  for (const item of data.items) {
    sel.append(new Option(
      `${item.transcript_id}　${item.filename}　${item.format}　${item.timestamp_status}`,
      item.transcript_id,
    ));
    const card = el("article", "", "card");
    card.append(el("p", `${item.transcript_id}　${item.filename}　${item.segment_count} 段`));
    const actions = el("div", "", "row-actions");
    const pick = el("button", "選取");
    pick.type = "button";
    pick.addEventListener("click", () => { sel.value = item.transcript_id; });
    const remove = el("button", "刪除此逐字稿", "danger");
    remove.type = "button";
    remove.addEventListener("click", () => deleteTranscript(item.transcript_id).catch((e) => showMsg(e.message, "error")));
    actions.append(pick, remove);
    card.append(actions);
    list.append(card);
  }
  if (!data.items.length) list.append(el("p", "這個專案還沒有匯入逐字稿。", "hint"));
}

async function openImport() {
  state.projects = (await api("/api/projects")).items;
  fillProjectSelect($("#import-project"), { selected: $("#f-project").value });
  $("#import-new-project").value = "";
  $("#import-errors").hidden = true;
  $("#import-preview").hidden = true;
  $("#reparse-preview").hidden = true;
  $("#reextract-preview").hidden = true;
  $("#dlg-import").showModal();
  await loadReparseTranscripts();
}

async function previewReparse() {
  const box = $("#import-errors");
  box.hidden = true;
  const id = $("#reparse-transcript").value;
  if (!id) {
    box.textContent = "請選擇要重新解析的 TXT 逐字稿";
    box.hidden = false;
    return;
  }
  const res = await api(`/api/transcripts/${encodeURIComponent(id)}/reparse/preview`, { method: "POST", json: {} });
  const lines = [
    res.message,
    `時間資訊：${res.timestamp_status}`,
    res.timestamp_detail,
    `完整時間 ${res.time_counts.complete}、只有開始時間 ${res.time_counts.start_only}、沒有時間 ${res.time_counts.no_time}、時間錯誤 ${res.time_counts.time_error}`,
    `草稿 ${res.drafts.length} 筆、事項 ${res.issue_ids.length} 筆（預覽不會改審核狀態或原文）`,
  ];
  for (const change of res.changes.filter((c) => c.changed).slice(0, 12)) {
    lines.push(`第 ${change.line} 行 ${change.segment_id}：${change.before.timestamp_label} → ${change.after.timestamp_label}`);
  }
  if (res.unmatched_existing.length) lines.push("對不上、因此不會刪除的段落：" + res.unmatched_existing.join("、"));
  if (res.unmatched_new_lines.length) lines.push("原文裡尚未有段落、套用時只補段落不產生草稿的行：" + res.unmatched_new_lines.join("、"));
  const pre = $("#reparse-preview");
  pre.textContent = lines.join("\n");
  pre.hidden = false;
}

async function applyReparse() {
  const box = $("#import-errors");
  box.hidden = true;
  const id = $("#reparse-transcript").value;
  if (!id) {
    box.textContent = "請選擇要重新解析的 TXT 逐字稿";
    box.hidden = false;
    return;
  }
  const res = await api(`/api/transcripts/${encodeURIComponent(id)}/reparse`, { method: "POST", json: { confirm: true } });
  showMsg(res.message);
  await previewReparse();
  await loadReparseTranscripts();
}

function selectedTranscriptId() {
  const id = $("#reparse-transcript").value;
  if (!id) {
    const box = $("#import-errors");
    box.textContent = "請選擇已匯入的逐字稿";
    box.hidden = false;
    return "";
  }
  return id;
}

async function previewReextract() {
  const box = $("#import-errors");
  box.hidden = true;
  const id = selectedTranscriptId();
  if (!id) return;
  const res = await api(`/api/transcripts/${encodeURIComponent(id)}/reextract/preview`, { method: "POST", json: {} });
  const lines = [
    res.message,
    `抽取版本：${res.extraction_method}`,
    `將新增 ${res.added_count}、來源位置已存在而略過 ${res.skipped_count}、既有草稿保留 ${res.kept_count}、待核對 ${res.pending_count || 0}`,
    `先前抽取紀錄 ${res.runs.length} 筆。這是預覽，尚未寫入，也不會確認入帳。`,
  ];
  for (const row of res.added.slice(0, 12)) {
    const cue = row.cue ? `　說法 ${row.cue}　信心 ${row.confidence || "—"}` : "";
    lines.push(`新增 ${row.kind} 規則 ${row.rule_id}${cue} 第 ${row.line || "—"} 行第 ${row.sentence_index} 句：${row.source_quote}`);
  }
  const why = {
    legacy_quote: "舊草稿已依原文一對一對上",
    legacy_position: "舊草稿已依來源位置對上，原句保留",
    ambiguous: "無法唯一定位，標為待核對，沒有新增",
    position: "來源位置已有草稿",
  };
  for (const row of res.skipped.slice(0, 8)) {
    lines.push(`略過 ${row.kind} 第 ${row.line || "—"} 行第 ${row.sentence_index} 句（${why[row.reason] || "來源位置已有草稿"}）`);
  }
  for (const row of (res.pending_review || []).slice(0, 8)) {
    lines.push(`待核對 ${row.draft_id}：${row.note || "無法對到唯一的來源位置，沒有新增草稿"}`);
  }
  const pre = $("#reextract-preview");
  pre.textContent = lines.join("\n");
  pre.hidden = false;
}

async function applyReextract() {
  const box = $("#import-errors");
  box.hidden = true;
  const id = selectedTranscriptId();
  if (!id) return;
  const res = await api(`/api/transcripts/${encodeURIComponent(id)}/reextract`, { method: "POST", json: { confirm: true } });
  showMsg(res.message);
  await previewReextract();
}

async function previewImport() {
  const box = $("#import-errors");
  box.hidden = true;
  try {
    const body = await importBody();
    const res = await api("/api/transcripts/preview", { method: "POST", json: body });
    const lines = [
      `專案：${res.project_name || "（未命名）"}（${res.project_id}）`,
      `檔名：${res.filename}`,
      `格式：${res.format}`,
      `段落數：${res.segment_count}`,
      `時間資訊：${res.timestamp_status}`,
      res.timestamp_detail,
      res.time_counts
        ? `完整時間 ${res.time_counts.complete}、只有開始時間 ${res.time_counts.start_only}、沒有時間 ${res.time_counts.no_time}、時間錯誤 ${res.time_counts.time_error}`
        : "",
      res.duplicate ? `相同內容已存在：${res.existing_transcript_id}，確認匯入不會重複建立。` : "尚未匯入過這份內容。",
      ...res.notes,
    ];
    const pre = $("#import-preview");
    pre.textContent = lines.join("\n");
    pre.hidden = false;
  } catch (e) {
    box.textContent = e.message;
    box.hidden = false;
  }
}

async function confirmImport() {
  const box = $("#import-errors");
  box.hidden = true;
  try {
    const body = await importBody();
    body.confirm = true;
    const res = await api("/api/transcripts/import", { method: "POST", json: body });
    showMsg(res.message, res.duplicate ? "warn" : "");
    $("#dlg-import").close();
    await refresh();
  } catch (e) {
    box.textContent = e.message;
    box.hidden = false;
  }
}

async function openDrafts() {
  state.projects = (await api("/api/projects")).items;
  fillProjectSelect($("#draft-project"), { selected: $("#f-project").value });
  $("#dlg-drafts").showModal();
  await reloadDrafts();
}

async function reloadDrafts() {
  const box = $("#draft-list");
  box.replaceChildren();
  const projectId = $("#draft-project").value;
  if (!projectId) {
    box.append(el("p", "請先建立專案。", "hint"));
    return;
  }
  const params = new URLSearchParams({ project_id: projectId });
  if ($("#draft-status").value) params.set("status", $("#draft-status").value);
  const data = await api("/api/drafts?" + params.toString());
  if (!data.items.length) {
    box.append(el("p", "沒有草稿。", "hint"));
    return;
  }
  for (const draft of data.items) box.append(renderDraft(draft));
}

function renderDraft(draft) {
  const card = el("article", "", "card");
  card.append(el("h3", `${draft.draft_id}　${draft.kind}　${draft.review_status}`));
  card.append(el("p", draft.location_label, "hint"));
  card.append(el("p", `時間：${draft.timestamp_label}`, "hint"));
  card.append(el("p", `抽取 ${draft.extraction_method || "—"}　句子位置 ${draft.sentence_index || "（舊資料未記錄）"}`, "hint"));
  if (draft.rule_id || draft.reason) {
    card.append(el("p", `規則 ${draft.rule_id || "—"}：${draft.reason || ""}`, "hint"));
  }
  if (draft.cue || draft.confidence) {
    card.append(el("p", `對上的說法：${draft.cue || "—"}　信心：${draft.confidence || "—"}`, "hint"));
  }
  if (draft.review_gaps) card.append(el("p", `需人工核對：${draft.review_gaps}`, "hint"));
  const refs = Array.isArray(draft.context_refs) ? draft.context_refs : [];
  for (const ref of refs) {
    const id = ref.segment_id || ref.source_segment_id || "";
    card.append(el("p", `上下文段落 ${id}　${ref.timestamp_label || ""}（沒有併進原始引用）`, "hint"));
    card.append(el("pre", ref.text || "", "quote"));
  }
  card.append(el("p", `負責人：${draft.owner || "（空）"}（${draft.owner_origin}）　期限：${draft.due_date || "（空）"}（${draft.due_origin}）`, "hint"));
  card.append(el("p", "原始引用（不會被摘要修改覆寫）"));
  card.append(el("pre", draft.source_quote, "quote"));
  const summary = document.createElement("textarea");
  summary.value = draft.summary;
  const owner = document.createElement("input");
  owner.value = draft.owner;
  owner.placeholder = "負責人，原文沒有就留空";
  const due = document.createElement("input");
  due.type = "date";
  due.value = draft.due_date;
  card.append(el("p", "摘要", "hint"), summary, el("p", "負責人／期限（可人工補，留空即未指定）", "hint"), owner, due);
  card.append(el("p", draft.limitation, "hint"));
  const actions = el("div", "", "row-actions");
  if (draft.review_status === "草稿") {
    const save = el("button", "儲存修改");
    save.type = "button";
    save.addEventListener("click", () => saveDraft(draft.draft_id, summary.value, owner.value, due.value).catch((e) => showMsg(e.message, "error")));
    const ok = el("button", "確認入帳", "primary");
    ok.type = "button";
    ok.addEventListener("click", () => confirmDraft(draft.draft_id, summary.value, owner.value, due.value).catch((e) => showMsg(e.message, "error")));
    const drop = el("button", "捨棄");
    drop.type = "button";
    drop.addEventListener("click", () => discardDraft(draft.draft_id).catch((e) => showMsg(e.message, "error")));
    const remove = el("button", "刪除", "danger");
    remove.type = "button";
    remove.addEventListener("click", () => deleteDraft(draft.draft_id));
    actions.append(save, ok, drop, remove);
  } else if (draft.review_status === "已捨棄") {
    const remove = el("button", "刪除", "danger");
    remove.type = "button";
    remove.addEventListener("click", () => deleteDraft(draft.draft_id));
    actions.append(remove);
  } else if (draft.issue_id) {
    actions.append(el("p", `已入帳：${draft.issue_id}`, "hint"));
  }
  card.append(actions);
  return card;
}

async function saveDraft(id, summary, owner, due) {
  await api("/api/drafts/" + encodeURIComponent(id), { method: "PUT", json: { summary, owner, due_date: due } });
  showMsg(`${id} 已更新。原始引用未改。`);
  await reloadDrafts();
}

async function confirmDraft(id, summary, owner, due) {
  await saveDraft(id, summary, owner, due);
  const res = await api(`/api/drafts/${encodeURIComponent(id)}/confirm`, { method: "POST", json: {} });
  showMsg(res.message);
  await reloadDrafts();
  await refresh();
}

async function discardDraft(id) {
  const res = await api(`/api/drafts/${encodeURIComponent(id)}/discard`, { method: "POST", json: {} });
  showMsg(res.message);
  await reloadDrafts();
  await refresh();
}

function askDelete(title, body, buttons) {
  $("#delete-title").textContent = title;
  $("#delete-body").textContent = body;
  const box = $("#delete-actions");
  box.replaceChildren();
  for (const spec of buttons) {
    const button = el("button", spec.label, spec.danger ? "danger" : "");
    button.type = "button";
    button.addEventListener("click", () => {
      $("#dlg-delete").close();
      spec.onClick();
    });
    box.append(button);
  }
  $("#dlg-delete").showModal();
}

async function deleteTranscript(id) {
  const plan = await api(`/api/transcripts/${encodeURIComponent(id)}/delete`, {
    method: "POST", json: { preview: true },
  });
  const lines = [
    plan.message,
    "不會刪磁碟上的原始 TXT。",
  ];
  const buttons = [{ label: "取消，不刪除", onClick: () => {} }];
  if (plan.confirmed_count || (plan.issue_ids || []).length) {
    buttons.push({
      label: "保留台帳並解除來源",
      danger: true,
      onClick: () => applyDeleteTranscript(id, "detach").catch((e) => showMsg(e.message, "error")),
    });
  } else {
    buttons.push({
      label: "刪除此逐字稿",
      danger: true,
      onClick: () => applyDeleteTranscript(id, "").catch((e) => showMsg(e.message, "error")),
    });
  }
  askDelete("刪除此逐字稿", lines.join("\n"), buttons);
}

async function applyDeleteTranscript(id, ledger) {
  const body = { confirm: true };
  if (ledger) body.ledger = ledger;
  const res = await api(`/api/transcripts/${encodeURIComponent(id)}/delete`, { method: "POST", json: body });
  showMsg(res.message, res.deleted ? "" : "warn");
  await loadReparseTranscripts();
  await refresh();
}

function deleteDraft(id) {
  askDelete(
    "刪除草稿",
    "刪除會拿掉這筆草稿，不會留下捨棄紀錄，也不會刪台帳或磁碟上的原始檔。",
    [
      { label: "取消", onClick: () => {} },
      {
        label: "刪除",
        danger: true,
        onClick: () => applyDeleteDraft(id).catch((e) => showMsg(e.message, "error")),
      },
    ],
  );
}

async function applyDeleteDraft(id) {
  const res = await api(`/api/drafts/${encodeURIComponent(id)}/delete`, { method: "POST", json: { confirm: true } });
  showMsg(res.message);
  await reloadDrafts();
}

function deleteIssue(id) {
  askDelete(
    "刪除事項",
    `刪除事項 ${id}？變更紀錄會留下這次刪除。文件關聯只移除資料庫裡的連結，不會刪磁碟上的檔案。若此事項來自草稿，該草稿會回到未入帳。`,
    [
      { label: "取消", onClick: () => {} },
      {
        label: "刪除事項",
        danger: true,
        onClick: () => applyDeleteIssue(id).catch((e) => showMsg(e.message, "error")),
      },
    ],
  );
}

async function applyDeleteIssue(id) {
  const res = await api(`/api/issues/${encodeURIComponent(id)}/delete`, { method: "POST", json: { confirm: true } });
  if ($("#dlg-issue").open) $("#dlg-issue").close();
  showMsg(res.message);
  await refresh();
}

function openWipe() {
  $("#wipe-confirm").value = "";
  $("#wipe-errors").hidden = true;
  $("#btn-wipe-go").disabled = true;
  $("#dlg-wipe").showModal();
}

async function applyWipe() {
  const box = $("#wipe-errors");
  box.hidden = true;
  const typed = $("#wipe-confirm").value.trim();
  if (typed !== META.clear_all_phrase) {
    box.textContent = `請輸入「${META.clear_all_phrase}」`;
    box.hidden = false;
    return;
  }
  try {
    const res = await api("/api/data/clear", { method: "POST", json: { confirm: typed } });
    $("#dlg-wipe").close();
    if ($("#dlg-import").open) $("#dlg-import").close();
    if ($("#dlg-drafts").open) $("#dlg-drafts").close();
    if ($("#dlg-issue").open) $("#dlg-issue").close();
    showMsg(`${res.message}\n備份：${res.backup}`);
    await refresh();
  } catch (e) {
    box.textContent = e.message;
    box.hidden = false;
  }
}

async function openRename() {
  state.projects = (await api("/api/projects")).items;
  fillProjectSelect($("#rename-project"), { selected: $("#f-project").value });
  const current = state.projects.find((p) => p.project_id === $("#rename-project").value);
  $("#rename-name").value = current ? current.name : "";
  $("#rename-errors").hidden = true;
  $("#dlg-project").showModal();
}

async function saveRename() {
  const box = $("#rename-errors");
  box.hidden = true;
  try {
    const res = await api("/api/projects/" + encodeURIComponent($("#rename-project").value), {
      method: "PUT", json: { name: $("#rename-name").value },
    });
    showMsg(`專案 ${res.project.project_id} 名稱已改為「${res.project.name || "（未命名）"}」。ID 沒有變。`);
    $("#dlg-project").close();
    await refresh();
  } catch (e) {
    box.textContent = e.message;
    box.hidden = false;
  }
}

async function showAgenda() {
  const id = currentProjectId();
  const data = await api("/api/agenda" + (id ? "?" + new URLSearchParams({ project_id: id }) : ""));
  $("#agenda-text").value = data.markdown;
  $("#dlg-agenda").showModal();
}

function downloadAgenda() {
  const blob = new Blob(["﻿" + $("#agenda-text").value], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `agenda-${META.today}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function importCsv(ev) {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  if (!confirm(`匯入「${file.name}」？\n系統會先自動備份；任一列有錯就整批不匯入。`)) return;
  try {
    const res = await api("/api/import", { method: "POST", body: await file.arrayBuffer(),
      headers: { "Content-Type": "text/csv" } });
    const lines = [`匯入完成：新增 ${res.created}、更新 ${res.updated}、未變更 ${res.unchanged}`,
      `匯入前備份：${res.backup}`, ...res.warnings];
    showMsg(lines.join("\n"), res.warnings.length ? "warn" : "");
    await refresh();
  } catch (e) {
    showMsg("匯入失敗，未寫入任何資料：\n" + e.message, "error");
  }
}

async function clearSample() {
  if (!confirm("確定要清除所有範例資料？（只清除標記為「範例」的事項）")) return;
  const typed = prompt(`請輸入「${META.clear_sample_phrase}」再次確認：`);
  if (typed === null) return;
  try {
    const res = await api("/api/sample/clear", { method: "POST", json: { confirm: typed.trim() } });
    showMsg(`已清除 ${res.cleared} 筆範例資料（清除前備份：${res.backup}）`);
    await refresh();
  } catch (e) {
    showMsg(e.message, "error");
  }
}

async function init() {
  META = await api("/api/meta");
  $("#operator").textContent = META.operator;
  $("#today").textContent = META.today;
  applyStoredTheme();
  applyStoredLayout();
  fillSelect($("#f-issue_type"), META.issue_types);
  fillSelect($("#f-status"), META.statuses);
  fillSelect($("#f-priority"), META.priorities);
  fillSelect($("#f-source_status"), META.source_statuses);

  let timer;
  const later = () => { clearTimeout(timer); timer = setTimeout(() => refresh().catch((e) => showMsg(e.message, "error")), 250); };
  for (const id of ["q", "owner"]) $("#f-" + id).addEventListener("input", later);
  for (const id of ["issue_type", "status", "priority", "source_status", "sort", "order", "project"]) {
    $("#f-" + id).addEventListener("change", later);
  }

  $("#btn-new").addEventListener("click", openNew);
  $("#form-issue").addEventListener("submit", saveIssue);
  $("#btn-cancel").addEventListener("click", () => $("#dlg-issue").close());
  $("#btn-origin").addEventListener("click", () => showSourceSegment().catch((e) => showMsg(e.message, "error")));
  $("#btn-agenda").addEventListener("click", () => showAgenda().catch((e) => showMsg(e.message, "error")));
  $("#btn-agenda-close").addEventListener("click", () => $("#dlg-agenda").close());
  $("#btn-agenda-copy").addEventListener("click", () => navigator.clipboard.writeText($("#agenda-text").value)
    .then(() => showMsg("議程已複製")).catch(() => showMsg("無法存取剪貼簿，請手動選取複製", "warn")));
  $("#btn-agenda-download").addEventListener("click", downloadAgenda);
  $("#file-import").addEventListener("change", importCsv);
  $("#btn-backup").addEventListener("click", () => api("/api/backup", { method: "POST", json: {} })
    .then((r) => showMsg(`已備份：data/backups/${r.file}`)).catch((e) => showMsg(e.message, "error")));
  $("#btn-sample-load").addEventListener("click", () => api("/api/sample/load", { method: "POST", json: {} })
    .then((r) => { showMsg(`已載入 ${r.loaded} 筆範例資料`); return refresh(); }).catch((e) => showMsg(e.message, "error")));
  $("#btn-sample-clear").addEventListener("click", clearSample);
  $("#btn-wipe").addEventListener("click", openWipe);
  $("#btn-wipe-cancel").addEventListener("click", () => $("#dlg-wipe").close());
  $("#wipe-confirm").addEventListener("input", () => {
    $("#btn-wipe-go").disabled = $("#wipe-confirm").value.trim() !== META.clear_all_phrase;
  });
  $("#btn-wipe-go").addEventListener("click", () => applyWipe());
  $("#btn-delete-issue").addEventListener("click", () => {
    if (state.editing) deleteIssue(state.editing);
  });
  $("#import-project").addEventListener("change", () => loadReparseTranscripts().catch((e) => showMsg(e.message, "error")));
  $("#btn-reparse-preview").addEventListener("click", () => previewReparse().catch((e) => {
    const box = $("#import-errors");
    box.textContent = e.message;
    box.hidden = false;
  }));
  $("#btn-reparse-apply").addEventListener("click", () => applyReparse().catch((e) => {
    const box = $("#import-errors");
    box.textContent = e.message;
    box.hidden = false;
  }));
  $("#btn-reextract-preview").addEventListener("click", () => previewReextract().catch((e) => {
    const box = $("#import-errors");
    box.textContent = e.message;
    box.hidden = false;
  }));
  $("#btn-reextract-apply").addEventListener("click", () => applyReextract().catch((e) => {
    const box = $("#import-errors");
    box.textContent = e.message;
    box.hidden = false;
  }));
  $("#btn-import").addEventListener("click", () => openImport().catch((e) => showMsg(e.message, "error")));
  $("#btn-import-close").addEventListener("click", () => $("#dlg-import").close());
  $("#btn-import-preview").addEventListener("click", () => previewImport());
  $("#btn-import-confirm").addEventListener("click", () => confirmImport());
  $("#import-file").addEventListener("change", (ev) => { state.importFile = ev.target.files[0] || null; });
  $("#btn-drafts").addEventListener("click", () => openDrafts().catch((e) => showMsg(e.message, "error")));
  $("#btn-draft-close").addEventListener("click", () => $("#dlg-drafts").close());
  $("#btn-draft-reload").addEventListener("click", () => reloadDrafts().catch((e) => showMsg(e.message, "error")));
  $("#draft-project").addEventListener("change", () => reloadDrafts().catch((e) => showMsg(e.message, "error")));
  $("#draft-status").addEventListener("change", () => reloadDrafts().catch((e) => showMsg(e.message, "error")));
  $("#btn-project").addEventListener("click", () => openRename().catch((e) => showMsg(e.message, "error")));
  $("#btn-rename-close").addEventListener("click", () => $("#dlg-project").close());
  $("#btn-rename-save").addEventListener("click", () => saveRename());
  $("#rename-project").addEventListener("change", () => {
    const current = state.projects.find((p) => p.project_id === $("#rename-project").value);
    $("#rename-name").value = current ? current.name : "";
  });
  $("#btn-doc-search").addEventListener("click", () => searchDocs());
  $("#btn-theme").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
  $("#btn-sidebar").addEventListener("click", () => {
    const collapsed = $("#sidebar").classList.toggle("is-collapsed");
    $("#btn-sidebar").setAttribute("aria-expanded", collapsed ? "false" : "true");
    $("#btn-sidebar").textContent = collapsed ? "展開篩選" : "收合篩選";
  });
  try { state.sidebarIcons = localStorage.getItem(ICONS_KEY) === "1"; } catch (e) { state.sidebarIcons = false; }
  applyIconMode(state.sidebarIcons);
  $("#btn-side-icons").addEventListener("click", () => applyIconMode(!state.sidebarIcons));
  // body 也有 data-layout（給版面 CSS）。若把監聽掛到 body，側欄分區的點擊會冒泡上來，
  // 重繪整個側欄並把鍵盤焦點打回 body。只綁真正的版面按鈕。
  for (const button of document.querySelectorAll("button[data-layout]")) {
    button.addEventListener("click", () => {
      applyLayout(button.dataset.layout);
      if (state.items) {
        paint();
        if (state.layout === "split" && state.selected && state.historyFor !== state.selected) {
          loadSplitHistory(state.selected).catch((e) => showMsg(e.message, "error"));
        }
      }
    });
  }
  $("#more-menu").addEventListener("click", (ev) => {
    if (ev.target.closest("button, a")) $("#more-menu").open = false;
  });
  await refresh();
}

init().catch((e) => showMsg("初始化失敗：" + e.message, "error"));
