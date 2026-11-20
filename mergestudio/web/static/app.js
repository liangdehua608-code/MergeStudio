/* ================================================================
 * 视频拼接工坊 v5 —— 前端
 * 结构：状态 S → 渲染函数 → 事件委托；队列每秒轮询一次。
 * ================================================================ */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const enc = encodeURIComponent;

const S = {
  cfg: null,
  queue: { tasks: [], counts: { queued: 0, running: 0, done: 0, error: 0, canceled: 0 }, paused: false, active_id: "" },
  active: null,
  scan: null,
  fileOrder: [],
  trim: { head: 0, tail: 0 },
  crop: { enabled: false, left: 0, top: 0, right: 0, bottom: 0, scale_back: false },
  audio: { scan: null },
  split: { plan: null },
  single: { path: "", width: 0, height: 0, duration: 0 },
  mux: { video: "", audio: "", vdur: 0, adur: 0 },
  encoders: {},
  detailId: "",
  view: "queue",
  busy: false,
  poll: null,
};

const STATE_TEXT = {
  idle: "空闲", queued: "排队中", running: "准备中", scanning: "扫描预检",
  copying: "合并中", encoding: "转码中", done: "已完成", error: "失败", canceled: "已取消",
};
const MODE_TEXT = { auto: "自动", copy: "无损直拷", encode: "全部转码" };

/* 图标（统一线条风格，避免 emoji 与界面打架） */
const ICONS = {
  up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  down: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  close: '<path d="M18 6L6 18M6 6l12 12"/>',
  retry: '<path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/>',
  open: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  chevron: '<path d="M9 18l6-6-6-6"/>',
};
function icon(name, size) {
  const s = size || 13;
  return '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
    ICONS[name] + "</svg>";
}

/* ---------------- 工具 ---------------- */
async function api(path, body) {
  const res = await fetch(path, {
    method: body !== undefined ? "POST" : "GET",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n <= 0) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(2) : n.toFixed(0)) + " " + u[i];
}

function fmtDur(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  return h ? h + ":" + String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0")
           : m + ":" + String(ss).padStart(2, "0");
}

function fmtDurPrecise(s) {
  if (!s && s !== 0) return "—";
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return h ? h + ":" + String(m).padStart(2, "0") + ":" + sec.toFixed(2).padStart(5, "0")
           : m + ":" + sec.toFixed(2).padStart(5, "0");
}

function timeAgo(ts) {
  if (!ts) return "—";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return Math.floor(d / 60) + " 分钟前";
  if (d < 86400) return Math.floor(d / 3600) + " 小时前";
  return Math.floor(d / 86400) + " 天前";
}

function clock(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0")
    + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function stateClass(st) {
  if (st === "done") return "done";
  if (st === "error") return "error";
  if (st === "canceled") return "canceled";
  if (st === "queued" || st === "idle") return "queued";
  return "running";
}

function toast(msg, type) {
  const el = document.createElement("div");
  el.className = "toast" + (type ? " " + type : "");
  el.innerHTML = '<span class="ic">' + (type === "ok" ? "✓" : type === "bad" ? "!" : "·") + "</span><span>" + esc(msg) + "</span>";
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3200);
  setTimeout(() => el.remove(), 3600);
}

/* ---------------- 路由 ---------------- */
const PAGES = {
  queue: { title: "任务队列", sub: "排队执行 · 单任务串行，互不抢占资源" },
  new: { title: "新建任务", sub: "选文件夹 → 预检 → 设参数 → 加入队列" },
  audio: { title: "提取音频", sub: "批量导出音轨 · m4a 直拷 / mp3 转码" },
  split: { title: "视频切割", sub: "按章节或固定时长切开 · 勾选要导出的片段" },
  single: { title: "单个视频", sub: "只处理一个视频 · 去水印 / 去黑边 / 剪片头片尾" },
  mux: { title: "音视频合并", sub: "把音频配到视频上 · 替换原音轨或混合" },
  settings: { title: "设置", sub: "ffmpeg 引擎与默认参数" },
};

