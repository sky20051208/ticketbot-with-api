/**
 * LINE 客服 bot — Cloudflare Workers 版，取代原本本機常駐的 LineBot/line_bind.py。
 *
 * 跑在 Cloudflare 邊緣節點，24/7 都在，不依賴本機電腦開機。
 * 對話進度（_pending：問到姓名還是演唱會）存 D1（Workers 是無狀態的，不能像本機版用記憶體字典）。
 *
 * 路由：
 *   GET/HEAD /webhook        LINE 設定 webhook endpoint 時的探測，回 200
 *   POST     /webhook        真正的事件（follow / message），HMAC 簽章驗證
 *   GET      /api/customers        列出客人（webgui 讀取用，X-Admin-Key 驗證）
 *   POST     /api/customers        新增 / 覆寫客人（webgui 手動新增用）
 *   DELETE   /api/customers/:id    刪除客人
 *
 * 環境變數 / secrets（wrangler secret put）：
 *   LINE_CHANNEL_ACCESS_TOKEN  LINE 推播 / 回覆用
 *   LINE_CHANNEL_SECRET        驗 webhook 簽章用
 *   ADMIN_KEY                  webgui 打 /api/customers 要帶的密鑰，跟 config.py 的
 *                              LINE_WORKER_ADMIN_KEY 要一致
 *   OWNER_LINE_USER_ID         你自己的 LINE userId（選填）。有設的話，客人打「專員」
 *                              會額外 push 一則訊息通知你（取代本機版的 winsound 響鈴，
 *                              因為 Worker 跑在雲端、沒辦法讓你的電腦發出聲音）。
 *                              第一次部署時還不知道自己的 userId，可以自己傳訊息給
 *                              bot 一次，用 `wrangler tail` 看 console.log 印出的 userId，
 *                              再設回來。
 */

const MENU_TEXT =
  "您好！\n" +
  "如果需要真人專員聯絡，請輸入「專員」\n" +
  "如果需要登記搶票，請輸入「搶票」";

// 用「包含」而非完全比對：容錯打字變化（多打字、簡繁體）。點 Rich Menu 按鈕送出的
// 也是這些字（type=message），跟客人手打完全同一條路徑，不用另外處理 postback。
const AGENT_KEYWORDS = ["專員", "专员", "客服", "真人"];
const TICKET_KEYWORDS = ["搶票", "抢票", "登記", "登记"];
const CANCEL_KEYWORDS = ["取消", "不要了"];

function matchesAny(text, keywords) {
  return keywords.some(k => text.includes(k));
}

