"""Step 1: 場次選擇 + T-0 高頻 polling。"""
import time
from dataclasses import dataclass

from curl_cffi import requests as cf_requests

from tixcraftapi import BASE
from tixcraftapi.errors import raise_if_blocked
from tixcraftapi.parsing import (game_not_open, parse_game_area_url,
                                 parse_area_availables)


@dataclass
class PollResult:
    """poll 的成果。`area_html` 有值代表**連 area 頁都已經抓到手**，
    後面可以直接選區、省掉 AREA 那一發 GET（實測 T-0 那發要 3~4 秒）。"""
    area_url: str
    area_html: str | None = None
    source: str = "GAME"


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


class _Target:
    """poll 的一個目標 URL。**每個目標同時最多一發在飛**（不排隊），
    所以「單一 URL 的請求頻率」跟舊版單目標時完全一樣 —— 多打的是「別的 URL」，
    這對 per-endpoint 的速率限制比較安全。"""

    def __init__(self, kind: str, url: str, headers: dict):
        self.kind = kind          # "GAME" / "AREA"
        self.url = url
        self.headers = headers
        self.futures: list = []   # 在飛的請求
        self.next_fire = 0.0
        self.n = 0
        self.rtts: list[float] = []


def _timed_get(session, url, headers):
    t0 = time.monotonic()
    try:
        res = session.get(url, headers=headers, allow_redirects=False, timeout=10)
        return res, (time.monotonic() - t0) * 1000, None
    except Exception as e:
        return None, (time.monotonic() - t0) * 1000, e


