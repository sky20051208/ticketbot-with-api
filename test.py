import re
import sys
import time
import json
import random
import datetime
import tempfile

from curl_cffi import requests as cf_requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import ddddocr
import config

BASE = "https://tixcraft.com"


# ==========================================
# 工具函式
# ==========================================

def build_session(cookie_str: str) -> cf_requests.Session:
    session = cf_requests.Session(impersonate="chrome124")
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            session.cookies.set(k.strip(), v.strip(), domain="tixcraft.com")
    return session


def build_headers(referer: str = "") -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": BASE,
        "Referer": referer or BASE,
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }


def parse_ticket_form(html: str) -> dict:
    payload = {}
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            name = name_m.group(1)
            value = val_m.group(1) if val_m else ""
            if "_csrf" in name or "TicketForm" in name:
                payload[name] = value
    return payload


def find_ticket_codes(payload: dict) -> list[str]:
    codes = []
    for k in payload:
        m = re.search(r'TicketForm\[(?:ticketPrice|priceSize)\]\[(\w+)\]', k)
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
    return codes


# ==========================================
# Step 1: 選場次 — GET /activity/game/{slug}
# ==========================================
def select_game(session: cf_requests.Session, slug: str,
                headers: dict, date_keyword: str = "") -> str | None:
    """
    從 game 頁面抓場次按鈕的 data-href，回傳 area URL。
    DOM 結構: <button data-href="https://tixcraft.com/ticket/area/{slug}/{id}">
    """
    url = f"{BASE}/activity/game/{slug}"
    res = session.get(url, headers={**headers, "Referer": f"{BASE}/activity/detail/{slug}"})

    if res.status_code != 200:
        print(f"[GAME] HTTP {res.status_code}")
        return None

    html = res.text

    # 檢查是否尚未開賣
    if "即將開賣" in html or "coming soon" in html.lower():
        print("[GAME] 尚未開賣")
        return None

    # 抓所有場次: <tr ... data-key="..."> 裡的 <button data-href="...">
    # 同時抓該 row 的文字內容來做日期篩選
    rows = re.findall(
        r'<tr[^>]*class="gridc[^"]*"[^>]*>(.*?)</tr>',
        html, re.DOTALL
    )

    for row_html in rows:
        href_m = re.search(r'data-href=["\']([^"\']+)["\']', row_html)
        if not href_m:
            continue

        area_url = href_m.group(1)

        # 日期篩選
        if date_keyword and date_keyword not in row_html:
            continue

        print(f"[GAME] 選中場次: {area_url}")
        return area_url

    # fallback: 直接抓任何 data-href 指向 /ticket/area/ 的
    fallback = re.search(r'data-href=["\']([^"\']*ticket/area/[^"\']+)["\']', html)
    if fallback:
        url = fallback.group(1)
        print(f"[GAME] fallback 場次: {url}")
        return url

    print("[GAME] 找不到任何場次")
    return None


# ==========================================
# Step 2: 驗證頁（presale code）— 如果被 redirect 到 verify
# ==========================================
def handle_verify(session: cf_requests.Session, verify_url: str,
                  headers: dict) -> bool:
    """
    處理 presale code 驗證頁。
    POST check-code endpoint 帶 _csrf + checkCode。
    """
    res = session.get(verify_url, headers=headers)
    if res.status_code != 200:
        print(f"[VERIFY] GET 失敗 HTTP {res.status_code}")
        return False

    html = res.text

    # 抽取 CSRF
    csrf_m = re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)', html)
    if not csrf_m:
        print("[VERIFY] 找不到 _csrf")
        return False
    csrf = csrf_m.group(1)

    # 嘗試從頁面抓答案（有些驗證頁會直接顯示答案在【】裡）
    ans_m = re.search(r'【([^】]+)】', html)
    if ans_m:
        answer = ans_m.group(1)
        print(f"[VERIFY] 頁面找到答案: {answer}")
    elif config.PRESALE_CODE:
        answer = config.PRESALE_CODE
        print(f"[VERIFY] 使用 config 會員碼: {answer[:3]}***")
    else:
        print("[VERIFY] 沒有 presale code，跳過")
        return False

    # 判斷 POST 目標: /activity/verify/ → /activity/check-code/
    #                  /ticket/verify/  → /ticket/check-code/
    check_url = verify_url.replace("/verify/", "/check-code/")

    post_res = session.post(check_url, data={
        "_csrf": csrf,
        "checkCode": answer,
    }, headers={**headers, "Referer": verify_url}, allow_redirects=False)

    print(f"[VERIFY] POST 回應: {post_res.status_code}")

    if post_res.status_code in (200, 301, 302):
        return True
    return False


