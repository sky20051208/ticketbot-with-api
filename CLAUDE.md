# CLAUDE.md

多平台搶票機器人（Tixcraft / KKTIX / 寬宏 / TicketPlus）+ 本機 War-Room 網頁 GUI。只剩 API 引擎（舊 nodriver 腳本已移除）。

## 架構 1 分鐘版

- **平台分派**：GUI 依 `config.PLATFORM` spawn 對應套件（見 [webgui/server.py](webgui/server.py) spawn 段）
  - `TIXCRAFT` → `python -m tixcraftapi`（curl_cffi 打 API，FSM 架構；userdata cookie 模式用 Selenium 開 Chrome 登入）
  - `KKTIX` → `python -m kktix_api`（nodriver 開瀏覽器登入 → 純封包偵測：base_info 抓票種目錄 + register_info 高頻偵測開賣 → fetch 送單。票種是 Angular 前端渲染，raw HTML 拿不到，所以走 KKTIX JSON API。slug 要用「場次」slug 非「活動」slug）
  - `TICKETPLUS` → `python -m ticketplus_api`（見下方專節；全 JSON API，**開賣偵測不需要登入**）
- **瀏覽器層分兩套（刻意的，不要統一）**：拓元走 **Selenium**（[browser_login.py](browser_login.py) / [finalize.py](tixcraftapi/finalize.py)），KKTIX / 寬宏 / 遠端登入走 **nodriver**。2026-07-26 曾把拓元改成 nodriver，實測被鎖，已整包回退——**不要再改**。改 nodriver 那次踩到的兩個坑（哪天真要動再看）：`Network.clearBrowserCookies` 是「整個 profile 全域清空」，連 Google / FB 登入 cookie 一起殺（selenium 的 `delete_all_cookies()` 只清目前 domain，語意完全不同，賠掉一個 profile）；`tab.evaluate` 值 falsy 時回原始 RemoteObject，`bool()` 恆真
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
| [tixcraftapi/session.py](tixcraftapi/session.py) | `build_session` (curl_cffi + cookie + proxy)、`build_headers`、`warmup_session`、`keep_alive_loop`（背景 ping 維持 TLS）、`WarmPool`（常駐熱連線，並行請求都走它） |
| [tixcraftapi/parsing.py](tixcraftapi/parsing.py) | 純 regex 解析，**「拓元頁面長什麼樣」的知識全集中這檔**：`game_not_open`、`has_area_button`、`parse_game_area_url`、`parse_hidden_inputs`、`parse_ticket_form`、`find_ticket_codes`、`parse_area_availables` |
| [tixcraftapi/game.py](tixcraftapi/game.py) | Step 1：`select_game`（選場次）、`poll_until_open`（T-0 高頻 polling 抓開賣，回 `PollResult`）|
| [tixcraftapi/verify.py](tixcraftapi/verify.py) | Step 2：`handle_verify`（presale code 驗證頁；一次 POST 帶 `confirmed=true` 省 RTT，失敗 fallback 走兩步） |
| [tixcraftapi/area.py](tixcraftapi/area.py) | Step 3：`select_area`（排除關鍵字 + 四種模式；被 redirect 時**回傳落點 URL 交 FSM**，不自己處理 verify；`prefetched_html` 有值就跳過 GET） |
| [tixcraftapi/captcha.py](tixcraftapi/captcha.py) | `fetch_captcha_image`、`solve_captcha`（重試直到 4 字）、`CaptchaPrefetch`（跑在 `WarmPool` 上做 GET+OCR，每步都印耗時；**啟動後到 submit POST 之間 session 不可再 GET /ticket/captcha**，server 只認最後一張） |
| [tixcraftapi/submit.py](tixcraftapi/submit.py) | Step 4：`submit_ticket`（並行 GET 表單+抓驗證碼；驗證碼錯換一張，倒數第二輪起重抓表單避免 _csrf 過期）。**坑（2026-07 花錢踩過）**：同頁可能有多個票區代碼（`find_ticket_codes` 回 list），只買 `cached_code`，**其餘代碼要明確歸零覆蓋**，不能只 `dict(cached_payload)` 沿用 hidden input 殘留值（殘留不一定是 0，會多買到別區） |
| [tixcraftapi/order.py](tixcraftapi/order.py) | Step 5：`follow_order` 跟隨 redirect；`_poll_order_loop` 打 `/ticket/check` 等到 checkout；非預期落點回傳 URL 交 FSM |
| [tixcraftapi/finalize.py](tixcraftapi/finalize.py) | `open_chrome_with_session`：搶到後開乾淨 Chrome 注入 cookie（**只給 string 模式用**；userdata 模式走 `browser_login.inject_cookies_and_go`） |

