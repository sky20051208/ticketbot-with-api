"""拓元搶票鏈路的「時間花在哪」完整分解 —— 一次把十個懷疑對象全部量出來。

為什麼要這支
────────────
既有的 bench 只告訴你「這發花了 500ms」，但那 500ms 裡 DNS 佔多少、TLS 佔多少、
伺服器想多久、解析花多少，全看不到。沒有分解就只能猜，而猜錯的代價是花時間優化了
根本不是瓶頸的地方（例如「換更小的 endpoint」—— 實測回應大小跟耗時完全無關）。

**為什麼用底層 `Curl` 而不是 `requests.Session`**：curl 本身會記錄每個階段的時間戳
（NAMELOOKUP → CONNECT → APPCONNECT(TLS) → PRETRANSFER → STARTTRANSFER(TTFB) → TOTAL），
但 curl_cffi 的 requests 層在請求結束後會 `reset()` handle，`getinfo` 全部歸零 ——
所以要自己拿 `Curl` 打。實測 `reset()` 不會清掉連線池，連線重用照常。

**一定要帶真實 cookie**：沒 cookie 打拓元會被 eps 擋成 HTTP 401（挑戰頁），
那個回應的伺服器時間跟真正的 GAME 頁完全不同，量了也沒意義。用 `--profile <名稱>`
開該 Chrome profile 抓 cookie。

**對拓元很客氣**：CLAUDE.md 記載同一個 URL 約 3 req/s 就開始 403，所以取樣數刻意壓低、
每發之間都有間隔。跑一次約 60~90 秒。

用法
────
    python bench_tixcraft_profile.py --profile 李小美
    python bench_tixcraft_profile.py --profile 李小美 --slug 26_joji
    python bench_tixcraft_profile.py --cookie-file ck.txt   # 已經有 cookie 字串
    python bench_tixcraft_profile.py                        # 沒 cookie（只會拿到 401）
"""
import argparse
import platform
import statistics
import threading
import time
from io import BytesIO
from pathlib import Path

from curl_cffi import Curl, CurlInfo, CurlOpt

import config
from tixcraftapi import BASE, parsing
from tixcraftapi.session import build_headers

GAP = 1.2          # 每發之間的間隔（秒）—— 拓元 ~3 req/s 就 403，別打太兇
PROFILES_DIR = Path(__file__).parent / "chrome_profiles" / "tixcraft"

# curl 的 CURL_HTTP_VERSION_* 常數
_HTTP_VER = {0: "未知", 1: "HTTP/1.0", 2: "HTTP/1.1", 3: "HTTP/2", 30: "HTTP/3"}


