"""eps 通關測試 —— 這個 IP 用「bot 真正的請求條件」打拓元，會不會被擋。

bench_vps.py 量的是延遲（裸 socket，被擋是正常的）。這支量的是**能不能用**：
同樣的 curl_cffi impersonate 指紋、同樣的 header，跟 tixcraftapi/session.py 一致。

機房 IP 被 eps 擋是整個搬遷方案唯一的 deal breaker，所以先測這個再談裝 Chrome。

用法（VPS 上）：
    pip install curl_cffi
    python3 bench_eps.py
    python3 bench_eps.py --proxy http://user:pass@host:port   # 走 CliProxy 的對照組
"""
import argparse
import statistics
import sys
import time

from curl_cffi import requests as cf_requests

BASE = "https://tixcraft.com"
# 由淺入深：首頁通常最寬鬆，活動頁次之，/ticket/ 底下的才是搶票真正要打的
PATHS = [
    ("首頁", "/"),
    ("活動列表", "/activity"),
    ("活動頁", "/activity/game/26_joji"),
]
N = 5

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# eps 擋人時吐的東西（不一定是 4xx，有時候 200 但內容是驗證頁）
BLOCK_MARKERS = [
    "Identity Verified",
    "Browsing Activity Has Been Paused",
    "Please enable JS",
    "challenge-platform",
]


def verdict(res):
    """回 (可用嗎, 說明)。

    **401 不是被擋**（2026-07-30 實測釐清）：eps 用 401 表示「你還沒跑過 JS 挑戰、
    沒有 eps_sid」，這是任何乾淨訪客的正常起點——使用者家裡的台灣 IP 打 / 和
    /activity/game/* 也是 401，而 bot 天天用那條線搶到票。

    真正的死刑是 **403 + `Your Browsing Activity Has Been Paused`**：那是 ASN 進黑名單，
    真 Chrome 跑滿 JS 挑戰也拿不到通行證（AWS AS16509 就是這樣）。
    """
    body = res.text or ""
    if res.status_code in (403, 429):
        return False, f"HTTP {res.status_code} 封鎖"
    if res.status_code == 401:
        return True, f"HTTP 401 待挑戰（正常，跟住宅 IP 同待遇）"
    if res.status_code >= 400:
        return False, f"HTTP {res.status_code}"
    for m in BLOCK_MARKERS:
        if m in body:
            return False, f"HTTP {res.status_code} 但內容是擋人頁（{m}）"
    return True, f"HTTP {res.status_code} ({len(body)}B)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None, help="走 proxy 的對照組，例：http://u:p@host:port")
    ap.add_argument("--cookie", default=None,
                    help="帶已通過 eps 挑戰的 cookie（分辨『IP 被封』還是『只是缺 cookie』）")
    ap.add_argument("--interface", default=None,
                    help="綁定來源 IP（OCI 次要私有 IP，例 10.0.0.5）—— 測多顆公網 IP 用")
    args = ap.parse_args()

    session = cf_requests.Session(impersonate="chrome124")
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}
    # curl_cffi 0.14 的 interface= 對應 CURLOPT_INTERFACE，決定封包從哪個本機 IP 出去。
    # 是 per-request 參數，所以收在 dict 裡每發都帶。
    bind = {"interface": args.interface} if args.interface else {}
    if args.cookie:
        HEADERS["Cookie"] = args.cookie

    print("=" * 74)
    print(f"模式        : {'proxy ' + args.proxy.split('@')[-1] if args.proxy else '直連（機房 IP）'}"
          f"{'  綁定來源 ' + args.interface if args.interface else ''}")
    try:
        ip = session.get("https://api.ipify.org", timeout=15, **bind).text
        print(f"出口 IP     : {ip}")
    except Exception as e:
        print(f"出口 IP     : (查不到 {type(e).__name__})")
    print(f"指紋        : chrome124（跟 tixcraftapi/session.py 一致）")
    print(f"Cookie      : {'有帶（' + str(len(args.cookie)) + ' 字元）' if args.cookie else '無'}")
    print("=" * 74)

    all_ok = True
    for label, path in PATHS:
        url = BASE + path
        rtts, res = [], None
        for _ in range(N):
            t0 = time.perf_counter()
            res = session.get(url, headers=HEADERS, timeout=20, allow_redirects=False, **bind)
            rtts.append((time.perf_counter() - t0) * 1000)
        ok, why = verdict(res)
        all_ok = all_ok and ok
        mark = "[OK] " if ok else "[!!] "
        loc = res.headers.get("location", "")
        print(f"{mark}{label:8s} {why:32s} 中位 {statistics.median(rtts):6.0f}ms"
              f"  min {min(rtts):6.0f}ms")
        if loc:
            print(f"       -> redirect: {loc}")
        if not ok:
            # 誰擋的？Fastly edge 自己擋 vs 回源後才擋，處理方式完全不同
            h = res.headers
            print(f"       -> server={h.get('server','-')} via={h.get('via','-')} "
                  f"x-cache={h.get('x-cache','-')}")
            sc = h.get("set-cookie", "")
            if sc:
                print(f"       -> set-cookie: {sc[:120]}")
            body = (res.text or "").strip().replace("\n", " ")
            print(f"       -> body[{len(res.text or '')}B]: {body[:200]!r}")

    print("=" * 74)
    if all_ok:
        print("結論：這個 IP 拿到住宅 IP 等級的待遇（200 / 401 待挑戰），沒被封")
        print("      -> 下一步跑 bench_browser.py，確認瀏覽器過得了挑戰")
    else:
        print("結論：被擋了（403 = ASN 黑名單）。三個選項：")
        print("  1. 搶票時走 CliProxy（--proxy 再跑一次對照，看走 proxy 過不過）")
        print("  2. 換一顆 EIP / 重開機器換 IP 再試（AWS IP 段被擋是整段擋，換 IP 通常無效）")
        print("  3. 這個位置不可用，回頭考慮其他機房")
    print("=" * 74)


if __name__ == "__main__":
    sys.exit(main())
