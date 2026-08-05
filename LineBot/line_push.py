"""LINE Messaging API 推播 — 搶到票後通知客人匯款 + 自行結帳。

只做單向 push（不需要 webhook / 公網），token 從 config 讀（call-time，GUI override 生效）。
客人的 userId 用 line_bind.py 一次性綁定工具取得，填進 GUI 卡片的 LINE USER ID 欄位。

推播失敗不 raise（票已到手，通知失敗不能影響後續開結帳頁），回 False + print 錯誤。
"""
import json
import time
import base64
import asyncio

import requests
from selenium.webdriver.common.by import By

import config

# 結帳確認頁的文字錨點：從「購票會員聯絡資訊」抓到「我同意本節目規則」按鈕之間整塊
# （聯絡資訊 + 付款方式 + 配送方式 + 訂單明細），跳過上面選位/下面條款等不相關內容。
_SCREENSHOT_TOP_XPATH = "//*[contains(text(), '購票會員聯絡資訊')]"
_SCREENSHOT_BOTTOM_XPATH = "//button[contains(., '我同意本節目規則')]"

PUSH_URL = "https://api.line.me/v2/bot/message/push"
_TIMEOUT = 5.0
_RETRY = 2  # 總嘗試次數


def push_text(user_id: str, text: str) -> bool:
    """推一則純文字訊息給 user_id。成功 True / 失敗 False（不 raise）。"""
    token = config.LINE_CHANNEL_ACCESS_TOKEN
    if not token or not user_id:
        print("[LINE] 缺 token 或 userId，略過推播")
        return False
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    last_err = ""
    for i in range(_RETRY):
        try:
            res = requests.post(PUSH_URL, headers=headers,
                                data=json.dumps(payload), timeout=_TIMEOUT)
            if res.status_code == 200:
                print(f"[LINE] 推播成功 → {user_id[:12]}…")
                return True
            last_err = f"HTTP {res.status_code}: {res.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    print(f"[LINE] 推播失敗（{_RETRY} 次）: {last_err}")
    return False


