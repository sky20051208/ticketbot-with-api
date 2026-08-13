"""實驗：**開賣前就送 enqueue**，到底會排到還是會卡死？

要回答的問題
────────────
現在的流程是被動的：高頻打票況 → 看到 status 翻 onsale → 才送單。偵測本身有
盲區（併行模式下平均 25ms）。如果開賣前就把 payload 丟出去、讓它在隊伍裡等，
那段偵測時間就完全省掉了。

但也可能相反 —— 提早送會拿到 errCode 137（排隊中），如果那個狀態在開賣後不會自己
變成「排到」，就等於白排一輪、比乖乖等還慢。**已知**開賣前送會回 137 + waitSecond 10
（CLAUDE.md 有記），**沒驗過的是**：照它給的秒數重試下去，開賣後到底拿不拿得到 uuid。

這支就是去量那個。它會同時記錄兩條時間軸再對起來：
    A. 票況 status 什麼時候翻 onsale（另一條 thread 在高頻 poll）
    B. 每一發 enqueue 什麼時候送、回什麼、什麼時候拿到 uuid

安全性
──────
**預設只排隊到拿 uuid 為止，不會真的下單**（不呼叫 reserve = 不會產生訂單、不用付錢）。
uuid 只是排隊憑證，放著不用會自己過期。要跑完整條鏈路才加 `--reserve`，那會產生
真實的 15 分鐘保留訂單。

用法
────
    # 先看有哪些場次可以測（不需要登入）
    python bench_ticketplus_prefire.py --event <eventId> --list

    # 開賣前 10 秒開始送（安全，只排隊不下單）
    python bench_ticketplus_prefire.py --event <eventId> --session s000002xxx \
        --lead 10 --token "<access_token 或整串 user cookie>"

    # 對照組：開賣前 1 秒
    python bench_ticketplus_prefire.py --event <eventId> --session s000002yyy --lead 1 ...

    # 再一組對照：完全不提早，等 status 翻 onsale 才送（現行行為）
    python bench_ticketplus_prefire.py ... --lead 0

同一個活動的不同場次可以用同一個帳號同時跑（排隊是 per session），開兩個終端機就好。
token 壽命只有 60 分鐘，**開賣前才去複製**。
"""
import argparse
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from ticketplus_api import CONFIG_API, QUEUE_API, TICKET_API, catalog, crypto, parsing
from ticketplus_api.session import (build_headers, build_session, describe_token,
                                    extract_token, warmup_queue, warmup_session)

TW = timezone(timedelta(hours=8))
_log_lock = threading.Lock()
_t0_mono = time.monotonic()
_t0_wall = datetime.now(TW)


def log(tag: str, msg: str):
    """每一行都帶「距開賣 T±x.xxx 秒」，兩條時間軸才對得起來。"""
    with _log_lock:
        now = _t0_wall + timedelta(seconds=time.monotonic() - _t0_mono)
        print(f"[{now:%H:%M:%S.%f}"[:-3] + f"] [{tag}] {msg}", flush=True)


def sale_start(session, session_id_plain: str) -> datetime:
    info = catalog.get_session_info(session, session_id_plain)
    raw = info.get("saleStart")
    if not raw:
        raise SystemExit("[ERR] 這個場次沒有 saleStart，無法定位 T-0")
    return datetime.fromisoformat(raw)


def show_sessions(session, event_id: str):
    data = catalog.fetch_catalog(session, event_id)
    print(f"\n{'場次 sessionId':<16} {'開賣時間':<21} {'狀態':<9} 名稱")
    print("─" * 96)
    for s in data["sessions"]:
        if s.get("hidden"):
            continue
        plain = crypto.decrypt_id(s["sessionId"])
        live = catalog.get_session_info(session, plain)
        print(f"{plain:<16} {str(live.get('saleStart')):<21} "
              f"{str(live.get('status')):<9} {s.get('name', '')[:44]}")


