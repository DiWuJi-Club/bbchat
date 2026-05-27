"use strict";

/* ---------- helpers ---------- */

const STATUS = {
  0: { label: "RUNNING", color: "secondary", dot: "bg-secondary", border: "border-secondary/30", bg: "bg-secondary/10", glow: "shadow-[0_0_8px_rgba(116,219,153,0.15)]" },
  1: { label: "STARTING", color: "tertiary", dot: "bg-tertiary", border: "border-tertiary/30", bg: "bg-tertiary/10", glow: "" },
  2: { label: "STOPPED", color: "outline", dot: "bg-outline", border: "border-outline/30", bg: "bg-surface-container-high", glow: "" },
  "-1": { label: "UNKNOWN", color: "error-red", dot: "bg-error-red", border: "border-error-red/30", bg: "bg-error/10", glow: "" },
};

function statusInfo(s) { return STATUS[String(s)] || STATUS["-1"]; }

function statusPill(s) {
  const i = statusInfo(s);
  return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full ${i.bg} border ${i.border} text-${i.color} font-label-caps text-label-caps ${i.glow}">${i.label}</span>`;
}

function iterStatusClass(st) {
  switch (st) {
    case "sent":            return "text-secondary";
    case "no_chat_opened":  return "text-outline";
    case "no_chat_candidates": return "text-outline";
    case "failed":          return "text-error-red";
    case "not_started":     return "text-tertiary";
    default:                return "text-on-surface-variant";
  }
}

function timeAgo(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 60) return diff + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
}

