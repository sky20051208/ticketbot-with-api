# CLAUDE.md

多平台搶票機器人（Tixcraft / KKTIX）+ 本機 War-Room 網頁 GUI。只剩 API 引擎（舊 nodriver 腳本已移除）。

## 架構 1 分鐘版

- **平台分派**：GUI 依 `config.PLATFORM` spawn 對應套件（見 [webgui/server.py](webgui/server.py) spawn 段）
  - `TIXCRAFT` → `python -m tixcraftapi`（curl_cffi 打 API，FSM 架構；userdata cookie 模式用 Selenium 開 Chrome 登入）
  - `KKTIX` → `python -m kktix_api`（nodriver 開瀏覽器登入 → 純封包偵測：base_info 抓票種目錄 + register_info 高頻偵測開賣 → fetch 送單。票種是 Angular 前端渲染，raw HTML 拿不到，所以走 KKTIX JSON API。slug 要用「場次」slug 非「活動」slug）
- **驗證碼**：統一走 [captchaAI/predict.py](captchaAI/predict.py) 的 `recognize_captcha(bytes)`（自訓 ONNX），[tixcraftapi/captcha.py](tixcraftapi/captcha.py) 呼叫它，**不要自己建 OCR 實例**
- **GUI**：[webgui/](webgui/) 是 FastAPI + 原生 JS 的本機網頁版（`python run_webgui.py` 啟動），每張卡片對應一個 Python 子進程

## tixcraftapi/ 套件結構（API 模式，FSM 架構）

FSM：`runner.run` 派發 handler → handler 呼叫步驟函式 → 回「落點 URL 或 None」→ `state.classify(url)` 決定下個 state。
Happy path：`GAME → AREA → TICKET → QUEUE → CHECKOUT`；AREA 被導去驗證頁 → `VERIFY` → 回 AREA（不吃 cooldown）；落到 `/login` → `LOGIN_FAIL` terminal。

| 檔案 | 職責 |
|------|------|
| [tixcraftapi/__init__.py](tixcraftapi/__init__.py) | 只放 `BASE = "https://tixcraft.com"` 常數 |
| [tixcraftapi/__main__.py](tixcraftapi/__main__.py) | entry point（`python -m tixcraftapi --config X`）：argparse + `load_config_override` + log timestamp monkey-patch + `main()` 串接 |
| [tixcraftapi/state.py](tixcraftapi/state.py) | `State` enum + `classify(url)` — **全套件唯一的 URL 語意判斷**，零依賴 |
| [tixcraftapi/runner.py](tixcraftapi/runner.py) | FSM runner + `Context` + handlers — **唯一讀 config 的地方**（call-time 注入步驟函式）；AREA 重入 cooldown 5s / 403 cooldown 8s；captcha prefetch 掛 `ctx.captcha_prefetch` |
| [tixcraftapi/errors.py](tixcraftapi/errors.py) | `Blocked403` + `raise_if_blocked(res, label)` — 步驟撞 403 一律 raise，音效/cooldown 由 runner 統一處理 |
| [tixcraftapi/alerts.py](tixcraftapi/alerts.py) | 音效（sounds/403.wav、sounds/checkout.wav），只被 runner 呼叫 |
| [tixcraftapi/proxy_bridge.py](tixcraftapi/proxy_bridge.py) | `LocalProxyBridge`：起 127.0.0.1 forwarder 自動補 `Proxy-Authorization`；給 Chrome 用（cmdline 不認 inline auth）。daemon thread，process 結束自動收 |
| [tixcraftapi/session.py](tixcraftapi/session.py) | `build_session` (curl_cffi + cookie + proxy)、`build_headers`、`warmup_session`、`keep_alive_loop`（背景 ping 維持 TLS） |
| [tixcraftapi/parsing.py](tixcraftapi/parsing.py) | 純 regex 解析，**「拓元頁面長什麼樣」的知識全集中這檔**：`game_not_open`、`has_area_button`、`parse_game_area_url`、`parse_hidden_inputs`、`parse_ticket_form`、`find_ticket_codes`、`parse_area_availables` |
| [tixcraftapi/game.py](tixcraftapi/game.py) | Step 1：`select_game`（選場次）、`poll_until_open`（T-0 高頻 polling 抓開賣） |
| [tixcraftapi/verify.py](tixcraftapi/verify.py) | Step 2：`handle_verify`（presale code 驗證頁；一次 POST 帶 `confirmed=true` 省 RTT，失敗 fallback 走兩步） |
| [tixcraftapi/area.py](tixcraftapi/area.py) | Step 3：`select_area`（排除關鍵字 + 四種模式；被 redirect 時**回傳落點 URL 交 FSM**，不自己處理 verify） |
| [tixcraftapi/captcha.py](tixcraftapi/captcha.py) | `fetch_captcha_image`、`solve_captcha`（重試直到 4 字）、`CaptchaPrefetch`（背景 GET+OCR；**啟動後到 submit POST 之間 session 不可再 GET /ticket/captcha**，server 只認最後一張） |
| [tixcraftapi/submit.py](tixcraftapi/submit.py) | Step 4：`submit_ticket`（並行 GET 表單+抓驗證碼；驗證碼錯換一張，倒數第二輪起重抓表單避免 _csrf 過期）。**坑（2026-07 花錢踩過）**：同頁可能有多個票區代碼（`find_ticket_codes` 回 list），只買 `cached_code`，**其餘代碼要明確歸零覆蓋**，不能只 `dict(cached_payload)` 沿用 hidden input 殘留值（殘留不一定是 0，會多買到別區） |
| [tixcraftapi/order.py](tixcraftapi/order.py) | Step 5：`follow_order` 跟隨 redirect；`_poll_order_loop` 打 `/ticket/check` 等到 checkout；非預期落點回傳 URL 交 FSM |
| [tixcraftapi/finalize.py](tixcraftapi/finalize.py) | `open_chrome_with_session`：搶到後開乾淨 Chrome 注入 cookie（**只給 string 模式用**；userdata 模式走 `browser_login.inject_cookies_and_go`） |

