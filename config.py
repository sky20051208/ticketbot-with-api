# config.py — 全平台搶票機器人設定
from selenium.webdriver.common.by import By
import os

# --- 平台選擇 ---
# "TIXCRAFT" = 拓元售票 | "KKTIX" = KKTIX | "TICKETPLUS" = 遠大售票
PLATFORM = "TIXCRAFT"

# --- 帳號 ---
COOKIE = ""  # 拓元登入後的完整 cookie 字串（COOKIE_SOURCE="string" 時用）

# cookie 來源:
#   "string"   = 直接用上面的 COOKIE 字串
#   "userdata" = bot 開 Chrome（用 CHROME_USER_DATA_DIR 的 profile）自己抓 cookie
COOKIE_SOURCE = "string"
CHROME_USER_DATA_DIR = ""  # COOKIE_SOURCE="userdata" 時，Chrome profile 資料夾路徑

# --- 活動設定 ---
ACTIVITY_SLUG = "26_joji"       # 活動 slug（從活動 URL 取）
TICKET_AMOUNT = "1"             # 購票張數
AREA_KEYWORD = "5800"               # 區域/價格關鍵字篩選（空字串 = 依選位策略）
AREA_AUTO_SELECT_MODE = "關鍵字優先"  # 選位策略: 關鍵字優先 / 由上而下 / 由下而上 / 隨機
EXCLUDE_AREA_KEYWORD = "輪椅;身障;身心;障礙;Restricted View;燈柱遮蔽;視線不完整;身障票"
DATE_KEYWORD = ""               # 場次日期篩選（空字串 = 選第一場）
PRESALE_CODE = ""               # 會員碼 / presale code（空字串 = 不使用，頁面有答案會自動抓）

# livenation 模式（搭配 COOKIE_SOURCE="userdata"）：browser 從這個 URL 開始而非 /login。
# 例 "https://www.livenation.com.tw/event/xxxx/"。使用者在 livenation 登入 + 點按鈕跳到
# tixcraft，bot 偵測 URL 進到 tixcraft 域就抓 cookie。session 帶著真實 livenation referer
# 歷史，比手動補 Referer header 更穩。空字串 = 走 default /login 流程。
LIVENATION_START_URL = ""

# --- 時間與監控 ---
ENABLE_TIME_WATCHER = False      # 是否啟用定時等待
TARGET_START_TIME = "12:00:00"  # 目標開賣時間 HH:MM:SS
ADVANCE_MS = 150                # 提前毫秒數
TIME_WATCH_URL = "https://www.tixcraft.com/activity/game/26_joji"  # 監控網址（瀏覽器模式用）

# --- 視窗平鋪 (GUI 多開時覆寫) ---
ACC_ID = 0
WINDOW_X = -1
WINDOW_Y = -1
WINDOW_W = -1
WINDOW_H = -1

# --- 重試與輪詢 ---
RETRY_INTERVAL = 0.3            # 主迴圈重試間隔（秒）
CAPTCHA_MAX_RETRY = 5           # 驗證碼辨識重試次數
TICKET_CAPTCHA_RETRY = 5        # 驗證碼錯誤重試輪數
ORDER_POLL_INTERVAL = 5.0       # 排隊輪詢間隔（秒）— 進 order 後純等待，不拚速度
ORDER_POLL_MAX = 180            # 排隊輪詢最大次數（5s × 180 = 15 分鐘上限）

# --- Proxy Pool (CliProxy 住宅 IP) ---
ENABLE_PROXY_POOL = True                       # 是否啟用 CliProxy（每個 instance 獨立 IP）
CLIPROXY_HOST = "sg2.cliproxy.io"  # 新加坡節點，台灣 RTT ~25ms（us2 美國要 135ms+）
CLIPROXY_PORT = 3010
# {acc_id} 會被 ACC_ID 替換 → 每個 instance 不同 sid → 不同出口 IP
# t-90 = sticky session 90 分鐘（建 profile / 等開賣 / 搶票全在窗口內 IP 一致）
CLIPROXY_USERNAME_TEMPLATE = "av5z1178126-region-TW-sid-acc{acc_id}-t-90"
CLIPROXY_PASSWORD = "r3jalstu"
CURRENT_PROXY = ""                               # 執行期由 proxy_pool.acquire() 寫入

# --- 系統路徑 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Selector:
    COOKIE_ACCEPT_BTN = (By.ID, "onetrust-accept-btn-handler")
    BUY_TICKET_BTN_SELECTOR = (By.CSS_SELECTOR, 'a[target="_new"]') 
    BUY_TICKET_BTN_TEXT = "立即購票"
    ORDER_BTN = (By.CSS_SELECTOR, 'button.btn.btn-primary.text-bold.m-0')
    TICKET_PRICE_SELECT = (By.ID, "TicketForm_ticketPrice_01")
    TICKET_AREA_A = (By.CLASS_NAME, "select_form_a") 
    TICKET_AREA_B = (By.CLASS_NAME, "select_form_b") 
    QUANTITY_DROPDOWN = (By.CSS_SELECTOR, '.mobile-select') 
    AGREEMENT_CHECKBOX = (By.CLASS_NAME, "form-check-input")
    CONFIRM_NEXT_BTN = (By.XPATH, '//*[@id="form-ticket-ticket"]/div[4]/button[2]')
    CAPTCHA_IMAGE = (By.ID, "TicketForm_verifyCode-image")
    CAPTCHA_INPUT = (By.ID, "TicketForm_verifyCode")
    CONFIRM_PURCHASE = (By.CSS_SELECTOR, 'button[type="submit"]')
    VERIFY_INPUT = (By.ID, "checkCode")
    VERIFY_BTN = (By.CLASS_NAME, "btn-primary")
