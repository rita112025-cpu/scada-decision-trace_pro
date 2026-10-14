"use strict";
// 所有資料一律用 textContent 寫入 DOM，避免把事項內容當 HTML 執行。

const $ = (sel) => document.querySelector(sel);
const VIEW_LABELS = { all: "全部", open: "未結事項", overdue: "逾期事項", disputed: "待確認／有爭議", accepted: "已驗收" };
const TEXTAREAS = new Set(["description", "source_excerpt", "acceptance_criteria"]);
const ACTION_LABELS = { create: "新增", update: "修改", import: "匯入", sample: "範例", "clear-sample": "清除範例" };

let META = null;
let state = { view: "open", editing: null };

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

function query() {
  const p = new URLSearchParams({ view: state.view });
  for (const k of ["q", "issue_type", "status", "priority", "source_status", "owner", "sort", "order"]) {
    const v = $("#f-" + k).value.trim();
    if (v) p.set(k, v);
  }
  return p.toString();
}

async function refresh() {
  const data = await api("/api/issues?" + query());
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
    tr.append(idCell, el("td", it.issue_type), el("td", it.title), el("td", it.owner));
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
  $("#dlg-title").textContent = "新增事項";
  buildForm(null);
  $("#form-errors").hidden = true;
  $("#history-wrap").hidden = true;
  $("#dlg-issue").showModal();
}

async function openIssue(id) {
  const issue = await api("/api/issues/" + encodeURIComponent(id));
  state.editing = id;
  $("#dlg-title").textContent = `編輯 ${id}（建立 ${issue.created_at}，更新 ${issue.updated_at}）`;
  buildForm(issue);
  $("#form-errors").hidden = true;
  const hist = await api(`/api/issues/${encodeURIComponent(id)}/history`);
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
  $("#dlg-issue").showModal();
}

async function saveIssue(ev) {
  ev.preventDefault();
  const form = $("#form-issue");
  const data = {};
  for (const input of form.querySelectorAll("[name]")) data[input.name] = input.value;
  try {
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
  for (const id of ["issue_type", "status", "priority", "source_status", "sort", "order"]) $("#f-" + id).addEventListener("change", later);

  $("#btn-new").addEventListener("click", openNew);
  $("#form-issue").addEventListener("submit", saveIssue);
  $("#btn-cancel").addEventListener("click", () => $("#dlg-issue").close());
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
  await refresh();
}

init().catch((e) => showMsg("初始化失敗：" + e.message, "error"));