function setRoute() {
  let name = (location.hash || "#/queue").replace(/^#\/?/, "") || "queue";
  if (!PAGES[name]) name = "queue";
  S.view = name;
  $$(".view").forEach(v => v.classList.toggle("active", v.dataset.view === name));
  $$("[data-nav]").forEach(a => a.classList.toggle("active", a.dataset.nav === name));
  $("#page-title").textContent = PAGES[name].title;
  $("#page-sub").textContent = PAGES[name].sub;
  $("#act-bar").classList.toggle("hidden", name !== "new");
  renderTopActions();
  if (name === "queue") renderQueue();
  if (name === "new") { renderScan(); updateBar(); }
  if (name === "audio") renderAudio();
  if (name === "split") renderSplit();
  if (name === "single") renderSingle();
  if (name === "mux") renderMux();
  if (name === "settings") loadConfig();
}

function renderTopActions() {
  const box = $("#top-actions");
  if (S.view === "queue") {
    const c = S.queue.counts || {};
    const running = c.running || 0;
    const status = S.queue.paused ? '<span class="pill warn">队列已暂停</span>'
      : running ? '<span class="pill brand">运行中 ' + running + "</span>"
      : '<span class="pill queued">队列空闲</span>';
    box.innerHTML = '<span class="pill queued">排队 ' + (c.queued || 0) + "</span>" + status;
  } else if (["new", "audio", "split", "single", "mux"].includes(S.view)) {
    box.innerHTML = '<a class="btn sm" href="#/queue">← 返回队列</a>';
  } else {
    box.innerHTML = "";
  }
}

/* ---------------- 配置 / 引擎卡 ---------------- */
async function loadConfig() {
  try {
    const cfg = await api("/api/config");
    S.cfg = cfg;
    renderEncoders(cfg.encoders || {});
    if (S.view === "settings") renderConfig(cfg);
    renderEngine(cfg);
  } catch (e) {
    $("#engine-text").textContent = "无法连接服务";
  }
}

function renderEngine(cfg) {
  const dot = document.querySelector("#engine-card .engine-dot");
  const ok = !!cfg.ready;
  dot.className = "engine-dot " + (ok ? "ok" : "bad");
  $("#engine-text").textContent = ok ? "ffmpeg 已就绪" : "ffmpeg 未就绪";
  const p = (cfg.ffmpeg || "").replace(/\\/g, "\\");
  $("#engine-path").textContent = ok ? p : "前往「设置」填写路径";
  $("#engine-path").title = p;
}

function renderConfig(cfg) {
  $("#cfg-ffmpeg").value = cfg.ffmpeg_input || "";
  $("#cfg-ffprobe").value = cfg.ffprobe_input || "";
  const pill = $("#cfg-pill"), banner = $("#cfg-banner");
  const ok = !!cfg.ready;
  pill.className = "pill " + (ok ? "done" : "error");
  pill.textContent = ok ? "已就绪" : "未就绪";
  if (ok) {
    banner.className = "banner ok";
    banner.innerHTML = "ffmpeg 与 ffprobe 均验证通过，可以开始处理任务。";
  } else {
    banner.className = "banner bad";
    const parts = [];
    if (!cfg.ffmpeg_ok) parts.push("<b>ffmpeg</b>：" + esc(cfg.ffmpeg_version || "未设置"));
    if (!cfg.ffprobe_ok) parts.push("<b>ffprobe</b>：" + esc(cfg.ffprobe_version || "未设置"));
    banner.innerHTML = "路径不可用，请检查后重新验证。<br>" + parts.join("<br>");
  }
  $("#cfg-ff-ver").textContent = (cfg.ffmpeg_version || "—").slice(0, 80);
  $("#cfg-fp-ver").textContent = (cfg.ffprobe_version || "—").slice(0, 80);
  $("#cfg-file").textContent = "保存于 " + (cfg.config_file || "—");

  // 运行与提醒
  $("#s-on-exists").value = cfg.on_exists || "rename";
  const n = cfg.notify || {};
  $("#s-notify").checked = n.enabled !== false;
  $("#s-tray").checked = n.tray !== false;
  renderNotifyState(cfg.notify_status || {});
}

function renderNotifyState(st) {
  const pill = $("#run-pill"), hint = $("#notify-hint");
  if (!pill) return;
  const on = !!st.enabled;
  const tray = !!st.tray;
  pill.className = "pill " + (on ? (tray ? "done" : "queued") : "queued");
  pill.textContent = !on ? "通知已关闭"
    : tray ? "通知 · 托盘在线"
    : st.platform === false ? "通知（无托盘）" : "通知已开 · 托盘不可用";
  if (st.platform === false) {
    hint.textContent = "当前系统没有托盘支持，任务完成时会尝试系统通知；不影响任务执行。";
  } else if (st.error) {
    hint.textContent = st.error + " —— 任务本身不受影响，只是不弹提示。";
  } else {
    hint.textContent = "托盘右键可暂停队列 / 打开界面 / 退出；关掉浏览器也不影响任务继续跑。";
  }
}

async function saveRuntimeSettings() {
  try {
    const r = await api("/api/settings", {
      on_exists: $("#s-on-exists").value,
      notify: { enabled: $("#s-notify").checked, tray: $("#s-tray").checked },
    });
    if (S.cfg) Object.assign(S.cfg, { on_exists: r.on_exists, notify: r.notify });
    renderNotifyState(r.notify_status || {});
    toast("设置已保存", "ok");
  } catch (e) {
    toast("保存失败：" + e.message, "bad");
  }
}

/* ================================================================
 * 队列页
 * ================================================================ */
function renderQueue() {
  renderStats();
  renderActive();
  renderTaskList();
  renderTopActions();
  $("#nav-badge").textContent = S.queue.counts.queued || 0;
  $("#btn-pause").textContent = S.queue.paused ? "继续队列" : "暂停队列";
  $("#btn-pause").classList.toggle("primary", S.queue.paused);
}

function renderStats() {
  const c = S.queue.counts || {};
  const cards = [
    { n: c.queued || 0, l: "排队中", i: "i1", svg: '<path d="M4 6h11M4 12h8M4 18h13"/>' },
    { n: c.running || 0, l: "进行中", i: "i2", svg: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>' },
    { n: c.done || 0, l: "已完成", i: "i3", svg: '<path d="M20 6L9 17l-5-5"/>' },
    { n: (c.error || 0) + (c.canceled || 0), l: "失败 / 取消", i: "i4", svg: '<path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="9"/>' },
  ];
  $("#stats").innerHTML = cards.map(x =>
    '<div class="stat"><div class="stat-ico ' + x.i + '">' +
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' + x.svg + "</svg>" +
    '</div><div><div class="stat-num">' + x.n + '</div><div class="stat-label">' + x.l + "</div></div></div>"
  ).join("");
}

function etaText(t) {
  const p = t.progress || 0;
  if (p < 0.02 || !t.elapsed) return "计算剩余时间…";
  const total = t.elapsed / p;
  const left = Math.max(0, total - t.elapsed);
  return "预计剩余 " + fmtDur(left) + " · 已用 " + fmtDur(t.elapsed);
}

function renderActive() {
  const box = $("#active-box");
  const t = S.active;

  if (!t || !["running", "scanning", "copying", "encoding"].includes(t.state)) {
    box.innerHTML =
      '<div class="empty-run">' +
      (S.queue.paused ? "⏸ <b>队列已暂停</b>，当前没有任务在跑；点右上「继续队列」恢复。"
                      : "💤 <b>队列空闲</b> —— 有新任务入队会自动开始，也可以先去新建一个。") +
      "</div>";
    $("#active-desc").textContent = S.queue.paused ? "暂停中：正在跑的任务不受影响，新任务不会启动" : "队列空闲时会自动开始下一个任务";
    return;
  }

  const pct = Math.round((t.progress || 0) * 1000) / 10;
  const st = StateOf(t.state);
  const opts = t.options || {};
  const trim = (Number(opts.trim_head) || 0) + (Number(opts.trim_tail) || 0);
  const cropOn = !!(opts.crop && opts.crop.enabled);
  const meta = (opts.op === "audio"
    ? [(t.total_files || 0) + " 个文件",
       "音频 " + String(opts.audio_format || "m4a").toUpperCase(),
       "保留原音质"]
    : opts.op === "split"
    ? [(t.total_files || 0) + " 段",
       opts.precise ? "精确切割（重编码）" : "流复制",
       "可随时取消"]
    : opts.op === "video"
    ? ["单个视频",
       (opts.crop && opts.crop.enabled) ? "画面裁剪" : "无裁剪",
       "输出为新文件"]
    : opts.op === "mux"
    ? ["视频 + 音轨", opts.mode === "mix" ? "与原声混合" : "替换原音轨",
       opts.fit === "video" ? "按视频时长" : "按较短者"]

    : [(t.total_files || 0) + " 个文件",
       MODE_TEXT[opts.mode] || opts.mode,
       trim > 0 ? "裁剪 " + trim + "s/集" : "不裁剪",
       cropOn ? "画面裁剪" : "",
       opts.chapters ? "写章节" : ""]
  ).filter(Boolean).join(" · ");

  // 轮询会重绘卡片，先把日志滚动位置记下来
  const prevLog = $("#a-log");
  const stick = !prevLog || (prevLog.scrollHeight - prevLog.scrollTop - prevLog.clientHeight < 40);
  const prevTop = prevLog ? prevLog.scrollTop : 0;

  box.innerHTML =
    '<div class="active-card">' +
      '<div class="active-top">' +
        '<div class="ring" id="a-ring" style="--p:' + pct + '"><span id="a-pct">' + pct.toFixed(0) + "%</span></div>" +
        '<div style="min-width:0">' +
          '<div class="active-name">' + esc(t.name) + '<span class="pill ' + st.cls + '">' + st.text + "</span></div>" +
          '<div class="active-path" title="' + esc(t.folder) + '">' + esc(t.folder) + "</div>" +
          '<div class="active-msg"><span id="a-msg">' + esc(t.message) + "</span>" +
            (t.current_file ? '<span class="file">' + esc(t.current_file) + "</span>" : "") +
          "</div>" +
          '<div class="bar running"><i id="a-bar" style="width:' + pct + '%"></i></div>' +
          '<div class="active-msg" style="margin-top:8px;color:var(--muted);font-size:11.5px">' + esc(meta) + "</div>" +
        "</div>" +
        '<div class="active-side">' +
          '<button class="btn sm danger" data-act="cancel" data-id="' + t.id + '">取消任务</button>' +
          '<div class="active-eta" id="a-eta">' + esc(etaText(t)) + "</div>" +
        "</div>" +
      "</div>" +
      '<div class="console" id="a-log"><div class="console-head"><span class="dots"><i></i><i></i><i></i></span>' +
      "<span>任务实时日志</span></div><pre id=\"a-log-pre\" style=\"margin:0;font:inherit;white-space:pre-wrap\"></pre></div>" +
    "</div>";

  $("#active-desc").textContent = "同一时刻只运行一个任务，其余在队列里等待";
  const logBox = $("#a-log"), logPre = $("#a-log-pre");
  if (logPre) logPre.textContent = (t.logs || []).join("\n");
  if (logBox) logBox.scrollTop = stick ? logBox.scrollHeight : prevTop;
}

function StateOf(st) {
  return { text: STATE_TEXT[st] || st, cls: stateClass(st) };
}

function renderTaskList() {
  const wrap = $("#task-list");
  const tasks = S.queue.tasks || [];
  $("#task-count").textContent = tasks.length;

  if (!tasks.length) {
    wrap.innerHTML =
      '<div class="list-empty"><div class="ill">' +
      '<svg width="120" height="76" viewBox="0 0 120 76" fill="none">' +
        '<rect x="16" y="12" width="88" height="52" rx="9" fill="#f4f5f8" stroke="#e4e7ee"/>' +
        '<rect x="26" y="22" width="42" height="6" rx="3" fill="#dfe3ea"/>' +
        '<rect x="26" y="34" width="62" height="6" rx="3" fill="#eceef3"/>' +
        '<rect x="26" y="46" width="30" height="6" rx="3" fill="#eceef3"/>' +
        '<circle cx="90" cy="50" r="13" fill="#eef0fe" stroke="#d9dcfb"/>' +
        '<path d="M86 44.6v10.8l8.6-5.4z" fill="#4f46e5"/>' +
      "</svg></div><div>还没有任何任务</div>" +
      '<div style="margin-top:6px">点右上角「新建任务」，或到「新建任务」页开始</div></div>';
    return;
  }

  wrap.innerHTML = tasks.map(t => {
    const st = StateOf(t.state);
    const pct = Math.round((t.progress || 0) * 100) || 0;
    const running = ["running", "scanning", "copying", "encoding"].includes(t.state);
    const opts = t.options || {};
    const sub = [
      (t.total_files || 0) + " 个文件",
      MODE_TEXT[opts.mode] || opts.mode || "",
      t.final_size ? fmtSize(t.final_size) : "",
      t.finished_at ? clock(t.finished_at) : clock(t.created_at) + " 创建",
    ].filter(Boolean).join(" · ");

    let acts = "";
    if (t.state === "queued") {
      acts += '<button class="icon-btn" title="上移" data-act="up" data-id="' + t.id + '">' + icon("up") + "</button>";
      acts += '<button class="icon-btn" title="下移" data-act="down" data-id="' + t.id + '">' + icon("down") + "</button>";
      acts += '<button class="icon-btn danger" title="取消" data-act="cancel" data-id="' + t.id + '">' + icon("close") + "</button>";
    } else if (running) {
      acts += '<button class="icon-btn danger" title="取消" data-act="cancel" data-id="' + t.id + '">' + icon("close") + "</button>";
    } else if (t.state === "done") {
      acts += '<button class="icon-btn" title="打开所在文件夹" data-act="reveal" data-id="' + t.id + '">' + icon("open") + "</button>";
      acts += '<button class="icon-btn" title="再跑一次" data-act="retry" data-id="' + t.id + '">' + icon("retry") + "</button>";
      acts += '<button class="icon-btn" title="移除" data-act="remove" data-id="' + t.id + '">' + icon("close") + "</button>";
    } else {
      acts += '<button class="icon-btn" title="重试" data-act="retry" data-id="' + t.id + '">' + icon("retry") + "</button>";
      acts += '<button class="icon-btn" title="移除" data-act="remove" data-id="' + t.id + '">' + icon("close") + "</button>";
    }

    const barCls = t.state === "done" ? " done" : (t.state === "error" ? " error" : "");
    const showBar = running || t.state === "queued";

    return '<div class="task-item" data-id="' + t.id + '">' +
      '<div class="task-status"><span class="dot ' + stateClass(t.state) + '"></span></div>' +
      '<div style="min-width:0">' +
        '<div class="task-name"><span class="nm" title="' + esc(t.name) + '">' + esc(t.name) + "</span>" +
          '<span class="op-tag ' + (opts.op === "audio" ? "audio" : "") + '">' +
            ({ merge: "合并", audio: "音频", split: "切割", video: "单视频", mux: "音视频" }[opts.op] || "合并") + "</span>" +
          '<span class="pill ' + st.cls + '">' + st.text + "</span>" +
          (t.state === "running" || t.state === "scanning" || t.state === "copying" || t.state === "encoding"
            ? '<span class="pill brand num">' + pct + "%</span>" : "") +
        "</div>" +
        '<div class="task-meta"><span class="p" title="' + esc(t.folder) + '">' + esc(t.folder) + "</span>" +
          '<span class="sep">·</span><span>' + esc(sub) + "</span></div>" +
        (t.state === "error" && t.error
          ? '<div class="task-meta" style="color:var(--bad)">' + esc(String(t.error).slice(0, 140)) + "</div>" : "") +
        (showBar ? '<div class="task-bar' + barCls + '"><i style="width:' + (t.state === "queued" ? 0 : pct) + '%"></i></div>' : "") +
      "</div>" +
      '<div class="task-nums"><b>' + (t.done_files || 0) + " / " + (t.total_files || 0) + "</b>" +
        (t.elapsed ? fmtDur(t.elapsed) : "—") + "</div>" +
      '<div class="task-acts">' + acts + "</div>" +
    "</div>";
  }).join("");
}

/* ---------------- 队列操作 ---------------- */
async function queueAction(act, id) {
  try {
    if (act === "cancel") {
      if (!confirm("确定取消这个任务？已转码的临时分段会被清理。")) return;
      await api("/api/queue/cancel", { id });
      toast("已发送取消指令", "ok");
    } else if (act === "retry") {
      await api("/api/queue/retry", { id });
      toast("已重新加入队列", "ok");
    } else if (act === "remove") {
      await api("/api/queue/remove", { id });
      toast("任务已移除");
    } else if (act === "up" || act === "down") {
      await api("/api/queue/move", { id, dir: act });
    } else if (act === "reveal") {
      const t = (S.queue.tasks || []).find(x => x.id === id);
      if (t && t.output) await api("/api/reveal", { path: t.output });
      else toast("还没有输出文件", "bad");
    }
    await refreshQueue();
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function refreshQueue() {
  try {
    S.queue = await api("/api/queue");
    if (S.queue.active_id) {
      try { S.active = await api("/api/task?id=" + enc(S.queue.active_id)); } catch (e) { S.active = null; }
    } else {
      S.active = null;
    }
    if (S.view === "queue") renderQueue();
    else renderTopActions();
    $("#nav-badge").textContent = S.queue.counts.queued || 0;
    if (S.detailId) loadDetail(S.detailId);
  } catch (e) { /* 服务重启时静默重试 */ }
}

/* ================================================================
 * 新建任务
 * ================================================================ */
function readTrim() {
  S.trim.head = Math.max(0, parseFloat($("#trim-head").value) || 0);
  S.trim.tail = Math.max(0, parseFloat($("#trim-tail").value) || 0);
}

function effDur(d) {
  d = Number(d) || 0;
  if (d <= 0) return 0;
  const head = S.trim.head < d - 0.2 ? S.trim.head : 0;
  const remain = d - head;
  const tail = Math.min(S.trim.tail, Math.max(0, remain - 0.2));
  return Math.max(0, remain - tail);
}

function trimmedTotal() {
  if (!S.scan) return 0;
  return (S.scan.files || []).filter(f => f.ok).reduce((a, f) => a + effDur(f.duration), 0);
}

function rawTotal() {
  if (!S.scan) return 0;
  return (S.scan.files || []).filter(f => f.ok).reduce((a, f) => a + (Number(f.duration) || 0), 0);
}

function updateTrimHint() {
  readTrim();
  const hint = $("#trim-hint"), badge = $("#s3-badge");
  if (!S.scan) { badge.textContent = ""; return; }
  const raw = rawTotal(), eff = trimmedTotal();
  if (S.trim.head <= 0 && S.trim.tail <= 0) {
    hint.textContent = "填 0 表示不裁剪。片头动画、片尾点赞投币可以一次性去掉。";
    badge.innerHTML = '<span class="pill queued">不裁剪</span>';
  } else {
    hint.innerHTML = "每集剪掉 <b>" + (S.trim.head + S.trim.tail).toFixed(1) + "s</b> → 总时长 <b>" +
      fmtDur(eff) + "</b>（原 " + fmtDur(raw) + "，共省 " + ((raw - eff) / 60).toFixed(1) +
      ' 分钟）<br><span class="muted">无损模式裁剪点对齐关键帧，单集误差约 ±0.3s</span>';
    badge.innerHTML = '<span class="pill warn">-' + ((raw - eff) / 60).toFixed(1) + " 分钟</span>";
  }
  renderScanTable();
  updateBar();
}

function renderScan() {
  const has = !!(S.scan && S.scan.files && S.scan.files.length);
  $("#scan-empty").classList.toggle("hidden", has);
  $("#scan-body").classList.toggle("hidden", !has);
  $("#s1-badge").innerHTML = has
    ? '<span class="pill done">' + S.scan.files.length + " 个文件</span>"
    : (S.busy ? '<span class="pill running">扫描中…</span>' : "");
  $("#s2-badge").innerHTML = has ? "" : '<span class="pill queued">待扫描</span>';
  if (!has) return;
  renderHealth();
  renderScanTable();
  updateCropPreview();
}

function renderHealth() {
  const h = S.scan.health_summary || { ok: 0, warn: 0, bad: 0 };
  $("#health-bar").innerHTML =
    '<span class="hchip"><i class="dot done"></i>正常 <b>' + h.ok + "</b></span>" +
    '<span class="hchip"><i class="dot" style="background:#d9a13a"></i>注意 <b>' + h.warn + "</b></span>" +
    '<span class="hchip"><i class="dot error"></i>异常 <b>' + h.bad + "</b></span>" +
    (S.scan.majority_ts ? '<span class="hchip">多数派时间基 <b>1/' + esc(S.scan.majority_ts) + "</b></span>" : "") +
    (S.scan.majority_sr ? '<span class="hchip">采样率 <b>' + esc(S.scan.majority_sr) + "Hz</b></span>" : "");

  const groups = S.scan.signature_groups || [];
  $("#group-bar").innerHTML = groups.map(g =>
    '<span class="gchip ' + (g.is_majority ? "maj" : "min") + '">' + g.count + " 个 · " + esc(g.label) +
    (g.is_majority ? " · 无损拷贝" : " · 转码") + "</span>").join("");

  const same = S.scan.signature_same;
  $("#scan-note").innerHTML = same
    ? "✅ 全部文件参数一致，将走<b>无损直拷</b>：最快、画质零损失。"
    : "⚠️ 检测到 " + groups.length + " 种参数组合。<b>自动模式</b>只会转码 " +
      S.scan.need_encode_count + " 个少数派，其余 " + S.scan.majority_count + " 个无损拷贝。";
}

function renderScanTable() {
  const tb = $("#file-body");
  if (!tb) return;
  const files = S.scan ? (S.scan.files || []) : [];
  const byPath = {};
  files.forEach(f => { byPath[f.path] = f; });
  const order = S.fileOrder.length ? S.fileOrder.filter(p => byPath[p]) : files.map(f => f.path);
  const trimming = S.trim.head > 0 || S.trim.tail > 0;

  tb.innerHTML = order.map((p, idx) => {
    const f = byPath[p];
    if (!f) return "";
    const health = f.health || (f.ok ? "ok" : "bad");
    const tip = (f.issues || []).length ? f.issues.join(" / ") : "预检正常";
    const dotColor = health === "bad" ? "error" : health === "warn" ? "" : "done";
    const dotStyle = health === "warn" ? ' style="background:#d9a13a"' : "";
    const pill = !f.ok ? '<span class="pill error">失败</span>'
      : f.is_majority ? '<span class="pill done">多数派</span>'
      : '<span class="pill warn">需转码</span>';
    const durCell = trimming && f.ok
      ? fmtDur(f.duration) + '<span class="dur-to">→ ' + fmtDur(effDur(f.duration)) + "</span>"
      : fmtDur(f.duration);

    return '<tr draggable="true" data-path="' + esc(f.path) + '">' +
      '<td><span class="drag-handle" style="color:#b9bfc9;cursor:grab">⋮⋮</span></td>' +
      "<td>" + (idx + 1) + "</td>" +
      '<td class="name" title="' + esc(f.name + " · " + tip) + '">' +
        '<i class="dot ' + dotColor + '"' + dotStyle + '></i> ' + esc(f.name) + "</td>" +
      '<td class="mono">' + (f.ok ? f.width + "×" + f.height : "—") + "</td>" +
      '<td class="mono">' + (f.ok ? String(f.vcodec || "").toUpperCase() : "—") + "</td>" +
      "<td>" + (f.size ? fmtSize(f.size) : "—") + "</td>" +
      "<td>" + durCell + "</td>" +
      "<td>" + pill + "</td>" +
    "</tr>";
  }).join("");

  bindDrag();
}

function bindDrag() {
  const tb = $("#file-body");
  let from = null;
  tb.querySelectorAll("tr").forEach((tr, idx) => {
    tr.addEventListener("dragstart", e => {
      from = idx; tr.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    tr.addEventListener("dragend", () => {
      tr.classList.remove("dragging");
      tb.querySelectorAll("tr").forEach(t => t.classList.remove("drag-over"));
    });
    tr.addEventListener("dragover", e => {
      e.preventDefault();
      tb.querySelectorAll("tr").forEach(t => t.classList.remove("drag-over"));
      tr.classList.add("drag-over");
    });
    tr.addEventListener("drop", e => {
      e.preventDefault();
      if (from === null || from === idx) return;
      const moved = S.fileOrder.splice(from, 1)[0];
      S.fileOrder.splice(idx, 0, moved);
      from = null;
      renderScanTable();
    });
  });
}

function updateBar() {
  const folder = $("#f-folder").value.trim();
  const batch = $("#f-batch").checked;
  const sum = $("#act-sum");
  const btn = $("#btn-enqueue");

  if (batch) {
    btn.textContent = "批量加入队列";
    btn.disabled = !folder || !S.cfg || !S.cfg.ready;
    sum.innerHTML = folder
      ? "批量模式：<b>" + esc(folder) + "</b> 下每个含视频的子文件夹各建一个任务"
      : "填写父文件夹后，将为其下每个子文件夹建一个任务";
    return;
  }
  btn.textContent = "加入队列";
  if (!S.scan || !S.scan.files || !S.scan.files.length) {
    btn.disabled = true;
    sum.textContent = "扫描后即可加入队列";
    return;
  }
  const n = S.scan.files.length;
  const trimming = S.trim.head > 0 || S.trim.tail > 0;
  btn.disabled = !S.cfg || !S.cfg.ready;
  sum.innerHTML = '<b>' + n + "</b> 个文件 · 合计 <b>" + fmtDur(trimmedTotal()) + "</b>" +
    (trimming ? ' <span style="color:var(--brand)">（已裁剪）</span>' : "") +
    " · " + fmtSize(S.scan.total_size) +
    ' · <span class="mono">' + esc(outputPreview()) + "</span>";
}

function outputPreview() {
  const out = $("#f-output").value.trim();
  const folder = $("#f-folder").value.trim();
  if (out) return out;
  if (!folder) return "默认输出到源文件夹";
  const name = folder.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "merged";
  return folder + "\\" + name + "_merged.mp4";
}

function collectParams() {
  readTrim();
  const mode = (document.querySelector('input[name="mode"]:checked') || {}).value || "auto";
  const params = {
    op: "merge",
    crop: readCrop(),
    folder: $("#f-folder").value.trim(),
    recursive: $("#f-recursive").checked,
    output: $("#f-output").value.trim(),
    mode,
    encoder: $("#f-encoder").value,
    parallel: parseInt($("#f-parallel").value, 10) || 2,
    crf: parseInt($("#f-crf").value, 10) || 20,
    preset: $("#f-preset").value,
    size: $("#f-size").value.trim(),
    fps: $("#f-fps").value.trim(),
    trim_head: S.trim.head,
    trim_tail: S.trim.tail,
    chapters: $("#f-chapters").checked,
    on_error: $("#f-on-error").value,
    file_order: S.fileOrder.slice(),
  };
  // 留空 = 跟随设置页的同名输出策略，别把默认值硬塞给后端
  const ow = $("#f-overwrite").value;
  if (ow) params.overwrite = ow === "overwrite";
  return params;
}

async function doScan() {
  readTrim();
  const folder = $("#f-folder").value.trim();
  if (!folder) return toast("请先选择源文件夹", "bad");
  if (!$("#f-batch").checked && (!S.cfg || !S.cfg.ready)) return toast("ffmpeg 未就绪，请先到设置页配置", "bad");

  const btn = $("#btn-scan");
  btn.disabled = true; btn.textContent = "扫描中…";
  S.busy = true; renderScan();
  try {
    const r = await api("/api/scan", {
      folder,
      recursive: $("#f-recursive").checked,
      out_dir: $("#f-output").value.trim(),
    });
    S.scan = r;
    S.fileOrder = (r.files || []).map(f => f.path);
    $("#step1").classList.add("done");
    toast("扫描完成：" + r.files.length + " 个文件", "ok");
  } catch (e) {
    toast("扫描失败：" + e.message, "bad");
  } finally {
    S.busy = false;
    btn.disabled = false; btn.textContent = "扫描";
    renderScan();
    updateTrimHint();
  }
}

async function enqueue() {
  const folder = $("#f-folder").value.trim();
  if (!folder) return toast("请先选择源文件夹", "bad");
  const batch = $("#f-batch").checked;
  const params = collectParams();

  if (!batch && (!S.scan || !S.scan.files.length)) return toast("请先扫描文件夹", "bad");
  if (!batch && S.trim.head >= 0 && trimmedTotal() <= 0) return toast("裁剪参数把内容剪空了", "bad");

  const btn = $("#btn-enqueue");
  btn.disabled = true; btn.textContent = "加入中…";
  try {
    let msg = "";
    if (batch) {
      const r = await api("/api/queue/batch", Object.assign({ parent: folder }, params));
      msg = "已加入 " + r.added.length + " 个任务" + (r.skipped.length ? "，跳过 " + r.skipped.length + " 个空文件夹" : "");
    } else {
      await api("/api/queue/add", Object.assign({ name: "" }, params));
      msg = "任务已加入队列";
    }
    toast(msg + "，可在队列页查看", "ok");
    await refreshQueue();
    location.hash = "#/queue";
  } catch (e) {
    toast("加入失败：" + e.message, "bad");
    btn.disabled = false;
  } finally {
    btn.textContent = batch ? "批量加入队列" : "加入队列";
  }
}

function resetForm() {
  S.scan = null; S.fileOrder = [];
  $("#f-folder").value = ""; $("#f-output").value = "";
  $("#f-recursive").checked = false; $("#f-batch").checked = false;
  $("#trim-head").value = 0; $("#trim-tail").value = 0;
  $("#step1").classList.remove("done");
  $("#s1-badge").innerHTML = ""; $("#s3-badge").innerHTML = "";
  renderScan(); updateTrimHint(); updateBar();
}

/* ================================================================
 * 详情抽屉
 * ================================================================ */
function openDetail(id) {
  S.detailId = id;
  $("#overlay").classList.add("show");
  $("#drawer").classList.add("show");
  $("#drawer").setAttribute("aria-hidden", "false");
  loadDetail(id);
}

function closeDetail() {
  S.detailId = "";
  $("#overlay").classList.remove("show");
  $("#drawer").classList.remove("show");
  $("#drawer").setAttribute("aria-hidden", "true");
}

async function loadDetail(id) {
  let t = null;
  try { t = await api("/api/task?id=" + enc(id)); }
  catch (e) { closeDetail(); return; }
  if (S.detailId !== id) return;

  const st = StateOf(t.state);
  $("#d-name").innerHTML = esc(t.name) + ' <span class="pill ' + st.cls + '">' + st.text + "</span>";
  $("#d-path").textContent = t.folder || "—";

  // 抽屉每秒刷新，保持阅读位置不跳
  const bodyEl = $("#d-body");
  const keepScroll = bodyEl.scrollTop;
  const oldLog = $("#d-log");
  const logStick = !oldLog || (oldLog.scrollHeight - oldLog.scrollTop - oldLog.clientHeight < 40);
  const logTop = oldLog ? oldLog.scrollTop : 0;

  const opts = t.options || {};
  const rep = t.report || {};
  const bits = [];
  bits.push('<div class="d-sec"><h4>概览</h4><div class="metrics">');
  bits.push(metric("状态", st.text, "", t.message));
  bits.push(metric("耗时", t.elapsed ? fmtDur(t.elapsed) : "—", ""));
  bits.push(metric("进度", Math.round((t.progress || 0) * 100) + "%", ""));
  bits.push(metric("输出时长", t.final_duration ? fmtDurPrecise(t.final_duration) : "—", "ok"));
  bits.push(metric("文件大小", fmtSize(t.final_size), ""));
  bits.push(metric("转码 / 拷贝", (t.encoded_count || 0) + " / " + (t.copied_count || 0), ""));
  bits.push("</div></div>");

  if (t.output) {
    bits.push('<div class="d-sec"><h4>输出文件</h4><div class="mono" style="background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:10px 12px;word-break:break-all;color:#3b4250">' + esc(t.output) + "</div>");
    if (t.output_note) {
      bits.push('<div class="rep-note warn" style="margin-top:8px">🛡 ' + esc(t.output_note) + "</div>");
    }
    bits.push("</div>");
  }

  bits.push('<div class="d-sec"><h4>任务参数</h4><div class="d-opts">');
  if (opts.op === "audio") {
    bits.push(chip("音频 " + String(opts.audio_format || "m4a").toUpperCase()));
    if (String(opts.audio_format || "m4a") === "mp3") bits.push(chip("音质 q" + (opts.audio_quality ?? 2)));
    if (opts.recursive) bits.push(chip("含子文件夹"));
  } else if (opts.op === "split") {
    bits.push(chip(opts.precise ? "精确切割（重编码）" : "流复制（关键帧对齐）"));
    bits.push(chip((t.total_files || 0) + " 段"));
  } else if (opts.op === "mux") {
    bits.push(chip(opts.mode === "mix" ? "与原声混合" : "替换原音轨"));
    if (opts.mode === "mix") bits.push(chip("新音频音量 " + (opts.volume ?? 100) + "%"));
    bits.push(chip(opts.fit === "video" ? "按视频时长" : "按较短者为准"));
  } else if (opts.op === "video") {
    if (opts.crop && opts.crop.enabled) {
      const cp = opts.crop;
      bits.push(chip("裁剪 上" + (cp.top || 0) + " 下" + (cp.bottom || 0) +
                     " 左" + (cp.left || 0) + " 右" + (cp.right || 0)));
      if (cp.scale_back) bits.push(chip("缩放回原尺寸"));
    }
    bits.push(chip("片头 -" + (opts.trim_head || 0) + "s"));
    bits.push(chip("片尾 -" + (opts.trim_tail || 0) + "s"));
    bits.push(chip("编码 " + (opts.encoder || "libx264")));
  } else {
    bits.push(chip("模式 " + (MODE_TEXT[opts.mode] || opts.mode)));
    bits.push(chip("编码 " + (opts.encoder || "—")));
    bits.push(chip("并行 " + (opts.parallel || 2) + " 路"));
    bits.push(chip("CRF " + (opts.crf || 20)));
    if (opts.size) bits.push(chip(opts.size));
    if (opts.fps) bits.push(chip(opts.fps + " fps"));
    if (opts.crop && opts.crop.enabled) {
      const cp = opts.crop;
      bits.push(chip("裁剪 上" + (cp.top || 0) + " 下" + (cp.bottom || 0) +
                     " 左" + (cp.left || 0) + " 右" + (cp.right || 0)));
      if (cp.scale_back) bits.push(chip("缩放回原尺寸"));
    }
    bits.push(chip("片头 -" + (opts.trim_head || 0) + "s"));
    bits.push(chip("片尾 -" + (opts.trim_tail || 0) + "s"));
    bits.push(chip(opts.chapters ? "写章节" : "不写章节"));
    bits.push(chip(opts.on_error === "skip" ? "失败集跳过" : "失败即中止"));
    if (rep.subtitles) bits.push(chip("字幕 " + rep.subtitles + " 条"));
    if (opts.overwrite) bits.push(chip("允许覆盖同名输出"));
    if (opts.recursive) bits.push(chip("含子文件夹"));
  }
  bits.push("</div></div>");

  if (rep && Array.isArray(rep.segments) && typeof rep.count === "number") {
    const failed = rep.failed || [];
    bits.push('<div class="d-sec"><h4>切割结果</h4>');
    bits.push('<div class="rep-note ' + (rep.count ? "ok" : "warn") + '">' +
      (rep.count ? "✅ 已导出 " + rep.count + " 段 · 合计 " + fmtDur(rep.duration || 0)
                 : "⚠️ 没有成功导出任何片段") + "</div>");
    bits.push('<div class="metrics">');
    bits.push(metric("片段", rep.count + " / " + rep.total, ""));
    bits.push(metric("总时长", fmtDur(rep.duration || 0), "ok"));
    bits.push(metric("方式", rep.precise ? "精确" : "流复制", ""));
    bits.push("</div>");
    if (rep.segments && rep.segments.length) {
      bits.push('<div style="margin-top:10px;font-size:11.5px;color:var(--muted);line-height:1.8">' +
        rep.segments.slice(0, 12).map(s =>
          "· " + esc(s.name) + " —— " + fmtDur(s.end - s.start)).join("<br>") +
        (rep.segments.length > 12 ? "<br>… 其余 " + (rep.segments.length - 12) + " 段" : "") +
        "</div>");
    }
    if (failed.length) {
      bits.push('<div style="margin-top:8px;color:var(--bad);font-size:11.5px">失败 ' +
        failed.length + " 段</div>");
    }
    bits.push("</div>");
  }

  if (rep && rep.duration && !rep.segments && !rep.format) {
    const deltaOk = Math.abs(rep.delta || 0) <= Math.max(2, (rep.expected_duration || 0) * 0.002);
    const syncOk = rep.sync_delta == null || rep.sync_delta <= 1;
    const frameOk = !rep.expected_frames || !rep.frames || Math.abs(rep.frames - rep.expected_frames) <= 2;
    bits.push('<div class="d-sec"><h4>合并后自检</h4>');
    bits.push('<div class="rep-note ' + (rep.level === "ok" ? "ok" : "warn") + '">' +
      (rep.level === "ok" ? "✅ 时长、音视频同步、帧数、时间基全部正常"
        : "⚠️ " + esc((rep.notes || []).join("；"))) + "</div>");
    bits.push('<div class="metrics">');
    bits.push(metric("源时长（裁剪后）", fmtDurPrecise(rep.expected_duration || 0), ""));
    bits.push(metric("输出时长", fmtDurPrecise(rep.duration), deltaOk ? "ok" : "warn",
      (rep.delta >= 0 ? "+" : "") + (rep.delta || 0).toFixed(2) + "s"));
    bits.push(metric("音视频同步差", syncOk ? (rep.sync_delta || 0).toFixed(2) + "s" : (rep.sync_delta || 0).toFixed(2) + "s", syncOk ? "ok" : "bad", syncOk ? "对齐正常" : "偏差偏大"));
    bits.push(metric("帧数", rep.frames ? String(rep.frames) : "—", frameOk ? "ok" : "warn",
      rep.expected_frames ? "源 " + rep.expected_frames : ""));
    bits.push(metric("时间基", rep.time_base || "—", "ok"));
    bits.push(metric("章节", (rep.chapters || 0) + " 个", rep.chapters ? "ok" : ""));
    bits.push("</div></div>");
  }

  if (rep && rep.format && typeof rep.count === "number") {
    const skipped = rep.skipped || [];
    bits.push('<div class="d-sec"><h4>提取结果</h4>');
    bits.push('<div class="rep-note ' + (rep.count ? "ok" : "warn") + '">' +
      (rep.count ? "✅ 已导出 " + rep.count + " / " + rep.total + " 个 " + esc(rep.format) + " 音频"
                 : "⚠️ 没有成功导出任何音频") + "</div>");
    bits.push('<div class="metrics">');
    bits.push(metric("成功", String(rep.count), rep.count ? "ok" : "bad"));
    bits.push(metric("跳过", String(skipped.length), skipped.length ? "warn" : ""));
    bits.push(metric("格式", esc(rep.format), ""));
    bits.push("</div>");
    if (skipped.length) {
      bits.push('<div style="margin-top:10px;font-size:11.5px;color:var(--muted);line-height:1.7">' +
        skipped.slice(0, 12).map(s => "· " + esc(s.name) + " —— " + esc(s.reason)).join("<br>") +
        (skipped.length > 12 ? "<br>… 其余 " + (skipped.length - 12) + " 个见日志" : "") + "</div>");
    }
    bits.push("</div>");
  }

  bits.push('<div class="d-sec"><h4>完整日志</h4><div class="d-log" id="d-log"></div></div>');

  const acts = [];
  if (["running", "scanning", "copying", "encoding"].includes(t.state)) {
    acts.push('<button class="btn danger" data-dact="cancel">取消任务</button>');
  }
  if (["done", "error", "canceled"].includes(t.state)) {
    if (t.output) acts.push('<button class="btn primary" data-dact="reveal">打开所在文件夹</button>');
    if (t.resumable && t.resume_count) {
      acts.push('<button class="btn primary" data-dact="resume">断点续跑（复用 ' +
        t.resume_count + ' 段）</button>');
    }
    acts.push('<button class="btn" data-dact="retry">重新开始</button>');
    acts.push('<button class="btn" data-dact="remove">移除任务</button>');
  }
  bits.push('<div class="d-acts">' + acts.join("") + "</div>");

  $("#d-body").innerHTML = bits.join("");
  $("#d-body").scrollTop = keepScroll;
  const logEl = $("#d-log");
  if (logEl) {
    logEl.textContent = (t.logs || []).join("\n");
    logEl.scrollTop = logStick ? logEl.scrollHeight : logTop;
  }
  $("#drawer").dataset.id = t.id;
}

function chip(text) { return '<span class="gchip">' + esc(text) + "</span>"; }
function metric(k, v, cls, sub) {
  return '<div class="metric"><div class="k">' + esc(k) + '</div><div class="v ' + (cls || "") + '">' +
    esc(v) + "</div>" + (sub ? '<div class="s">' + esc(sub) + "</div>" : "") + "</div>";
}

/* ================================================================
 * 事件绑定
 * ================================================================ */
function bindEvents() {
  window.addEventListener("hashchange", setRoute);

  // 队列操作（事件委托，列表重绘也不会丢）
  document.addEventListener("click", e => {
    const actBtn = e.target.closest("[data-act]");
    if (actBtn) {
      e.stopPropagation();
      const act = actBtn.dataset.act;
      const id = actBtn.dataset.id;
      if (act === "pause") return;
      queueAction(act, id);
      return;
    }
    const row = e.target.closest(".task-item");
    if (row && !e.target.closest("button") && !e.target.closest("a")) {
      openDetail(row.dataset.id);
    }
  });

  $("#btn-pause").onclick = async () => {
    try {
      await api("/api/queue/pause", { paused: !S.queue.paused });
      S.queue.paused = !S.queue.paused;
      toast(S.queue.paused ? "队列已暂停，当前任务会继续跑完" : "队列已恢复", "ok");
      await refreshQueue();
    } catch (e) { toast(e.message, "bad"); }
  };

  $("#btn-clear").onclick = async () => {
    try {
      const r = await api("/api/queue/clear", { scope: "done" });
      toast("已清理 " + r.removed + " 个任务");
      await refreshQueue();
    } catch (e) { toast(e.message, "bad"); }
  };

  // 详情抽屉
  $("#overlay").onclick = closeDetail;
  $("#d-close").onclick = closeDetail;
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeDetail();
  });
  $("#d-body").addEventListener("click", async e => {
    const btn = e.target.closest("[data-dact]");
    if (!btn) return;
    const id = $("#drawer").dataset.id;
    const act = btn.dataset.dact;
    if (act === "resume") {
      try {
        await api("/api/queue/retry", { id, resume: true });
        toast("已从断点继续（复用已完成分段）", "ok");
        await refreshQueue();
        loadDetail(id);
      } catch (e) { toast(e.message, "bad"); }
      return;
    }
    if (act === "remove" || act === "cancel") {
      if (!confirm(act === "remove" ? "确定移除这个任务？" : "确定取消这个任务？")) return;
    }
    await queueAction(act, id);
    if (act === "remove") closeDetail();
    loadDetail(id);
  });

  // 画面裁剪
  $("#f-crop").addEventListener("change", updateCropPreview);
  ["#crop-top", "#crop-bottom", "#crop-left", "#crop-right"].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("input", updateCropPreview);
  });
  $$("[data-crop-preset]").forEach(b => {
    b.onclick = () => {
      const ui = b.dataset.scope === "video" ? CROP_VIDEO : CROP_MERGE;
      applyCropPresetFor(ui, b.dataset.cropPreset);
    };
  });
  const dc = $("#btn-detect-crop");
  if (dc) dc.onclick = detectCrop;

  // 单个视频
  $("#v-crop").addEventListener("change", () => updateCropPreviewFor(CROP_VIDEO));
  ["#v-top", "#v-bottom", "#v-left", "#v-right"].forEach(sel => {
    $(sel).addEventListener("input", () => updateCropPreviewFor(CROP_VIDEO));
  });
  $("#v-scale-back").addEventListener("change", () => updateCropPreviewFor(CROP_VIDEO));
  $("#v-detect-crop").onclick = () => {
    if (!S.single.path) return toast("请先选择视频文件", "bad");
    return detectCropFor(CROP_VIDEO, { path: S.single.path });
  };
  $("#v-pick").onclick = async () => {
    const btn = $("#v-pick");
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await api("/api/pick-video", { title: "选择要处理的视频" });
      if (r.path) {
        $("#v-target").value = r.path;
        S.single = { path: r.path, width: 0, height: 0 };
        await probeSingle();
      }
    } catch (e) { toast("打开失败：" + e.message, "bad"); }
    finally { btn.disabled = false; btn.textContent = "浏览"; }
  };
  $("#v-probe").onclick = probeSingle;
  $("#v-target").addEventListener("change", probeSingle);
  $("#v-start").onclick = startSingle;
  $("#v-trim-head").addEventListener("input", updateSingleSummary);
  $("#v-trim-tail").addEventListener("input", updateSingleSummary);
  const te = $("#btn-test-enc");
  if (te) te.onclick = async () => {
    te.disabled = true; te.textContent = "检测中…";
    try {
      const r = await api("/api/encoders/test", {});
      renderEncoders(r.encoders || {});
      toast("编码器检测完成", "ok");
    } catch (e) { toast("检测失败：" + e.message, "bad"); }
    finally { te.disabled = false; te.textContent = "重新检测"; }
  };

  // 输出目录：点击选择而不是手填
  document.addEventListener("click", async e => {
    const b = e.target.closest("[data-pick-dir]");
    if (!b) return;
    e.preventDefault();
    const input = $(b.dataset.pickDir);
    if (!input) return;
    const old = b.textContent;
    b.disabled = true; b.textContent = "…";
    try {
      const r = await api("/api/pick-folder", { title: "选择输出目录" });
      if (r.path) {
        input.value = r.path;
        input.dispatchEvent(new Event("input"));
        input.dispatchEvent(new Event("change"));
      }
    } catch (err) { toast("打开失败：" + err.message, "bad"); }
    finally { b.disabled = false; b.textContent = old; }
  });

  // 音频：单个 / 批量
  $("#a-single").addEventListener("change", () => {
    const single = $("#a-single").checked;
    $("#a-recursive").disabled = single;
    $("#a-folder").placeholder = single
      ? "选择一个视频文件"
      : "选择课程文件夹，例如 E://videos//课程01";
    S.audio.scan = null;
    renderAudio();
  });
  $("#a-pick").onclick = async () => {
    const btn = $("#a-pick");
    const single = $("#a-single").checked;
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await api(single ? "/api/pick-video" : "/api/pick-folder",
                          { title: single ? "选择视频文件" : "选择视频文件夹" });
      if (r.path) {
        $("#a-folder").value = r.path;
        S.audio.scan = null;
        renderAudio();
      }
    } catch (e) { toast("打开失败：" + e.message, "bad"); }
    finally { btn.disabled = false; btn.textContent = "浏览"; }
  };

  // 音视频合并
  $("#m-fit").addEventListener("change", renderMux);
  $("#m-pick-video").onclick = () => pickMux("video");
  $("#m-pick-audio").onclick = () => pickMux("audio");
  $("#m-start").onclick = startMux;

  // 视频切割
  $("#p-plan").onclick = makeSplitPlan;
  $("#p-pick").onclick = async () => {
    const btn = $("#p-pick");
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await api("/api/pick-video", { title: "选择要切割的视频" });
      if (r.path) {
        $("#p-target").value = r.path;
        S.split.plan = null;
        renderSplit();
      }
    } catch (e) { toast("打开失败：" + e.message, "bad"); }
    finally { btn.disabled = false; btn.textContent = "浏览"; }
  };
  $("#p-mode").addEventListener("change", () => {
    $("#p-minutes").disabled = $("#p-mode").value !== "time";
    S.split.plan = null;
    renderSplit();
  });
  $("#plan-body").addEventListener("change", e => {
    const ck = e.target.closest(".plan-ck");
    if (!ck || !S.split.plan) return;
    const i = parseInt(ck.dataset.idx, 10);
    if (S.split.plan.segments[i]) S.split.plan.segments[i].keep = ck.checked;
    renderSplit();
  });
  $("#plan-all").onclick = () => {
    if (!S.split.plan) return;
    S.split.plan.segments.forEach(s => { s.keep = true; });
    renderSplit();
  };
  $("#plan-none").onclick = () => {
    if (!S.split.plan) return;
    S.split.plan.segments.forEach(s => { s.keep = false; });
    renderSplit();
  };
  $("#plan-enqueue").onclick = enqueueSplit;

  // 提取音频
  $("#a-scan").onclick = scanAudio;
  $("#a-enqueue").onclick = enqueueAudio;
  $("#a-format").addEventListener("change", () => {
    $("#a-quality").disabled = $("#a-format").value !== "mp3";
  });

  // 新建任务
  $("#btn-scan").onclick = doScan;
  $("#btn-enqueue").onclick = enqueue;
  $("#btn-reset").onclick = resetForm;
  $("#trim-head").addEventListener("input", updateTrimHint);
  $("#trim-tail").addEventListener("input", updateTrimHint);
  $("#f-folder").addEventListener("input", updateBar);
  $("#f-output").addEventListener("input", () => { updateBar(); renderOutHint(); });
  $("#f-batch").addEventListener("change", () => {
    const batch = $("#f-batch").checked;
    $("#step2").style.opacity = batch ? ".55" : "1";
    $("#scan-empty").innerHTML = batch
      ? "批量模式不需要逐文件预检 —— 父目录下每个含视频的子文件夹会各自建任务，参数当前统一。"
      : "还没有扫描结果 —— 选好文件夹后点上方「扫描」";
    updateBar();
  });

  $("#btn-pick-folder").onclick = async () => {
    const btn = $("#btn-pick-folder");
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await api("/api/pick-folder", { title: "选择源视频文件夹" });
      if (r.path) { $("#f-folder").value = r.path; renderOutHint(); updateBar(); }
    } catch (e) { toast("打开失败：" + e.message + "（可直接粘贴路径）", "bad"); }
    finally { btn.disabled = false; btn.textContent = "浏览"; }
  };

  $("#btn-pick-out").onclick = async () => {
    const btn = $("#btn-pick-out");
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await api("/api/pick-folder", { title: "选择输出目录" });
      if (r.path) { $("#f-output").value = r.path; updateBar(); renderOutHint(); }
    } catch (e) { toast("打开失败：" + e.message, "bad"); }
    finally { btn.disabled = false; btn.textContent = "浏览"; }
  };

  // 设置
  $("#btn-save-cfg").onclick = async () => {
    const btn = $("#btn-save-cfg");
    btn.disabled = true; btn.textContent = "验证中…";
    try {
      const cfg = await api("/api/config", {
        ffmpeg: $("#cfg-ffmpeg").value.trim(),
        ffprobe: $("#cfg-ffprobe").value.trim(),
      });
      S.cfg = cfg;
      renderConfig(cfg); renderEngine(cfg); updateBar();
      toast("已保存并验证通过", "ok");
    } catch (e) {
      toast("保存失败：" + e.message, "bad");
      if (S.view === "settings") loadConfig();
    } finally {
      btn.disabled = false; btn.textContent = "验证并保存";
    }
  };

  $("#btn-browse-ffmpeg").onclick = async () => {
    try {
      const r = await api("/api/pick-exe", { which: "ffmpeg" });
      if (r.path) {
        $("#cfg-ffmpeg").value = r.path;
        if (!$("#cfg-ffprobe").value.trim()) {
          $("#cfg-ffprobe").placeholder = "留空自动推导：" + r.path.replace(/[\\/][^\\/]+$/, "") + "\\ffprobe.exe";
        }
      }
    } catch (e) { toast("打开失败：" + e.message, "bad"); }
  };

  $("#btn-browse-ffprobe").onclick = async () => {
    try {
      const r = await api("/api/pick-exe", { which: "ffprobe" });
      if (r.path) $("#cfg-ffprobe").value = r.path;
    } catch (e) { toast("打开失败：" + e.message, "bad"); }
  };

  $("#btn-save-defaults").onclick = () => {
    const d = {
      mode: $("#d-mode").value,
      encoder: $("#d-encoder").value,
      head: parseFloat($("#d-head").value) || 0,
      tail: parseFloat($("#d-tail").value) || 0,
    };
    try { localStorage.setItem("v5.defaults", JSON.stringify(d)); } catch (e) {}
    toast("默认参数已保存", "ok");
  };

  // 运行与提醒：改一个存一个，不用再点保存
  ["#s-on-exists", "#s-notify", "#s-tray"].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("change", saveRuntimeSettings);
  });
  const nt = $("#btn-notify-test");
  if (nt) nt.onclick = async () => {
    const old = nt.textContent;
    nt.disabled = true; nt.textContent = "发送中…";
    try {
      const r = await api("/api/notify/test", {});
      renderNotifyState(r);
      toast(r.sent ? "已发送，留意系统通知" : "没发出去：检查通知开关与托盘状态",
            r.sent ? "ok" : "bad");
    } catch (e) {
      toast("测试失败：" + e.message, "bad");
    } finally {
      nt.disabled = false; nt.textContent = old;
    }
  };
}

