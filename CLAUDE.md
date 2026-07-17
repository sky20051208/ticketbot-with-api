# CLAUDE.md

多平台搶票機器人（Tixcraft / KKTIX / TicketPlus）+ 本機 War-Room 網頁 GUI。
以下是專案結構與關鍵約束，避免每次對話都重新摸索。

## 架構 1 分鐘版

- **兩種搶票模式（Tixcraft）**
  - `python -m tixcraftapi` → API 模式（entry 在 [tixcraftapi/__main__.py](tixcraftapi/__main__.py)：log monkey-patch + config override + main 串接；搶票步驟在同套件其他檔）
  - `main.py` → 瀏覽器模式（`nodriver` 控 Chrome，能手動介入、清票）
  - GUI 的「API模式 / 瀏覽器模式」下拉就是在切這兩隻腳本
- **其他平台**：`kktix/kkbot.py`、`ticketplus/ticketplusbot.py`，由 `main.py` 根據 `config.PLATFORM` 分派
- **驗證碼**：統一走 [captchaAI/predict.py](captchaAI/predict.py) 的 `recognize_captcha(bytes)`（ddddocr beta mode），[tixcraftapi/captcha.py](tixcraftapi/captcha.py) 和 [bot.py](bot.py) 都呼叫它，**不要自己建 `ddddocr` 實例**
- **GUI**：[webgui/](webgui/) 是 FastAPI + 原生 JS 的本機網頁版（`python run_webgui.py` 啟動），每張卡片對應一個 Python 子進程。`webgui/server.py` 後端、`webgui/static/` 前端

## tixcraftapi/ 套件結構（API 模式，FSM 架構）

FSM：`runner.run` 派發 handler → handler 呼叫步驟函式 → 回「落點 URL 或 None」→ `state.classify(url)` 決定下個 state。
Happy path：`GAME → AREA → TICKET → QUEUE → CHECKOUT`；AREA 被導去驗證頁 → `VERIFY` → 回 AREA（不吃 cooldown）；落到 `/login` → `LOGIN_FAIL` terminal。

| 檔案 | 職責 |
|------|------|
| [tixcraftapi/__init__.py](tixcraftapi/__init__.py) | 只放 `BASE = "https://tixcraft.com"` 常數，套件說明 |
| [tixcraftapi/__main__.py](tixcraftapi/__main__.py) | API 模式 entry point（`python -m tixcraftapi --config X` 啟動）：argparse + `load_config_override` + log timestamp monkey-patch + `main()` 串接 |
| [tixcraftapi/state.py](tixcraftapi/state.py) | `State` enum + `classify(url)` — **全套件唯一的 URL 語意判斷**，零依賴 |
| [tixcraftapi/runner.py](tixcraftapi/runner.py) | FSM runner + `Context` + handlers — **唯一讀 config 的地方**（call-time 注入步驟函式）；AREA 重入 cooldown 5s / 403 cooldown 8s；captcha prefetch 掛 `ctx.captcha_prefetch`（AREA 啟動、TICKET 消費） |
| [tixcraftapi/errors.py](tixcraftapi/errors.py) | `Blocked403` + `raise_if_blocked(res, label)` — 步驟撞 403 一律 raise，音效/cooldown 由 runner 統一處理 |
| [tixcraftapi/alerts.py](tixcraftapi/alerts.py) | 音效（sounds/403.wav、sounds/checkout.wav），只被 runner 呼叫，步驟模組不 import |
| [tixcraftapi/proxy_bridge.py](tixcraftapi/proxy_bridge.py) | `LocalProxyBridge`：起 127.0.0.1 forwarder 自動補 `Proxy-Authorization`；給 Chrome 用（cmdline 不認 inline auth）。daemon thread，process 結束自動收 |
| [tixcraftapi/session.py](tixcraftapi/session.py) | `build_session` (curl_cffi + cookie + proxy)、`build_headers`、`warmup_session`、`keep_alive_loop`（背景 ping 維持 TLS） |
| [tixcraftapi/parsing.py](tixcraftapi/parsing.py) | 純 regex 解析，**「拓元頁面長什麼樣」的知識全集中這檔**：`game_not_open`、`has_area_button`、`parse_game_area_url`、`parse_hidden_inputs`、`parse_ticket_form`、`find_ticket_codes`、`parse_area_availables` |
| [tixcraftapi/game.py](tixcraftapi/game.py) | Step 1：`select_game`（單次選場次）、`poll_until_open`（T-0 高頻 polling 抓開賣瞬間） |
| [tixcraftapi/verify.py](tixcraftapi/verify.py) | Step 2：`handle_verify`（presale code 驗證頁；一次 POST 帶 `confirmed=true` 省 RTT，server 不買單再 fallback 走兩步）— 由 FSM VERIFY handler 呼叫 |
| [tixcraftapi/area.py](tixcraftapi/area.py) | Step 3：`select_area`（純選位策略：排除關鍵字 + 四種模式；被 redirect 時**回傳落點 URL 交給 FSM**，不自己處理 verify） |
| [tixcraftapi/captcha.py](tixcraftapi/captcha.py) | `fetch_captcha_image`、`solve_captcha`（重試直到 4 字）、`CaptchaPrefetch`（背景 GET+OCR；啟動後到 submit POST 之間 session 不可再 GET /ticket/captcha，server 只認最後一張） |
| [tixcraftapi/submit.py](tixcraftapi/submit.py) | Step 4：`submit_ticket`（並行 GET 表單+抓驗證碼省 RTT；驗證碼錯換一張，倒數第二輪起重抓表單避免 _csrf 過期；prefetch 以參數傳入）。**同頁可能不只一個票區代碼**（`find_ticket_codes` 回傳 list），只買 `cached_code`（第一個）那個，其餘代碼要明確歸零覆蓋——不能只 `dict(cached_payload)` 沿用原始 hidden input 殘留值，實測過殘留值不一定是 0，會多買到別的代碼（2026-07 真的花錢踩過這個坑：設定買 1 張、實際搶到 2 張） |
| [tixcraftapi/order.py](tixcraftapi/order.py) | Step 5：`follow_order` 跟隨 redirect；`_poll_order_loop` 打 `/ticket/check` 等到 checkout；非預期落點回傳 URL 交 FSM 分類 |
| [tixcraftapi/finalize.py](tixcraftapi/finalize.py) | `open_chrome_with_session`：搶到後開乾淨 Chrome 注入 cookie（**只給 string 模式用**；userdata 模式走 `browser_login.inject_cookies_and_go`） |

