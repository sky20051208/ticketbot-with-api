"""實驗：**開賣前就送 enqueue**，到底會排到還是會卡死？

要回答的問題
────────────
現在的流程是被動的：高頻打票況 → 看到 status 翻 onsale → 才送單。偵測本身有盲區
（併行模式下平均 25ms）。如果開賣前就把 payload 丟出去、讓它在隊伍裡等，那段偵測
時間就完全省掉了。

但也可能相反 —— 提早送會拿到 errCode 137（排隊中），如果那個狀態在開賣後不會自己
變成「排到」，就等於白排一輪、比乖乖等還慢。**已知**開賣前送會回 137 + waitSecond 10
（CLAUDE.md 有記），**沒驗過的是**：照它給的秒數重試下去，開賣後到底拿不拿得到 uuid。

這支就是去量那個。它同時記兩條時間軸再對起來：
    A. 票況 status 什麼時候翻 onsale（另一條 thread 在盯）
    B. 每一發 enqueue 什麼時候送、回什麼、什麼時候拿到 uuid

設定沿用 GUI
────────────
`--config profiles/acc_N/config.json` 直接吃 GUI 卡片存下來的設定 —— 帳號（chrome
profile）、活動、場次、票種、張數全部在 GUI 上設好，CLI 只負責 `--lead` 這個**唯一
的實驗變數**。這樣既不用手貼 cookie，也**完全不動到正式搶票的程式碼**。

安全性
──────
**預設只排隊到拿 uuid 為止，不會真的下單**（不呼叫 reserve = 不產生訂單、不用付錢）。
uuid 只是排隊憑證，放著不用會自己過期。要跑完整條鏈路才加 `--reserve`。

用法
────
    # 0. 先看活動有哪些場次（不需要登入）
    python bench_ticketplus_prefire.py --event <eventId> --list

    # 1. 開賣前先驗 token（不排隊、不下單）
    python bench_ticketplus_prefire.py --config profiles/acc_1/config.json --check

    # 2. 三個終端機，三支帳號，打同一個場次，只差在提前秒數
    python bench_ticketplus_prefire.py --config profiles/acc_1/config.json --lead 10
    python bench_ticketplus_prefire.py --config profiles/acc_2/config.json --lead 1
    python bench_ticketplus_prefire.py --config profiles/acc_3/config.json --lead 0

**三張卡片的 ACTIVITY_SLUG 和 DATE_KEYWORD 要設成一樣**（同一個場次），才是乾淨的
對照 —— 只有提前秒數不同。`--lead 0` = 等 status 翻 onsale 才送 = 現行行為。

跑之前一定要做的兩件事
──────────────────────
1. **把上一次留下來的 Chrome 關掉**。userdata 模式是拿那個 profile 開瀏覽器讀登入態，
   profile 同時只能被一個 Chrome 開著 —— 沒關的話 nodriver 開不起來，會**默默卡在
   「請完成登入」等 600 秒**（不報錯，很容易以為是網路慢）。
2. **用專案的 .venv 跑**。全域 Python 跟 .venv 的套件版本不一樣（實測 curl_cffi
   0.14 vs 0.15，指紋種類 5 vs 8），而 GUI spawn 子進程是用 sys.executable，
   兩邊混用會讓「測起來正常、正式跑卻不一樣」。
       .\\.venv\\Scripts\\python.exe bench_ticketplus_prefire.py ...
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import config
from ticketplus_api import QUEUE_API, TICKET_API, USER_API, catalog, crypto, parsing
from ticketplus_api.__main__ import _resolve_token, build_plan, parse_event_id
from ticketplus_api.session import (build_headers, build_session, describe_token,
                                    extract_token, warmup_queue, warmup_session)

TW = timezone(timedelta(hours=8))
_log_lock = threading.Lock()


def log(tag: str, msg: str):
    with _log_lock:
        print(f"[{datetime.now(TW):%H:%M:%S.%f}"[:-3] + f"] [{tag}] {msg}", flush=True)


def load_gui_config(path: str):
    """吃 GUI 卡片存下來的 config.json，語意跟 load_config_override 一致
    （只蓋 config.py 裡真的存在的欄位）。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    for key, val in data.items():
        if hasattr(config, key):
            setattr(config, key, val)
    log("CONFIG", f"已載入 {path}")
    log("CONFIG", f"活動={config.ACTIVITY_SLUG}  場次關鍵字='{config.DATE_KEYWORD}'  "
                  f"票區關鍵字='{config.AREA_KEYWORD}'  張數={config.TICKET_AMOUNT}")
    log("CONFIG", f"登入方式={config.COOKIE_SOURCE}"
                  + (f"  profile={config.CHROME_USER_DATA_DIR}"
                     if config.COOKIE_SOURCE == "userdata" else ""))