function renderOutHint() {
  const folder = $("#f-folder").value.trim();
  const out = $("#f-output").value.trim();
  const hint = $("#out-hint");
  if (out) { hint.textContent = "输出：" + out; return; }
  if (!folder) { hint.textContent = "留空 → 输出到源文件夹内，文件名 <文件夹名>_merged.mp4"; return; }
  const name = folder.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "merged";
  hint.textContent = "将输出到 " + folder + "\\" + name + "_merged.mp4";
}

function applyDefaults() {
  let d = null;
  try { d = JSON.parse(localStorage.getItem("v5.defaults") || "null"); } catch (e) {}
  if (!d) return;
  if (d.mode) {
    const el = document.querySelector('input[name="mode"][value="' + d.mode + '"]');
    if (el) el.checked = true;
  }
  if (d.encoder) $("#f-encoder").value = d.encoder;
  $("#trim-head").value = d.head || 0;
  $("#trim-tail").value = d.tail || 0;
  $("#d-mode").value = d.mode || "auto";
  $("#d-encoder").value = d.encoder || "libx264";
  $("#d-head").value = d.head || 0;
  $("#d-tail").value = d.tail || 0;
  readTrim();
}


/* ================================================================
 * v6：画面裁剪（合并任务 / 单个视频共用）
 * ================================================================ */
