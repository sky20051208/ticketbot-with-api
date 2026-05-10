const $  = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

const PLATFORMS  = ["TIXCRAFT", "KKTIX", "TICKETPLUS"];
const AREA_MODES = ["關鍵字優先", "由上而下", "由下而上", "隨機"];
const RUN_MODES  = ["API模式", "瀏覽器模式"];

const sockets   = new Map();  // id -> WebSocket
const cardLogs  = new Map();  // id -> string[]
const TIMER_PREFIX  = "[TIMER] 倒數";
const STATUS_PREFIX = "[STATUS] ";
const ANSI_RE = /\[[\d;?]*[a-zA-Z]/g;

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function lineClass(line) {
  if (line.startsWith("[ERR") || line.startsWith("[ERROR")) return "log-err";
  if (line.startsWith(TIMER_PREFIX)) return "log-timer";
  if (line.startsWith("[PAUSE]")) return "log-pause";
  if (line.startsWith("[EXIT]")) return "log-exit";
  if (line.startsWith("[INFO]") || line.startsWith("[WATCH]")) return "log-info";
  return "";
}
function renderLogBuf(buf) {
  return buf.map(l => {
    const cls = lineClass(l);
    const safe = escapeHtml(l);
    return cls ? `<span class="${cls}">${safe}</span>` : safe;
  }).join("\n");
}
function setCardStatus(card, status) {
  card.querySelector(".status").textContent = status;
  card.classList.remove("running", "stopped");
  if (status.startsWith("運行中")) card.classList.add("running");
  else if (status.startsWith("已停止") || status.startsWith("已結束") || status.startsWith("啟動失敗")) {
    card.classList.add("stopped");
  }
  // 更新 terminal header 右側資訊
  const meta = card.querySelector(".term-meta");
  if (meta) {
    const m = status.match(/PID\s+(\d+)/);
    meta.textContent = m ? `PID ${m[1]}` : "";
  }
}

function screenInfo() {
  return { screen_w: window.screen.width, screen_h: window.screen.height - 80 };
}

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${method} ${path}: ${r.status}`);
  return r.json();
}

async function refresh() {
  const items = await api("GET", "/api/instances");
  renderGrid(items);
}

function renderGrid(items) {
  const grid = $("#grid");

  // 收掉不再存在的 instance 的 WS
  const presentIds = new Set(items.map(i => i.id));
  for (const id of [...sockets.keys()]) {
    if (!presentIds.has(id)) {
      try { sockets.get(id).close(); } catch (_) {}
      sockets.delete(id);
      cardLogs.delete(id);
    }
  }

  // 重建卡片（log 內容靠 cardLogs map 還原）
  grid.innerHTML = "";
  for (const item of items) {
    grid.appendChild(renderCard(item));
  }
  for (const item of items) openWS(item.id);
}

function renderCard(item) {
  const tpl  = $("#card-template").content.cloneNode(true);
  const card = tpl.querySelector(".card");
  card.dataset.id = item.id;
  card.querySelector(".card-name").textContent = `ACC-${item.id}`;
  setCardStatus(card, item.status);

  const cfg = item.config;
  bindSelect(card, ".f-runmode",  RUN_MODES,  cfg.run_mode);
  bindSelect(card, ".f-platform", PLATFORMS,  cfg.PLATFORM);
  bindText  (card, ".f-slug",     cfg.ACTIVITY_SLUG);
  bindText  (card, ".f-time",     cfg.TARGET_START_TIME);
  bindText  (card, ".f-qty",      cfg.TICKET_AMOUNT);
  bindSelect(card, ".f-areamode", AREA_MODES, cfg.AREA_AUTO_SELECT_MODE);
  bindText  (card, ".f-area",     cfg.AREA_KEYWORD);
  bindText  (card, ".f-exclude",  cfg.EXCLUDE_AREA_KEYWORD);
  bindText  (card, ".f-date",     cfg.DATE_KEYWORD);
  bindText  (card, ".f-presale",  cfg.PRESALE_CODE);
  bindText  (card, ".f-watchurl", cfg.TIME_WATCH_URL);
  bindCheck (card, ".f-timer",    cfg.ENABLE_TIME_WATCHER);
  bindCheck (card, ".f-proxy",    cfg.ENABLE_PROXY_POOL);
  bindText  (card, ".f-cookie",   cfg.COOKIE);

  // 還原 log
  const buf = cardLogs.get(item.id);
  if (buf && buf.length) {
    card.querySelector(".log").innerHTML = renderLogBuf(buf);
  }

  // PAUSE 按鈕初始字
  if (item.paused) card.querySelector(".btn-pause").textContent = "繼續";

  card.querySelector(".btn-start").addEventListener("click", () => startOne(item.id));
  card.querySelector(".btn-pause").addEventListener("click", (e) => pauseOne(item.id, e.currentTarget));
  card.querySelector(".btn-stop") .addEventListener("click", () => stopOne(item.id));

  return card;
}

const pendingSaves = new Map();
function scheduleSave(card) {
  const id = parseInt(card.dataset.id, 10);
  if (pendingSaves.has(id)) clearTimeout(pendingSaves.get(id));
  pendingSaves.set(id, setTimeout(() => {
    pendingSaves.delete(id);
    api("PUT", `/api/instances/${id}/config`, readCardConfig(card)).catch(console.error);
  }, 400));
}

function readCardConfig(card) {
  return {
    run_mode:                card.querySelector(".f-runmode").value,
    PLATFORM:                card.querySelector(".f-platform").value,
    ACTIVITY_SLUG:           card.querySelector(".f-slug").value,
    TARGET_START_TIME:       card.querySelector(".f-time").value,
    TICKET_AMOUNT:           card.querySelector(".f-qty").value,
    AREA_AUTO_SELECT_MODE:   card.querySelector(".f-areamode").value,
    AREA_KEYWORD:            card.querySelector(".f-area").value,
    EXCLUDE_AREA_KEYWORD:    card.querySelector(".f-exclude").value,
    DATE_KEYWORD:            card.querySelector(".f-date").value,
    PRESALE_CODE:            card.querySelector(".f-presale").value,
    TIME_WATCH_URL:          card.querySelector(".f-watchurl").value,
    ENABLE_TIME_WATCHER:     card.querySelector(".f-timer").checked,
    ENABLE_PROXY_POOL:       card.querySelector(".f-proxy").checked,
    COOKIE:                  card.querySelector(".f-cookie").value,
  };
}

function bindText(card, sel, value) {
  const el = card.querySelector(sel);
  el.value = value ?? "";
  el.addEventListener("input", () => scheduleSave(card));
}
function bindSelect(card, sel, opts, value) {
  const el = card.querySelector(sel);
  el.innerHTML = "";
  for (const o of opts) {
    const opt = document.createElement("option");
    opt.value = o; opt.textContent = o;
    el.appendChild(opt);
  }
  el.value = value;
  el.addEventListener("change", () => scheduleSave(card));
}
function bindCheck(card, sel, value) {
  const el = card.querySelector(sel);
  el.checked = !!value;
  el.addEventListener("change", () => scheduleSave(card));
}

function appendLog(id, line) {
  // 去除 ANSI escape sequences（Python 偶爾會吐進度條/顏色碼）
  line = line.replace(ANSI_RE, "");
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if (line.startsWith(STATUS_PREFIX)) {
    if (card) setCardStatus(card, line.slice(STATUS_PREFIX.length));
    return;
  }
  let buf = cardLogs.get(id);
  if (!buf) { buf = []; cardLogs.set(id, buf); }
  if (line.startsWith(TIMER_PREFIX) && buf.length && buf[buf.length-1].startsWith(TIMER_PREFIX)) {
    buf[buf.length-1] = line;
  } else {
    buf.push(line);
  }
  if (buf.length > 1000) buf.splice(0, buf.length - 1000);
  if (!card) return;
  const logEl = card.querySelector(".log");
  const stick = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 30;
  logEl.innerHTML = renderLogBuf(buf);
  if (stick) logEl.scrollTop = logEl.scrollHeight;
}

function openWS(id) {
  const existing = sockets.get(id);
  if (existing && existing.readyState <= 1) return;
  cardLogs.set(id, cardLogs.get(id) || []);
  const ws = new WebSocket(`ws://${location.host}/ws/${id}`);
  ws.onmessage = (ev) => appendLog(id, ev.data);
  ws.onclose = () => {
    sockets.delete(id);
    if (document.querySelector(`.card[data-id="${id}"]`)) {
      setTimeout(() => openWS(id), 1500);
    }
  };
  sockets.set(id, ws);
}