function toast(msg, isError) {
  let t = document.querySelector(".toast");
  if (!t) {
    t = document.createElement("div");
    t.className = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.className = "toast show" + (isError ? " err" : "");
  setTimeout(() => t.classList.remove("show"), 2400);
}

async function api(path, opts) {
  const resp = await fetch(path, opts || {});
  const text = await resp.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function setHealthTs() {
  const el = document.getElementById("health-ts");
  if (el) el.textContent = "synced " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

/* ---------- dashboard ---------- */

function initDashboard(groupId) {
  let timer = null;
  let phoneCache = [];
  let jobsCache = {};
  let recentByPhone = {};

  async function refresh() {
    try {
      const data = await api(`/api/phones?group_id=${encodeURIComponent(groupId)}`);
      phoneCache = data.phones || [];
      jobsCache = data.jobs || {};
      renderKpis();
      renderRoster();
      renderUpstreamHealth(data);
    } catch (e) {
      toast("拉取 phone 列表失败: " + e.message, true);
    }
    try {
      const r = await api("/api/runs");
      renderRuns(r.runs || []);
    } catch (e) { /* silent */ }
    setHealthTs();
  }

  function renderKpis() {
    const total = phoneCache.length;
    const running = phoneCache.filter(p => p.status === 0).length;
    const jobs = Object.values(jobsCache).filter(j => j.status === "running").length;
    document.getElementById("kpi-total").textContent = total || "0";
    document.getElementById("kpi-running").textContent = running;
    document.getElementById("kpi-running-pct").textContent =
      total ? Math.round((running / total) * 100) + "%" : "0%";
    document.getElementById("kpi-jobs").textContent = jobs;
    // KPI replies is filled lazily after per-row recent loads
    let totalReplies = 0;
    Object.values(recentByPhone).forEach(r => totalReplies += (r.replies || []).length);
    document.getElementById("kpi-replies").textContent = totalReplies;
  }

  function renderRoster() {
    const tbody = document.getElementById("phones-tbody");
    if (!phoneCache.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="px-4 py-6 text-center text-outline font-mono-sm text-mono-sm">
        没拿到 phone 列表 — 检查 GeeLark API 状态 (/api/health)
      </td></tr>`;
      return;
    }
    tbody.innerHTML = phoneCache.map(p => {
      const job = jobsCache[p.id];
      const dotInfo = statusInfo(p.status);
      const jobCell = job
        ? (job.status === "running"
            ? `<span class="text-tertiary">pid ${job.pid}</span>`
            : `<span class="text-outline">last ${job.pid}</span>`)
        : `<span class="text-outline">—</span>`;
      return `<tr data-id="${escapeHtml(p.id)}" class="hover:bg-surface-variant/50 transition-colors group relative">
        <td class="px-4 py-3 relative">
          <div class="absolute left-0 top-0 bottom-0 w-0.5 bg-transparent group-hover:${dotInfo.dot} transition-colors"></div>
          <div class="w-2 h-2 rounded-full ${dotInfo.dot}"></div>
        </td>
        <td class="px-4 py-3 font-mono-sm text-mono-sm text-on-surface">
          <a href="/phone/${escapeHtml(p.id)}" class="hover:text-primary">${escapeHtml(p.serialNo || "")}</a>
        </td>
        <td class="px-4 py-3 font-mono-sm text-mono-sm text-on-surface-variant">${escapeHtml(p.serialName || "")}</td>
        <td class="px-4 py-3">${statusPill(p.status)}</td>
        <td class="px-4 py-3 font-mono-sm text-mono-sm" data-cell="last-iter"><span class="text-outline">—</span></td>
        <td class="px-4 py-3 font-mono-sm text-mono-sm" data-cell="replies"><span class="text-outline">—</span></td>
        <td class="px-4 py-3 font-mono-sm text-mono-sm">${jobCell}</td>
        <td class="px-4 py-3 text-right">
          <div class="opacity-0 group-hover:opacity-100 transition-opacity flex justify-end gap-1">
            <button data-act="start" title="Start phone" class="text-outline hover:text-secondary transition-colors p-1 rounded hover:bg-surface-container"><span class="material-symbols-outlined text-[16px]">play_arrow</span></button>
            <button data-act="stop" title="Stop phone" class="text-outline hover:text-error-red transition-colors p-1 rounded hover:bg-surface-container"><span class="material-symbols-outlined text-[16px]">stop</span></button>
            <button data-act="capture" title="Capture + reply now" class="text-outline hover:text-primary transition-colors p-1 rounded hover:bg-surface-container"><span class="material-symbols-outlined text-[16px]">play_circle</span></button>
            <a href="/phone/${escapeHtml(p.id)}" title="Detail" class="text-outline hover:text-primary transition-colors p-1 rounded hover:bg-surface-container"><span class="material-symbols-outlined text-[16px]">arrow_forward</span></a>
          </div>
        </td>
      </tr>`;
    }).join("");
    bindRowActions();
    phoneCache.forEach(p => lazyLoadRow(p.id));
  }

  async function lazyLoadRow(profileId) {
    try {
      const data = await api(`/api/phone/${profileId}/recent`);
      recentByPhone[profileId] = data;
      const row = document.querySelector(`tr[data-id="${profileId}"]`);
      if (!row) return;
      const iters = data.iters || [];
      const replies = data.replies || [];
      const lastCell = row.querySelector('[data-cell="last-iter"]');
      const replyCell = row.querySelector('[data-cell="replies"]');
      if (iters[0]) {
        lastCell.innerHTML = `<span class="${iterStatusClass(iters[0].status)}">${escapeHtml(iters[0].status || "?")}</span>
          <div class="text-outline text-[10px]">${timeAgo(iters[0].captured_at)}</div>`;
      } else {
        lastCell.innerHTML = `<span class="text-outline">no data</span>`;
      }
      if (replies.length) {
        replyCell.innerHTML = `<span class="text-secondary font-bold">${replies.length}</span>
          <div class="text-outline text-[10px]">→ ${escapeHtml(replies[0].chat_title || "")}</div>`;
      } else {
        replyCell.innerHTML = `<span class="text-outline">0</span>`;
      }
      renderKpis();
    } catch (e) { /* silent */ }
  }

  function bindRowActions() {
    document.querySelectorAll("#phones-tbody button[data-act]").forEach(btn => {
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const tr = ev.currentTarget.closest("tr");
        const id = tr.dataset.id;
        const act = ev.currentTarget.dataset.act;
        ev.currentTarget.disabled = true;
        try {
          if (act === "start") {
            await api(`/api/phone/${id}/start`, { method: "POST" });
            toast("已发送启动指令 " + id);
          } else if (act === "stop") {
            await api(`/api/phone/${id}/stop`, { method: "POST" });
            toast("已发送关闭指令 " + id);
          } else if (act === "capture") {
            const r = await api(`/api/phone/${id}/capture`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ max_chats: 3, send_ai_replies: true, keep_phone_running: true }),
            });
            toast(`已启动 capture (pid ${r.pid})`);
          }
        } catch (e) {
          toast(act + " 失败: " + e.message, true);
        } finally {
          ev.currentTarget.disabled = false;
          refresh();
        }
      });
    });
  }

  function renderUpstreamHealth(data) {
    const el = document.getElementById("health-ts");
    if (!el) return;
    const ageTxt = data.cache_age > 0 ? ` (cached ${data.cache_age}s)` : "";
    if (data.upstream_ok === false) {
      el.innerHTML = `<span class="text-error-red">⚠ GeeLark API 离线</span>${ageTxt}`;
    } else {
      el.textContent = "synced " + new Date().toLocaleTimeString("zh-CN", { hour12: false }) + ageTxt;
    }
  }

  function renderRuns(runs) {
    const el = document.getElementById("runs-stream");
    if (!runs.length) {
      el.innerHTML = `<div class="text-outline">no runs yet</div>`;
      return;
    }
    el.innerHTML = runs.slice(0, 30).map(r => `
      <div class="flex gap-3 hover:bg-surface-container-low px-1 rounded transition-colors">
        <span class="text-outline shrink-0">[${escapeHtml(r.modified.replace("T", " ").slice(0, 19))}]</span>
        <span class="text-secondary shrink-0">DIR</span>
        <span class="text-on-surface-variant truncate">${escapeHtml(r.name)}</span>
      </div>`).join("");
  }

  document.getElementById("refresh-btn")?.addEventListener("click", refresh);
  document.getElementById("bulk-stop-btn")?.addEventListener("click", async () => {
    if (!confirm("确定要停止 group 内所有运行中的 phone 吗?")) return;
    const running = phoneCache.filter(p => p.status === 0);
    for (const p of running) {
      try { await api(`/api/phone/${p.id}/stop`, { method: "POST" }); } catch (e) {}
    }
    toast(`已发送 ${running.length} 个 stop 指令`);
    setTimeout(refresh, 1000);
  });
  document.getElementById("auto-refresh")?.addEventListener("change", (ev) => {
    if (timer) { clearInterval(timer); timer = null; }
    if (ev.target.checked) timer = setInterval(refresh, 5000);
  });
  timer = setInterval(refresh, 5000);
  refresh();
}

/* ---------- phone detail ---------- */

function initPhonePage(profileId) {
  document.getElementById("phone-title").textContent = profileId;

  async function refresh() {
    try {
      const data = await api(`/api/phones`);
      const p = (data.phones || []).find(x => x.id === profileId);
      if (p) {
        document.getElementById("phone-subtitle").textContent =
          `${p.serialNo || ""} · ${p.serialName || ""}`;
        document.getElementById("phone-status-badge").innerHTML = statusPill(p.status);
      }
    } catch (e) { /* silent */ }

    try {
      const r = await api(`/api/phone/${profileId}/recent`);
      renderIters(r.iters || []);
      renderReplies(r.replies || []);
    } catch (e) {
      toast("拉取数据失败: " + e.message, true);
    }
    try {
      const cl = await api(`/api/phone/${profileId}/chat-list-latest`);
      const pre = document.getElementById("chat-list-pre");
      const src = document.getElementById("chat-list-source");
      pre.textContent = cl.text || "(暂无 chat list dump)";
      src.textContent = cl.source || "";
    } catch (e) { /* silent */ }
    try {
      const log = await api(`/api/phone/${profileId}/log`);
      document.getElementById("job-log").textContent = log.log || (log.status === "no job" ? "no job" : "(empty)");
      document.getElementById("job-status").textContent = log.status || "";
    } catch (e) { /* silent */ }
    setHealthTs();
  }

  function renderIters(iters) {
    const tbody = document.getElementById("iter-tbody");
    if (!iters.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="px-4 py-6 text-center text-outline">no iters yet</td></tr>`;
      return;
    }
    tbody.innerHTML = iters.map(i => `
      <tr class="hover:bg-surface-variant/30 transition-colors">
        <td class="px-4 py-2 font-mono-sm text-mono-sm">
          <div class="text-on-surface">${timeAgo(i.captured_at)}</div>
          <div class="text-outline text-[10px]">${escapeHtml(i.iter || "")}</div>
        </td>
        <td class="px-4 py-2 font-mono-sm text-mono-sm ${iterStatusClass(i.status)}">${escapeHtml(i.status || "?")}</td>
        <td class="px-4 py-2 font-mono-sm text-mono-sm text-on-surface">${i.opened_chats || 0}</td>
        <td class="px-4 py-2 font-mono-sm text-mono-sm text-outline truncate" style="max-width:240px" title="${escapeHtml((i.candidates || []).join(', '))}">
          ${escapeHtml((i.candidates || []).join(', ')) || "—"}
        </td>
      </tr>`).join("");
  }

  function renderReplies(replies) {
    const ul = document.getElementById("replies-list");
    if (!replies.length) {
      ul.innerHTML = `<li class="px-4 py-4 text-outline font-mono-sm text-mono-sm">还没有已 sent 的 AI 回复</li>`;
      return;
    }
    ul.innerHTML = replies.map(r => `
      <li class="px-4 py-3 hover:bg-surface-variant/30 transition-colors">
        <div class="flex justify-between items-center mb-1">
          <span class="font-headline-md text-headline-md text-on-surface">→ ${escapeHtml(r.chat_title || "")}</span>
          <span class="font-label-caps text-label-caps text-tertiary">HEAT ${r.heat_score ?? "?"}</span>
        </div>
        <div class="font-body-md text-body-md text-on-surface-variant whitespace-pre-wrap">${escapeHtml(r.reply || "")}</div>
        <div class="font-mono-sm text-mono-sm text-outline mt-1">${fmtTime(r.captured_at)} · ${escapeHtml(r.iter || "")}</div>
      </li>`).join("");
  }

  document.querySelectorAll("button[data-action]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.action;
      btn.disabled = true;
      try {
        if (act === "start") {
          await api(`/api/phone/${profileId}/start`, { method: "POST" });
          toast("已发送启动指令");
        } else if (act === "stop") {
          await api(`/api/phone/${profileId}/stop`, { method: "POST" });
          toast("已发送关闭指令");
        } else if (act === "capture") {
          const r = await api(`/api/phone/${profileId}/capture`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ max_chats: 3, send_ai_replies: true, keep_phone_running: true }),
          });
          toast(`已启动 capture (pid ${r.pid})`);
        } else if (act === "job-stop") {
          await api(`/api/phone/${profileId}/job/stop`, { method: "POST" });
          toast("已发 SIGTERM 给当前 job");
        }
      } catch (e) {
        toast(act + " 失败: " + e.message, true);
      } finally {
        btn.disabled = false;
        refresh();
      }
    });
  });

  setInterval(refresh, 4000);
  refresh();
}