**新增 / 改步驟時的耦合鐵律**：
- 步驟函式只收 `(session, url, 明確參數)`、回「落點 URL 或 None」；**不讀 config、不 import 彼此、不自己解讀 redirect 去向**（交給 `state.classify`）
- config 值由 [runner.py](tixcraftapi/runner.py) 的 handler 在 call-time 讀取後以參數注入；handler 層照舊 `import config` + `config.XXX`，**絕不要** `from config import X`、**絕不要**把 `config.X` 放 function default arg（import/def 時鎖死值，GUI override 會失效）
- 跨步驟共享狀態只走 `Context` 欄位，不准掛在 session 或其他物件上暗渡
- 新增「被踢去的頁面」：`state.py` 加 enum + classify pattern → `runner.py` 加 handler，三步完成
- 共用 `BASE` 從 `from tixcraftapi import BASE` 取；HTML/regex 解析一律放 [parsing.py](tixcraftapi/parsing.py)

## Cookie 來源（兩種）

- `config.COOKIE_SOURCE = "string"` → 直接用 `config.COOKIE` 字串
- `config.COOKIE_SOURCE = "userdata"` → bot 用 `config.CHROME_USER_DATA_DIR` 的 Chrome profile 開瀏覽器自己抓 cookie（[browser_login.py](browser_login.py)）；搶到票後 cookie 灌回同一視窗跳結帳頁。**啟用 proxy 時 Chrome 也走同一 proxy**（經 [proxy_bridge.py](tixcraftapi/proxy_bridge.py) 起 localhost forwarder 補 auth header），全程同 IP 避免風控。proxy_url 為空時 Chrome 直連、bridge 不啟動，不影響本機效能
- `create_profile.py --name 帳號名` 建立 `chrome_profiles/帳號名/` 專用 profile（一帳號一資料夾，cookie jar 隔離）。**坑（2026-07 實測）**：有帶 proxy 時先開平台首頁（`PLATFORM_HOME`）暖機、別直衝登入頁——剛換的 proxy IP 直攻 FB/Google OAuth 容易卡 reCAPTCHA（是 proxy IP 風評問題非 bot 偵測，暖機能降低但不保證，本質是對方風控）

## Config 串接規則（非常重要）

GUI 每個 instance 把設定寫到 `profiles/acc_{id}/config.json`，Python 端用 `--config` 在啟動時透過 `load_config_override()` 以 `setattr(config, key, val)` 動態蓋掉 [config.py](config.py) 的模組變數。

**所以所有 Python 檔案都必須用 `import config` + `config.XXX`，絕不用 `from config import XXX`**（後者會在 override 前就抓成本地變數，GUI 設定失效）。

GUI 寫出的 JSON key 必須和 config.py 變數名一字不差：
`PLATFORM`, `COOKIE`, `COOKIE_SOURCE`, `CHROME_USER_DATA_DIR`, `ACTIVITY_SLUG`,
`TICKET_AMOUNT`, `AREA_KEYWORD`, `AREA_AUTO_SELECT_MODE`, `EXCLUDE_AREA_KEYWORD`,
`DATE_KEYWORD`, `PRESALE_CODE`, `TARGET_START_TIME`, `ENABLE_TIME_WATCHER`,
`TIME_WATCH_URL`, `ENABLE_PROXY_POOL`, `LINE_USER_ID`, `TICKET_FEE`。

新增任何 config 欄位時，要同時改：
1. [config.py](config.py) — 加預設值
2. [webgui/server.py](webgui/server.py) `InstanceConfig` model 加欄位（純前端欄位如 `run_mode`、`chrome_profile` 也放這，在 `save_config()` 轉成真正的 config key）
3. [webgui/static/index.html](webgui/static/index.html) `card-template` 加對應 UI 元素
4. [webgui/static/app.js](webgui/static/app.js) `renderCard()` bind、`readCardConfig()` 回傳該欄位

## LINE（LineBot/ 本機推播 + LineBotWorker/ 雲端客服 bot）

搶到票推播（本機 Python）和客服選單（雲端 Cloudflare Workers）是兩個獨立系統，分開部署：

