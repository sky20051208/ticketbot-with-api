# TicketBot War-Room (多開矩陣系統)

## 專案背景

這是從單機版 `ticketBot` 複製出來的新專案，目標是升級為「C# 母程式操控 N 個獨立 Python 執行緒」的矩陣搶票系統。

原始專案已經有完整的三平台搶票邏輯（拓元 Tixcraft / KKTIX / TicketPlus），本專案的任務是**改造 Python 端使其支援被外部程式參數化啟動與多開**，不需要重寫搶票邏輯本身。

## 目前檔案結構與職責

```
main.py              → 進入點，根據 config.PLATFORM 分發到對應平台模組
config.py            → 所有平台的搶票參數與設定
bot.py               → 拓元 (Tixcraft) 核心自動化，使用 nodriver (undetected chrome)
timeWatcher.py       → 精準校時模組，對 utimetool.com 取 HTTP Date Header
kktix/kkbot.py       → KKTIX 平台邏輯
ticketplus/ticketplusbot.py → 遠大售票平台邏輯
captchaAI/predict.py → CRNN 驗證碼辨識（被 bot.py import）
captchaAI/model/     → 模型權重 (.h5)
profiles/            → 多開帳號的 Chrome user_data_dir 隔離資料夾（執行時自動建立）
```

## 待辦開發任務（Python 端）

### 1. main.py — 加入 argparse 參數化啟動
- `--acc {id}` : 帳號編號（整數），用於隔離 profile 和 debug port
- `--port {number}` : 指定 `--remote-debugging-port`，預設為 `9222 + acc_id`，避免多開 CDP 衝突
- `--proxy {ip:port}` : 可選，直接指定代理；若未指定則自動呼叫 `get_proxy()`
- 接收參數後，啟動 nodriver 時套用：
  - `user_data_dir=f"./profiles/acc_{id}"`
  - `--remote-debugging-port={port}`
  - `--proxy-server={proxy}`

### 2. 新增 proxy_pool.py — 動態代理對接
- 實作 `get_proxy() -> str` 函式
- 向 `http://localhost:5010/get/` 發 GET 請求，取得可用代理 IP
- 回傳格式：`"http://{ip}:{port}"`
- 失敗時 print 警告並回傳 None（不掛代理直連）

### 3. stdout 即時輸出 — 給 C# 母程式讀取
- 在 main.py 最上方加入 `sys.stdout.reconfigure(line_buffering=True)`
- 確保所有 print 都能被 C# 的 `Process.StandardOutput` 即時讀到，不會被 buffer 吞掉

### 4. config.py — 支援參數覆寫
- 保留原有的預設值作為 fallback
- main.py 解析 argparse 後，應能覆寫 config 中的關鍵欄位（如 WANTED_TICKET_COUNT 等）
- 未來 C# 端會透過 command line args 傳入每個帳號的個別設定

### 5. bot.py / kkbot.py / ticketplusbot.py — 適配多開
- `run_initial_setup()` 中啟動瀏覽器的部分需接收 user_data_dir、port、proxy 參數
- 確保每個 instance 的 Chrome profile 完全隔離
- 各模組的 `main()` 函式需能接收從 main.py 傳入的參數

## 技術限制與注意事項

- 瀏覽器自動化使用 `nodriver`（undetected-chromedriver 的 async 版本），不是 selenium
- config.py 中的 `Selector` class 使用 selenium 的 `By`，這是歷史遺留，實際 bot.py 已改用 nodriver 的 JS 注入方式操作 DOM
- 打包相關的 .spec 和 gui.py 已刻意不帶入本專案，GUI 由 C# WPF 負責
- `chrome_profile/` 也未帶入，每個帳號會在 `profiles/acc_{id}/` 下自動建立獨立 profile

## C# 端互動協議（供 Python 端設計參考）

C# 母程式會這樣啟動每個 Python instance：
```
python main.py --acc 0 --port 9222
python main.py --acc 1 --port 9223 --proxy 127.0.0.1:8080
python main.py --acc 2 --port 9224
```

C# 透過重定向 stdout 讀取每個 instance 的即時日誌，顯示在 WPF 的 UniformGrid 網格中。