def push_image(user_id: str, image_url: str) -> bool:
    """推一張圖片給 user_id（image_url 必須是公開可存取的 HTTPS 網址，LINE 會主動抓）。"""
    token = config.LINE_CHANNEL_ACCESS_TOKEN
    if not token or not user_id:
        return False
    payload = {
        "to": user_id,
        "messages": [{
            "type": "image",
            "originalContentUrl": image_url,
            "previewImageUrl": image_url,
        }],
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        res = requests.post(PUSH_URL, headers=headers,
                            data=json.dumps(payload), timeout=_TIMEOUT)
        if res.status_code == 200:
            print(f"[LINE] 截圖推播成功 → {user_id[:12]}…")
            return True
        print(f"[LINE] 截圖推播失敗 HTTP {res.status_code}: {res.text[:200]}")
    except Exception as e:
        print(f"[LINE] 截圖推播例外: {type(e).__name__}: {e}")
    return False


# 圖床（Cloudflare D1）單筆 BLOB 有上限，整頁截圖（尤其含座位圖的長頁）PNG 常破 1MB，
# INSERT 會回 500。上傳前先確保夠小：太大就等比縮寬 + 轉 JPEG（Worker 依實際位元組回
# 正確 content-type，見 LineBotWorker/src/index.js handleGetImage）。
_MAX_UPLOAD_BYTES = 900_000


def _shrink_for_upload(image_bytes: bytes) -> bytes:
    """截圖 > 上限就壓到上限內：先等比縮寬（1080→900→760），每級再降 JPEG 品質。
    已經夠小就原樣回傳（維持 PNG，tixcraft 小截圖不受影響）。Pillow 出問題就原樣回。"""
    if len(image_bytes) <= _MAX_UPLOAD_BYTES:
        return image_bytes
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        for width in (1080, 900, 760):
            w = min(width, im.width)
            small = im.resize((w, int(im.height * w / im.width)))
            for q in (80, 70, 60, 45):
                buf = io.BytesIO()
                small.save(buf, "JPEG", quality=q)
                if buf.tell() <= _MAX_UPLOAD_BYTES:
                    print(f"[LINE] 截圖 {len(image_bytes)//1024}KB 過大，"
                          f"壓成 JPEG {small.size[0]}px q{q} = {buf.tell()//1024}KB")
                    return buf.getvalue()
        return buf.getvalue()   # 保底：最狠那級還是回出去，讓上傳端決定
    except Exception as e:
        print(f"[LINE] 截圖壓縮失敗（原樣上傳）: {type(e).__name__}: {e}")
        return image_bytes


def upload_screenshot(image_bytes: bytes) -> str | None:
    """上傳截圖到 LineBotWorker 暫存（1 小時後自動失效），回傳可公開存取的網址。
    LINE 圖片訊息一定要公開 HTTPS 網址，本機 Python 沒有，借 Worker 當臨時圖床。失敗回 None。"""
    if not config.LINE_WORKER_URL:
        print("[LINE] 沒設定 LINE_WORKER_URL，略過截圖上傳")
        return None
    image_bytes = _shrink_for_upload(image_bytes)
    try:
        res = requests.post(
            f"{config.LINE_WORKER_URL}/api/screenshot",
            headers={"X-Admin-Key": config.LINE_WORKER_ADMIN_KEY,
                    "Content-Type": "application/octet-stream"},
            data=image_bytes, timeout=15,
        )
        if res.status_code == 200:
            return res.json()["url"]
        print(f"[LINE] 截圖上傳失敗 HTTP {res.status_code}: {res.text[:200]}")
    except Exception as e:
        print(f"[LINE] 截圖上傳例外: {type(e).__name__}: {e}")
    return None


_SCREENSHOT_WIDTH = 1280  # 搶票階段視窗可能為了多開排版開得很窄，截圖前拉回正常桌面寬度


def _ensure_normal_window_state(driver):
    """視窗是 maximized 狀態時，chromedriver 對 set_window_size 常直接噴
    'failed to change window state to normal, current state is maximized'
    （已知限制，不是每次都會踩到但機率不低）。用 CDP 明確把狀態切回 normal 再繼續，
    已經是 normal 時呼叫這個也無害。"""
    try:
        window_info = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        driver.execute_cdp_cmd("Browser.setWindowBounds", {
            "windowId": window_info["windowId"],
            "bounds": {"windowState": "normal"},
        })
    except Exception:
        pass


def _capture_confirmation_area(driver) -> bytes:
    """截「購票會員聯絡資訊」到「我同意本節目規則」按鈕之間的整塊確認區域。

    做法：先確保視窗是 normal 狀態、拉回正常桌面寬度（多開 tile 時視窗可能很窄，
    窄版會 reflow 成擠壓/手機版排版，文字會很小；搶到票時視窗也可能是 maximized
    狀態），再找頭尾兩個文字錨點算出頁面座標，用 CDP 的 `captureBeyondViewport`
    直接截那塊矩形。

    **為什麼不再把視窗拉高**（2026-08-01 搬美東 VPS 時改的）：舊版是把視窗高度拉到
    足以容納整塊再截。在有實體螢幕的 Windows 上沒問題，但 VPS 上 Chrome 跑在 Xvfb
    的虛擬螢幕裡，視窗拉不到螢幕以外 —— 確認頁那塊常常超過螢幕高度，截出來就被裁掉。
    `captureBeyondViewport` 由 renderer 直接算，跟視窗多大、螢幕多大都無關。
    （nodriver 那條路徑 `_capture_page_nodriver` 本來就是這樣做的。）

    抓不到錨點（頁面改版/不同活動版型）就退回「捲到最底 + 一般截圖」，不讓整支流程掛掉。
    """
    try:
        _ensure_normal_window_state(driver)
        driver.set_window_size(_SCREENSHOT_WIDTH, 900)
        time.sleep(0.3)  # 等寬度變化後版面 reflow

        top_el = driver.find_element(By.XPATH, _SCREENSHOT_TOP_XPATH)
        bottom_el = driver.find_element(By.XPATH, _SCREENSHOT_BOTTOM_XPATH)
        rect = driver.execute_script(
            "const t = arguments[0].getBoundingClientRect();"
            "const b = arguments[1].getBoundingClientRect();"
            "return {top: t.top + window.scrollY, bottom: b.bottom + window.scrollY,"
            " width: document.documentElement.clientWidth};",
            top_el, bottom_el)

        top = max(rect["top"] - 30, 0)
        clip = {
            "x": 0,
            "y": top,
            "width": rect["width"],
            "height": rect["bottom"] - top + 30,
            "scale": 1,
        }
        data = driver.execute_cdp_cmd("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True, "clip": clip,
        })
        return base64.b64decode(data["data"])
    except Exception as e:
        print(f"[LINE] 定位確認區塊失敗（改用捲到頁面最底部）: {type(e).__name__}: {e}")

    try:
        _ensure_normal_window_state(driver)
        driver.set_window_size(_SCREENSHOT_WIDTH, 900)
        time.sleep(0.3)
    except Exception:
        pass
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.4)
    return driver.get_screenshot_as_png()