- [LineBot/line_push.py](LineBot/line_push.py)：單向 push（不需 webhook / 公網），token / 匯款資訊在 config.py 的 LINE 區塊。`notify_grabbed()` 由 [runner.py](tixcraftapi/runner.py) 在 TERMINAL_OK 時呼叫（跟 `alerts.play_checkout` 同位置）。**推播失敗只 print 不 raise**（票已到手，不能影響開結帳頁）。訊息提醒客人匯款備註填本名方便對帳
  - `notify_checkout_from_driver(user_id, driver)`：搶到票後截結帳頁推給客人證明。`_capture_confirmation_area()` 用文字錨點（「購票會員聯絡資訊」→「我同意本節目規則」按鈕）定位、拉寬到 1280 捲動後一次截完；抓不到錨點退回捲到最底。呼叫點在 [__main__.py](tixcraftapi/__main__.py)（userdata）和 [finalize.py](tixcraftapi/finalize.py)（string）。LINE 圖片訊息要公開 HTTPS 網址，借 LineBotWorker 的 `/api/screenshot` + `/img/:id` 當臨時圖床（1 小時失效）
  - **坑（2026-07）：先截圖、後 `minimize_window()`**。Chrome 對真正最小化的視窗停止渲染，截圖會被迫還原（CDP 硬截甚至卡死）；開在螢幕外雖能截但難找回。順序才是關鍵，minimize 不影響已拍好的截圖
- [LineBot/setup_richmenu.py](LineBot/setup_richmenu.py)：**一次性帳號設定**（`python -m LineBot.setup_richmenu`），Pillow 產生底部選單圖上傳 LINE，按鈕 `type=message` 送出文字跟客人手打一樣，直接吃 Worker 現有關鍵字比對。改文案/顏色就改這支重跑
- [LineBotWorker/](LineBotWorker/)：客服選單 + 搶票登記，**跑在 Cloudflare Workers（`wrangler deploy`），非本機 process**。「專員」→ push 通知 `OWNER_LINE_USER_ID`；「搶票」→ 兩段式問答（姓名 → 演唱會）完成後 upsert 進 D1 `customers` 表。**對話進度存 D1 `pending` 表，不能用記憶體字典**（Workers 無狀態、跨請求不保證同節點）
  - schema 見 [schema.sql](LineBotWorker/schema.sql)；`wrangler secret put` 設 `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET` / `ADMIN_KEY` / `OWNER_LINE_USER_ID`
  - webgui 不存本機客人 DB，`/api/customers` 全是 proxy，帶 `config.LINE_WORKER_ADMIN_KEY` 打 Worker（要跟 Worker 的 `ADMIN_KEY` 一致）。`config.LINE_WORKER_URL` 沒填時客人下拉照常開、只是空的
  - GUI topbar「客人管理」面板增刪；卡片「客人」下拉選 userId 存進 `LINE_USER_ID`（選 (不推播) = 空字串）；`TICKET_FEE` 每人不同、卡片填

## 其他約束

- **多開隔離**：每個 instance 獨立資料夾 `profiles/acc_{id}/`
- **PAUSE 按鈕目前無效**：GUI 寫 `pause.lock` 但 tixcraftapi / kktix_api 都沒讀（舊 `check_pause()` 在已刪的 bot.py），只有 START / STOP 有作用。要恢復得在 FSM 迴圈加 pause.lock 輪詢
- **定時啟動**：`config.ENABLE_TIME_WATCHER = False` 時所有平台直接開搶、不等 `TARGET_START_TIME`（GUI「定時啟動」checkbox 就是這個）
- **驗證碼 DOM ID**（硬編在 [captchaAI/predict.py](captchaAI/predict.py) 的 `CAPTCHA_IMAGE_ID`，**不要放回 config**，config.py 的 `Selector` class 是遺留物）：輸入框 `TicketForm_verifyCode`、圖片 `TicketForm_verifyCode-image`
- **GUI 啟動**：`python run_webgui.py`（預設 `127.0.0.1:7860`，需先 `pip install fastapi uvicorn`）。`chrome_profile` 下拉在載入 / INIT 時掃 `chrome_profiles/`，新增後要重整網頁才出現

## 不要做的事

- **不要加 proxy pool 相關程式碼**，除非我主動給 proxy 來源
- **不要 mock `load_config_override` 的行為**或用 feature flag 包住它
- **不要把 `from config import X` 加回來**，即使 IDE 嫌 `config.XXX` 囉嗦
- **不要把 `config.X` 當 function default arg**
- **不要把 tixcraftapi/ 的步驟函式塞回 __main__.py**（只做串接）
- **不要為了沉默 warning 而加 try/except**，讓錯誤浮出來

## 對話風格偏好

- 我用繁體中文，你也用繁體中文回
- 改檔時直接動手，不要先問要不要 clone / fetch 資料
- 回報簡短，diff 我自己會看
- 已建立 planner / coder / tester / reviewer subagent 與 `/dev-cycle` skill，可視情況主動使用；但小改直接動手就好，別硬拖整套 dev-cycle（大型需求才跑全套）