const CROP_MERGE = {
  name: "merge", root: "#crop-adv", toggle: "#f-crop", body: "#crop-body",
  top: "#crop-top", bottom: "#crop-bottom", left: "#crop-left",
  right: "#crop-right", scale: "#crop-scale-back", img: "#crop-img",
  keep: "#crop-keep", size: "#crop-size",
  strips: { top: "#strip-top", bottom: "#strip-bottom",
            left: "#strip-left", right: "#strip-right" },
};
const CROP_VIDEO = {
  name: "video", root: null, toggle: "#v-crop", body: "#v-crop-body",
  top: "#v-top", bottom: "#v-bottom", left: "#v-left", right: "#v-right",
  scale: "#v-scale-back", img: "#v-crop-img", keep: "#v-keep",
  size: "#v-crop-size",
  strips: { top: "#v-strip-top", bottom: "#v-strip-bottom",
            left: "#v-strip-left", right: "#v-strip-right" },
};

function cropDims(ui) {
  if (ui === CROP_MERGE) {
    const files = (S.scan && S.scan.files) || [];
    const f = files.find(x => x.ok) || files[0];
    return f ? { W: Number(f.width) || 1280, H: Number(f.height) || 720, path: f.path } : null;
  }
  return S.single && S.single.width
    ? { W: S.single.width, H: S.single.height, path: S.single.path } : null;
}

