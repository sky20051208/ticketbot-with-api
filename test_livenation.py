"""一次性測試：在 livenation 預購 gating 期間，curl_cffi 帶不同 Referer 打 /game/，
看哪種能拿到完整選票頁（有場次按鈕），哪種拿到縮水版（即將開賣 / 無按鈕）。

跑法：
    python test_livenation.py --config profiles/acc_0/config.json
或：
    編輯下方 CONFIG 區，python test_livenation.py

判讀：
    四個都有「場次按鈕」    → session 已被 server 認可（cookie 已 blessed），Referer 不是 gating
    只有 livenation 有按鈕  → Referer 是 gating 關鍵，純改 header 就能繞過
    四個都沒按鈕            → Referer 沒用，server 看 cookie / IP 狀態，必須走 livenation chain
"""
import argparse
import json
import re
import sys
from pathlib import Path

from curl_cffi import requests as cf_requests

BASE = "https://tixcraft.com"

# ─── 預設 CONFIG（也可從 --config 載 JSON 覆蓋）─────────
SLUG = ""                # 預購活動 slug
COOKIE = ""              # tixcraft cookie 字串
LIVENATION_EVENT_URL = "https://www.livenation.com.tw/event/xxx/"  # 該活動在 livenation 的頁
# ──────────────────────────────────────────────────────


_GAME_BTN_RE = re.compile(r'data-href=["\'][^"\']*?/ticket/area/')


def build_session(cookie_str: str) -> cf_requests.Session:
    sess = cf_requests.Session(impersonate="chrome124")
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            sess.cookies.set(k.strip(), v.strip(), domain="tixcraft.com")
    return sess


def build_headers(referer: str = "") -> dict:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }
    if referer:
        h["Referer"] = referer
    return h


def probe(session, slug: str, label: str, referer: str):
    url = f"{BASE}/activity/game/{slug}"
    print(f"\n── {label} ──")
    print(f"   Referer: {referer or '(無)'}")
    try:
        res = session.get(url, headers=build_headers(referer), timeout=10)
    except Exception as e:
        print(f"   ✗ Request 失敗: {e}")
        return
    html = res.text
    coming_soon = ("即將開賣" in html) or ("coming soon" in html.lower())
    has_btn = bool(_GAME_BTN_RE.search(html))
    print(f"   HTTP : {res.status_code}")
    print(f"   Size : {len(res.content):,} bytes")
    print(f"   即將開賣: {'YES' if coming_soon else 'no'}")
    print(f"   場次按鈕: {'YES (有票)' if has_btn else 'no (無票或被擋)'}")


def main():
    global SLUG, COOKIE, LIVENATION_EVENT_URL

    p = argparse.ArgumentParser()
    p.add_argument("--config", help="JSON profile config 路徑（例 profiles/acc_0/config.json）")
    p.add_argument("--slug", help="覆寫 slug")
    p.add_argument("--livenation", help="覆寫 livenation event URL")
    args = p.parse_args()

    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        SLUG = cfg.get("ACTIVITY_SLUG", SLUG)
        COOKIE = cfg.get("COOKIE", COOKIE)
        LIVENATION_EVENT_URL = cfg.get("LIVENATION_START_URL") or LIVENATION_EVENT_URL
    if args.slug:
        SLUG = args.slug
    if args.livenation:
        LIVENATION_EVENT_URL = args.livenation

    if not SLUG or not COOKIE:
        print("[ERR] 缺 SLUG 或 COOKIE。編輯腳本上方 CONFIG，或用 --config <path>")
        sys.exit(1)

    print(f"target : /activity/game/{SLUG}")
    print(f"cookie : {len(COOKIE)} chars")
    print(f"liven. : {LIVENATION_EVENT_URL}")

    session = build_session(COOKIE)

    probe(session, SLUG, "1. 無 Referer (cold)", "")
    probe(session, SLUG, "2. tixcraft /detail/ (主程式現用)", f"{BASE}/activity/detail/{SLUG}")
    probe(session, SLUG, "3. livenation 根域", "https://www.livenation.com.tw/")
    probe(session, SLUG, "4. livenation 活動頁", LIVENATION_EVENT_URL)

    print("\n── 判讀 ──")
    print("  四個都有「場次按鈕」    → session 已 blessed，Referer 不是 gating")
    print("  只有 3, 4 有按鈕        → Referer 是 gating 關鍵（純 header 能繞）")
    print("  四個都沒按鈕            → Referer 沒用，要 cookie/IP state 配合")


if __name__ == "__main__":
    main()
