"""Step 1: 場次選擇 + T-0 高頻 polling。"""
import time

from curl_cffi import requests as cf_requests

from tixcraftapi import BASE
from tixcraftapi.errors import raise_if_blocked
from tixcraftapi.parsing import game_not_open, parse_game_area_url


def select_game(session: cf_requests.Session, slug: str,
                headers: dict, date_keyword: str = "") -> str | None:
    """從 game 頁面抓場次按鈕的 data-href，回傳 area URL。
    DOM 結構: <button data-href="https://tixcraft.com/ticket/area/{slug}/{id}">
    """
    url = f"{BASE}/activity/game/{slug}"
    res = session.get(url, headers={**headers, "Referer": f"{BASE}/activity/detail/{slug}"})

    raise_if_blocked(res, "GAME")
    if res.status_code != 200:
        print(f"[GAME] HTTP {res.status_code}")
        return None

    html = res.text

    if game_not_open(html):
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
    抗本地時鐘偏差 + 比 sleep 完再單次 GET 更早抓到開賣瞬間。

    節奏用 wall-clock 對齊：sleep 只補「interval 扣掉 RTT」的剩餘時間。
    （舊寫法是回應完才睡滿 interval，RTT 300ms 時採樣率掉到 1/0.45s；
    session 延遲越大掉越多 — 對齊後採樣率固定 1/interval，RTT 只影響「看到結果的時差」。）
    """
    url = f"{BASE}/activity/game/{slug}"
    poll_headers = {**headers, "Referer": f"{BASE}/activity/detail/{slug}"}
    deadline = time.monotonic() + max_duration
    attempt = 0
    rtts: list[float] = []
    last_summary = time.monotonic()
    while time.monotonic() < deadline:
        attempt += 1
        t_req = time.monotonic()
        try:
            res = session.get(url, headers=poll_headers)
            rtt = (time.monotonic() - t_req) * 1000
            rtts.append(rtt)
            if attempt == 1:
                print(f"[POLL] #1 RTT {rtt:.0f}ms（T-0 第一發，session 延遲直接看這裡）")
            if res.status_code == 200:
                html = res.text
                if not game_not_open(html):
                    area_url = parse_game_area_url(html, date_keyword)
                    if area_url:
                        print(f"[POLL] #{attempt} 偵測到開賣 (RTT {rtt:.0f}ms): {area_url}")
                        return area_url
            else:
                print(f"[POLL] #{attempt} HTTP {res.status_code} ({rtt:.0f}ms)")
        except Exception as e:
            print(f"[POLL] #{attempt} 異常: {e}")

        # 每秒吐一次 RTT 摘要，開賣瞬間卡在哪一段看得出來
        now = time.monotonic()
        if now - last_summary >= 1.0 and rtts:
            recent = rtts[-20:]
            print(f"[POLL] {attempt} 次 | RTT avg {sum(recent) / len(recent):.0f}ms"
                  f" / min {min(recent):.0f} / max {max(recent):.0f}")
            last_summary = now

        elapsed = time.monotonic() - t_req
        if elapsed < interval:
            time.sleep(interval - elapsed)

    if rtts:
        print(f"[POLL] 超時 {max_duration}s（{attempt} 次，RTT avg {sum(rtts) / len(rtts):.0f}ms），fallback 走主迴圈")
    else:
        print(f"[POLL] 超時 {max_duration}s（{attempt} 次全數異常），fallback 走主迴圈")
    return None
