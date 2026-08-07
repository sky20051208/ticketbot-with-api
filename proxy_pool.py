from urllib.parse import urlsplit

import config
from curl_cffi import requests as cf_requests


VALIDATE_URL = "https://tixcraft.com/"
IP_ECHO_URL = "https://api.ipify.org"


def cliproxy_region() -> str:
    """出口住宅 IP 要哪一國。跟 CLIPROXY_HOST（節點）是**兩件獨立的事**，必須配對。

    舊的 config.py 把地區寫死在 CLIPROXY_USERNAME_TEMPLATE 裡（`-region-TW-`），
    所以這裡預設 TW、且 template 沒有 `{region}` 佔位符時 format 會直接忽略 ——
    舊設定檔不用改也能跑。
    """
    return getattr(config, "CLIPROXY_REGION", "") or "TW"


def _build_cliproxy_url(sid_suffix: str = "") -> str:
    """組 CliProxy 認證 URL；sid 用 ACC_ID 確保多開時每個 instance 拿到不同出口 IP。

    sid_suffix 是「這顆 IP 不能用，換一顆」用的 —— sticky session 綁在 sid 上，
    不改 sid 重連拿到的是同一顆 IP。**只能加英數**（sid 裡有連字號會被 CliProxy
    當成分隔符、靜默退回帳號預設 IP，見 CLAUDE.md 的 sid 坑）。
    """
    user = config.CLIPROXY_USERNAME_TEMPLATE.format(
        acc_id=f"{config.ACC_ID}{sid_suffix}", region=cliproxy_region())
    return f"http://{user}:{config.CLIPROXY_PASSWORD}@{config.CLIPROXY_HOST}:{config.CLIPROXY_PORT}"


def _warn_if_node_region_mismatch() -> None:
    """節點和出口地區不搭時出聲。

    這兩個值分開設定就有改一半的風險，而最糟的組合（美國節點 + 台灣住宅 IP）
    實測比原本的組合還慢：2026-08-07 從 Oracle Ashburn 打拓元，
      us2 + US = 281ms、sg2 + TW = 530ms、**us2 + TW = 561ms**。
    壞掉的方式是「還是能跑、只是慢一倍」，不講就永遠不會發現。
    """
    host = (config.CLIPROXY_HOST or "").lower()
    region = cliproxy_region().upper()
    node = "US" if host.startswith("us") else ("TW" if host.startswith("tw") else
                                               "SG" if host.startswith("sg") else "")
    if node and node != region:
        print(f"[PROXY] ⚠ 節點是 {node}（{config.CLIPROXY_HOST}）但出口地區設 {region} —— "
              f"流量會多繞一趟，確認 CLIPROXY_HOST 和 CLIPROXY_REGION 是不是該一致")


VALIDATE_TIMEOUT = 12  # sg2 新加坡節點正常 < 3s 完成；留 buffer 防偶發抖動
MAX_VALIDATE_TRIES = 3


# 住宅代理的 IP 是跟別人共用的，難免抽到已經被目標站台燒掉的那幾顆。
# 403 = 被擋、429 = 被限流，這兩種**不能當成驗證通過**（舊版 `status_code < 500`
# 會直接放行 403，整場搶票就綁在一顆進不去的 IP 上）。
# 401 是 eps 的挑戰頁，代表 IP 沒問題、只是還沒解挑戰 —— 那是正常的。
BLOCKED_CODES = (403, 429)


def _validate(proxy: str) -> bool:
    try:
        r = cf_requests.get(
            VALIDATE_URL,
            proxies=as_dict(proxy),
            impersonate="chrome124",
            timeout=VALIDATE_TIMEOUT,
        )
    except Exception as e:
        print(f"[PROXY] 驗證失敗: {type(e).__name__}: {e}")
        return False
    if r.status_code in BLOCKED_CODES:
        print(f"[PROXY] 這顆出口 IP 被擋（HTTP {r.status_code}）—— 換一顆")
        return False
    return r.status_code < 500


def _echo_ip(proxy: str) -> None:
    """印出出口 IP，方便確認是 CliProxy 給的台灣住宅 IP。"""
    try:
        r = cf_requests.get(IP_ECHO_URL, proxies=as_dict(proxy), timeout=8)
        print(f"[PROXY] 出口 IP: {r.text.strip()}")
    except Exception as e:
        print(f"[PROXY] 查 IP 失敗: {type(e).__name__}")


def acquire() -> str:
    """啟用 PROXY POOL 時建立 CliProxy 連線；不同 ACC_ID = 不同 sid = 不同 IP。

    驗不過就**換 sid 再抽一顆**，不是拿同一條重連 —— sticky session 綁在 sid 上，
    同 sid 重連拿到的永遠是同一顆 IP，舊版那樣重試 3 次等於原地打轉。
    """
    if not config.ENABLE_PROXY_POOL:
        return ""

    _warn_if_node_region_mismatch()
    print(f"[PROXY] CliProxy 啟用 (acc={config.ACC_ID}, "
          f"{config.CLIPROXY_HOST} region={cliproxy_region()})")
    for i in range(1, MAX_VALIDATE_TRIES + 1):
        # 第一次用乾淨的 sid（跟 create_profile 那邊對得起來），之後才加後綴換 IP
        suffix = "" if i == 1 else f"r{i}"
        proxy_url = _build_cliproxy_url(suffix)
        if _validate(proxy_url):
            config.CURRENT_PROXY = proxy_url
            _echo_ip(proxy_url)
            return proxy_url
        print(f"[PROXY] 第 {i}/{MAX_VALIDATE_TRIES} 顆不能用，換 sid 再抽…")

    print("[PROXY] 連抽 3 顆都不能用 —— 可能是 CliProxy 帳號問題，或這個地區的 IP 被大量封鎖")
    config.CURRENT_PROXY = ""
    return ""


def as_dict(proxy: str) -> dict:
    """把 proxy URL 轉成 curl_cffi / requests 用的 proxies dict。"""
    if not proxy:
        return {}
    url = proxy if proxy.startswith("http") else f"http://{proxy}"
    return {"http": url, "https": url}


def redact(proxy: str) -> str:
    """proxy URL → 可以安全印進 log 的字串（節點 + sid，**不含帳號密碼**）。

    要有這支是因為 proxy URL 長成 `http://user:password@host:port`，整串印出去
    等於公開 CliProxy 帳密 —— 而這些 log 會被 webgui 直接顯示在網頁上、也常被
    截圖貼給別人看。診斷真正需要的只有「走哪個節點、哪個 sid」。
    """
    if not proxy:
        return "（無）"
    p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    sid = ""
    if p.username:
        parts = p.username.split("-")
        if "sid" in parts:
            i = parts.index("sid")
            if i + 1 < len(parts):
                sid = f" sid={parts[i + 1]}"
    return f"{p.hostname}:{p.port}{sid}"