**T-0 偵測（`poll_until_open`）**：同時 poll「GAME 頁」和「開賣前用 `data-key` 預先組好的 area 頁」（`__main__.prefetch_area_urls`）。**AREA 目標命中時那份 HTML 直接當 `ctx.area_html` 傳給 `select_area`，整發 area GET 省掉**（實測 T-0 那發 3~4 秒）；GAME 先命中就照舊。多目標會錯開相位（模擬：偵測延遲 0.11s → 0.01s）。`inflight_per_target` 預設 1 = **每個 URL 的請求頻率跟舊版一樣**，設 2 才會加倍 —— 實測拓元 eps 約 3 req/s 同一 URL 就開始 403，別亂調。

**連線就是延遲（2026-07-26 實測，別再破壞）**：curl_cffi 的 Session 連線池是 **thread-local**（`use_thread_local_curl=True`），新 thread = 新連線 = 重跑一次 TLS 握手。走 CliProxy 實測：重用連線 ~350ms、新建 750~3500ms。所以
- 並行請求**一律丟 `ctx.pool`（`WarmPool` 的常駐 worker）**，不要再 `with ThreadPoolExecutor(...)` 現開
- **form GET / POST 等主線請求留在主執行緒**，那條在倒數期間被 `make_main_thread_keepalive` 一直 ping 著（背景 keep-alive thread 暖不到主執行緒，這就是以前 T-0 第一發要 2.9 秒的原因）
- 診斷看這幾行：`[WARMUP] 第2發`（純 RTT 基準）、`[POOL]`（worker 連線）、`[KEEPALIVE] 主執行緒`、`[PREFETCH] GET=/OCR=`、`[PERF] form GET=/並行總=`

**新增 / 改步驟時的耦合鐵律**：
- 步驟函式只收 `(session, url, 明確參數)`、回「落點 URL 或 None」；**不讀 config、不 import 彼此、不自己解讀 redirect 去向**（交給 `state.classify`）
- config 值由 [runner.py](tixcraftapi/runner.py) 的 handler 在 call-time 讀取後以參數注入；handler 層照舊 `import config` + `config.XXX`，**絕不要** `from config import X`、**絕不要**把 `config.X` 放 function default arg（import/def 時鎖死值，GUI override 會失效）
- 跨步驟共享狀態只走 `Context` 欄位，不准掛在 session 或其他物件上暗渡
- 新增「被踢去的頁面」：`state.py` 加 enum + classify pattern → `runner.py` 加 handler，三步完成
- 共用 `BASE` 從 `from tixcraftapi import BASE` 取；HTML/regex 解析一律放 [parsing.py](tixcraftapi/parsing.py)

## ticketplus_api/ 套件結構（遠大 TicketPlus，全 JSON API）

前台是 Vue SPA，**沒有任何 HTML 要解析**，全部走 JSON API。整條鏈路只有「登入」需要瀏覽器，
其餘（目錄、開賣偵測、送單）都是 curl_cffi 純封包。實測 RTT：票況 ~65ms、enqueue ~55ms。

**id 有兩種形式，最容易搞混的就這個**：網址 `/activity/<32 hex>` 跟 static S3 檔用「加密 id」，
config API 的 `/get` 用「明文 id」（`e000001451` / `s000002136`）。AES-128-CBC，key `ILOVEFETIXFETIX!`
iv `!@#$FETIXEVENTiv`（前端 bundle 模組 9263 硬寫的），見 [crypto.py](ticketplus_api/crypto.py)。
`ticketAreaId` / `productId` 在 S3 目錄裡本來就是明文，**主線流程不需要解密**。

