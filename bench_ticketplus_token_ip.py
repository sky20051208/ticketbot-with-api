"""驗 TicketPlus 的 access_token 有沒有綁發放時的 IP。

**為什麼這件事很重要**：它決定「日本機器要多大」。
  - 沒綁 → 在家登入、把 cookie 帶過去就能用。遠端那台不用瀏覽器、不用桌面，
           最小的機器（1GB）就夠，因為除了登入以外整條鏈路都是純封包。
  - 有綁 → 遠端那台必須自己開瀏覽器登入，就得跟拓元那台一樣整套 Xorg + 遠端桌面。

拓元的 `eps_sid` 是綁 IP 的（換 IP 直接作廢），所以不能想當然耳套用到遠大。

做法：從你自己的 Chrome profile 讀出 token（**token 全程不離開這台機器**），
然後打 `{USER_API}/verifyToken` 兩次 —— 一次直連（家裡 IP）、一次走 CliProxy
（不同出口 IP）。兩邊都過就是沒綁。

用法：
    python bench_ticketplus_token_ip.py --profile chrome_profiles/ticketplus/<帳號名>
    python bench_ticketplus_token_ip.py --token "<access_token>"   # 已經有 token 就直接給
"""
import argparse
import asyncio
import sys

from curl_cffi import requests as cf_requests

import config
from ticketplus_api import USER_API
from ticketplus_api.session import build_headers, describe_token, extract_token


def _cliproxy_url() -> str:
    """借用 config 裡的 CliProxy 設定當「另一個出口 IP」。sid 隨便給一個沒用過的即可。"""
    if not config.CLIPROXY_HOST:
        return ""
    user = config.CLIPROXY_USERNAME_TEMPLATE.format(acc_id="iptest")
    return f"http://{user}:{config.CLIPROXY_PASSWORD}@{config.CLIPROXY_HOST}:{config.CLIPROXY_PORT}"


def probe(label: str, token: str, proxy_url: str = "") -> None:
    session = cf_requests.Session(impersonate="chrome142")
    kw = {"proxies": {"http": proxy_url, "https": proxy_url}} if proxy_url else {}
    # 先確認這條路的出口 IP，不然「兩邊都過」可能只是 proxy 沒生效
    try:
        ip = session.get("https://api.ipify.org", timeout=15, **kw).text.strip()
    except Exception as e:
        print(f"  {label:22s} 查出口 IP 失敗: {type(e).__name__}: {e}")
        return
    # 用 GET /getMaskedUserInfo：唯讀、需要登入、而且是**遮罩版**，回傳的個資最少。
    # （/verifyToken 在 bundle 的路徑清單裡但實際打是 404；getUserInfo 會回未遮罩的資料）
    try:
        res = session.get(f"{USER_API}/getMaskedUserInfo",
                          headers=build_headers(token), timeout=15, **kw)
        data = res.json() if res.text.startswith("{") else {}
    except Exception as e:
        print(f"  {label:22s} 出口 {ip:16s} 請求失敗: {type(e).__name__}: {e}")
        return
    code = str(data.get("errCode"))
    ok = code == "00"
    # 只印 errCode，不印回傳內容 —— 那裡面是會員資料
    print(f"  {label:22s} 出口 {ip:16s} HTTP {res.status_code} errCode={code} "
          f"{data.get('errMsg', '')} {'✅ 通過' if ok else '❌ 被拒'}")


async def token_from_profile(profile_dir: str) -> str:
    """開瀏覽器讀 profile 裡的 `user` cookie。token 只留在本機記憶體。"""
    import nodriver as uc
    from ticketplus_api.browser_session import read_user_cookie
    from ticketplus_api import BASE
    browser = await uc.start(headless=False, user_data_dir=profile_dir,
                             browser_args=["--no-first-run", "--no-default-browser-check"])
    try:
        tab = await browser.get(BASE)
        await asyncio.sleep(3)
        return extract_token(await read_user_cookie(tab))
    finally:
        browser.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="", help="Chrome profile 資料夾（讀裡面的 user cookie）")
    ap.add_argument("--token", default="", help="直接給 access_token，跳過開瀏覽器")
    args = ap.parse_args()

    token = args.token or extract_token(config.COOKIE)
    if not token and args.profile:
        token = asyncio.run(token_from_profile(args.profile))
    if not token:
        raise SystemExit("拿不到 token —— 用 --profile 指到 chrome_profiles/ticketplus/<帳號名>，"
                         "或用 --token 直接給")

    print("=" * 78)
    print(f"token: {len(token)} 字，{describe_token(token)}")
    print("=" * 78)
    probe("直連（你家 IP）", token)
    proxy_url = _cliproxy_url()
    if proxy_url:
        probe("走 proxy（換 IP）", token, proxy_url)
    else:
        print("  （config 沒有 CliProxy 設定，無法測第二個 IP）")
    print("=" * 78)
    print("兩邊都 ✅ → token 沒綁 IP，日本機可以只跑純封包（不用瀏覽器 / 桌面）")
    print("只有直連 ✅ → token 綁 IP，日本機得自己開瀏覽器登入，要整套桌面")
    print("=" * 78)


if __name__ == "__main__":
    sys.exit(main())
