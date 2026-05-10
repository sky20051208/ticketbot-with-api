# config.py — 全平台搶票機器人設定
from selenium.webdriver.common.by import By
import os

# --- 平台選擇 ---
# "TIXCRAFT" = 拓元售票 | "KKTIX" = KKTIX | "TICKETPLUS" = 遠大售票
PLATFORM = "TIXCRAFT"

# --- 帳號 ---
COOKIE = "tagHash=; BID=z7CDvdL0tlEGkkZ6R11HYZzFsXLO6OoME08J116Gi8TGRwDL_co-NcAGpba5gvhNDLA5Z05nw14hXtEx; _fbp=fb.1.1764901744781.200169633462818592; OptanonAlertBoxClosed=2025-12-05T02:29:10.780Z; eps_sid=3c1bbe4b0bcf5b33.1775928208.AzZRJtdhkoM9t7mcvSdhF2EZLIQMEutGAMtiqxf0L7Q=; tmpt=1:CAESGJcWM3_vFyZmih2_Lhe8QMtuyi9DNG1LxBiuu6yk2DMiMHA-tTqT_BxIL0JZSazEQyhdyvMWpzDaVAhBGm-0nwXG1YaE8I6GFEO880Oac5BS-w; _gid=GA1.2.1571375359.1776045204; __gads=ID=715a8e6d2d7f5af3:T=1764901744:RT=1776048538:S=ALNI_MZ7taZn6K0zjBZWrTBipJMbNrceIQ; __gpi=UID=00001318b085eba1:T=1764901744:RT=1776048538:S=ALNI_Ma0FVxNVzR-VdiwvmUF2mdHEEi7RA; __eoi=ID=874b9ba1a3ea287e:T=1764901744:RT=1776048538:S=AA-Afjb1LRFfiyIBVfCNx0mJzDIg; ab.storage.deviceId.e715aa3d-e50f-4f5a-a073-a3d081f5fa19=%7B%22g%22%3A%2219e13a13-d376-2e5b-801f-a7b5da7955a3%22%2C%22c%22%3A1774365533465%2C%22l%22%3A1776048537501%7D; ab.storage.userId.e715aa3d-e50f-4f5a-a073-a3d081f5fa19=%7B%22g%22%3A%22g115683976434745682842%22%2C%22c%22%3A1774365561982%2C%22l%22%3A1776048537502%7D; TIXUISID=rqhh7qid94gnqnpt1tjlk53ouc; _csrf=f8c24729beb9f271cbbef0d37207e881883d297643224039500f238a38e4b9d8a%3A2%3A%7Bi%3A0%3Bs%3A5%3A%22_csrf%22%3Bi%3A1%3Bs%3A32%3A%22DlbUtjiUWaUJ4mg1ebx4Zz76nC64DrIc%22%3B%7D; _ga_YP2REVX6BV=GS2.1.s1776048559$o22$g1$t1776048571$j60$l0$h0; OptanonConsent=isGpcEnabled=0&datestamp=Mon+Apr+13+2026+10%3A49%3A32+GMT%2B0800+(%E5%8F%B0%E5%8C%97%E6%A8%99%E6%BA%96%E6%99%82%E9%96%93)&version=202601.1.0&browserGpcFlag=0&isIABGlobal=false&hosts=&consentId=fd6e5ac1-cb4d-4013-b6d0-9607b9a147e9&interactionCount=1&isAnonUser=1&landingPath=NotLandingPage&groups=C0001%3A1%2CC0003%3A1%2CC0002%3A1%2CC0004%3A1&intType=1&geolocation=TW%3BTPE&AwaitingReconsent=false; _ga=GA1.2.1183430695.1764901745; _ga_C3KRPGTSF6=GS2.1.s1776048537$o26$g1$t1776048572$j60$l0$h0; ab.storage.sessionId.e715aa3d-e50f-4f5a-a073-a3d081f5fa19=%7B%22g%22%3A%22fb536d5b-d6ae-6248-3501-1ed537b3c14d%22%2C%22e%22%3A1776050372530%2C%22c%22%3A1776048537501%2C%22l%22%3A1776048572530%7D"  # 拓元登入後的完整 cookie 字串

# --- 活動設定 ---
ACTIVITY_SLUG = "26_joji"       # 活動 slug（從活動 URL 取）
TICKET_AMOUNT = "1"             # 購票張數
AREA_KEYWORD = "5800"               # 區域/價格關鍵字篩選（空字串 = 依選位策略）
AREA_AUTO_SELECT_MODE = "關鍵字優先"  # 選位策略: 關鍵字優先 / 由上而下 / 由下而上 / 隨機
EXCLUDE_AREA_KEYWORD = "輪椅;身障;身心;障礙;Restricted View;燈柱遮蔽;視線不完整;身障票"
DATE_KEYWORD = ""               # 場次日期篩選（空字串 = 選第一場）
PRESALE_CODE = ""               # 會員碼 / presale code（空字串 = 不使用，頁面有答案會自動抓）

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
ORDER_POLL_INTERVAL = 0.3       # 排隊輪詢間隔（秒）
ORDER_POLL_MAX = 100            # 排隊輪詢最大次數

# --- Proxy Pool (CliProxy 住宅 IP) ---
ENABLE_PROXY_POOL = False                        # 是否啟用 CliProxy（每個 instance 獨立 IP）
CLIPROXY_HOST = "sg2.cliproxy.io"  # 新加坡節點，台灣 RTT ~25ms（us2 美國要 135ms+）
CLIPROXY_PORT = 3010
# {acc_id} 會被 ACC_ID 替換 → 每個 instance 不同 sid → 不同出口 IP
# t-5 是 sticky session 5 分鐘；要更長就改 t-30 / t-60（看 CliProxy 方案是否支援）
CLIPROXY_USERNAME_TEMPLATE = "av5z1178126-region-TW-sid-acc{acc_id}-t-5"
CLIPROXY_PASSWORD = "r3jalstu"
CURRENT_PROXY = ""                               # 執行期由 proxy_pool.acquire() 寫入

# --- 系統路徑 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTCHA_DATASET_DIR = os.path.join(BASE_DIR, "captchaAI", "dataset")
CAPTCHA_MODEL_DIR = os.path.join(BASE_DIR, "captchaAI", "model")
MODEL_FILENAME = "crnn_ctc_model.h5"
MODEL_PATH = os.path.join(CAPTCHA_MODEL_DIR, MODEL_FILENAME)


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