function readCropFor(ui) {
  const num = sel => Math.max(0, parseInt($(sel).value, 10) || 0);
  return {
    enabled: $(ui.toggle).checked,
    left: num(ui.left), top: num(ui.top),
    right: num(ui.right), bottom: num(ui.bottom),
    scale_back: $(ui.scale).checked,
  };
}
function readCrop() { return readCropFor(CROP_MERGE); }

function updateCropPreviewFor(ui) {
  const on = $(ui.toggle).checked;
  $(ui.body).classList.toggle("hidden", !on);
  if (on && ui.root && $(ui.root)) $(ui.root).open = true;
  if (!on) { afterCropChange(ui); return; }

  const dims = cropDims(ui);
  if (!dims) { $(ui.size).textContent = "等待素材"; afterCropChange(ui); return; }

  const c = readCropFor(ui);
  const W = Number(dims.W) || 1280, H = Number(dims.H) || 720;
  const l = Math.min(c.left, W - 64), r = Math.min(c.right, W - 64);
  const tp = Math.min(c.top, H - 64), bt = Math.min(c.bottom, H - 64);
  const lp = l / W * 100, rp = r / W * 100, tpp = tp / H * 100, bp = bt / H * 100;

  const set = (sel, prop, val) => { const el = $(sel); if (el) el.style[prop] = val; };
  set(ui.strips.top, "height", tpp + "%");
  set(ui.strips.bottom, "height", bp + "%");
  set(ui.strips.left, "width", lp + "%");
  set(ui.strips.right, "width", rp + "%");
  set(ui.strips.left, "top", tpp + "%");
  set(ui.strips.left, "bottom", bp + "%");
  set(ui.strips.right, "top", tpp + "%");
  set(ui.strips.right, "bottom", bp + "%");

  const canvas = document.querySelector(ui.body + " .crop-canvas");
  if (canvas) canvas.style.aspectRatio = W + " / " + H;

  const keep = $(ui.keep);
  if (keep) {
    keep.style.left = lp + "%"; keep.style.right = rp + "%";
    keep.style.top = tpp + "%"; keep.style.bottom = bp + "%";
  }

  const img = $(ui.img);
  if (img && dims.path) {
    const url = "/api/thumb?path=" + encodeURIComponent(dims.path) + "&t=2";
    if (img.dataset.src !== url) {
      img.dataset.src = url;
      img.style.display = "none";
      img.onload = () => { img.style.display = "block"; };
      img.src = url;
    }
  }

  $(ui.size).textContent = (W - l - r) + "×" + (H - tp - bt) + "  ←  " + W + "×" + H;
  afterCropChange(ui);
}