def show_sessions(session, event_id: str):
    data = catalog.fetch_catalog(session, event_id)
    print(f"\n{'場次 sessionId':<16} {'開賣時間':<21} {'狀態':<9} 名稱")
    print("─" * 96)
    for s in data["sessions"]:
        if s.get("hidden"):
            continue
        live = catalog.get_session_info(session, crypto.decrypt_id(s["sessionId"]))
        print(f"{crypto.decrypt_id(s['sessionId']):<16} {str(live.get('saleStart')):<21} "
              f"{str(live.get('status')):<9} {s.get('name', '')[:44]}")


def check_token(session, token: str):
    """開賣前確認 token 真的有效。打 getMaskedUserInfo（唯讀、不碰排隊也不下單），
    **只印 errCode 不印回傳內容** —— 那裡面是會員個資。"""
    headers = {**build_headers(), "Authorization": f"Bearer {token}"}
    try:
        res = session.get(f"{USER_API}/getMaskedUserInfo", headers=headers, timeout=15)
        code = str(res.json().get("errCode"))
    except Exception as e:
        log("CHECK", f"連不上: {type(e).__name__}")
        return
    if code == "00":
        log("CHECK", f"✅ token 有效（{describe_token(token)}）—— 可以開跑")
    else:
        log("CHECK", f"❌ token 無效 errCode={code} —— 回瀏覽器重新登入")


def _poll_gap(remaining: float) -> float:
    """離 T-0 還很遠時不要用 20 次/秒去盯 —— 提早一小時開跑就會打出 7 萬發，
    很可能讓這顆 IP 在正式開賣前就被標記。只有臨門那段才需要高頻。"""
    if remaining > 120:
        return 5.0
    if remaining > 20:
        return 0.5
    return 0.05


def watch_status(session, product_ids, area_ids, stop_evt, found, secs_to_t0):
    """另一條 thread：盯票況，記下 status 翻 onsale 的**確切時刻**。"""
    seen = None
    while not stop_evt.is_set():
        result, blocked = catalog.get_infos(session, product_ids, area_ids, log=False)
        if blocked:
            log("STATUS", "被流量管制")
            time.sleep(1.0)
            continue
        infos, _ = parsing.index_infos(result)
        states = "/".join(sorted({str(i.get("status")) for i in infos.values()}))
        if states != seen:
            log("STATUS", f"票況 → {states}")
            seen = states
            if "onsale" in states and not found.is_set():
                found.set()
                log("STATUS", "★ 開賣了（這是被動偵測會看到的時間點）")
        time.sleep(_poll_gap(secs_to_t0()))


def _ping_queue(session):
    """安靜地把 queue 網域的連線 ping 熱。實測這個網域**新建一條連線第一發要 ~2.1s**，
    提早開跑的話連線早就閒置斷了，T-0 那發會整個吃掉那 2.1s、實驗就白做。"""
    # timeout 給 10s 不是 5s：建這條連線本身就要 ~2.1s，實測 5s 會偶發 timeout，
    # 而這支的重點就是**別讓 T-0 那發去付建線成本**，寧可等久一點也要暖成功。
    try:
        session.get(f"{QUEUE_API}/", headers=build_headers(), timeout=10)
    except Exception as e:
        log("PING", f"queue 暖機失敗（會再試）: {type(e).__name__}")