class Probe:
    """用底層 Curl 打，才拿得到各階段時間。同一個 handle 重複用 = 連線會被重用。"""

    def __init__(self, cookie: str = "", impersonate: str = "chrome124"):
        self.c = Curl()
        self.cookie = cookie
        self.imp = impersonate

    def get(self, url: str, referer: str = "") -> dict | None:
        buf = BytesIO()
        c = self.c
        try:
            c.setopt(CurlOpt.URL, url.encode())
            c.setopt(CurlOpt.WRITEDATA, buf)
            c.setopt(CurlOpt.FOLLOWLOCATION, 1)
            c.setopt(CurlOpt.TIMEOUT, 25)
            # 空字串 = 「所有我支援的壓縮」，**而且叫 curl 自動解壓**。
            # 不設的話 impersonate 仍會送 Accept-Encoding，但 curl 把壓縮後的 bytes
            # 原樣丟給我們 —— 量到的「下載時間/大小」會是壓縮值，HTML 也解析不了。
            c.setopt(CurlOpt.ACCEPT_ENCODING, b"")
            c.setopt(CurlOpt.HTTPHEADER,
                     [f"{k}: {v}".encode() for k, v in build_headers(referer).items()])
            if self.cookie:
                c.setopt(CurlOpt.COOKIE, self.cookie.encode())
            c.impersonate(self.imp)
            c.perform()
        except Exception as e:
            print(f"    ✗ {type(e).__name__}: {e}")
            c.reset()
            return None

        g = lambda k: (c.getinfo(k) or 0)          # noqa: E731
        dns, conn, tls = (g(CurlInfo.NAMELOOKUP_TIME), g(CurlInfo.CONNECT_TIME),
                          g(CurlInfo.APPCONNECT_TIME))
        pre, ttfb, total = (g(CurlInfo.PRETRANSFER_TIME), g(CurlInfo.STARTTRANSFER_TIME),
                            g(CurlInfo.TOTAL_TIME))
        out = {
            "dns": dns * 1000,
            "tcp": (conn - dns) * 1000 if conn else 0.0,
            "tls": (tls - conn) * 1000 if tls else 0.0,
            # 請求送出 → 第一個 byte 回來：伺服器 render + 一趟往返
            "server": (ttfb - pre) * 1000 if ttfb else 0.0,
            "download": (total - ttfb) * 1000 if total else 0.0,
            "total": total * 1000,
            "size": len(buf.getvalue()),
            "http": _HTTP_VER.get(g(CurlInfo.HTTP_VERSION), f"?{g(CurlInfo.HTTP_VERSION)}"),
            "status": g(CurlInfo.RESPONSE_CODE),
            "redirects": g(CurlInfo.REDIRECT_COUNT),
            "text": buf.getvalue().decode("utf-8", "replace"),
        }
        c.reset()      # reset 只清 option，不清連線池（實測第 2 發仍然重用）
        return out


HEAD = (f"  {'':<20}{'總計':>8} │{'DNS':>6}{'TCP':>7}{'TLS':>7}{'伺服器':>9}{'下載':>7}"
        f" │ {'大小':>9}  協定      狀態")


def row(label, t):
    if t is None:
        return
    warn = "  ← 401=eps挑戰頁，數字不代表真實頁面" if t["status"] == 401 else ""
    print(f"  {label:<20}{t['total']:>7.0f}ms │{t['dns']:>6.0f}{t['tcp']:>7.0f}"
          f"{t['tls']:>7.0f}{t['server']:>9.0f}{t['download']:>7.0f} │"
          f"{t['size']:>9,}B  {t['http']:<8}{t['status']}{warn}")