| 檔案 | 職責 |
|------|------|
| [ticketplus_api/__init__.py](ticketplus_api/__init__.py) | 四組 API base URL 常數（CONFIG / QUEUE / TICKET / USER） |
| [ticketplus_api/crypto.py](ticketplus_api/crypto.py) | id 加解密（診斷用，主線用不到；`cryptography` 走 lazy import） |
| [ticketplus_api/session.py](ticketplus_api/session.py) | curl_cffi session、XHR headers、`extract_token`、暖機、`make_keepalive`（主執行緒續命 callback） |
| [ticketplus_api/catalog.py](ticketplus_api/catalog.py) | 唯讀 API：S3 靜態目錄（sessions/ticketAreas/products）+ 即時票況 `/get`。**都不需要登入** |
| [ticketplus_api/parsing.py](ticketplus_api/parsing.py) | 純挑選邏輯（零 HTTP）：挑場次、票區優先序、可買判斷、`pick_target` |
| [ticketplus_api/reserve.py](ticketplus_api/reserve.py) | 送單：`enqueue`（排隊拿 uuid）→ `reserve`（換 orderId）。**唯一需要 token 的地方** |
| [ticketplus_api/browser_session.py](ticketplus_api/browser_session.py) | nodriver：登入抓 token、搶到後導向訂單頁。沒有 /login 路由（登入是 dialog），開活動頁等 `user` cookie 出現 |
| [ticketplus_api/__main__.py](ticketplus_api/__main__.py) | 串接：取 token → 建 plan → 倒數 → `poll_and_grab` → 訂單頁 + LINE 推播 |

**登入態不是 session cookie**：是 JS 寫的 `user` cookie（非 httpOnly），內容 URL-encoded JSON
`{"access_token": "<JWT>"}`。前端每支 API 自己塞 `Authorization: Bearer`，所以**只要那串 token，
cookie 本身不用帶**。使用者說「找不到 cookie」通常是在看 Network 的 request cookie —— 要看
DevTools → Application → Cookies → `user`。`extract_token()` 吃整串 cookie / 只有 user 的值 / 純 JWT。

**其他踩點**：
- **兩種活動，票種掛的位置完全不同**（2026-07-29 踩過，無劃位的活動被誤判成沒票）：
  `session.ticketArea=True` → ticketAreas.json 有內容、票種帶 `ticketAreaId`，關鍵字比對票區名；
  `session.ticketArea=False` → **ticketAreas.json 是空陣列**、票種身上沒有 `ticketAreaId` 欄位，
  關鍵字直接比對票種名。統一走 `parsing.rank_targets()` 產出扁平的 `[(票區 or None, 票種)]`，
  **不要再假設「票種一定掛在票區底下」**
- 多場次活動的 products.json 是**所有場次混在一起**的，一定要先用 `sessionId` 篩過再排優先序
- 即時票況裡 ticketArea 的 id 欄位叫 `id`，**不是** S3 目錄的 `ticketAreaId`（`index_infos` 處理掉了）
- 「售完」判斷要看旗標：`productLimit` / `ticketAreaLimit` 為 false 時 `count=0` **不代表售完**（不限量）
- `finalizedSeats=True` = 系統配位（前端 `seatReserve` 預設值），拿到 orderId 座位就定了直接進結帳；設 False 會多一頁自己點位子，搶票沒意義
- token 失效官方回 **HTTP 200 + errCode 103**，不是 401 —— 這種錯要標 `fatal` 直接中止，不然 poll 迴圈會對每個票區空打（見 `reserve.AUTH_ERR_CODES`）
- **access_token 只有 60 分鐘壽命**（JWT `exp`-`iat`）。手貼 COOKIE 模式要臨開賣前才貼，不然等倒數的時候就過期了；長時間掛機一律用 `COOKIE_SOURCE="userdata"`
- enqueue 常見 **errCode 137 + waitSecond** = 排隊中、叫你等 N 秒（實測 10s），照做就好，硬打不會比較快。2026-07-29 實測全鏈路：enqueue 105ms → 等 10s → enqueue 83ms 拿 uuid → reserve 2138ms 拿 orderId（reserve 慢是伺服器端在配位寫訂單，壓不下來）
- **TicketPlus 一律回 HTTP 200，成敗全看 errCode**，不要看 status code。完整對照表在
  `reserve.ERR_MESSAGES`（官方沒文件，是拿前端 `errorHandler` 的分支 × `/assets/lang/tw.json`
  的訊息湊出來的）。特別注意 **999 不是伺服器回的** —— 是前端 axios `.catch()` 自己捏的碼，
  代表網路/HTTP 層掛了，該重試不該當成沒票；121 同理也是前端自產
