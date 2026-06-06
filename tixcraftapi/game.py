"""Step 1: 場次選擇 + T-0 高頻 polling。"""
import time

from curl_cffi import requests as cf_requests

from tixcraftapi import BASE, alerts
from tixcraftapi.parsing import parse_game_area_url


def select_game(session: cf_requests.Session, slug: str,
                headers: dict, date_keyword: str = "") -> str | None:
    """從 game 頁面抓場次按鈕的 data-href，回傳 area URL。
    DOM 結構: <button data-href="https://tixcraft.com/ticket/area/{slug}/{id}">
    """
    url = f"{BASE}/activity/game/{slug}"
    res = session.get(url, headers={**headers, "Referer": f"{BASE}/activity/detail/{slug}"})

    if res.status_code != 200:
        print(f"[GAME] HTTP {res.status_code}")
        if res.status_code == 403:
            alerts.play_403("GAME")
            raise alerts.Blocked403("GAME")
        return None

    html = res.text

    if "即將開賣" in html or "coming soon" in html.lower():
        print("[GAME] 尚未開賣")
        return None

    area_url = parse_game_area_url(html, date_keyword)
    if area_url:
        print(f"[GAME] 選中場次: {area_url}")
        return area_url

    print("[GAME] 找不到任何場次")
    return None


def poll_until_open(session: cf_requests.Session, slug: str, headers: dict,
                    max_duration: float = 8.0, interval: float = 0.15,
                    date_keyword: str = "") -> str | None:
    """T-0 後高頻打 game page，看到場次按鈕立刻回傳 area_url。
    抗本地時鐘偏差 + 比 sleep 完再單次 GET 更早抓到開賣瞬間。"""
    url = f"{BASE}/activity/game/{slug}"
    poll_headers = {**headers, "Referer": f"{BASE}/activity/detail/{slug}"}
    deadline = time.monotonic() + max_duration
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            res = session.get(url, headers=poll_headers)
            if res.status_code == 200:
                html = res.text
                if "即將開賣" not in html and "coming soon" not in html.lower():
                    area_url = parse_game_area_url(html, date_keyword)
                    if area_url:
                        print(f"[POLL] #{attempt} 偵測到開賣: {area_url}")
                        return area_url
        except Exception as e:
            print(f"[POLL] #{attempt} 異常: {e}")
        time.sleep(interval)
    print(f"[POLL] 超時 {max_duration}s（{attempt} 次），fallback 走主迴圈")
    return None