**新增 / 改步驟時的耦合鐵律**：
- 步驟函式只收 `(session, url, 明確參數)`、回「落點 URL 或 None」；**不讀 config、不 import 彼此、不自己解讀 redirect 去向**（交給 `state.classify`）
- config 值由 [runner.py](tixcraftapi/runner.py) 的 handler 在 call-time 讀取後以參數注入；handler 層照舊 `import config` + `config.XXX`，**絕不要** `from config import X`、**絕不要**把 `config.X` 放 function default arg（import/def 時鎖死值，GUI override 會失效）
- 跨步驟共享狀態只走 `Context` 欄位，不准掛在 session 或其他物件上暗渡
- 新增「被踢去的頁面」：`state.py` 加 enum + classify pattern → `runner.py` 加 handler，三步完成
- 共用 `BASE` 從 `from tixcraftapi import BASE` 取；HTML/regex 解析一律放 [parsing.py](tixcraftapi/parsing.py)

## Cookie 來源（兩種）

- `config.COOKIE_SOURCE = "string"` → 直接用 `config.COOKIE` 字串
- `config.COOKIE_SOURCE = "userdata"` → bot 用 `config.CHROME_USER_DATA_DIR` 的 Chrome profile 開瀏覽器，自己抓 cookie（[browser_login.py](browser_login.py)）；搶到票後 cookie 灌回同一個視窗跳結帳頁。**啟用 proxy 時 Chrome 也會走同一個 proxy**：經由 [tixcraftapi/proxy_bridge.py](tixcraftapi/proxy_bridge.py) 起一個 localhost forwarder 自動補 `Proxy-Authorization` header（Chrome cmdline 不認 inline auth，MV3 extension 又 race 不過 auth dialog，bridge 最穩）。login / 搶票 / 結帳全程同 IP，避免「同帳號兩個 IP」被風控標記。proxy_url 為空時 Chrome 直連、bridge 也不啟動，**完全不影響本機網路效能**
- `create_profile.py --name 帳號名` 建立 `chrome_profiles/帳號名/` 專用 profile（一帳號一資料夾，cookie jar 隔離）。有帶 proxy 時會先開平台首頁「暖機」（`PLATFORM_HOME`）而不是直衝登入頁，等使用者逛一下再手動點進登入頁——剛換的代理 IP 一上來就直攻 Facebook/Google OAuth 容易被判定可疑、卡 reCAPTCHA（2026-07 實測：關掉 proxy 直連就正常，確認是 proxy IP 風評問題，不是 bot 偵測），暖機能降低機率但不保證完全解決，本質是對方風控、我們控制不到