- 搶到後票只保留 **15 分鐘**（實測有活動設 10 分鐘，以 reserve 回應的 `expiryTimestamp` 為準），逾時自動釋放；要主動放掉打 `POST ticket/api/v1/release` `{orderId, culturePointInfo:{}}`
- **專屬代碼 / 序號**（`PRESALE_CODE` → `payload.serialNumber`，enqueue 跟 reserve 都要帶）：
  欄位只在 `hasSerial && transactionValidType` 都成立時渲染 —— 票種的 `serialKey` 非空
  **且** `session.transactionValidType` 非空（它的值就是那個 serialKey，例如 `sk00000433`）。
  說明文字在 `session.SNDescription[serialKey][語系]`。填錯回 **124**、已被用過回 **125**，
  兩者都標 fatal（重試無用）；`build_plan` 會在開賣前先查 `serialKey` 預警。
  **實測結論（2026-08 掃過全站 90 場活動 / 1746 個票種）：這機制實務上是「加購序號」不是
  「會員優先購搶門票」** —— 只有 6 場在用，全部是 VIP PASS / 手燈 / 特典這類周邊加購
  （文案就寫「加購序號」「預購序號」，價格 0/1/900/1800），序號是用來證明「你是有票的人」
  防黃牛掃周邊的。**搶門票沒有需要打會員碼的場次**，`PRESALE_CODE` 平常留空即可。
  另外前端有「問答題模式」（票種帶 `hint` → 欄位變答案欄）但 1746 個票種裡 `hint` 全是空的，
  等於沒人在用，別為它花時間
- **圖形驗證碼**：errCode **135**（驗證失敗）/ **136**（已過期）代表這場有 captcha（`api/captcha/api/v1/generate`，另有 reCAPTCHA sitekey 在 bundle 裡）。本 bot **未實作**，遇到只能中止 —— 目前實測的活動都沒觸發
- 送單成功後**直接導結帳確認頁**（`browser_session.checkout_url`）：有劃位 → `/confirmSeat/<加密eventId>/<加密sessionId>`（ConfirmSt），無劃位 → `/confirm/…`（Order2），跟前端 `nextStep` 自己的路由判斷一致。這兩頁 `init()` 會先打 `getUserCurrentReservedOrder`，有保留中的訂單就 `setReservedData()` 純從伺服器重建（**不依賴搶票當下的前端狀態**），沒訂單才自己退回 Order1 —— 所以直接跳是安全的，不用先繞 Order1

## Cookie 來源（兩種）

- `config.COOKIE_SOURCE = "string"` → 直接用 `config.COOKIE` 字串
- `config.COOKIE_SOURCE = "userdata"` → bot 用 `config.CHROME_USER_DATA_DIR` 的 Chrome profile 開瀏覽器自己抓 cookie（[browser_login.py](browser_login.py)）；搶到票後 cookie 灌回同一視窗跳結帳頁。**啟用 proxy 時 Chrome 也走同一 proxy**（經 [proxy_bridge.py](tixcraftapi/proxy_bridge.py) 起 localhost forwarder 補 auth header），全程同 IP 避免風控。proxy_url 為空時 Chrome 直連、bridge 不啟動，不影響本機效能
- `create_profile.py --name 帳號名` 建立 `chrome_profiles/<平台>/<帳號名>/` 專用 profile（一帳號一資料夾，cookie jar 隔離）。**預設每執行一次就換一個 proxy 出口 IP**（sid 加亂數尾巴），卡 reCAPTCHA 就原指令重跑換 IP；`--fixed-ip` 才固定同 name 同 IP。啟動時會打 ipify 印出本次出口 IP。**坑（2026-07-26 實測）：CliProxy 的 sid 只能英數，不能有連字號** —— 帳號字串 `-region-TW-sid-{sid}-t-90` 是用 `-` 分隔 key/value，sid 裡有 `-` 會靜默退回帳號預設 IP（看起來換了其實沒換，舊的 `p-{name}` 就是這樣壞的）。**坑（2026-07 實測）**：有帶 proxy 時先開平台首頁（`PLATFORM_HOME`）暖機、別直衝登入頁——剛換的 proxy IP 直攻 FB/Google OAuth 容易卡 reCAPTCHA（是 proxy IP 風評問題非 bot 偵測，暖機能降低但不保證，本質是對方風控）

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
