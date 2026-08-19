"""每一發請求的階段分解儀表（DNS / TCP / TLS / 伺服器 / 下載）。

**預設不啟用**，只有環境變數 `TIX_PERF=1` 時才會掛上去 —— 正式搶票不該為了觀測多做任何事。

怎麼拿到階段時間
────────────────
curl 本身會記每個階段的時間戳（NAMELOOKUP → CONNECT → APPCONNECT(TLS) →
PRETRANSFER → STARTTRANSFER(TTFB) → TOTAL），但 curl_cffi 的
`Session._request_with_retry` 在 `finally: c.reset()` 把 handle 清掉，等 `request()`
回來時 `getinfo` 全部是 0。

關鍵是 `_parse_response(c, ...)` 是在那個 reset **之前**被呼叫的 —— 從那裡攔截就拿得到。
所以這裡掛兩層：
  1. `_parse_response` → 把階段時間塞進 response（`res.perf`）
  2. `request`         → 回來之後把它印出來

只讀不改：不碰任何 curl option、不動連線池、不改重試邏輯，關掉就完全回到原樣。

讀法
────
    [PERF] GAME     513ms │ DNS   0 TCP   0 TLS   0 │ 伺服器 499 │ 下載 12 │ 58KB h2 port=51234
      DNS/TCP/TLS 全 0     = 連線重用中（正常）
      這三個有數字         = 這發在重建連線，多付的就是那些毫秒
      伺服器欄位          = 拓元 render + 一趟往返，**客戶端無能為力的部分**
      port                = 同一個 = 同一條 TCP 連線
"""
import os
import time

_ENABLED = os.environ.get("TIX_PERF") == "1"
_HTTP_VER = {0: "?", 1: "h1.0", 2: "h1.1", 3: "h2", 30: "h3"}
_installed = False

# 累計統計，跑完印總表
_records: list[dict] = []


def enabled() -> bool:
    return _ENABLED


def _phases(curl) -> dict | None:
    from curl_cffi import CurlInfo
    try:
        g = lambda k: (curl.getinfo(k) or 0)          # noqa: E731
        dns, conn, tls = (g(CurlInfo.NAMELOOKUP_TIME), g(CurlInfo.CONNECT_TIME),
                          g(CurlInfo.APPCONNECT_TIME))
        pre, ttfb, total = (g(CurlInfo.PRETRANSFER_TIME),
                            g(CurlInfo.STARTTRANSFER_TIME), g(CurlInfo.TOTAL_TIME))
        if not total:
            return None
        return {
            "dns": dns * 1000,
            "tcp": (conn - dns) * 1000 if conn else 0.0,
            "tls": (tls - conn) * 1000 if tls else 0.0,
            "server": (ttfb - pre) * 1000 if ttfb else 0.0,
            "download": (total - ttfb) * 1000 if total else 0.0,
            "total": total * 1000,
            "http": _HTTP_VER.get(g(CurlInfo.HTTP_VERSION), "?"),
            "port": g(CurlInfo.LOCAL_PORT),
        }
    except Exception:
        return None


def _label(url: str) -> str:
    """把 URL 縮成一眼看得出是哪一步的短標籤。"""
    for frag, name in (("/ticket/area/", "AREA"), ("/ticket/ticket/", "TICKET表單"),
                       ("/ticket/captcha", "驗證碼"), ("/ticket/order", "ORDER"),
                       ("/ticket/check", "CHECK"), ("/activity/game/", "GAME"),
                       ("/login", "LOGIN"), ("/activity/detail", "詳情")):
        if frag in url:
            return name
    return url.split("/")[-1][:14] or "其他"


def install():
    """掛上儀表。`TIX_PERF=1` 才有作用，重複呼叫安全。"""
    global _installed
    if _installed or not _ENABLED:
        return
    from curl_cffi.requests.session import Session

    orig_parse = Session._parse_response
    orig_request = Session.request

    def parse(self, curl, *a, **kw):
        rsp = orig_parse(self, curl, *a, **kw)
        # 一定要在這裡取 —— 外面 finally 就 reset() 了
        try:
            rsp.perf = _phases(curl)
        except Exception:
            pass
        return rsp

    def request(self, method, url, *a, **kw):
        t0 = time.perf_counter()
        res = orig_request(self, method, url, *a, **kw)
        wall = (time.perf_counter() - t0) * 1000
        p = getattr(res, "perf", None)
        if p:
            p.update(label=_label(str(url)), method=method,
                     status=res.status_code, size=len(res.content or b""), wall=wall)
            _records.append(p)
            cold = "  ← **新連線**" if (p["dns"] + p["tcp"] + p["tls"]) > 1 else ""
            print(f"[PERF] {p['label']:<9}{p['total']:>6.0f}ms │"
                  f" DNS{p['dns']:>4.0f} TCP{p['tcp']:>4.0f} TLS{p['tls']:>4.0f} │"
                  f" 伺服器{p['server']:>5.0f} │ 下載{p['download']:>4.0f} │"
                  f" {p['size'] / 1024:>5.1f}KB {p['http']} port={p['port']}"
                  f" {res.status_code}{cold}", flush=True)
        return res

    Session._parse_response = parse
    Session.request = request
    _installed = True
    print("[PERF] 階段分解已啟用（TIX_PERF=1）—— 每發請求會多印一行 [PERF]")


def summary():
    """跑完印總表：時間到底花在哪一類。"""
    if not _records:
        return
    import statistics as st
    tot = sum(r["total"] for r in _records)
    srv = sum(r["server"] for r in _records)
    hs = sum(r["dns"] + r["tcp"] + r["tls"] for r in _records)
    dl = sum(r["download"] for r in _records)
    print("\n" + "=" * 92)
    print(f"[PERF] 總表：{len(_records)} 發請求，網路合計 {tot:.0f}ms")
    print("=" * 92)
    by = {}
    for r in _records:
        by.setdefault(r["label"], []).append(r)
    print(f"  {'步驟':<12}{'發數':>4}{'合計':>9}{'p50':>8}{'握手':>8}{'伺服器':>9}{'下載':>8}")
    for label, rs in sorted(by.items(), key=lambda kv: -sum(x["total"] for x in kv[1])):
        print(f"  {label:<12}{len(rs):>4}{sum(x['total'] for x in rs):>8.0f}ms"
              f"{st.median([x['total'] for x in rs]):>7.0f}ms"
              f"{sum(x['dns'] + x['tcp'] + x['tls'] for x in rs):>7.0f}ms"
              f"{sum(x['server'] for x in rs):>8.0f}ms"
              f"{sum(x['download'] for x in rs):>7.0f}ms")
    print(f"\n  ① ② ③ 握手（連線重用沒吃到的部分）  {hs:>7.0f}ms  ({hs / tot * 100:>4.1f}%)")
    print(f"  ⑦   伺服器 render（拓元那邊，動不了）  {srv:>7.0f}ms  ({srv / tot * 100:>4.1f}%)")
    print(f"  ④   下載（HTTP/2、大小無關）          {dl:>7.0f}ms  ({dl / tot * 100:>4.1f}%)")
    ports = {r["port"] for r in _records}
    print(f"\n  ① 用到 {len(ports)} 條 TCP 連線"
          + ("（全程重用同一條）" if len(ports) == 1 else f" ports={sorted(ports)}"))
    print("  ⑤⑥ 解析、⑧ CPU：見 bench_tixcraft_profile.py（實測 0.12% / 0.0%）")
    print("=" * 92)
