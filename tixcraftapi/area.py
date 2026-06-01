"""Step 3: 區域選擇（內部會處理被 redirect 到驗證頁的情況）。"""
import json
import random
import re

from curl_cffi import requests as cf_requests

import config
from tixcraftapi import BASE
from tixcraftapi.captcha import CaptchaPrefetch
from tixcraftapi.verify import handle_verify


def select_area(session: cf_requests.Session, area_url: str,
                headers: dict, area_keyword: str = "") -> str | None:
    """從 area 頁面抓票區連結。
    <a id="21567_22"> 的 id 對應 JS 變數 areaUrlList 裡的 key。
    已售完的區域沒有 <a> 標籤。
    """
    # 背景啟動 captcha prefetch（GET 圖 + OCR 一條龍）。
    # 從這裡到 submit_ticket POST 之間，session 不可再 GET /ticket/captcha
    # （server 只認最後一張），這條約束在 submit.py 第一輪邏輯內維持。
    captcha_headers = {**headers, "Referer": area_url}
    session._captcha_prefetch = CaptchaPrefetch(session, captcha_headers)

    res = session.get(area_url, headers={**headers, "Referer": area_url},
                      allow_redirects=False)

    # 可能被 redirect 到驗證頁
    if res.status_code in (301, 302):
        loc = res.headers.get("Location", "")
        full_loc = loc if loc.startswith("http") else BASE + loc
        if "verify" in loc:
            print(f"[AREA] 被導向驗證頁: {full_loc}")
            if handle_verify(session, full_loc, headers):
                res = session.get(area_url, headers={**headers, "Referer": full_loc})
            else:
                return None
        else:
            print(f"[AREA] 被導向: {full_loc}")
            return None

    if res.status_code != 200:
        print(f"[AREA] HTTP {res.status_code}")
        return None

    html = res.text

    # 1. 抓 areaUrlList JS 變數。可能是 dict、空陣列 [] 或逐筆 areaUrlList[id]=url。
    url_list_m = re.search(r'areaUrlList\s*=\s*(\{.*?\})', html, re.DOTALL)
    if not url_list_m:
        assigns = re.findall(
            r'areaUrlList\[(["\']?)([^\]"\']+)\1\]\s*=\s*["\']([^"\']+)["\']', html
        )
        if assigns:
            area_urls = {key: val for _, key, val in assigns}
            url_list_m = True
        else:
            print("[AREA] 無可購買區域（全部售完或尚未開賣）")
            return None
    else:
        try:
            area_urls = json.loads(url_list_m.group(1))
        except json.JSONDecodeError:
            print("[AREA] areaUrlList JSON 解析失敗")
            return None

    # 2. 抓所有可購買的 <a id="..."> 標籤及其文字（含區域名稱和狀態）
    available = []
    for m in re.finditer(
        r'<a\s+id=["\'](\d+_\d+)["\'][^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        area_id = m.group(1)
        area_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if area_id in area_urls:
            available.append((area_id, area_text, area_urls[area_id]))

    if not available:
        print("[AREA] 沒有可購買的區域")
        return None

    # 3. 排除關鍵字過濾
    exclude_kw = config.EXCLUDE_AREA_KEYWORD
    if exclude_kw:
        exclude_list = [kw.strip() for kw in exclude_kw.split(";") if kw.strip()]
        before = len(available)
        available = [
            (aid, text, url) for aid, text, url in available
            if not any(ex in text for ex in exclude_list)
        ]
        if len(available) < before:
            print(f"[AREA] 排除 {before - len(available)} 個區域 (排除詞: {', '.join(exclude_list[:3])}...)")

    if not available:
        print("[AREA] 排除後沒有可購買的區域")
        return None

    # 4. 印 summary（過去會列出所有 54 個區域，hot path 上拖時間 ~10-20ms，砍掉）
    print(f"[AREA] 找到 {len(available)} 個有票區域")

    # 5. 依選位策略選區
    strategy = config.AREA_AUTO_SELECT_MODE
    print(f"[AREA] 策略: {strategy} | 關鍵字: {area_keyword}")

    if strategy == "關鍵字優先" and area_keyword:
        filtered = [(aid, text, url) for aid, text, url in available
                     if area_keyword in text]
        if filtered:
            selected = filtered[0]
        else:
            print(f"[AREA] 關鍵字 '{area_keyword}' 無匹配，fallback 選第一個")
            selected = available[0]
    elif strategy == "由下而上":
        selected = available[-1]
    elif strategy == "隨機":
        selected = random.choice(available)
    else:
        selected = available[0]

    aid, text, ticket_url = selected
    print(f"[AREA] 選中: {text} -> {ticket_url}")
    return ticket_url