def fire(session, payload, token, deadline_mono, found):
    """照官方 waitSecond 重試，直到拿到 uuid 或超時。回 (uuid, 次數, 秒數)。"""
    url = f"{QUEUE_API}/enqueue"
    headers = {**build_headers(), "Authorization": f"Bearer {token}"}
    attempt, started = 0, time.monotonic()
    while time.monotonic() < deadline_mono:
        attempt += 1
        t = time.monotonic()
        try:
            data = session.post(url, json=payload, headers=headers, timeout=15).json()
        except Exception as e:
            log("ENQUEUE", f"#{attempt} 例外 {type(e).__name__}，1s 後重試")
            time.sleep(1.0)
            continue
        rtt = (time.monotonic() - t) * 1000
        code = str(data.get("errCode"))
        log("ENQUEUE", f"#{attempt} {'開賣後' if found.is_set() else '開賣前'} "
                       f"errCode={code} waitSecond={data.get('waitSecond')} "
                       f"localCheck={data.get('localCheck')} RTT={rtt:.0f}ms")
        if code == "00":
            if data.get("currentReservedOrderId"):
                log("ENQUEUE", f"★ 帳號本來就有保留中的訂單 "
                               f"{data['currentReservedOrderId']} —— 這局不算數，"
                               f"先去把它取消再重跑")
                return None, attempt, time.monotonic() - started
            spent = time.monotonic() - started
            log("ENQUEUE", f"★★ 拿到 uuid={data.get('uuid')}"
                           f"（第 {attempt} 次，共排 {spent:.2f}s）")
            return data.get("uuid"), attempt, spent
        if code in ("101", "103"):
            raise SystemExit(f"[ERR] token 失效（errCode {code}）—— 重新登入再跑")
        wait = float(data.get("waitSecond") or 0) or (0.5 if data.get("localCheck") else 15.0)
        time.sleep(wait)
    return None, attempt, time.monotonic() - started


