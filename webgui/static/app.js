const $  = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

const PLATFORMS  = ["TIXCRAFT", "KKTIX", "TICKETPLUS"];
const AREA_MODES = ["關鍵字優先", "由上而下", "由下而上", "隨機"];
const CLEAR_MODES = ["寬鬆", "嚴格"];
const MANUAL_COOKIE = "(手貼COOKIE)";
let CUSTOMERS = [];  // [{name, user_id, concert}]，由 /api/customers 填（proxy 到 Cloudflare Worker + D1）
let CHROME_PROFILES_BY_PLATFORM = {};  // {平台: [profile名...]}，由 /api/chrome_profiles 填
// 這台機器可用的出口 IP，由 /api/local_ips 填（讀網卡，所以 OCI 上增減會自動反映）
const DEFAULT_BIND_OPTION = "(預設出口)";
let LOCAL_IPS = [DEFAULT_BIND_OPTION];

function _bindIpValue(card) {
  const v = card.querySelector(".f-bindip").value;
  return v === DEFAULT_BIND_OPTION ? "" : v;   // 空字串 = 不綁，走主 IP
}
function profilesFor(platform) {
  const list = CHROME_PROFILES_BY_PLATFORM[platform];
  return (list && list.length) ? list : [MANUAL_COOKIE];
}

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
  try {
    const entries = await Promise.all(
      PLATFORMS.map(p =>
        api("GET", `/api/chrome_profiles?platform=${encodeURIComponent(p)}`)
          .then(list => [p, list])));
    CHROME_PROFILES_BY_PLATFORM = Object.fromEntries(entries);
  } catch (_) {
    CHROME_PROFILES_BY_PLATFORM = {};  // server 還沒重啟也讓 grid 照常 render
  }
  try {
    LOCAL_IPS = await api("GET", "/api/local_ips");
  } catch (_) {
    LOCAL_IPS = [DEFAULT_BIND_OPTION];  // 舊版 server 沒這個端點也不要炸掉整頁
  }
  try {
    CUSTOMERS = await api("GET", "/api/customers");
  } catch (_) {
    CUSTOMERS = [];
  }
  renderGrid(items);
}