async function lineApi(env, method, path, payload) {
  return fetch(`https://api.line.me${path}`, {
    method,
    headers: {
      "Authorization": `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: payload !== undefined ? JSON.stringify(payload) : undefined,
  });
}

async function reply(env, replyToken, text) {
  if (!replyToken) return;
  const res = await lineApi(env, "POST", "/v2/bot/message/reply", {
    replyToken,
    messages: [{ type: "text", text }],
  });
  if (res.status !== 200) {
    console.error(`[LINE] reply 失敗 HTTP ${res.status}: ${await res.text()}`);
  }
}

async function push(env, userId, text) {
  if (!userId) {
    console.log("[LINE] push 略過：OWNER_LINE_USER_ID 沒設定");
    return;
  }
  const res = await lineApi(env, "POST", "/v2/bot/message/push", {
    to: userId,
    messages: [{ type: "text", text }],
  });
  if (res.status !== 200) {
    console.error(`[LINE] push 失敗 HTTP ${res.status}: ${await res.text()}`);
  } else {
    console.log(`[LINE] push 成功 -> ${userId}`);
  }
}

async function displayName(env, userId) {
  const res = await lineApi(env, "GET", `/v2/bot/profile/${userId}`);
  if (res.status === 200) {
    const data = await res.json();
    return data.displayName || "?";
  }
  return "?";
}

async function verifySignature(secret, body, signatureB64) {
  if (!signatureB64) return false;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sigBuf = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  const computed = btoa(String.fromCharCode(...new Uint8Array(sigBuf)));
  return computed === signatureB64;
}

// ---------- D1 helpers ----------

async function getPending(env, userId) {
  return env.DB.prepare("SELECT stage, name FROM pending WHERE user_id = ?")
    .bind(userId).first();
}

async function setPending(env, userId, stage, name) {
  await env.DB.prepare(
    "INSERT INTO pending (user_id, stage, name) VALUES (?, ?, ?) " +
    "ON CONFLICT(user_id) DO UPDATE SET stage = excluded.stage, name = excluded.name"
  ).bind(userId, stage, name ?? null).run();
}

async function clearPending(env, userId) {
  await env.DB.prepare("DELETE FROM pending WHERE user_id = ?").bind(userId).run();
}

async function upsertCustomer(env, userId, name, concert) {
  await env.DB.prepare(
    "INSERT INTO customers (user_id, name, concert) VALUES (?, ?, ?) " +
    "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, concert = excluded.concert"
  ).bind(userId, name, concert).run();
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM customers").first();
  return row.n;
}

// ---------- webhook 對話邏輯 ----------

async function handleEvent(env, ev) {
  const userId = ev.source && ev.source.userId;
  if (!userId) return;
  const replyToken = ev.replyToken;
  const etype = ev.type;

  if (etype === "follow") {
    console.log(`[EVENT] follow userId=${userId}`);
    await reply(env, replyToken, MENU_TEXT);
    return;
  }
  if (etype !== "message") return;

  const pending = await getPending(env, userId);
  // 真人客服模式：客人打過「專員」之後，bot 對這個人完全靜默（不回選單、不重複推播），
  // 讓你直接在 LINE 官方帳號手動跟客人對話，不會被自動回覆蓋掉。客人重新輸入「搶票」
  // 才會把 bot 拉回來繼續走登記流程；其餘一律不回應（包含再打一次「專員」）。
  const inAgentMode = pending && pending.stage === "agent";

  // 貼圖／圖片／位置等非文字訊息：LINE 上很常見，之前完全不回應會讓 bot 看起來壞掉，
  // 但真人客服模式下要保持靜默。
  if (!ev.message || ev.message.type !== "text") {
    console.log(`[EVENT] non-text message userId=${userId} type=${ev.message && ev.message.type}`);
    if (!inAgentMode) await reply(env, replyToken, MENU_TEXT);
    return;
  }

  const text = (ev.message.text || "").trim();
  console.log(`[EVENT] message userId=${userId} text=${text}`);
  if (!text) return;

  // 取消：優先權最高，任何狀態下（包含真人客服模式、登記到一半）都能跳出重來。
  if (matchesAny(text, CANCEL_KEYWORDS)) {
    await clearPending(env, userId);
    await reply(env, replyToken, "已取消。\n" + MENU_TEXT);
    return;
  }

  if (inAgentMode) {
    if (matchesAny(text, TICKET_KEYWORDS)) {
      await setPending(env, userId, "name", null);
      await reply(env, replyToken, "好的！請輸入您的姓名（搶到票會用這個名字聯繫、對帳；輸入「取消」可跳出）");
    }
    return;
  }

  if (matchesAny(text, AGENT_KEYWORDS)) {
    await setPending(env, userId, "agent", null);
    const name = await displayName(env, userId);
    console.log(`[專員] ${name} (${userId}) 要求真人客服，請盡快聯繫！`);
    await push(env, env.OWNER_LINE_USER_ID, `🔔 ${name} 要求真人客服，請盡快聯繫！`);
    await reply(env, replyToken, "已通知專員，將盡快與您聯繫！");
    return;
  }

  if (matchesAny(text, TICKET_KEYWORDS)) {
    await setPending(env, userId, "name", null);
    await reply(env, replyToken, "好的！請輸入您的姓名（搶到票會用這個名字聯繫、對帳）");
    return;
  }

  if (pending && pending.stage === "name") {
    await setPending(env, userId, "concert", text);
    await reply(env, replyToken, "收到！請輸入您想搶的演唱會名稱（輸入「取消」可跳出）");
    return;
  }
  if (pending && pending.stage === "concert") {
    const name = pending.name;
    await clearPending(env, userId);

    // 重複登記時要讓客人知道舊資料被取代了，不要默默蓋掉造成困惑。
    const existing = await env.DB.prepare("SELECT concert FROM customers WHERE user_id = ?")
      .bind(userId).first();
    const replacedNote = (existing && existing.concert && existing.concert !== text)
      ? `（原本登記的《${existing.concert}》已被取代）\n` : "";

    const total = await upsertCustomer(env, userId, name, text);
    console.log(`[BIND] 姓名=${name} 演唱會=${text} userId=${userId} 共 ${total} 位`);
    await reply(env, replyToken,
      `登記完成！\n姓名：${name}\n演唱會：${text}\n${replacedNote}` +
      "搶到票會用這個 LINE 帳號通知您匯款，請留意訊息。");
    return;
  }

  await reply(env, replyToken, MENU_TEXT);
}

// ---------- /api/customers（webgui 用） ----------

function checkAdminKey(env, request) {
  const key = request.headers.get("X-Admin-Key") || "";
  return !!env.ADMIN_KEY && key === env.ADMIN_KEY;
}

async function handleCustomersApi(env, request, url) {
  if (!checkAdminKey(env, request)) {
    return new Response("unauthorized", { status: 401 });
  }

  const delMatch = url.pathname.match(/^\/api\/customers\/(.+)$/);
  if (delMatch && request.method === "DELETE") {
    const userId = decodeURIComponent(delMatch[1]);
    const res = await env.DB.prepare("DELETE FROM customers WHERE user_id = ?")
      .bind(userId).run();
    if (res.meta.changes === 0) return new Response("not found", { status: 404 });
    return Response.json({ ok: true });
  }

  if (url.pathname === "/api/customers" && request.method === "GET") {
    const { results } = await env.DB.prepare(
      "SELECT user_id, name, concert FROM customers ORDER BY name"
    ).all();
    return Response.json(results.map(r => (
      { user_id: r.user_id, name: r.name, concert: r.concert || "" }
    )));
  }

  if (url.pathname === "/api/customers" && request.method === "POST") {
    const body = await request.json();
    const userId = (body.user_id || "").trim();
    if (!userId) return new Response("user_id required", { status: 400 });
    await upsertCustomer(env, userId,
      (body.name || "").trim() || "(未命名)", (body.concert || "").trim());
    return Response.json({ ok: true });
  }

  return new Response("not found", { status: 404 });
}

// ---------- 結帳頁截圖（Selenium 截圖只含瀏覽器內容，不含桌面/工具列） ----------
// LINE 圖片訊息要求公開 HTTPS 網址讓 LINE 主動抓圖，本機 Python 沒有公開網址，
// 借這裡暫存 + 服務：POST 上傳、GET /img/:id 公開讀取（id 是 UUID，無法猜測）。

const SCREENSHOT_TTL_MS = 60 * 60 * 1000; // 1 小時：夠 LINE 抓圖 + 重試，過期即失效降低外流風險

async function handleUploadScreenshot(env, request) {
  if (!checkAdminKey(env, request)) {
    return new Response("unauthorized", { status: 401 });
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength === 0) {
    return new Response("empty body", { status: 400 });
  }
  const id = crypto.randomUUID();
  const now = Date.now();
  await env.DB.prepare("INSERT INTO screenshots (id, data, created_at) VALUES (?, ?, ?)")
    .bind(id, bytes, now).run();
  // 順手清掉過期的舊截圖（沒有另外排 cron，量體小，搭上傳時機清就夠）
  await env.DB.prepare("DELETE FROM screenshots WHERE created_at < ?")
    .bind(now - SCREENSHOT_TTL_MS).run();
  const url = `${new URL(request.url).origin}/img/${id}`;
  return Response.json({ id, url });
}

async function handleGetImage(env, id) {
  const row = await env.DB.prepare("SELECT data, created_at FROM screenshots WHERE id = ?")
    .bind(id).first();
  if (!row) return new Response("not found", { status: 404 });
  if (Date.now() - row.created_at > SCREENSHOT_TTL_MS) {
    await env.DB.prepare("DELETE FROM screenshots WHERE id = ?").bind(id).run();
    return new Response("expired", { status: 404 });
  }
  // D1 撈回的 BLOB 直接塞給 Response 會被當成數字陣列字串化（逗號分隔），
  // 明確包成 Uint8Array 才會正確輸出成 binary。
  return new Response(new Uint8Array(row.data), {
    headers: { "Content-Type": "image/png", "Cache-Control": "private, max-age=3600" },
  });
}

// ---------- entry ----------

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/webhook") {
      if (request.method === "GET" || request.method === "HEAD") {
        return new Response(null, { status: 200 });
      }
      if (request.method === "POST") {
        const body = await request.text();
        const sig = request.headers.get("X-Line-Signature") || "";
        if (!(await verifySignature(env.LINE_CHANNEL_SECRET, body, sig))) {
          return new Response("bad signature", { status: 403 });
        }
        const events = JSON.parse(body).events || [];
        await Promise.all(events.map(ev =>
          handleEvent(env, ev).catch(e => console.error("[ERROR]", e))
        ));
        return new Response("OK", { status: 200 });
      }
    }

    if (url.pathname.startsWith("/api/customers")) {
      return handleCustomersApi(env, request, url);
    }

    if (url.pathname === "/api/screenshot" && request.method === "POST") {
      return handleUploadScreenshot(env, request);
    }

    const imgMatch = url.pathname.match(/^\/img\/([0-9a-f-]+)$/);
    if (imgMatch && request.method === "GET") {
      return handleGetImage(env, imgMatch[1]);
    }

    return new Response("not found", { status: 404 });
  },
};