## Config 串接規則（非常重要）

GUI 每個 instance 會把設定寫到 `profiles/acc_{id}/config.json`，Python 端用 `--config` 參數在啟動時透過 `load_config_override()` 以 `setattr(config, key, val)` 動態蓋掉 [config.py](config.py) 的模組變數。

**這表示所有 Python 檔案都必須用 `import config` + `config.XXX`，絕對不要用 `from config import XXX`**。`from config import X` 會在 override 發生之前就把值抓成本地變數，導致 GUI 設定完全無效。

GUI 寫出的 JSON key 必須和 config.py 變數名一字不差：
`PLATFORM`, `COOKIE`, `COOKIE_SOURCE`, `CHROME_USER_DATA_DIR`, `ACTIVITY_SLUG`,
`TICKET_AMOUNT`, `AREA_KEYWORD`, `AREA_AUTO_SELECT_MODE`, `EXCLUDE_AREA_KEYWORD`,
`DATE_KEYWORD`, `PRESALE_CODE`, `TARGET_START_TIME`, `ENABLE_TIME_WATCHER`,
`TIME_WATCH_URL`, `ENABLE_PROXY_POOL`, `LINE_USER_ID`, `TICKET_FEE`。

新增任何 config 欄位時，要同時改：
1. [config.py](config.py) — 加預設值
2. [webgui/server.py](webgui/server.py) `InstanceConfig` model 加欄位（純前端用的欄位也放這，像 `run_mode`、`chrome_profile`；若是純前端欄位則在 `save_config()` 轉成真正的 config key）
3. [webgui/static/index.html](webgui/static/index.html) `card-template` 加對應 UI 元素
4. [webgui/static/app.js](webgui/static/app.js) `renderCard()` bind、`readCardConfig()` 回傳該欄位

## LINE（LineBot/ 本機推播 + LineBotWorker/ 雲端客服 bot）

搶到票的推播（本機、Python）和客人登記用的客服選單（雲端、Cloudflare Workers）是兩個獨立系統，分開部署：

- [LineBot/line_push.py](LineBot/line_push.py)：單向 push（不需 webhook / 公網），token / 匯款資訊在 config.py 的 LINE 區塊；`notify_grabbed()` 由 [tixcraftapi/runner.py](tixcraftapi/runner.py) 在 TERMINAL_OK 時呼叫（跟 `alerts.play_checkout` 同位置，`from LineBot import line_push`）。推播失敗不 raise，只 print — 票已到手，不能影響後續開結帳頁。訊息帶「備註請打上您的本名」提醒客人匯款備註填本名方便對帳
  - `notify_checkout_from_driver(user_id, driver)`：搶到票後截結帳頁畫面推給客人證明真的搶到，`_capture_confirmation_area()` 用文字錨點（「購票會員聯絡資訊」→「我同意本節目規則」按鈕）定位，拉寬視窗到 1280 避免窄版擠壓、拉高視窗到剛好塞下整段內容，捲動後一次截完；抓不到錨點就退回捲到頁面最底部。呼叫點在 [tixcraftapi/__main__.py](tixcraftapi/__main__.py)（userdata 模式，`inject_cookies_and_go` 之後）和 [tixcraftapi/finalize.py](tixcraftapi/finalize.py)（string 模式，`open_chrome_with_session` 裡 blocking `input()` 之前）。LINE 圖片訊息要求公開 HTTPS 網址，本機沒有，借 LineBotWorker 的 `/api/screenshot` + `/img/:id` 當臨時圖床（1 小時後自動失效，降低外流風險）
  - 截圖用的 Chrome 全程正常開啟顯示，**截圖拍完之後**才呼叫 `driver.minimize_window()` 縮到工具列（`__main__.py` userdata 模式 / `finalize.py` string 模式都有）。**踩過的坑**（2026-07）：曾經試過讓視窗在截圖「當下」就已經是最小化或開在螢幕座標範圍外，但 Chrome 對真正最小化的視窗會停止渲染合成，`driver.get_screenshot_as_png()` 會被迫把視窗還原才能截圖（改用底層 CDP `Page.captureScreenshot` 硬截甚至直接卡死）；開在螢幕外雖然能截圖，但座標抓不好會變得很難手動找回視窗。順序才是關鍵——**先截圖、後最小化**完全沒問題，minimize 動作本身不影響已經拍好的截圖