// f-lineuser 下拉：(不推播) + Worker 客人清單；已刪除的客人保留原值顯示避免默默丟設定
function fillCustomerSelect(sel, currentUid) {
  sel.innerHTML = "";
  const add = (value, label) => {
    const o = document.createElement("option");
    o.value = value; o.textContent = label;
    sel.appendChild(o);
  };
  add("", "(不推播)");
  for (const c of CUSTOMERS) add(c.user_id, c.concert ? `${c.name} － ${c.concert}` : c.name);
  if (currentUid && !CUSTOMERS.some(c => c.user_id === currentUid)) {
    add(currentUid, `${currentUid.slice(0, 10)}…(已不在清單)`);
  }
  sel.value = currentUid || "";
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
  bindSelect(card, ".f-platform", PLATFORMS,  cfg.PLATFORM);
  bindText  (card, ".f-slug",     cfg.ACTIVITY_SLUG);
  bindText  (card, ".f-time",     cfg.TARGET_START_TIME);
  bindText  (card, ".f-qty",      cfg.TICKET_AMOUNT);
  bindText  (card, ".f-grabdelay", cfg.GRAB_DELAY_AFTER_OPEN);
  bindSelect(card, ".f-areamode", AREA_MODES, cfg.AREA_AUTO_SELECT_MODE);
  bindSelect(card, ".f-clearmode", CLEAR_MODES, cfg.CLEAR_MODE);
  bindText  (card, ".f-area",     cfg.AREA_KEYWORD);
  bindText  (card, ".f-exclude",  cfg.EXCLUDE_AREA_KEYWORD);
  bindText  (card, ".f-date",     cfg.DATE_KEYWORD);
  bindText  (card, ".f-presale",  cfg.PRESALE_CODE);
  bindText  (card, ".f-livenationstart", cfg.LIVENATION_START_URL);
  bindText  (card, ".f-watchurl", cfg.TIME_WATCH_URL);
  bindCheck (card, ".f-timer",    cfg.ENABLE_TIME_WATCHER);
  bindCheck (card, ".f-proxy",    cfg.ENABLE_PROXY_POOL);
  bindCheck (card, ".f-requirefull", cfg.REQUIRE_FULL_AMOUNT);
  // 出口 IP：值存空字串代表「走主 IP」，但下拉不能有空白選項，所以用一個顯示用的
  // 標籤代表它（readCardConfig 再轉回空字串）。機器上沒掛次要 IP 時就只有這一個選項。
  bindSelect(card, ".f-bindip", LOCAL_IPS,
             cfg.LOCAL_BIND_IP && LOCAL_IPS.includes(cfg.LOCAL_BIND_IP)
               ? cfg.LOCAL_BIND_IP : DEFAULT_BIND_OPTION);
  const _custSel = card.querySelector(".f-lineuser");
  fillCustomerSelect(_custSel, cfg.LINE_USER_ID);
  _custSel.addEventListener("change", () => scheduleSave(card));
  bindText  (card, ".f-fee",      cfg.TICKET_FEE);
  bindText  (card, ".f-cookie",   cfg.COOKIE);

  // 每平台各記一個 chrome profile：下拉只列「當前平台」的 profile，切 PLATFORM 時自動重列+套用
  card._profileMap = Object.assign({}, cfg.chrome_profile_map || {});
  const _platSel = card.querySelector(".f-platform");
  const _profSel = card.querySelector(".f-profile");
  const fillProfiles = (platform, wanted) => {
    const opts = profilesFor(platform);
    _profSel.innerHTML = "";
    for (const o of opts) {
      const e = document.createElement("option");
      e.value = o; e.textContent = o; _profSel.appendChild(e);
    }
    _profSel.value = opts.includes(wanted) ? wanted : MANUAL_COOKIE;
  };
  fillProfiles(_platSel.value, card._profileMap[_platSel.value] || cfg.chrome_profile || MANUAL_COOKIE);
  card._profileMap[_platSel.value] = _profSel.value;
  let _prevPlat = _platSel.value;
  _platSel.addEventListener("change", () => {
    card._profileMap[_prevPlat] = _profSel.value;                                   // 存舊平台
    fillProfiles(_platSel.value, card._profileMap[_platSel.value] || MANUAL_COOKIE); // 重列+套用新平台
    card._profileMap[_platSel.value] = _profSel.value;
    _prevPlat = _platSel.value;
    applyLocks();
  });
  _profSel.addEventListener("change", () => {
    card._profileMap[_platSel.value] = _profSel.value;
    scheduleSave(card);
  });

  // 欄位連動鎖定：
  //   選了 chrome profile（非「手貼COOKIE」）→ 鎖 COOKIE 輸入
  //   AREA MODE 非「關鍵字優先」→ 鎖 AREA KEYWORD 輸入
  const applyLocks = () => {
    const usingProfile = card.querySelector(".f-profile").value !== MANUAL_COOKIE;
    card.querySelector(".f-cookie").disabled = usingProfile;
    const byKeyword = card.querySelector(".f-areamode").value === "關鍵字優先";
    card.querySelector(".f-area").disabled = !byKeyword;
  };
  card.querySelector(".f-profile").addEventListener("change", applyLocks);
  card.querySelector(".f-areamode").addEventListener("change", applyLocks);
  applyLocks();

  // 還原 log
  const buf = cardLogs.get(item.id);
  if (buf && buf.length) {
    card.querySelector(".log").innerHTML = renderLogBuf(buf);
  }

  // PAUSE 按鈕初始字
  if (item.paused) card.querySelector(".btn-pause").textContent = "繼續";

  card.querySelector(".btn-save").addEventListener("click", (e) => saveOne(item.id, card, e.currentTarget));
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
  // 把當前平台的 profile 寫回 map，並以它當作要送出的 chrome_profile（當前生效的）
  const _platform = card.querySelector(".f-platform").value;
  const _prof     = card.querySelector(".f-profile").value;
  const _map      = Object.assign({}, card._profileMap || {});
  _map[_platform] = _prof;
  card._profileMap = _map;
  return {
    chrome_profile:          _prof,
    chrome_profile_map:      _map,
    PLATFORM:                _platform,
    ACTIVITY_SLUG:           card.querySelector(".f-slug").value,
    TARGET_START_TIME:       card.querySelector(".f-time").value,
    TICKET_AMOUNT:           card.querySelector(".f-qty").value,
    AREA_AUTO_SELECT_MODE:   card.querySelector(".f-areamode").value,
    CLEAR_MODE:              card.querySelector(".f-clearmode").value,
    AREA_KEYWORD:            card.querySelector(".f-area").value,
    EXCLUDE_AREA_KEYWORD:    card.querySelector(".f-exclude").value,
    DATE_KEYWORD:            card.querySelector(".f-date").value,
    PRESALE_CODE:            card.querySelector(".f-presale").value,
    LIVENATION_START_URL:    card.querySelector(".f-livenationstart").value,
    TIME_WATCH_URL:          card.querySelector(".f-watchurl").value,
    ENABLE_TIME_WATCHER:     card.querySelector(".f-timer").checked,
    ENABLE_PROXY_POOL:       card.querySelector(".f-proxy").checked,
    REQUIRE_FULL_AMOUNT:     card.querySelector(".f-requirefull").checked,
    GRAB_DELAY_AFTER_OPEN:   parseFloat(card.querySelector(".f-grabdelay").value) || 0,
    LOCAL_BIND_IP:           _bindIpValue(card),
    LINE_USER_ID:            card.querySelector(".f-lineuser").value,
    TICKET_FEE:              card.querySelector(".f-fee").value,
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
  // **START 前先把當前 DOM 設定同步到 server 記憶體**。不做的話，剛改的欄位還卡在
  // scheduleSave 的 400ms debounce 裡，_start_one 會拿到舊 config 寫檔 + 開 bot ——
  // 實測就是「選嚴格卻跑寬鬆」的根因（任何開跑前才改的欄位都會中）。
  if (card) {
    const p = pendingSaves.get(id);
    if (p) { clearTimeout(p); pendingSaves.delete(id); }
    await api("PUT", `/api/instances/${id}/config`, readCardConfig(card));
  }
  await api("POST", `/api/instances/${id}/start`, screenInfo());
}
async function stopOne(id) {
  await api("POST", `/api/instances/${id}/stop`);
}
// 只存不跑。欄位改動只會存在記憶體（scheduleSave 打的是 PUT config），
// 要落地成 profiles/acc_N/config.json 一定要按這顆或按 START。
async function saveOne(id, card, btn) {
  await api("PUT", `/api/instances/${id}/config`, readCardConfig(card));
  const r = await api("POST", `/api/instances/${id}/save`, screenInfo());
  const old = btn.textContent;
  btn.textContent = "已存";
  setTimeout(() => { btn.textContent = old; }, 1200);
  console.log("saved:", r.path);
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
  // 跟 startOne 同理：全部開跑前，先把每張卡當前 DOM 設定同步到 server（清掉 debounce），
  // 否則剛改的欄位還沒落地就被 start_all 用舊 config 開起來了。
  for (const card of document.querySelectorAll(".card")) {
    const id = parseInt(card.dataset.id, 10);
    const p = pendingSaves.get(id);
    if (p) { clearTimeout(p); pendingSaves.delete(id); }
    await api("PUT", `/api/instances/${id}/config`, readCardConfig(card)).catch(console.error);
  }
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

// ---------- 客人管理面板（資料存 Cloudflare D1，客服 bot 登記也寫同一份，這裡走 /api/customers proxy） ----------

async function reloadCustomers() {
  try { CUSTOMERS = await api("GET", "/api/customers"); } catch (_) { CUSTOMERS = []; }
  renderCustomerList();
  // 所有卡片的下拉就地更新，保住當前選擇，不重建卡片（避免打斷 log / 未存編輯）
  for (const card of $$(".card")) {
    const sel = card.querySelector(".f-lineuser");
    if (sel) fillCustomerSelect(sel, sel.value);
  }
}

function renderCustomerList() {
  const box = $("#customer-list");
  box.innerHTML = "";
  if (!CUSTOMERS.length) {
    box.innerHTML = '<div class="customer-empty">還沒有客人 — 叫客人加 LINE 官方帳號好友輸入「搶票」登記，或下方手動新增</div>';
    return;
  }
  for (const c of CUSTOMERS) {
    const row = document.createElement("div");
    row.className = "customer-row";
    const name = document.createElement("span");
    name.className = "customer-name";
    name.textContent = c.name;
    const concert = document.createElement("span");
    concert.className = "customer-concert";
    concert.textContent = c.concert || "";
    const uid = document.createElement("span");
    uid.className = "customer-uid";
    uid.textContent = c.user_id;
    uid.title = c.user_id;
    const del = document.createElement("button");
    del.className = "btn btn-red";
    del.textContent = "刪除";
    del.addEventListener("click", async () => {
      if (!confirm(`刪除客人「${c.name}」？\n（已選這個客人的卡片會顯示「已不在清單」但設定不會被清掉）`)) return;
      await api("DELETE", `/api/customers/${encodeURIComponent(c.user_id)}`);
      await reloadCustomers();
    });
    row.append(name, concert, uid, del);
    box.appendChild(row);
  }
}

$("#btn-customers").addEventListener("click", async () => {
  await reloadCustomers();
  $("#customer-modal").hidden = false;
});
$("#btn-customers-close").addEventListener("click", () => {
  $("#customer-modal").hidden = true;
});
$("#customer-modal").addEventListener("click", (e) => {
  if (e.target === $("#customer-modal")) $("#customer-modal").hidden = true;
});
$("#btn-cust-add").addEventListener("click", async () => {
  const name    = $("#cust-name").value.trim();
  const concert = $("#cust-concert").value.trim();
  const uid     = $("#cust-uid").value.trim();
  if (!uid) { alert("userId 不能為空（U 開頭那串）"); return; }
  await api("POST", "/api/customers", { name, concert, user_id: uid });
  $("#cust-name").value = "";
  $("#cust-concert").value = "";
  $("#cust-uid").value = "";
  await reloadCustomers();
});

refresh();