function afterCropChange(ui) {
  if (ui === CROP_MERGE) updateBar();
  else updateSingleSummary();
}

function updateCropPreview() { updateCropPreviewFor(CROP_MERGE); }

function applyCropPresetFor(ui, name) {
  const dims = cropDims(ui);
  const H = dims ? (Number(dims.H) || 720) : 720;
  const set = (t, b, l, r) => {
    $(ui.top).value = t; $(ui.bottom).value = b;
    $(ui.left).value = l; $(ui.right).value = r;
  };
  if (name === "top60") set(Math.min(60, H - 64), 0, 0, 0);
  else if (name === "top80") set(Math.min(80, H - 64), 0, 0, 0);
  else if (name === "tb40") set(40, 40, 0, 0);
  else set(0, 0, 0, 0);
  updateCropPreviewFor(ui);
}
function applyCropPreset(name) { applyCropPresetFor(CROP_MERGE, name); }

async function detectCropFor(ui, payload) {
  const btn = ui === CROP_MERGE ? $("#btn-detect-crop") : $("#v-detect-crop");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "检测中…";
  try {
    const r = await api("/api/detect-crop", payload);
    if (!r.ok) { toast("没有检测到黑边，可用预设去水印/台标", "bad"); return; }
    $(ui.left).value = r.left; $(ui.top).value = r.top;
    $(ui.right).value = r.right; $(ui.bottom).value = r.bottom;
    $(ui.toggle).checked = true;
    updateCropPreviewFor(ui);
    toast("已套用：左 " + r.left + " / 上 " + r.top + " / 右 " + r.right +
          " / 下 " + r.bottom + " px", "ok");
  } catch (e) {
    toast("检测失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}
async function detectCrop() {
  const folder = $("#f-folder").value.trim();
  if (!folder) return toast("请先选择源文件夹", "bad");
  return detectCropFor(CROP_MERGE, { folder });
}

/* ================================================================
 * v6.2：单个视频（裁剪 / 剪头尾）
 * ================================================================ */
function renderSingle() {
  updateCropPreviewFor(CROP_VIDEO);
  updateSingleSummary();
}

function updateSingleSummary() {
  const s = S.single || {};
  const pill = $("#v-out-size");
  const btn = $("#v-start");
  if (!s.width) {
    pill.textContent = "—";
    if (btn) btn.disabled = true;
    return;
  }
  const c = readCropFor(CROP_VIDEO);
  const W = Number(s.width) || 0, H = Number(s.height) || 0;
  const cw = Math.max(16, W - c.left - c.right);
  const ch = Math.max(16, H - c.top - c.bottom);
  const head = parseFloat($("#v-trim-head").value) || 0;
  const tail = parseFloat($("#v-trim-tail").value) || 0;
  const dur = Math.max(0, (Number(s.duration) || 0) - head - tail);
  pill.textContent = (c.enabled ? cw + "×" + ch : W + "×" + H) + " · " + fmtDur(dur);
  if (btn) btn.disabled = false;
}

async function probeSingle() {
  const path = $("#v-target").value.trim();
  if (!path) return toast("请先选择视频文件", "bad");
  if (!(S.cfg && S.cfg.ready)) return toast("ffmpeg 未就绪，请先到设置页配置", "bad");
  const btn = $("#v-probe");
  btn.disabled = true; btn.textContent = "读取中…";
  try {
    const r = await api("/api/probe?path=" + encodeURIComponent(path));
    S.single = { path: r.path, width: r.width, height: r.height,
                 duration: r.duration, fps: r.fps, acodec: r.acodec };
    $("#v-meta").innerHTML =
      "分辨率 <b>" + r.width + "×" + r.height + "</b> · 时长 <b>" + fmtDur(r.duration) +
      "</b> · 视频 <b>" + String(r.vcodec || "—").toUpperCase() + "</b> · 音频 <b>" +
      (r.acodec ? String(r.acodec).toUpperCase() : "无") + "</b>" +
      (r.chapters ? " · 章节 " + r.chapters + " 个" : "");
    renderSingle();
  renderMux();
    toast("已读取视频信息", "ok");
  } catch (e) {
    S.single = { path: "", width: 0, height: 0 };
    renderSingle();
    toast("读取失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "读取信息";
  }
}

async function startSingle() {
  const s = S.single || {};
  if (!s.path) return toast("请先选择视频文件", "bad");
  const crop = readCropFor(CROP_VIDEO);
  const head = Math.max(0, parseFloat($("#v-trim-head").value) || 0);
  const tail = Math.max(0, parseFloat($("#v-trim-tail").value) || 0);
  if (!crop.enabled && head <= 0 && tail <= 0) {
    return toast("请启用画面裁剪，或设置片头片尾", "bad");
  }
  const btn = $("#v-start");
  btn.disabled = true; btn.textContent = "加入中…";
  try {
    await api("/api/queue/add", {
      op: "video", target: s.path,
      output: $("#v-output").value.trim(),
      crop: crop, trim_head: head, trim_tail: tail,
      encoder: $("#v-encoder").value,
      crf: parseInt($("#v-crf").value, 10) || 20,
      preset: $("#v-preset").value,
    });
    toast("处理任务已加入队列", "ok");
    await refreshQueue();
    location.hash = "#/queue";
  } catch (e) {
    toast("加入失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "加入队列";
  }
}

/* ================================================================
 * v6.2：编码器可用性
 * ================================================================ */
function renderEncoders(list) {
  S.encoders = list || {};
  const box = $("#enc-status");
  if (box) {
    box.innerHTML = Object.entries(S.encoders).map(([key, v]) =>
      '<div class="enc-row"><i class="enc-dot ' + (v.ok ? "ok" : "bad") + '"></i>' +
      "<b>" + esc(v.label) + '</b><span class="mono">' + esc(key) + "</span>" +
      '<span class="state">' + (v.ok
        ? '<span class="pill done">可用</span>'
        : '<span class="pill queued">不可用</span>' +
          '<span class="enc-reason">' + esc(v.reason || "") + "</span>") +
      "</span></div>").join("");
  }
  renderEncHint();
  ["#f-encoder", "#v-encoder", "#d-encoder"].forEach(applyEncoderAvailability);
}

/* 四个全挂基本不是显卡问题，而是 ffmpeg 不对——单独提醒一句 */
function renderEncHint() {
  const hint = $("#enc-hint");
  if (!hint) return;
  const all = Object.values(S.encoders || {});
  const bad = all.filter(v => !v.ok);
  if (!all.length || !bad.length) {
    hint.className = "hidden";
    hint.innerHTML = "";
    return;
  }
  if (bad.length === all.length) {
    hint.className = "banner bad";
    hint.style.marginTop = "10px";
    hint.innerHTML = "<b>四个编码器全部不可用</b>，这通常不是显卡驱动问题，" +
      "而是 ffmpeg 路径指向了别的程序、或版本太旧（需要 5.0+ 的 full build）。<br>" +
      "当前原因：" + esc(bad[0].reason || "未知");
  } else {
    hint.className = "banner warn";
    hint.style.marginTop = "10px";
    hint.innerHTML = "有 " + bad.length + " 个编码器不可用，其余可以正常使用（原因见下表）。";
  }
}

function applyEncoderAvailability(sel) {
  const el = $(sel);
  if (!el || !S.encoders) return;
  Array.from(el.options).forEach(op => {
    const st = S.encoders[op.value];
    const base = op.textContent.replace("（不可用）", "");
    if (st && !st.ok) {
      op.disabled = true;
      op.textContent = base + "（不可用）";
    } else {
      op.disabled = false;
      op.textContent = base;
    }
  });
  const cur = el.options[el.selectedIndex];
  if (cur && cur.disabled) {
    el.value = "libx264";
    toast("所选编码器在本机不可用，已切换为 libx264", "bad");
  }
}


/* ================================================================
 * v6.3：音视频合并
 * ================================================================ */
function renderMux() {
  const m = S.mux || {};
  const box = $("#m-meta");
  if (!box) return;
  box.innerHTML = (m.vdur || m.adur)
    ? "视频 <b>" + (m.vdur ? fmtDur(m.vdur) : "—") + "</b> · 音频 <b>" +
      (m.adur ? fmtDur(m.adur) : "—") + "</b> · 按当前设置输出约 <b>" +
      fmtDur($("#m-fit").value === "video" ? (m.vdur || 0)
            : Math.min(m.vdur || 1e9, m.adur || 1e9)) + "</b>"
    : "选好后会显示两边的时长";
}

async function pickMux(kind) {
  const isVideo = kind === "video";
  const btn = isVideo ? $("#m-pick-video") : $("#m-pick-audio");
  const input = isVideo ? $("#m-video") : $("#m-audio");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    const r = await api(isVideo ? "/api/pick-video" : "/api/pick-audio",
                        { title: isVideo ? "选择视频文件" : "选择音频文件" });
    if (!r.path) return;
    input.value = r.path;
    try {
      const p = await api("/api/probe?path=" + encodeURIComponent(r.path));
      if (isVideo) { S.mux.video = r.path; S.mux.vdur = p.duration || 0; }
      else { S.mux.audio = r.path; S.mux.adur = p.duration || 0; }
    } catch (e) { /* 探测失败不影响选择 */ }
    renderMux();
  } catch (e) {
    toast("打开失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function startMux() {
  const video = $("#m-video").value.trim();
  const audio = $("#m-audio").value.trim();
  if (!video || !audio) return toast("请同时选择视频和音频文件", "bad");
  const mode = (document.querySelector('input[name="m-mode"]:checked') || {}).value || "replace";
  const btn = $("#m-start");
  btn.disabled = true; btn.textContent = "加入中…";
  try {
    await api("/api/queue/add", {
      op: "mux", video, audio,
      output: $("#m-output").value.trim(),
      mode,
      volume: parseInt($("#m-volume").value, 10) || 100,
      fit: $("#m-fit").value,
    });
    toast("音视频合并任务已加入队列", "ok");
    await refreshQueue();
    location.hash = "#/queue";
  } catch (e) {
    toast("加入失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "加入队列";
  }
}

/* ---------------- 启动 ---------------- *//* ================================================================
 * v6：提取音频
 * ================================================================ */
function renderAudio() {
  const sc = S.audio.scan;
  const files = sc ? (sc.files || []) : [];
  $("#a-count").textContent = files.length;
  const btn = $("#a-enqueue");

  if (!sc) {
    $("#a-list").className = "empty-inline";
    $("#a-list").textContent = "还没有扫描结果 —— 选好文件夹后点「扫描」";
    $("#a-desc").textContent = "扫描后可确认文件数量与总时长";
    btn.disabled = true;
    return;
  }

  const withAudio = files.filter(f => f.ok && f.acodec);
  const dur = files.reduce((a, f) => a + (Number(f.duration) || 0), 0);
  $("#a-desc").textContent = (sc.single ? "单个视频 · " : files.length + " 个视频 · ") +
    "有音轨 " + withAudio.length + " 个 · 总时长 " + fmtDur(dur);
  btn.disabled = !files.length || !(S.cfg && S.cfg.ready) || !withAudio.length;
  $("#a-list").className = "";
  $("#a-list").innerHTML =
    '<div class="table-wrap"><table><thead><tr>' +
      '<th>文件名</th><th style="width:120px">时长</th>' +
      '<th style="width:90px">大小</th><th style="width:110px">音轨</th>' +
    '</tr></thead><tbody>' +
    files.map(f =>
      '<tr><td class="name" title="' + esc(f.name) + '">' + esc(f.name) + "</td>" +
      "<td>" + fmtDur(f.duration) + "</td>" +
      "<td>" + fmtSize(f.size) + "</td>" +
      "<td>" + (f.ok && f.acodec
        ? '<span class="pill done">' + esc(String(f.acodec).toUpperCase()) + "</span>"
        : '<span class="pill warn">无音轨</span>') + "</td></tr>").join("") +
    "</tbody></table></div>";
}

async function scanAudio() {
  const path = $("#a-folder").value.trim();
  if (!path) return toast("请先选择视频文件或文件夹", "bad");
  if (!(S.cfg && S.cfg.ready)) return toast("ffmpeg 未就绪，请先到设置页配置", "bad");
  const btn = $("#a-scan");
  const single = $("#a-single").checked;
  btn.disabled = true; btn.textContent = "扫描中…";
  try {
    if (single) {
      const r = await api("/api/probe?path=" + encodeURIComponent(path));
      const name = path.split(/[\\/]/).pop();
      S.audio.scan = { single: true, files: [{
        name, path: r.path, duration: r.duration, size: 0,
        ok: true, acodec: r.acodec,
      }] };
    } else {
      S.audio.scan = await api("/api/scan", {
        folder: path, recursive: $("#a-recursive").checked,
      });
      S.audio.scan.single = false;
    }
    renderAudio();
    toast("扫描完成：" + S.audio.scan.files.length + " 个文件", "ok");
  } catch (e) {
    toast("扫描失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "扫描";
  }
}

async function enqueueAudio() {
  const path = $("#a-folder").value.trim();
  if (!path) return toast("请先选择视频文件或文件夹", "bad");
  const single = $("#a-single").checked;
  const btn = $("#a-enqueue");
  btn.disabled = true; btn.textContent = "加入中…";
  try {
    const payload = {
      op: "audio",
      output: $("#a-output").value.trim(),
      audio_format: $("#a-format").value,
      audio_quality: parseInt($("#a-quality").value, 10) || 2,
      recursive: single ? false : $("#a-recursive").checked,
    };
    if (single) payload.target = path; else payload.folder = path;
    await api("/api/queue/add", payload);
    toast("音频任务已加入队列", "ok");
    await refreshQueue();
    location.hash = "#/queue";
  } catch (e) {
    toast("加入失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "加入队列";
  }
}


/* ================================================================
 * v6：视频切割
 * ================================================================ */
function splitOutputName(path, index, title) {
  const stem = String(path).replace(/\.[^.\\/]+$/, "").split(/[\\/]/).pop();
  const clean = String(title || "").replace(/[<>:"/\\|?*\x00-\x1f]/g, "").trim();
  return stem + "_P" + String(index + 1).padStart(2, "0") +
    (clean ? "_" + clean.slice(0, 40) : "") + ".mp4";
}

function renderSplit() {
  const plan = S.split.plan;
  const panel = $("#plan-panel");
  if (!plan || !plan.segments || !plan.segments.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");

  const picked = plan.segments.filter(s => s.keep !== false);
  const total = picked.reduce((a, s) => a + s.duration, 0);
  $("#plan-count").textContent = plan.segments.length;
  $("#plan-desc").textContent =
    (plan.path.split(/[\\/]/).pop()) + " · 共 " + plan.segments.length +
    " 段 · 已选 " + picked.length + " 段 · 合计 " + fmtDur(total) +
    (plan.mode === "chapters" ? " · 按章节" : " · 按固定时长");

  $("#plan-body").innerHTML = plan.segments.map((s, i) =>
    '<tr data-idx="' + i + '">' +
      '<td><input type="checkbox" class="plan-ck" data-idx="' + i + '"' +
        (s.keep !== false ? " checked" : "") + "></td>" +
      "<td>" + (i + 1) + "</td>" +
      '<td class="name" title="' + esc(s.title) + '">' + esc(s.title) + "</td>" +
      "<td>" + fmtDurPrecise(s.start) + "</td>" +
      "<td>" + fmtDur(s.duration) + "</td>" +
      '<td class="mono" style="max-width:300px;overflow:hidden;text-overflow:ellipsis">' +
        esc(splitOutputName(plan.path, i, s.title)) + "</td>" +
    "</tr>").join("");

  $("#plan-enqueue").disabled = !picked.length;
}

async function makeSplitPlan() {
  const target = $("#p-target").value.trim();
  if (!target) return toast("请先选择视频文件", "bad");
  if (!(S.cfg && S.cfg.ready)) return toast("ffmpeg 未就绪，请先到设置页配置", "bad");
  const btn = $("#p-plan");
  btn.disabled = true; btn.textContent = "分析中…";
  try {
    const r = await api("/api/split/plan", {
      path: target,
      mode: $("#p-mode").value,
      minutes: parseFloat($("#p-minutes").value) || 30,
    });
    if (!r.ok) {
      S.split.plan = null;
      renderSplit();
      toast(r.error || "无法生成切割方案", "bad");
      return;
    }
    r.segments.forEach(s => { s.keep = true; });
    S.split.plan = r;
    renderSplit();
    toast("已生成 " + r.segments.length + " 段", "ok");
  } catch (e) {
    toast("生成失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "生成方案";
  }
}

async function enqueueSplit() {
  const plan = S.split.plan;
  if (!plan) return;
  const segs = plan.segments.filter(s => s.keep !== false)
    .map(s => ({ start: s.start, end: s.end, title: s.title }));
  if (!segs.length) return toast("至少勾选一段", "bad");
  const btn = $("#plan-enqueue");
  btn.disabled = true; btn.textContent = "加入中…";
  try {
    await api("/api/queue/add", {
      op: "split",
      target: plan.path,
      output: $("#p-output").value.trim(),
      precise: $("#p-precise").checked,
      segments: segs,
    });
    toast("切割任务已加入队列", "ok");
    await refreshQueue();
    location.hash = "#/queue";
  } catch (e) {
    toast("加入失败：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "加入队列";
  }
}

/* ---------------- 启动 ---------------- */
(async function init() {
  bindEvents();
  applyDefaults();
  await loadConfig();
  try {
    S.queue = await api("/api/queue");
    if (S.queue.active_id) S.active = await api("/api/task?id=" + enc(S.queue.active_id));
  } catch (e) {}

  // 支持 ?folder=<路径>&scan=1 直接带入并扫描（便于从文件管理器/脚本跳转）
  const qp = new URLSearchParams(location.search);
  const pre = qp.get("folder");
  if (pre) {
    $("#f-folder").value = pre;
    $("#a-folder").value = pre;
    renderOutHint();
  }
  // ?crop=60 → 直接带上「去顶部 60px」的裁剪参数
  const preVideo = qp.get("video");
  if (preVideo) {
    $("#p-target").value = preVideo;
    $("#v-target").value = preVideo;
  }
  const cropPre = qp.get("crop");
  if (cropPre) {
    $("#f-crop").checked = true;
    $("#crop-top").value = parseInt(cropPre, 10) || 0;
    $("#v-crop").checked = true;
    $("#v-top").value = parseInt(cropPre, 10) || 0;
  }

  // 打开时若有任务在跑，直接落在队列页
  if (!location.hash) {
    location.hash = preVideo ? "#/split" : (pre ? "#/new" : "#/queue");
  }
  setRoute();
  renderScan();
  updateTrimHint();
  updateBar();
  $("#a-quality").disabled = $("#a-format").value !== "mp3";
  renderAudio();
  $("#p-minutes").disabled = $("#p-mode").value !== "time";
  renderSplit();
  renderSingle();
  if (preVideo && qp.get("plan") === "1") setTimeout(makeSplitPlan, 120);
  if (preVideo && location.hash === "#/single") setTimeout(probeSingle, 120);
  if (pre && qp.get("scan") === "1") {
    setTimeout(() => (location.hash === "#/audio" ? scanAudio() : doScan()), 80);
  }
  if (qp.get("task")) setTimeout(() => openDetail(qp.get("task")), 200);

  S.poll = setInterval(refreshQueue, 1000);
})();
