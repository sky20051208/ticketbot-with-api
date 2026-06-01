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

## tixcraftapi/ 套件結構（API 模式搶票步驟）

呼叫鏈：`game → area → submit → order → finalize`（`area` 內部會在被 redirect 時 call `verify`；`submit` 並行抓表單+驗證碼走 `captcha`）。

| 檔案 | 職責 |
|------|------|
| [tixcraftapi/__init__.py](tixcraftapi/__init__.py) | 只放 `BASE = "https://tixcraft.com"` 常數，套件說明 |
| [tixcraftapi/__main__.py](tixcraftapi/__main__.py) | API 模式 entry point（`python -m tixcraftapi --config X` 啟動）：argparse + `load_config_override` + log timestamp monkey-patch + `main()` 串接 |
| [tixcraftapi/proxy_bridge.py](tixcraftapi/proxy_bridge.py) | `LocalProxyBridge`：起 127.0.0.1 forwarder 自動補 `Proxy-Authorization`；給 Chrome 用（cmdline 不認 inline auth）。daemon thread，process 結束自動收 |
| [tixcraftapi/session.py](tixcraftapi/session.py) | `build_session` (curl_cffi + cookie + proxy)、`build_headers`、`warmup_session`、`keep_alive_loop`（背景 ping 維持 TLS） |
| [tixcraftapi/parsing.py](tixcraftapi/parsing.py) | 純 regex 解析：`parse_ticket_form`、`find_ticket_codes`、`parse_game_area_url` |
| [tixcraftapi/game.py](tixcraftapi/game.py) | Step 1：`select_game`（單次選場次）、`poll_until_open`（T-0 高頻 polling 抓開賣瞬間） |
| [tixcraftapi/verify.py](tixcraftapi/verify.py) | Step 2：`handle_verify`（presale code 驗證頁；一次 POST 帶 `confirmed=true` 省 RTT，server 不買單再 fallback 走兩步） |
| [tixcraftapi/area.py](tixcraftapi/area.py) | Step 3：`select_area`（解析 `areaUrlList`、排除關鍵字、四種選位策略；遇 redirect 自動 call `handle_verify`） |
| [tixcraftapi/captcha.py](tixcraftapi/captcha.py) | `fetch_captcha_image`（抓 PNG/JPEG bytes，可被 thread pool 平行呼叫）、`solve_captcha`（重試直到 4 字） |
| [tixcraftapi/submit.py](tixcraftapi/submit.py) | Step 4：`submit_ticket`（並行 GET 表單+抓驗證碼省 RTT；驗證碼錯換一張，倒數第二輪起重抓表單避免 _csrf 過期） |
| [tixcraftapi/order.py](tixcraftapi/order.py) | Step 5：`follow_order` 跟隨 redirect 判定落點；`_poll_order_loop` 打 `/ticket/check` 等到 checkout |
| [tixcraftapi/finalize.py](tixcraftapi/finalize.py) | `open_chrome_with_session`：搶到後開乾淨 Chrome 注入 cookie（**只給 string 模式用**；userdata 模式走 `browser_login.inject_cookies_and_go`） |

**新增 / 改步驟時**：每個檔都用 `import config` + `config.XXX`，**絕不要**把 `config.X` 放在 function default arg（會在 import 時鎖死值，跟 `from config import X` 同一個陷阱 — 見 [tixcraftapi/captcha.py](tixcraftapi/captcha.py) `solve_captcha` 的處理範例）。共用 `BASE` 從 `from tixcraftapi import BASE` 取。

## Cookie 來源（兩種）

- `config.COOKIE_SOURCE = "string"` → 直接用 `config.COOKIE` 字串
- `config.COOKIE_SOURCE = "userdata"` → bot 用 `config.CHROME_USER_DATA_DIR` 的 Chrome profile 開瀏覽器，自己抓 cookie（[browser_login.py](browser_login.py)）；搶到票後 cookie 灌回同一個視窗跳結帳頁。**啟用 proxy 時 Chrome 也會走同一個 proxy**：經由 [tixcraftapi/proxy_bridge.py](tixcraftapi/proxy_bridge.py) 起一個 localhost forwarder 自動補 `Proxy-Authorization` header（Chrome cmdline 不認 inline auth，MV3 extension 又 race 不過 auth dialog，bridge 最穩）。login / 搶票 / 結帳全程同 IP，避免「同帳號兩個 IP」被風控標記。proxy_url 為空時 Chrome 直連、bridge 也不啟動，**完全不影響本機網路效能**
- `create_profile.py --name 帳號名` 建立 `chrome_profiles/帳號名/` 專用 profile（一帳號一資料夾，cookie jar 隔離）

## Config 串接規則（非常重要）

GUI 每個 instance 會把設定寫到 `profiles/acc_{id}/config.json`，Python 端用 `--config` 參數在啟動時透過 `load_config_override()` 以 `setattr(config, key, val)` 動態蓋掉 [config.py](config.py) 的模組變數。

**這表示所有 Python 檔案都必須用 `import config` + `config.XXX`，絕對不要用 `from config import XXX`**。`from config import X` 會在 override 發生之前就把值抓成本地變數，導致 GUI 設定完全無效。

GUI 寫出的 JSON key 必須和 config.py 變數名一字不差：
`PLATFORM`, `COOKIE`, `COOKIE_SOURCE`, `CHROME_USER_DATA_DIR`, `ACTIVITY_SLUG`,
`TICKET_AMOUNT`, `AREA_KEYWORD`, `AREA_AUTO_SELECT_MODE`, `EXCLUDE_AREA_KEYWORD`,
`DATE_KEYWORD`, `PRESALE_CODE`, `TARGET_START_TIME`, `ENABLE_TIME_WATCHER`,
`TIME_WATCH_URL`, `ENABLE_PROXY_POOL`。

新增任何 config 欄位時，要同時改：
1. [config.py](config.py) — 加預設值
2. [webgui/server.py](webgui/server.py) `InstanceConfig` model 加欄位（純前端用的欄位也放這，像 `run_mode`、`chrome_profile`；若是純前端欄位則在 `save_config()` 轉成真正的 config key）
3. [webgui/static/index.html](webgui/static/index.html) `card-template` 加對應 UI 元素
4. [webgui/static/app.js](webgui/static/app.js) `renderCard()` bind、`readCardConfig()` 回傳該欄位

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