async def run(args):
    if args.config:
        load_gui_config(args.config)

    probe = build_session()
    event_id = parse_event_id(args.event or config.ACTIVITY_SLUG)
    if args.list:
        show_sessions(probe, event_id)
        return

    # token：優先用手貼的，否則走 config 的登入方式（userdata 會開 Chrome）
    token, browser, _tab = None, None, None
    raw = args.token
    if args.token_file:
        # utf-8-sig：PowerShell 的 Out-File 和記事本都會加 BOM，用 utf-8 讀會在最前面
        # 多一個看不見的字元、token 就解不出來（很難看出來的那種錯）
        with open(args.token_file, "r", encoding="utf-8-sig") as f:
            raw = f.read().strip()
    if raw:
        token = extract_token(raw)
        if not token:
            raise SystemExit("[ERR] 解不出 access_token —— 要貼 DevTools → Application "
                             "→ Cookies → `user` 那一格的值")
    else:
        token, browser, _tab = await _resolve_token(event_id)
    if not token:
        raise SystemExit("[ERR] 拿不到 access_token")
    log("INIT", f"token: {describe_token(token)}")

    session = build_session(token)
    if args.check:
        check_token(session, token)
        return

    plan = build_plan(session, event_id)
    if not plan:
        raise SystemExit("[ERR] build_plan 失敗（檢查 ACTIVITY_SLUG / DATE_KEYWORD）")

    session_plain = crypto.decrypt_id(plan["session_id"])
    info = catalog.get_session_info(session, session_plain)
    if not info.get("saleStart"):
        raise SystemExit("[ERR] 這個場次沒有 saleStart，無法定位 T-0")
    t0 = datetime.fromisoformat(info["saleStart"])

    area, product = plan["targets"][0]
    amount = plan["amount"]
    seat = bool(plan["session"].get("ticketArea"))
    payload = {"products": [{"productId": product["productId"], "count": amount}]}
    if seat:
        payload.update(reserveSeats=True, consecutiveSeats=False, finalizedSeats=True)
    if config.PRESALE_CODE:
        payload["serialNumber"] = config.PRESALE_CODE

    log("INIT", f"場次: {plan['session'].get('name')}  ({session_plain})")
    log("INIT", f"目標: {parsing.target_label(area, product)} "
                f"${product.get('price')} × {amount} 張")
    log("INIT", f"開賣時間 T-0 = {t0:%Y-%m-%d %H:%M:%S}")
    log("INIT", f"策略: {'等 status 翻 onsale 才送（對照組）' if args.lead <= 0 else f'T-{args.lead:g}s 就送第一發'}")
    log("INIT", f"下單: {'**會真的下單**' if args.reserve else '只排隊拿 uuid，不下單（安全）'}")
    log("INIT", f"payload = {json.dumps(payload, ensure_ascii=False)}")

    warmup_session(session)
    warmup_queue(session)

    def secs_to_t0():
        return (t0 - datetime.now(TW)).total_seconds()

    stop_evt, found = threading.Event(), threading.Event()
    watcher = build_session()
    warmup_session(watcher)
    threading.Thread(target=watch_status, daemon=True,
                     args=(watcher, plan["poll_products"], plan["poll_areas"],
                           stop_evt, found, secs_to_t0)).start()

    remaining = secs_to_t0()
    log("INIT", f"距開賣還有 {remaining:.1f}s（{remaining / 60:.1f} 分）")
    if remaining < args.lead:
        log("WARN", f"已經過了 T-{args.lead:g}s，馬上開始送")

    if args.lead > 0:
        last_ping = 0.0
        while secs_to_t0() > args.lead:
            rem = secs_to_t0()
            if rem > 15 and time.monotonic() - last_ping > 30:
                _ping_queue(session)      # 保持 queue 連線是熱的
                last_ping = time.monotonic()
            time.sleep(0.01 if rem < 2 else 0.2)
        log("FIRE", f"到 T-{args.lead:g}s，開始送 enqueue"
                    f"（此刻票況: {'已開賣' if found.is_set() else '未開賣'}）")
    else:
        while not found.is_set():
            time.sleep(0.005)
        log("FIRE", "偵測到 onsale，開始送 enqueue")

    fired_at = datetime.now(TW)
    uuid, attempts, queued = fire(session, payload, token,
                                  time.monotonic() + args.timeout, found)
    got_at = datetime.now(TW)
    stop_evt.set()

    print()
    log("RESULT", "─" * 60)
    log("RESULT", f"策略             {'對照組（等 onsale）' if args.lead <= 0 else f'提前 {args.lead:g}s'}")
    log("RESULT", f"第一發送出        {fired_at:%H:%M:%S.%f}"[:-3])
    log("RESULT", f"enqueue 次數      {attempts}")
    log("RESULT", f"結果             {'拿到 uuid' if uuid else '沒排到（逾時）'}")
    if uuid:
        log("RESULT", f"**拿到 uuid 的時刻  {got_at:%H:%M:%S.%f}"[:-3] + "**  ← 三組要比的就是這個")
        log("RESULT", f"排隊耗時          {queued:.2f}s")
    log("RESULT", "─" * 60)

    if uuid and args.reserve:
        log("RESERVE", "送 reserve（這會產生真實訂單）…")
        headers = {**build_headers(), "Authorization": f"Bearer {token}"}
        j = session.post(f"{TICKET_API}/reserve", json={**payload, "uuid": uuid},
                         headers=headers, timeout=30).json()
        log("RESERVE", f"errCode={j.get('errCode')} orderId={j.get('orderId')}")
    elif uuid:
        log("RESERVE", "有 uuid 但沒有 --reserve，**不下單**，排隊憑證放著過期就好")

    if browser is not None:
        log("INIT", "（Chrome 保持開著，要關自己關）")
        # nodriver 的 subprocess transport 在直譯器關閉時會噴一整片
        # "Exception ignored in __del__ / I/O operation on closed pipe"——純粹是
        # Windows asyncio 的收尾雜訊，出現在 RESULT 之後、不影響任何結果。
        # 但開賣當下看到一片紅字很難第一眼分辨有沒有真的出事，所以結果印完就直接離開。
        # 用 os._exit 是因為要跳過的正是那段會噴錯的 cleanup；Chrome 是另一個 process，
        # 不會被一起帶走。先手動 flush，確保上面的輸出都已經寫出去。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="GUI 卡片的設定檔，例 profiles/acc_1/config.json")
    ap.add_argument("--lead", type=float, default=10.0,
                    help="開賣前幾秒送第一發。**這是唯一的實驗變數**。"
                         "0 = 等 status 翻 onsale 才送（現行行為，當對照組）")
    ap.add_argument("--event", help="不用 --config 時，直接給活動 id")
    ap.add_argument("--list", action="store_true", help="只列出場次跟開賣時間就結束")
    ap.add_argument("--check", action="store_true",
                    help="只驗 token 有沒有效就結束（不排隊、不下單）")
    ap.add_argument("--token", default="", help="手貼 access_token / 整串 user cookie")
    ap.add_argument("--token-file", default="", help="改從檔案讀 token（避免 shell 吃字元）")
    ap.add_argument("--timeout", type=float, default=180.0, help="最久排多久就放棄")
    ap.add_argument("--reserve", action="store_true",
                    help="拿到 uuid 後**真的下單**（會產生 15 分鐘保留訂單）。預設不下單")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
