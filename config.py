# config.py — API 機器人設定

# --- 帳號 ---
COOKIE = ""  # 拓元登入後的完整 cookie 字串

# --- 活動設定 ---
ACTIVITY_SLUG = "26_mltr"       # 活動 slug（從活動 URL 取，例如 /activity/detail/26_mltr）
TICKET_AMOUNT = "1"             # 購票張數
AREA_KEYWORD = "5800"           # 區域關鍵字篩選（空字串 = 選第一個有票的）
DATE_KEYWORD = ""               # 場次日期篩選（空字串 = 選第一場）
PRESALE_CODE = ""               # 會員碼 / presale code（空字串 = 不使用，頁面有答案會自動抓）

# --- 定時啟動 ---
TARGET_START_TIME = "18:03:00"  # 目標開賣時間 HH:MM:SS
ADVANCE_MS = 150                # 提前毫秒數

# --- 重試與輪詢 ---
RETRY_INTERVAL = 0.3            # 主迴圈重試間隔（秒）
CAPTCHA_MAX_RETRY = 5           # 驗證碼辨識重試次數
TICKET_CAPTCHA_RETRY = 5       # 驗證碼錯誤重試輪數（每輪重新抓驗證碼）
ORDER_POLL_INTERVAL = 0.3      # 排隊輪詢間隔（秒）
ORDER_POLL_MAX = 100            # 排隊輪詢最大次數