def notify_checkout_from_driver(user_id: str, driver) -> bool:
    """從 selenium driver 截結帳確認區塊並推播給客人（只含瀏覽器 viewport，不含桌面/工具列）。
    證明票真的搶到了。截圖/上傳/推播任何一步失敗都不 raise——票已到手，不能影響後續流程。"""
    if not user_id:
        return False
    try:
        image_bytes = _capture_confirmation_area(driver)
    except Exception as e:
        print(f"[LINE] 結帳頁截圖失敗: {type(e).__name__}: {e}")
        return False
    url = upload_screenshot(image_bytes)
    if not url:
        return False
    return push_image(user_id, url)


async def _capture_page_nodriver(tab, width: int = 1280) -> bytes:
    """nodriver（CDP）版整頁截圖。跟 selenium 的 _capture_confirmation_area 對應，
    但 nodriver 沒有 selenium 那套 find_element/set_window_size，改走 CDP：
      先用 setDeviceMetricsOverride 把 layout 寬度拉到 1280（多開時視窗可能很窄，窄版會
      reflow 成手機排版、字很小），再用 capture_beyond_viewport 一次截完整頁確認資訊。"""
    from nodriver import cdp
    await tab.send(cdp.emulation.set_device_metrics_override(
        width=width, height=900, device_scale_factor=1, mobile=False))
    await asyncio.sleep(0.5)   # 等寬度變化後版面 reflow
    data = await tab.send(cdp.page.capture_screenshot(
        format_="png", capture_beyond_viewport=True))
    try:
        await tab.send(cdp.emulation.clear_device_metrics_override())
    except Exception:
        pass
    return base64.b64decode(data)


async def notify_checkout_from_tab(user_id: str, tab) -> bool:
    """從 nodriver tab 截結帳確認頁並推播給客人（TicketPlus / KKTIX 等 nodriver 平台用）。
    對應 selenium 的 notify_checkout_from_driver。任何一步失敗都不 raise——票已到手。"""
    if not user_id:
        return False
    try:
        image_bytes = await _capture_page_nodriver(tab)
    except Exception as e:
        print(f"[LINE] 結帳頁截圖失敗（nodriver）: {type(e).__name__}: {e}")
        return False
    url = upload_screenshot(image_bytes)
    if not url:
        return False
    return push_image(user_id, url)


# 各平台「自行結帳」指示不同：站名 / 登入網址 / 訂單路徑 / 系統保留分鐘數
_SITE_INFO = {
    "TIXCRAFT":   ("拓元",           "https://tixcraft.com/",        "會員專區 → 訂單管理 → 完成付款",   10),
    "TICKETPLUS": ("遠大 TicketPlus", "https://ticketplus.com.tw/",   "會員中心 → 我的訂單 → 完成付款",   10),
    "KKTIX":      ("KKTIX",          "https://kktix.com/",           "我的票券 → 完成付款",              None),
}


def notify_grabbed(user_id: str, *, slug: str, amount: str, fee: str,
                   platform: str = "TIXCRAFT") -> bool:
    """搶到票的制式通知：匯款資訊 + 自行登入結帳指示。各平台保留時限/登入頁不同。"""
    site_name, site_url, path_hint, hold_min = _SITE_INFO.get(
        platform, _SITE_INFO["TIXCRAFT"])
    limit_txt = f"只保留 {hold_min} 分鐘" if hold_min else "有結帳時限"
    tail_txt = (f"⚠️ 超過 {hold_min} 分鐘票會被系統釋出" if hold_min
                else "⚠️ 逾時票會被系統釋出")
    fee_line = f"{fee} 元" if fee else "（依約定金額）"
    text = (
        "🎫 搶票成功！\n"
        f"活動：{slug}\n"
        f"張數：{amount} 張\n"
        f"票已進入結帳流程，系統{limit_txt}，請立刻完成以下兩步：\n"
        "\n"
        f"1️⃣ 匯款搶票費 {fee_line}\n"
        f"{config.PAYMENT_INFO}\n"
        "備註請打上您的thread名（方便對帳）\n"
        "\n"
        f"2️⃣ 匯款後馬上登入你的{site_name}帳號\n"
        f"{site_url} → {path_hint}\n"
        "\n"
        f"{tail_txt}，請把握時間，匯款後回傳截圖！"
    )
    return push_text(user_id, text)
