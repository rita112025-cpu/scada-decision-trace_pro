"use strict";
// 所有資料一律用 textContent 寫入 DOM，避免把事項內容當 HTML 執行。

const $ = (sel) => document.querySelector(sel);
const VIEW_LABELS = { all: "全部", open: "未結事項", overdue: "逾期事項", disputed: "待確認／有爭議", accepted: "已驗收" };
const TEXTAREAS = new Set(["description", "source_excerpt", "acceptance_criteria"]);
const ACTION_LABELS = {
  create: "新增", update: "修改", import: "匯入", sample: "範例", "clear-sample": "清除範例",
  "confirm-draft": "確認入帳", "rename-project": "重新命名專案",
};

let META = null;
let state = { view: "open", editing: null, editingIssue: null, projects: [], importFile: null };

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

function fillProjectSelect(sel, { includeNew = false, selected = "" } = {}) {
  sel.replaceChildren();
  for (const p of state.projects) sel.append(new Option(projectLabel(p), p.project_id));
  if (includeNew) sel.append(new Option("＋ 新增專案…", "__new__"));
  if (selected && [...sel.options].some((o) => o.value === selected)) sel.value = selected;
  else if (!sel.options.length && includeNew) sel.value = "__new__";
}

async function refresh() {
  const [data, projects] = await Promise.all([api("/api/issues?" + query()), api("/api/projects")]);
  state.projects = projects.items;
  fillProjectFilter();
  renderTabs(data.counts);
  renderRows(data.items);
  $("#btn-sample-clear").hidden = !data.has_sample;
  $("#btn-sample-load").hidden = data.has_sample;
}

function renderTabs(counts) {
  const tabs = $("#tabs");
  tabs.replaceChildren();
  for (const v of META.views) {
    const b = el("button", `${VIEW_LABELS[v]}（${counts[v]}）`);
    b.setAttribute("aria-pressed", String(v === state.view));
    b.addEventListener("click", () => { state.view = v; refresh().catch((e) => showMsg(e.message, "error")); });
    tabs.append(b);
  }
}

function renderRows(items) {
  const body = $("#rows");
  body.replaceChildren();
  $("#empty").hidden = items.length > 0;
  for (const it of items) {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    const idCell = el("td", it.issue_id);
    if (it.is_sample) { idCell.append(" "); idCell.append(el("span", "範例", "tag sample")); }
    const projectCell = el("td");
    projectCell.append(document.createTextNode(it.project || "（未命名）"));
    if (it.project_id) {
      projectCell.append(document.createElement("br"));
      projectCell.append(el("span", it.project_id, "hint"));
    }
    tr.append(idCell, projectCell, el("td", it.issue_type), el("td", it.title), el("td", it.owner));
    tr.append(el("td", (it.due_date || "—") + (it.overdue ? " 逾期" : ""), it.overdue ? "overdue" : ""));
    tr.append(el("td", it.priority), el("td", it.status), el("td", it.source_status));
    tr.append(el("td", [it.drawing_no, it.drawing_rev && "版次 " + it.drawing_rev].filter(Boolean).join(" ")));
    tr.append(el("td", [it.model_file, it.model_element_id].filter(Boolean).join(" #")));
    const open = () => openIssue(it.issue_id).catch((e) => showMsg(e.message, "error"));
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") open(); });
    body.append(tr);
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
      const lab = el("label", "專案（ID 不可變）");
      const sel = document.createElement("select");
      sel.name = "project_id";
      fillProjectSelect(sel, { includeNew: true, selected: issue ? issue.project_id : "" });
      const nameInput = document.createElement("input");
      nameInput.name = "new_project_name";
      nameInput.placeholder = "新專案名稱";
      nameInput.hidden = sel.value !== "__new__";
      sel.addEventListener("change", () => { nameInput.hidden = sel.value !== "__new__"; });
      const hint = el("span", issue && issue.project_id ? `目前 ID：${issue.project_id}` : "選擇既有專案，或建立新的 ID");
      lab.append(sel, nameInput, hint);
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

async function openImport() {
  state.projects = (await api("/api/projects")).items;
  fillProjectSelect($("#import-project"), { selected: $("#f-project").value });
  $("#import-new-project").value = "";
  $("#import-errors").hidden = true;
  $("#import-preview").hidden = true;
  $("#dlg-import").showModal();
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
    actions.append(save, ok, drop);
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
  const data = await api("/api/agenda");
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
  await refresh();
}

init().catch((e) => showMsg("初始化失敗：" + e.message, "error"));