# ==========================================
# Step 3: 選區域 — GET /ticket/area/{slug}/{id}
# ==========================================
def select_area(session: cf_requests.Session, area_url: str,
                headers: dict, area_keyword: str = "") -> str | None:
    """
    從 area 頁面抓票區連結。
    <a id="21567_22"> 的 id 對應 JS 變數 areaUrlList 裡的 key。
    已售完的區域沒有 <a> 標籤。
    """
    res = session.get(area_url, headers={**headers, "Referer": area_url},
                      allow_redirects=False)

    # 可能被 redirect 到驗證頁
    if res.status_code in (301, 302):
        loc = res.headers.get("Location", "")
        full_loc = loc if loc.startswith("http") else BASE + loc
        if "verify" in loc:
            print(f"[AREA] 被導向驗證頁: {full_loc}")
            if handle_verify(session, full_loc, headers):
                # 驗證成功，重新 GET area
                res = session.get(area_url, headers=headers)
            else:
                return None
        else:
            print(f"[AREA] 被導向: {full_loc}")
            return None

    if res.status_code != 200:
        print(f"[AREA] HTTP {res.status_code}")
        return None

    html = res.text

    # 1. 抓 areaUrlList JS 變數
    url_list_m = re.search(r'areaUrlList\s*=\s*(\{.*?\})', html, re.DOTALL)
    if not url_list_m:
        print("[AREA] 找不到 areaUrlList")
        return None

    try:
        area_urls = json.loads(url_list_m.group(1))
    except json.JSONDecodeError:
        print("[AREA] areaUrlList JSON 解析失敗")
        return None

    # 2. 抓所有可購買的 <a id="..."> 標籤及其文字（含區域名稱和狀態）
    #    有票的: <li class="select_form_b"><a id="21567_22">...1樓特B區8800 剩餘 18...</a>
    #    售完的: <li><span>...已售完...</span> （沒有 <a> 標籤）
    available = []
    for m in re.finditer(
        r'<a\s+id=["\'](\d+_\d+)["\'][^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        area_id = m.group(1)
        area_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()  # 去 HTML tag
        if area_id in area_urls:
            available.append((area_id, area_text, area_urls[area_id]))

    if not available:
        print("[AREA] 沒有可購買的區域")
        return None

    # 3. 印出所有可選區域
    print(f"[AREA] 找到 {len(available)} 個有票區域:")
    for aid, text, url in available:
        print(f"  {aid}: {text}")

    # 4. 用關鍵字篩選
    if area_keyword:
        filtered = [(aid, text, url) for aid, text, url in available
                     if area_keyword in text]
        if filtered:
            selected = filtered[0]
        else:
            print(f"[AREA] 關鍵字 '{area_keyword}' 無匹配，選第一個")
            selected = available[0]
    else:
        selected = available[0]

    aid, text, ticket_url = selected
    print(f"[AREA] 選中: {text} -> {ticket_url}")
    return ticket_url


# ==========================================
# 精準計時器
# ==========================================
def wait_until_start(target_time_str: str, advance_ms: int):
    now = datetime.datetime.now()
    target = datetime.datetime.strptime(
        now.strftime("%Y-%m-%d") + " " + target_time_str,
        "%Y-%m-%d %H:%M:%S",
    ) - datetime.timedelta(milliseconds=advance_ms)

    if now > target:
        print("[TIMER] 目標時間已過，直接啟動")
        return

    print(f"[TIMER] 待命中 目標: {target_time_str} (提前 {advance_ms}ms)")
    while True:
        now = datetime.datetime.now()
        remaining = (target - now).total_seconds()
        if now >= target:
            print(f"\n[TIMER] 啟動! {now.strftime('%H:%M:%S.%f')[:-3]}")
            break
        if remaining > 1:
            print(f"\r[TIMER] 倒數: {remaining:.1f}s", end="")
            time.sleep(0.5)
        else:
            print(f"\r[TIMER] 倒數: {remaining:.3f}s", end="")


# ==========================================
# 驗證碼辨識（帶重試）
# ==========================================
def solve_captcha(session: cf_requests.Session, ocr: ddddocr.DdddOcr,
                  headers: dict, max_retry: int = config.CAPTCHA_MAX_RETRY) -> str:
    result = "0000"
    for attempt in range(1, max_retry + 1):
        try:
            resp = session.get(
                f"{BASE}/ticket/captcha",
                params={"refresh": str(random.random())},
                headers=headers,
            )
            if resp.status_code != 200:
                print(f"[CAPTCHA] #{attempt} 取圖失敗 HTTP {resp.status_code}")
                continue

            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                data = resp.json()
                img_url = data.get("url", "")
                if not img_url:
                    continue
                img_resp = session.get(f"{BASE}{img_url}", headers=headers)
                img_bytes = img_resp.content
            else:
                img_bytes = resp.content

            if len(img_bytes) < 100:
                continue

            result = ocr.classification(img_bytes)
            print(f"[CAPTCHA] #{attempt} 辨識: {result} ({len(result)} chars)")

            if len(result) == 4:
                return result

        except Exception as e:
            print(f"[CAPTCHA] #{attempt} 異常: {e}")

    print("[CAPTCHA] 重試耗盡，使用最後結果")
    return result


# ==========================================
# 排隊輪詢 / 訂單結果處理
# ==========================================
def follow_order(session: cf_requests.Session, redirect_url: str,
                 headers: dict) -> str | None:
    """
    POST 成功後被 302 到某個 URL，follow 它並判斷結果。
    可能的情況:
    - 直接到 checkout → 成功
    - 到 /ticket/order → 可能需要排隊，也可能直接有付款表單
    - 被踢回 → 失敗
    """
    print(f"[ORDER] 跟隨 redirect: {redirect_url}")

    # 先 follow 一次看實際內容
    resp = session.get(redirect_url,
                       headers={**headers, "Referer": redirect_url},
                       allow_redirects=True)
    final_url = resp.url
    html = resp.text
    status = resp.status_code

    print(f"[ORDER] 最終 URL: {final_url} (HTTP {status})")

    # 直接到結帳
    if "checkout" in final_url:
        print("[ORDER] 直接結帳!")
        return final_url

    # 被踢回登入
    if "login" in final_url:
        print("[ORDER] Cookie 失效")
        return None

    # 被踢回活動頁或售罄
    if "activity" in final_url or "game" in final_url:
        print("[ORDER] 被踢回活動頁（售罄或異常）")
        return None

    # 到了 /ticket/order 頁面 — 檢查是排隊還是已經有表單
    if "/ticket/order" in final_url or "/order" in final_url:
        # 檢查頁面是否已經有付款表單（不需要排隊）
        if "deliveryType" in html or "paymentId" in html or "checkout" in html:
            print("[ORDER] 付款表單已出現!")
            return final_url

        # 需要排隊，開始輪詢
        print("[ORDER] 進入排隊...")
        return _poll_order_loop(session, final_url, headers)

    # 未知頁面，印出一段內容幫助 debug
    print(f"[ORDER] 未知頁面，前 500 字:")
    clean = re.sub(r'<[^>]+>', ' ', html)
    print(clean[:500])
    return None


def _poll_order_loop(session: cf_requests.Session, order_url: str,
                     headers: dict) -> str | None:
    check_url = f"{BASE}/ticket/check"

    for i in range(1, config.ORDER_POLL_MAX + 1):
        try:
            # 打 /ticket/check API 拿 JSON 狀態
            check_resp = session.get(check_url, headers={
                **headers,
                "Referer": order_url,
                "X-Requested-With": "XMLHttpRequest",
            })

            ts = time.strftime('%H:%M:%S')

            # 嘗試解析 JSON
            try:
                data = check_resp.json()
                waiting = data.get("waiting", None)
                msg = data.get("message", "")
                print(f"  [{ts}] 輪詢 #{i} | HTTP {check_resp.status_code} | waiting={waiting} | msg={msg[:80]}")

                # 排隊結束
                if waiting is False or waiting == 0:
                    # 嘗試從 message 抓 redirect URL
                    loc_m = re.search(r"location\.(?:replace|href)\s*[=(]\s*['\"]([^'\"]+)", msg)
                    if loc_m:
                        target = loc_m.group(1)
                        full = target if target.startswith("http") else BASE + target
                        print(f"[QUEUE] #{i} JSON 指示跳轉: {full}")
                        return full

                    # 沒有 redirect，直接 GET order 頁看看
                    resp = session.get(order_url, headers=headers, allow_redirects=True)
                    if "checkout" in resp.url:
                        print(f"[QUEUE] #{i} 導向結帳: {resp.url}")
                        return resp.url
                    if "deliveryType" in resp.text or "paymentId" in resp.text:
                        print(f"[QUEUE] #{i} 表單出現!")
                        return resp.url

                    print(f"[QUEUE] #{i} 排隊結束但無結帳頁")
                    return None

            except (ValueError, KeyError):
                # 不是 JSON，印出原始回應
                body = check_resp.text[:200]
                print(f"  [{ts}] 輪詢 #{i} | HTTP {check_resp.status_code} | 非 JSON: {body}")

        except Exception as e:
            print(f"[QUEUE] #{i} 異常: {e}")

        time.sleep(config.ORDER_POLL_INTERVAL)

    print("[QUEUE] 輪詢超時")
    return None


# ==========================================
# Step 4: 送出訂單表單（驗證碼錯會在內部重試）
# ==========================================
def submit_ticket(session: cf_requests.Session, ocr: ddddocr.DdddOcr,
                  ticket_url: str, headers: dict) -> str | None:
    captcha_headers = {**headers, "Referer": ticket_url}

    for round_n in range(1, config.TICKET_CAPTCHA_RETRY + 1):
        # 每輪都重新 GET 頁面（拿新的 _csrf 和新的表單狀態）
        res = session.get(ticket_url, headers=headers)
        if res.status_code != 200:
            print(f"[TICKET] GET 失敗 HTTP {res.status_code}")
            return None

        html = res.text
        payload = parse_ticket_form(html)

        if "_csrf" not in str(payload):
            print("[TICKET] 找不到 _csrf，Cookie 可能過期")
            return None

        codes = find_ticket_codes(payload)
        if not codes:
            print("[TICKET] 找不到票區代碼（尚未開賣？）")
            return None

        code = codes[0]
        if round_n == 1:
            print(f"[TICKET] 票區代碼: {code}")

        # 辨識驗證碼
        verify_code = solve_captcha(session, ocr, captcha_headers)

        # 組裝 payload
        payload[f"TicketForm[ticketPrice][{code}]"] = config.TICKET_AMOUNT
        payload[f"TicketForm[priceSize][{code}]"] = config.TICKET_AMOUNT
        payload["TicketForm[verifyCode]"] = verify_code
        payload["TicketForm[agree]"] = "1"

        # POST
        post_res = session.post(ticket_url, data=payload, headers=headers,
                                allow_redirects=False)
        status = post_res.status_code
        loc = post_res.headers.get("Location", "")
        print(f"[TICKET] 第{round_n}輪 POST: {status} -> {loc}")

        # 302/301 = 成功（不管導去哪，交給主程式判斷）
        if status in (301, 302) and loc:
            return loc if loc.startswith("http") else BASE + loc

        # 200 = 驗證碼錯或其他表單錯誤，換一張重來
        if status == 200:
            print(f"[TICKET] 驗證碼錯誤，換一張重試 ({round_n}/{config.TICKET_CAPTCHA_RETRY})")
            continue

        # 其他狀態碼
        print(f"[TICKET] 未預期狀態碼: {status}")
        return None

    print("[TICKET] 驗證碼重試耗盡")
    return None


# ==========================================
# 主程式
# ==========================================
def main():
    sys.stdout.reconfigure(line_buffering=True)

    if not config.COOKIE:
        print("[ERROR] 請先在 config.py 填入 COOKIE")
        return

    session = build_session(config.COOKIE)
    ocr = ddddocr.DdddOcr(show_ad=False)

    # 定時等待
    wait_until_start(config.TARGET_START_TIME, config.ADVANCE_MS)

    retry_cnt = 0
    try:
        while True:
            retry_cnt += 1
            ts = time.strftime('%H:%M:%S')
            print(f"\n[{ts}] === 第 {retry_cnt} 次嘗試 ===")

            # --- Step 1: 選場次 ---
            headers = build_headers(referer=f"{BASE}/activity/detail/{config.ACTIVITY_SLUG}")
            area_url = select_game(session, config.ACTIVITY_SLUG, headers, config.DATE_KEYWORD)
            if not area_url:
                time.sleep(config.RETRY_INTERVAL)
                continue

            # --- Step 3: 選區域（Step 2 驗證頁在 select_area 內部自動處理）---
            headers = build_headers(referer=area_url)
            ticket_url = select_area(session, area_url, headers, config.AREA_KEYWORD)
            if not ticket_url:
                time.sleep(config.RETRY_INTERVAL)
                continue

            # --- Step 4: 送出表單 ---
            headers = build_headers(referer=ticket_url)
            redirect_url = submit_ticket(session, ocr, ticket_url, headers)

            if redirect_url is None:
                time.sleep(config.RETRY_INTERVAL)
                continue

            # 跟隨 redirect，判定結果
            result_url = follow_order(session, redirect_url, headers)
            if result_url:
                print(f"[SUCCESS] 搶到了! {result_url}")
                open_chrome_with_session(session, result_url)
                break
            else:
                print("[FAIL] 未成功，重試")

            time.sleep(config.RETRY_INTERVAL)

    except KeyboardInterrupt:
        print("\n[EXIT] 手動中斷")


def open_chrome_with_session(session: cf_requests.Session, target_url: str):
    """開一個乾淨的 Chrome，注入 session cookie，跳轉到目標頁面"""
    tmp_dir = tempfile.mkdtemp(prefix="tixcraft_chrome_")
    print(f"[CHROME] 隔離 Profile: {tmp_dir}")

    opts = Options()
    opts.add_argument(f"--user-data-dir={tmp_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=opts)

    # 先開 tixcraft 首頁讓 domain 生效
    driver.get("https://tixcraft.com")
    time.sleep(0.5)
    driver.delete_all_cookies()

    # 注入 curl_cffi session 中的所有 cookie
    for name, value in session.cookies.items():
        try:
            driver.add_cookie({
                "name": name,
                "value": value,
                "domain": "tixcraft.com",
                "path": "/",
            })
        except Exception:
            pass

    print(f"[CHROME] Cookie 注入完畢，跳轉: {target_url}")
    driver.get(target_url)

    input("\n按 Enter 關閉瀏覽器...")
    driver.quit()


if __name__ == "__main__":
    main()