def pick_product(session, event_id: str, session_id_enc: str, keyword: str):
    """挑一個要送的票種 —— 用跟正式流程同一套排序，實驗才有代表性。"""
    data = catalog.fetch_catalog(session, event_id)
    areas = parsing.areas_of_session(data["ticketAreas"], session_id_enc)
    products = [p for p in data["products"]
                if p.get("sessionId") == session_id_enc and not p.get("hidden")]
    targets = parsing.rank_targets(products, areas, keyword=keyword,
                                   strategy="關鍵字優先")
    if not targets:
        raise SystemExit("[ERR] 這個場次挑不到票種")
    area, product = targets[0]
    return area, product, [p["productId"] for p in products], \
        [a["ticketAreaId"] for a in areas]


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
    提早開跑的話連線早就閒置斷了，T-0 那發會整個吃掉那 2.1s，實驗就白做了。"""
    try:
        session.get(f"{QUEUE_API}/", headers=build_headers(), timeout=5)
    except Exception:
        pass


def fire(session, payload, token, deadline_mono, found):
    """照官方 waitSecond 重試，直到拿到 uuid 或超時。回 (uuid, 幾次, 秒數)。"""
    url = f"{QUEUE_API}/enqueue"
    headers = {**build_headers(), "Authorization": f"Bearer {token}"}
    attempt = 0
    started = time.monotonic()
    while time.monotonic() < deadline_mono:
        attempt += 1
        t = time.monotonic()
        try:
            res = session.post(url, json=payload, headers=headers, timeout=15)
            data = res.json()
        except Exception as e:
            log("ENQUEUE", f"#{attempt} 例外 {type(e).__name__}，1s 後重試")
            time.sleep(1.0)
            continue
        rtt = (time.monotonic() - t) * 1000
        code = str(data.get("errCode"))
        opened = "開賣後" if found.is_set() else "開賣前"
        log("ENQUEUE", f"#{attempt} {opened} errCode={code} "
                       f"waitSecond={data.get('waitSecond')} "
                       f"localCheck={data.get('localCheck')} RTT={rtt:.0f}ms")
        if code == "00":
            if data.get("currentReservedOrderId"):
                log("ENQUEUE", f"★ 帳號本來就有保留中的訂單 "
                               f"{data['currentReservedOrderId']} —— 這局不算數")
                return None, attempt, time.monotonic() - started
            log("ENQUEUE", f"★★ 拿到 uuid={data.get('uuid')} "
                           f"（第 {attempt} 次，共排 {time.monotonic() - started:.2f}s）")
            return data.get("uuid"), attempt, time.monotonic() - started
        if code in ("103", "101"):
            raise SystemExit(f"[ERR] token 失效（errCode {code}）—— 重新複製一份再跑")
        wait = float(data.get("waitSecond") or 0) or (0.5 if data.get("localCheck") else 15.0)
        time.sleep(wait)
    return None, attempt, time.monotonic() - started


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True, help="活動網址 /activity/ 後面那串")
    ap.add_argument("--session", help="場次的**明文** id（s000002xxx），用 --list 查")
    ap.add_argument("--list", action="store_true", help="只列出場次跟開賣時間")
    ap.add_argument("--lead", type=float, default=10.0,
                    help="開賣前幾秒送第一發。0 = 等 status 翻 onsale 才送（現行行為）")
    ap.add_argument("--keyword", default="", help="票區關鍵字，空 = 用預設優先序第一個")
    ap.add_argument("--amount", type=int, default=1)
    ap.add_argument("--token", default="", help="access_token / 整串 user cookie")
    ap.add_argument("--timeout", type=float, default=180.0, help="最久排多久就放棄")
    ap.add_argument("--reserve", action="store_true",
                    help="拿到 uuid 後**真的下單**（會產生 15 分鐘保留訂單）。預設不下單")
    args = ap.parse_args()

    session = build_session()
    if args.list:
        show_sessions(session, args.event)
        return
    if not args.session or not args.token:
        raise SystemExit("[ERR] 要 --session 和 --token（先用 --list 查場次）")

    token = extract_token(args.token)
    if not token:
        raise SystemExit("[ERR] --token 解不出 access_token")
    log("INIT", f"token: {describe_token(token)}")

    data = catalog.fetch_catalog(session, args.event)
    match = [s for s in data["sessions"]
             if crypto.decrypt_id(s["sessionId"]) == args.session]
    if not match:
        raise SystemExit(f"[ERR] 這個活動裡找不到場次 {args.session}")
    session_id_enc = match[0]["sessionId"]

    area, product, pids, aids = pick_product(session, args.event, session_id_enc,
                                             args.keyword)
    t0 = sale_start(session, args.session)
    log("INIT", f"場次: {match[0].get('name')}")
    log("INIT", f"目標: {parsing.target_label(area, product)} ${product.get('price')} "
                f"× {args.amount} 張")
    log("INIT", f"開賣時間 T-0 = {t0:%Y-%m-%d %H:%M:%S}")
    log("INIT", f"策略: {'等 status 翻 onsale 才送' if args.lead <= 0 else f'T-{args.lead:g}s 就送第一發'}")
    log("INIT", f"下單: {'**會真的下單**' if args.reserve else '只排隊拿 uuid，不下單（安全）'}")

    seat = bool(match[0].get("ticketArea"))
    payload = {"products": [{"productId": product["productId"], "count": args.amount}]}
    if seat:
        payload.update(reserveSeats=True, consecutiveSeats=False, finalizedSeats=True)
    log("INIT", f"payload = {json.dumps(payload, ensure_ascii=False)}")

    warmup_session(session)
    warmup_queue(session)

    # 兩條時間軸：一條盯票況、一條送單
    stop_evt, found = threading.Event(), threading.Event()
    watcher_sess = build_session()
    warmup_session(watcher_sess)
    def secs_to_t0():
        return (t0 - datetime.now(TW)).total_seconds()

    threading.Thread(target=watch_status, daemon=True,
                     args=(watcher_sess, pids, aids, stop_evt, found,
                           secs_to_t0)).start()

    remaining = secs_to_t0()
    log("INIT", f"距開賣還有 {remaining:.1f}s（{remaining / 3600:.1f} 小時）")
    if remaining < args.lead:
        log("WARN", f"已經過了 T-{args.lead:g}s，馬上開始送")

    if args.lead > 0:
        last_ping = 0.0
        while secs_to_t0() > args.lead:
            rem = secs_to_t0()
            # 每 30s 把 queue 連線 ping 熱，最後 15s 停手不干擾
            if rem > 15 and time.monotonic() - last_ping > 30:
                _ping_queue(session)
                last_ping = time.monotonic()
            time.sleep(0.01 if rem < 2 else 0.2)
        log("FIRE", f"到 T-{args.lead:g}s，開始送 enqueue（此刻票況: "
                    f"{'已開賣' if found.is_set() else '未開賣'}）")
    else:
        while not found.is_set():
            time.sleep(0.005)
        log("FIRE", "偵測到 onsale，開始送 enqueue")

    uuid, attempts, queued = fire(session, payload, token,
                                  time.monotonic() + args.timeout, found)
    stop_evt.set()

    print()
    log("RESULT", "─" * 56)
    log("RESULT", f"策略           T-{args.lead:g}s 送第一發")
    log("RESULT", f"enqueue 次數   {attempts}")
    log("RESULT", f"排隊耗時       {queued:.2f}s")
    log("RESULT", f"結果           {'拿到 uuid ' + str(uuid) if uuid else '沒排到（逾時）'}")
    log("RESULT", "─" * 56)

    if uuid and args.reserve:
        log("RESERVE", "送 reserve（這會產生真實訂單）…")
        body = {**payload, "uuid": uuid}
        headers = {**build_headers(), "Authorization": f"Bearer {token}"}
        res = session.post(f"{TICKET_API}/reserve", json=body, headers=headers,
                           timeout=30)
        j = res.json()
        log("RESERVE", f"errCode={j.get('errCode')} orderId={j.get('orderId')}")
    elif uuid:
        log("RESERVE", "有 uuid 但沒有 --reserve，**不下單**，排隊憑證放著過期就好")


if __name__ == "__main__":
    main()