- [LineBot/setup_richmenu.py](LineBot/setup_richmenu.py)：**一次性帳號設定**（`python -m LineBot.setup_richmenu`），用 Pillow 產生底部選單圖（搶票登記／真人專員兩塊）上傳給 LINE，按鈕動作是 `type=message`，點擊送出的文字跟客人手打完全一樣，直接吃 Worker 現有的關鍵字比對，不用改 webhook。改選單文案/顏色就改這支腳本重跑
- [LineBotWorker/](LineBotWorker/)：客服選單 + 搶票登記，**跑在 Cloudflare Workers（`wrangler deploy`），不是本機 process** — 電腦關著客人一樣能互動，也不佔本機資源。客人加好友 / 傳訊息收到選單：「專員」→ push 一則訊息通知 `OWNER_LINE_USER_ID`（雲端沒辦法讓你電腦發出聲音，用這個取代原本設想的本機響鈴）；「搶票」→ 兩段式問答（先問姓名、再問演唱會），完成後 upsert 進 D1 的 `customers` 表。對話進度（問到哪一步）存 D1 的 `pending` 表，不能用記憶體字典（Workers 無狀態、跨請求不保證同一節點）
  - schema 見 [LineBotWorker/schema.sql](LineBotWorker/schema.sql)；`wrangler secret put` 設 `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET` / `ADMIN_KEY` / `OWNER_LINE_USER_ID`
  - webgui 完全不存本機客人資料庫，`/api/customers` 全是 proxy（見 [webgui/server.py](webgui/server.py)），帶 `config.LINE_WORKER_ADMIN_KEY` 打 Worker 的同名 API（要跟 Worker 的 `ADMIN_KEY` secret 一致）。`config.LINE_WORKER_URL` 沒填時 GUI 客人下拉照常開、只是空的，不會炸
  - GUI topbar「客人管理」面板做增刪、顯示演唱會；卡片的「客人」下拉選 userId 存進 `LINE_USER_ID`（選 (不推播) = 空字串 = 該卡不通知）；`TICKET_FEE` 搶票費每人不同、卡片填

## 多開隔離

- 每個 instance 獨立資料夾：`profiles/acc_{id}/`
- 瀏覽器模式 debug port：`9222 + id`
- 暫停機制：寫入 `profiles/acc_{id}/pause.lock` 檔案，bot 的 `check_pause()` 會阻塞；刪掉檔案繼續

## 定時啟動

`config.ENABLE_TIME_WATCHER = False` 時，所有平台都**直接開搶**、不等 `TARGET_START_TIME`。GUI 的「定時啟動」checkbox 就是這個。

## 驗證碼 DOM ID（Tixcraft）

硬編在 [bot.py](bot.py) 和 [captchaAI/predict.py](captchaAI/predict.py)，**不要再放回 config**（config.py 裡的 `Selector` class 是遺留物，只被舊程式用到）：
- 輸入框：`TicketForm_verifyCode`
- 圖片：`TicketForm_verifyCode-image`

## GUI 啟動 / 注意

- 啟動：`python run_webgui.py`（預設 `127.0.0.1:7860`，自動開瀏覽器），需先 `pip install fastapi uvicorn`
- 純前端的設定（`run_mode`、`chrome_profile`）放 `InstanceConfig` model，但**不會直接寫進 config.json**；在 `save_config()` 轉成真正的 config key（例如 `chrome_profile` → `COOKIE_SOURCE` + `CHROME_USER_DATA_DIR`）
- `chrome_profile` 下拉選單在載入頁面 / 按 INIT 時掃 `chrome_profiles/`，新增 profile 後要重整網頁才會出現

## 不要做的事

- **不要加 proxy pool 相關程式碼**，除非我主動給 proxy 來源
- **不要 mock `load_config_override` 的行為**或用 feature flag 包住它
- **不要把模組頂層的 `from config import X` 加回來**，即使 IDE 嫌 `config.XXX` 囉嗦
- **不要把 `config.X` 當 function default arg**（同上陷阱：def 時就鎖死值）
- **不要把 tixcraftapi/ 的步驟函式塞回 __main__.py**，__main__.py 只做串接
- **不要為了沉默 warning 而加 try/except**，讓錯誤浮出來

## 對話風格偏好

- 我會用繁體中文，你也用繁體中文回
- 改檔時直接動手，不要先問要不要 clone 參考 repo 或 fetch 資料
- 回報簡短，diff 我自己會看
- 不要用 agent / subagent 除非我明說
