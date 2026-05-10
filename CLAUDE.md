# CLAUDE.md

多平台搶票機器人（Tixcraft / KKTIX / TicketPlus）+ C# WPF War-Room GUI。
以下是專案結構與關鍵約束，避免每次對話都重新摸索。

## 架構 1 分鐘版

- **兩種搶票模式（Tixcraft）**
  - `test.py` → API 模式（`curl_cffi` 直打 HTTP，快）
  - `main.py` → 瀏覽器模式（`nodriver` 控 Chrome，能手動介入、清票）
  - GUI 的「API模式 / 瀏覽器模式」下拉就是在切這兩隻腳本
- **其他平台**：`kktix/kkbot.py`、`ticketplus/ticketplusbot.py`，由 `main.py` 根據 `config.PLATFORM` 分派
- **驗證碼**：統一走 [captchaAI/predict.py](captchaAI/predict.py) 的 `recognize_captcha(bytes)`（ddddocr beta mode），`test.py` 和 `bot.py` 都呼叫它，**不要自己建 `ddddocr` 實例**
- **GUI**：[gui/](gui/) 是 .NET 10 WPF 專案（namespace `TicketBotWarRoom`），每個 instance 卡片對應一個 Python 子進程

## Config 串接規則（非常重要）

GUI 每個 instance 會把設定寫到 `profiles/acc_{id}/config.json`，Python 端用 `--config` 參數在啟動時透過 `load_config_override()` 以 `setattr(config, key, val)` 動態蓋掉 [config.py](config.py) 的模組變數。

**這表示所有 Python 檔案都必須用 `import config` + `config.XXX`，絕對不要用 `from config import XXX`**。`from config import X` 會在 override 發生之前就把值抓成本地變數，導致 GUI 設定完全無效。

GUI 寫出的 JSON key 必須和 config.py 變數名一字不差：
`PLATFORM`, `COOKIE`, `ACTIVITY_SLUG`, `TICKET_AMOUNT`, `AREA_KEYWORD`,
`AREA_AUTO_SELECT_MODE`, `EXCLUDE_AREA_KEYWORD`, `DATE_KEYWORD`, `PRESALE_CODE`,
`TARGET_START_TIME`, `ENABLE_TIME_WATCHER`, `TIME_WATCH_URL`。

新增任何 config 欄位時，要同時改：
1. [config.py](config.py) — 加預設值
2. [gui/MainWindow.xaml.cs](gui/MainWindow.xaml.cs) `LaunchBot()` 的 cfg Dictionary
3. [gui/MainWindow.xaml.cs](gui/MainWindow.xaml.cs) `BotInstance` ViewModel 的 Cfg 屬性
4. [gui/MainWindow.xaml](gui/MainWindow.xaml) 加對應 UI 欄位

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

## GUI 已知地雷

- **ComboBox 項目用 `sys:String`，不要用 `ComboBoxItem`**。`ComboBoxItem` 會讓 `SelectedItem="{Binding}"` 抓到整個物件而不是字串，寫入 JSON 會變 `"System.Windows.Controls.ComboBoxItem: ..."`。
- Build：`cd gui && dotnet build`（Windows，bash shell）

## 不要做的事

- **不要加 proxy pool 相關程式碼**，除非我主動給 proxy 來源
- **不要 mock `load_config_override` 的行為**或用 feature flag 包住它
- **不要把模組頂層的 `from config import X` 加回來**，即使 IDE 嫌 `config.XXX` 囉嗦
- **不要為了沉默 warning 而加 try/except**，讓錯誤浮出來

## 對話風格偏好

- 我會用繁體中文，你也用繁體中文回
- 改檔時直接動手，不要先問要不要 clone 參考 repo 或 fetch 資料
- 回報簡短，diff 我自己會看
- 不要用 agent / subagent 除非我明說
