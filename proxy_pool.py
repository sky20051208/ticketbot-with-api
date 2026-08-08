from urllib.parse import urlsplit

import config
from curl_cffi import requests as cf_requests


VALIDATE_URL = "https://tixcraft.com/"
IP_ECHO_URL = "https://api.ipify.org"


def _build_cliproxy_url() -> str:
    """組 CliProxy 認證 URL；sid 用 ACC_ID 確保多開時每個 instance 拿到不同出口 IP。"""
    user = config.CLIPROXY_USERNAME_TEMPLATE.format(acc_id=config.ACC_ID)
    return f"http://{user}:{config.CLIPROXY_PASSWORD}@{config.CLIPROXY_HOST}:{config.CLIPROXY_PORT}"


VALIDATE_TIMEOUT = 12  # sg2 新加坡節點正常 < 3s 完成；留 buffer 防偶發抖動
MAX_VALIDATE_TRIES = 3


def _validate(proxy: str) -> bool:
    try:
        r = cf_requests.get(
            VALIDATE_URL,
            proxies=as_dict(proxy),
            impersonate="chrome124",
            timeout=VALIDATE_TIMEOUT,
        )
        return r.status_code < 500
    except Exception as e:
        print(f"[PROXY] 驗證失敗: {type(e).__name__}: {e}")
        return False


def _echo_ip(proxy: str) -> None:
    """印出出口 IP，方便確認是 CliProxy 給的台灣住宅 IP。"""
    try:
        r = cf_requests.get(IP_ECHO_URL, proxies=as_dict(proxy), timeout=8)
        print(f"[PROXY] 出口 IP: {r.text.strip()}")
    except Exception as e:
        print(f"[PROXY] 查 IP 失敗: {type(e).__name__}")


def acquire() -> str:
    """啟用 PROXY POOL 時建立 CliProxy 連線；不同 ACC_ID = 不同 sid = 不同 IP。"""
    if not config.ENABLE_PROXY_POOL:
        return ""

    proxy_url = _build_cliproxy_url()
    print(f"[PROXY] CliProxy 啟用 (acc={config.ACC_ID})")
    for i in range(1, MAX_VALIDATE_TRIES + 1):
        if _validate(proxy_url):
            config.CURRENT_PROXY = proxy_url
            _echo_ip(proxy_url)
            return proxy_url
        print(f"[PROXY] 第 {i}/{MAX_VALIDATE_TRIES} 次驗證失敗，重試中...")

    print("[PROXY] CliProxy 連不上")
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