async function startOne(id) {
  cardLogs.set(id, []);
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if (card) card.querySelector(".log").innerHTML = "";
  await api("POST", `/api/instances/${id}/start`, screenInfo());
}
async function stopOne(id) {
  await api("POST", `/api/instances/${id}/stop`);
}
async function pauseOne(id, btn) {
  const r = await api("POST", `/api/instances/${id}/pause`);
  btn.textContent = r.paused ? "繼續" : "PAUSE";
}

$("#btn-init").addEventListener("click", async () => {
  const n = parseInt($("#instance-count").value, 10);
  if (!Number.isFinite(n) || n < 1) { alert("請輸入正整數"); return; }
  if (!confirm(`要重設成 ${n} 個 instance 嗎？\n所有正在執行的 bot 會被停掉。`)) return;
  cardLogs.clear();
  await api("POST", "/api/init", { count: n });
  await refresh();
});
$("#btn-start-all").addEventListener("click", async () => {
  for (const id of cardLogs.keys()) {
    cardLogs.set(id, []);
    const card = document.querySelector(`.card[data-id="${id}"]`);
    if (card) card.querySelector(".log").innerHTML = "";
  }
  await api("POST", "/api/start_all", screenInfo());
});
$("#btn-stop-all").addEventListener("click", async () => {
  await api("POST", "/api/stop_all");
});

refresh();