def cookies_from_profile(name: str, target_url: str) -> str:
    """開該 Chrome profile 抓 tixcraft cookie（含 eps 挑戰通過後的 eps_sid）。

    **一定要讓瀏覽器先載入「要量的那一頁」**，不能只開首頁 —— 實測只開首頁拿到的
    cookie 打 GAME 頁還是 401（挑戰是按路徑跑的），量到的就會是挑戰頁而不是真頁面。
    """
    udd = PROFILES_DIR / name
    if not udd.exists():
        avail = ", ".join(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())
        raise SystemExit(f"[ERR] 找不到 profile「{name}」。可用的：{avail}")
    from browser_login import extract_cookies, launch_browser
    print(f"[COOKIE] 開 Chrome 讀 {name} 的登入態…（過 eps 挑戰要幾秒）")
    driver = launch_browser(str(udd))
    try:
        driver.get(target_url)
        time.sleep(8)                     # 等 eps 挑戰在這個路徑上跑完
        print(f"[COOKIE] 瀏覽器頁面：{driver.title[:60]}")
        ck = extract_cookies(driver)
    finally:
        driver.quit()
    print(f"[COOKIE] 取得 {len(ck)} 字元，eps_sid：{'有' if 'eps_sid' in ck else '**沒有**'}")
    return ck


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="", help="chrome_profiles/tixcraft/ 底下的名稱")
    ap.add_argument("--cookie-file", default="")
    ap.add_argument("--slug", default="")
    ap.add_argument("--samples", type=int, default=4)
    args = ap.parse_args()
    slug = args.slug or config.ACTIVITY_SLUG

    game_url = f"{BASE}/activity/game/{slug}"
    cookie = ""
    if args.cookie_file:
        cookie = Path(args.cookie_file).read_text(encoding="utf-8-sig").strip()
    elif args.profile:
        cookie = cookies_from_profile(args.profile, game_url)
    elif config.COOKIE:
        cookie = config.COOKIE

    print("\n" + "=" * 108)
    print(f"拓元鏈路時間分解   {platform.node()}  {platform.system()} {platform.machine()}")
    print(f"slug={slug}   proxy={'有' if config.CURRENT_PROXY else '無（直連）'}   "
          f"cookie={'有（' + str(len(cookie)) + ' 字）' if cookie else '**無 → 會被 eps 擋成 401**'}")
    print("=" * 108)

    p = Probe(cookie)

    # ── ①②③④ ─────────────────────────────────────────────────
    print("\n【①連線重用 ②DNS ③TLS ④HTTP 版本】")
    print(HEAD)
    cold = p.get(game_url)
    row("GAME 第1發(冷)", cold)
    warm = []
    for i in range(3):
        time.sleep(GAP)
        t = p.get(game_url)
        warm.append(t)
        row(f"GAME 第{i + 2}發(熱)", t)
    warm = [t for t in warm if t]

    if cold and warm:
        hs = cold["dns"] + cold["tcp"] + cold["tls"]
        hs_warm = max(t["dns"] + t["tcp"] + t["tls"] for t in warm)
        srv = statistics.median([t["server"] for t in warm])
        dl = statistics.median([t["download"] for t in warm])
        print(f"\n  ① 連線重用：熱連線的 DNS+TCP+TLS = {hs_warm:.0f}ms"
              f"（冷連線是 {hs:.0f}ms）→ "
              f"{'**已完全重用，沒有優化空間**' if hs_warm < 1 else '**沒重用到，有問題**'}")
        print(f"  ② DNS：冷 {cold['dns']:.0f}ms，熱 0ms（curl handle 自帶 DNS cache）")
        print(f"  ③ TLS：冷 {cold['tls']:.0f}ms，熱 0ms（session 重用）")
        print(f"  ④ 協定 {cold['http']}"
              + ("　→ 已是最佳，沒空間" if "2" in cold["http"] or "3" in cold["http"]
                 else "　→ **沒升 H2，可能有空間**"))
        print(f"\n  ⇒ 熱連線下一發仍要 {statistics.median([t['total'] for t in warm]):.0f}ms，"
              f"其中**伺服器 {srv:.0f}ms、下載只有 {dl:.0f}ms**")

    # 全新 handle = 模擬新 thread / 新進程
    time.sleep(GAP)
    print("\n【新連線的代價（= 新 thread / 新進程要付的）】")
    print(HEAD)
    fresh = Probe(cookie).get(game_url)
    row("全新 handle", fresh)
    if fresh:
        print(f"  → 多付 {fresh['dns'] + fresh['tcp'] + fresh['tls']:.0f}ms。"
              f"這就是「並行一律走 WarmPool、不要現開 thread」的理由")

    # ── ⑦ 各 endpoint ───────────────────────────────────────────
    print("\n【⑦ 不必要的 request：每一發值多少】")
    print(HEAD)
    costs = {}
    for url, label in ((f"{BASE}/", "首頁"), (f"{BASE}/activity", "活動列表"),
                       (game_url, "GAME 頁")):
        time.sleep(GAP)
        t = p.get(url)
        row(label, t)
        if t and t["status"] == 200:
            costs[label] = t["total"]
    if costs:
        print(f"\n  → 動態頁 ≈ {statistics.median(costs.values()):.0f}ms/發。"
              f"**砍掉一發就是實打實省這麼多**（cache.py 砍 GAME、fast path 砍 form GET）")

    # ── ⑤⑥ 解析 ────────────────────────────────────────────────
    print("\n【⑤ 序列化 ⑥ 解析】")
    html = (cold or {}).get("text") or ""
    net = statistics.median([t["total"] for t in warm]) if warm else 0
    if not html:
        print("  （沒有 HTML，跳過）")
    else:
        print(f"  HTML {len(html):,} 字元")
        worst = 0.0
        for name, fn in (("game_not_open", lambda: parsing.game_not_open(html)),
                         ("has_area_button", lambda: parsing.has_area_button(html)),
                         ("parse_game_keys", lambda: parsing.parse_game_keys(html, "")),
                         ("parse_game_area_url", lambda: parsing.parse_game_area_url(html, "")),
                         ("parse_hidden_inputs", lambda: parsing.parse_hidden_inputs(html)),
                         ("parse_area_availables", lambda: parsing.parse_area_availables(html))):
            n = 200
            t0 = time.perf_counter()
            for _ in range(n):
                fn()
            per = (time.perf_counter() - t0) / n * 1000
            worst = max(worst, per)
            print(f"    {name:<24}{per:>8.3f}ms/次")
        if net:
            print(f"\n  → 最慢的解析 {worst:.3f}ms vs 一發網路 {net:.0f}ms "
                  f"= **{worst / net * 100:.2f}%**，⑤⑥ 完全不是瓶頸")
        print("  （拓元回的是 HTML 不是 JSON，沒有 JSON 反序列化成本）")

    # ── ⑧ CPU ─────────────────────────────────────────────────
    print("\n【⑧ CPU / context switching】")
    time.sleep(GAP)
    w0, c0 = time.perf_counter(), time.process_time()
    p.get(game_url)
    dw, dc = (time.perf_counter() - w0) * 1000, (time.process_time() - c0) * 1000
    if dw > 0:
        print(f"  一發請求：牆鐘 {dw:.0f}ms，CPU 只用 {dc:.1f}ms（{dc / dw * 100:.1f}%）")
        print(f"  → 其餘 {100 - dc / dw * 100:.1f}% 純等網路，"
              f"**CPU 和 context switch 不可能是瓶頸**")
    print(f"  目前 thread 數 {threading.active_count()}")

    # ── ⑨⑩ ────────────────────────────────────────────────────
    print("\n【⑨ VM CPU steal time】")
    print(f"  {_steal_time()}")

    print("\n【⑩ 網路品質（熱連線連續取樣看抖動）】")
    lat = []
    for i in range(args.samples):
        time.sleep(GAP)
        t = p.get(game_url)
        if t:
            lat.append(t["server"])
            print(f"    #{i + 1}  伺服器+往返 {t['server']:>6.0f}ms   總計 {t['total']:>6.0f}ms")
    if len(lat) >= 2:
        med = statistics.median(lat)
        print(f"\n  p50 {med:.0f}ms   最快 {min(lat):.0f}   最慢 {max(lat):.0f}   "
              f"抖動 ±{(max(lat) - min(lat)) / 2:.0f}ms")
        print("  → " + ("**抖動很大，單次數字別當定論**" if max(lat) - min(lat) > med * 0.5
                        else "抖動穩定"))
    print("\n" + "=" * 108)


def _steal_time() -> str:
    """steal time = 實體主機把 CPU 拿去給別的 VM。只有 Linux VM 量得到。"""
    if platform.system() != "Linux":
        return (f"本機是 {platform.system()}，不是 VM —— **沒有 steal time 這回事**。"
                f"這項要到 VPS 上跑（讀 /proc/stat 第 8 欄）")
    try:
        parts = open("/proc/stat").readline().split()
        total, steal = sum(int(x) for x in parts[1:]), int(parts[8])
        pct = steal / total * 100 if total else 0
        verdict = "正常" if pct < 1 else ("偏高" if pct < 5 else "**嚴重，該換機器**")
        return f"steal {pct:.2f}%（開機以來累計）→ {verdict}"
    except Exception as e:
        return f"讀不到 /proc/stat: {type(e).__name__}"


if __name__ == "__main__":
    main()