def poll_until_open(session: cf_requests.Session, slug: str, headers: dict,
                    max_duration: float = 8.0, interval: float = 0.15,
                    date_keyword: str = "", area_url_hints: list[str] | None = None,
                    pool=None, inflight_per_target: int = 1) -> PollResult | None:
    """T-0 後高頻偵測開賣，抓到就回 `PollResult`。抗本地時鐘偏差，比「睡飽再打一發」更早抓到。

    兩個目標同時 poll：
      - **GAME 頁**：等場次按鈕（data-href）出現 → 回 area_url（舊行為）
      - **AREA 頁**（`area_url_hints`，倒數時用 data-key 預先組好）：一旦回 200 且解析得到
        票區，**那份 HTML 本身就是 AREA 步驟要的東西** → 連 area URL 帶 HTML 一起回，
        FSM 可以直接選區進 TICKET，省掉一整發 GET

    **相位錯開**：多目標時把各自的第一發錯開 interval，兩個 URL 的採樣點才會平均分佈在
    時間軸上（同時發等於白費一半機會），偵測延遲直接砍半。

    `inflight_per_target`（預設 1）：同一個 URL 最多幾發在飛。
    **1 = 每個 URL 的請求頻率跟舊版完全一樣**（= 1/max(interval, RTT)，RTT 300ms 時約 3/s），
    多打的只有「另一個 URL」，對 per-endpoint 的速率限制最安全。
    設 2 才會真的把單一 URL 的採樣率拉到 1/interval —— 但 2026-07-26 實測拓元的 eps 在
    大約 3 req/s 同一 URL 就開始回 403，加倍前先確認你願意承擔被限流的風險。
    沒給 pool 就退回舊的循序模式（行為不變）。
    """
    game_url = f"{BASE}/activity/game/{slug}"
    # AREA 目標排前面：相位錯開時第一個目標最早發射，而 AREA 命中比 GAME 命中值錢
    # （直接省掉一整發 area GET），把最早的那個時槽讓給它
    targets = [_Target("AREA", u, {**headers, "Referer": game_url})
               for u in (area_url_hints or [])]
    targets.append(_Target("GAME", game_url,
                           {**headers, "Referer": f"{BASE}/activity/detail/{slug}"}))
    if len(targets) > 1:
        print(f"[POLL] 同時偵測 {len(targets)} 個 URL（GAME 頁 + {len(targets)-1} 個預測 area 頁）")

    # 在飛的請求總數不能超過 pool 的 worker 數，否則會在 executor 裡排隊 —— 排隊 =
    # 舊的請求佔著位置、新的發不出去，採樣率反而更差（比不重疊還糟）
    if pool is not None:
        cap = max(1, getattr(pool, "size", 2) // len(targets))
        if inflight_per_target > cap:
            print(f"[POLL] inflight {inflight_per_target} → {cap}（pool 只有 "
                  f"{getattr(pool, 'size', 2)} 條熱連線，再多會排隊）")
            inflight_per_target = cap

    def _inspect(t: _Target, res) -> PollResult | None:
        """看這一發夠不夠格宣告「開賣了」。"""
        if res.status_code != 200:
            return None
        html = res.text
        if t.kind == "AREA":
            available = parse_area_availables(html)
            if available:
                print(f"[POLL] #{t.n} 直接命中 AREA 頁（{len(available)} 個區域，省掉一發 GET）: {t.url}")
                return PollResult(area_url=t.url, area_html=html, source="AREA")
            return None
        if game_not_open(html):
            return None
        area_url = parse_game_area_url(html, date_keyword)
        if area_url:
            return PollResult(area_url=area_url, source="GAME")
        return None

    start = time.monotonic()
    deadline = start + max_duration
    last_summary = start
    first_logged = False
    # 相位錯開：第 i 個目標晚 i*interval 才開始，採樣點才會平均分佈
    for i, t in enumerate(targets):
        t.next_fire = start + i * interval

    while time.monotonic() < deadline:
        now = time.monotonic()

        # 1) 該發就發（不等前一發回來）
        for t in targets:
            if len(t.futures) < inflight_per_target and now >= t.next_fire:
                t.next_fire = now + interval
                t.n += 1
                if pool is not None:
                    t.futures.append(pool.submit(_timed_get, session, t.url, t.headers))
                else:
                    t.futures.append(_timed_get(session, t.url, t.headers))  # 循序：直接拿結果

        # 2) 收成
        for t in targets:
            if not t.futures:
                continue
            if pool is not None:
                if not t.futures[0].done():
                    continue
                res, rtt, err = t.futures.pop(0).result()
            else:
                res, rtt, err = t.futures.pop(0)
            t.rtts.append(rtt)
            if not first_logged:
                first_logged = True
                print(f"[POLL] #1 RTT {rtt:.0f}ms（T-0 第一發，session 延遲直接看這裡）")
            if err is not None:
                print(f"[POLL][{t.kind}] #{t.n} 異常: {err}")
                continue
            if res.status_code == 403:
                # 被限流：這個目標退避 3 秒，別把 IP 越打越死（另一個目標照常跑）
                print(f"[POLL][{t.kind}] #{t.n} HTTP 403 被限流 → 該 URL 退避 3s")
                t.next_fire = time.monotonic() + 3.0
                continue
            hit = _inspect(t, res)
            if hit:
                print(f"[POLL] #{t.n} 偵測到開賣 (RTT {rtt:.0f}ms, 來源 {hit.source}): {hit.area_url}")
                return hit
            if res.status_code not in (200, 301, 302):
                print(f"[POLL][{t.kind}] #{t.n} HTTP {res.status_code} ({rtt:.0f}ms)")

        # 3) 每秒吐一次摘要，開賣瞬間卡在哪一段看得出來
        now = time.monotonic()
        if now - last_summary >= 1.0:
            for t in targets:
                if t.rtts:
                    recent = t.rtts[-20:]
                    print(f"[POLL][{t.kind}] {t.n} 次 | RTT avg {sum(recent) / len(recent):.0f}ms"
                          f" / min {min(recent):.0f} / max {max(recent):.0f}")
            last_summary = now

        time.sleep(0.005)   # 讓出 CPU；發射時機由各目標的 next_fire 決定

    total = sum(t.n for t in targets)
    all_rtts = [r for t in targets for r in t.rtts]
    if all_rtts:
        print(f"[POLL] 超時 {max_duration}s（{total} 次，RTT avg {sum(all_rtts) / len(all_rtts):.0f}ms），fallback 走主迴圈")
    else:
        print(f"[POLL] 超時 {max_duration}s（{total} 次全數異常），fallback 走主迴圈")
    return None
