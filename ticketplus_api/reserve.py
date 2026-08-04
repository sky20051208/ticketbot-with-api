"""送單：enqueue（排隊拿 uuid）→ reserve（換 orderId）。**這兩支是唯一需要 token 的地方**。

前端流程（app.js 模組 8a8f + 活動頁 chunk-3225fb1b）照抄：
    payload = {products:[{productId, count}]}
    有劃位（session.ticketArea 且票種 seatAssignment）再加：
        reserveSeats=True, consecutiveSeats=False, finalizedSeats=True(系統配位)
    有序號（presale code）再加 serialNumber

    POST queue/enqueue  → errCode "00" 拿 uuid；waitSecond = 叫你等幾秒再排
    POST ticket/reserve → 帶 uuid，拿到 orderId 就是**票已保留**（15 分鐘內結帳）

finalizedSeats=True 是「系統配位」（前端 seatReserve 預設值），拿到 orderId 後座位就
定了，直接進結帳；設 False 會多一頁自己點位子，搶票時沒有意義還多花時間。
"""
import time

from curl_cffi import requests as cf_requests

from ticketplus_api import QUEUE_API, TICKET_API
from ticketplus_api.session import build_headers


def build_payload(product_id: str, count: int, seat_assignment: bool = False,
                  serial_number: str = "") -> dict:
    payload = {"products": [{"productId": product_id, "count": count}]}
    if seat_assignment:
        payload["reserveSeats"] = True
        payload["consecutiveSeats"] = False
        payload["finalizedSeats"] = True      # 系統配位
    if serial_number:
        payload["serialNumber"] = serial_number
    return payload


# errCode 對照表：從前端 `errorHandler` 的分支 × /assets/lang/tw.json 的訊息湊出來的
# （官方沒有公開文件）。TicketPlus 一律回 HTTP 200，成敗全看 errCode，別看 status code。
ERR_MESSAGES = {
    "00": "成功",
    "101": "操作異常，官方會強制登出重登",
    "102": "查無資料（S3 靜態檔不存在，通常是 id 打錯）",
    "103": "token 無效／已過期，要重新登入",
    "110": "流量管制，重試即可",
    "111": "票種已售完或超過本活動購票上限",
    "112": "票種已售完或超過本活動購票上限",
    "113": "該票區已無座位",
    "114": "購票逾時",
    "115": "票種已售完或超過購票上限（官方會踢回活動頁）",
    "116": "座位已售完／無連續座位",
    "121": "票況已變（前端自己產的碼，不是伺服器回的）",
    "124": "專屬代碼錯誤／問答題答錯（PRESALE_CODE 填錯）",
    "125": "專屬代碼已被使用過，要換一組新的",
    "135": "圖形驗證碼驗證失敗",
    "136": "圖形驗證碼已過期",
    "137": "排隊中／處理中，照 waitSecond 等",
    "999": "請求本身失敗（前端 axios catch 用的碼，代表網路或 HTTP 層掛了）",
}

# token 死掉時官方回 HTTP 200 + errCode 103，重試再多次也不會活過來 —— 直接中止，
# 讓使用者去重登；不標成致命的話 poll 迴圈會對每個票區一直空打。
AUTH_ERR_CODES = {"101", "103"}

# 這些是「這個票種沒了」，換下一個票區馬上重試，不用等
SOLDOUT_ERR_CODES = {"111", "112", "113", "115", "116", "121"}

# 專屬代碼／驗證碼問題：重試一百次也不會過，直接中止叫人處理
#   124/125 = PRESALE_CODE 填錯或已被用掉
#   135/136 = 這場有圖形驗證碼（本 bot 未實作，見 CLAUDE.md）
CODE_ERR_CODES = {"124", "125", "135", "136"}

# 排隊中 —— 不是錯誤，log 要顯示成「排隊中」而不是一串 errCode
QUEUE_WAIT_CODES = {"137"}

# 這些碼呼叫端會自己印一行看得懂的訊息（含 RTT），_post 就不要再印技術行洗版
_QUIET_CODES = QUEUE_WAIT_CODES | {"00"}


def describe(code) -> str:
    """errCode → 人看得懂的說明，未知碼原樣回。"""
    return ERR_MESSAGES.get(str(code), "未知錯誤碼")


def is_auth_error(status: int, data: dict) -> bool:
    return status in (401, 403) or str(data.get("errCode")) in AUTH_ERR_CODES


def is_fatal(status: int, data: dict) -> bool:
    """重試也不會好的錯：token 死了、專屬代碼錯、需要圖形驗證碼。"""
    return is_auth_error(status, data) or str(data.get("errCode")) in CODE_ERR_CODES


def _post(session: cf_requests.Session, url: str, payload: dict, token: str,
          label: str, timeout: float = 20.0):
    """回 (status, data, rtt_ms)。errCode 137（排隊中）不在這裡印 —— 那不是錯誤，
    交給呼叫端印一行看得懂的「排隊中」，不要用技術性錯誤訊息嚇人。"""
    t0 = time.perf_counter()
    res = session.post(url, json=payload, headers=build_headers(token), timeout=timeout)
    rtt = (time.perf_counter() - t0) * 1000
    try:
        data = res.json()
    except Exception:
        data = {"errCode": "999", "errMsg": res.text[:120]}
    code = str(data.get("errCode"))
    if code not in _QUIET_CODES:
        print(f"[{label}] HTTP {res.status_code} errCode={code} — {describe(code)} "
              f"RTT {rtt:.0f}ms")
    return res.status_code, data, rtt


def enqueue(session: cf_requests.Session, payload: dict, token: str,
            max_wait: float = 300.0) -> dict:
    """排隊直到拿到 uuid。回 {"uuid": …} / {"currentReservedOrderId": …} / {"error": …}。

    errCode 不是 "00" 時：
      waitSecond → 官方叫我們等 N 秒再排（照做，硬打只會被推更後面）
      localCheck → 叫前端重新確認票況（我們當作「再排一次」）
    """
    started = time.monotonic()
    deadline = started + max_wait
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        status, data, rtt = _post(session, f"{QUEUE_API}/enqueue", payload, token, "ENQUEUE")
        waited = time.monotonic() - started
        if data.get("errCode") == "00":
            if data.get("currentReservedOrderId"):
                print(f"[ENQUEUE] 已有保留中的訂單: {data['currentReservedOrderId']}")
                return {"currentReservedOrderId": data["currentReservedOrderId"]}
            print(f"[ENQUEUE] ✅ 排到了！（第 {attempt} 次，共排 {waited:.1f}s，"
                  f"RTT {rtt:.0f}ms） uuid={data.get('uuid')}")
            return {"uuid": data.get("uuid"), "queue_s": waited}
        if is_fatal(status, data):
            return {"error": f"errCode={data.get('errCode')} {describe(data.get('errCode'))}",
                    "fatal": True}
        wait = data.get("waitSecond")
        if wait:
            print(f"[ENQUEUE] ⏳ 排隊中… 官方要求 {wait}s 後再排"
                  f"（第 {attempt} 次，已排 {waited:.1f}s，RTT {rtt:.0f}ms）")
            time.sleep(float(wait))
        elif data.get("localCheck"):
            print(f"[ENQUEUE] ⏳ 排隊中… 官方要求重新確認票況（第 {attempt} 次）")
            time.sleep(0.5)
        else:
            return {"error": f"errCode={data.get('errCode')} {describe(data.get('errCode'))}"}
    return {"error": f"排隊超過 {max_wait}s 未輪到"}


def reserve(session: cf_requests.Session, payload: dict, uuid: str, token: str,
            max_wait: float = 180.0, default_wait: float = 15.0) -> dict:
    """用 uuid 換 orderId。回 {"orderId": …, "raw": data} 或 {"error": …}。"""
    body = {**payload, "uuid": uuid}
    started = time.monotonic()
    deadline = started + max_wait
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        status, data, rtt = _post(session, f"{TICKET_API}/reserve", body, token, "RESERVE")
        if data.get("orderId"):
            spent = time.monotonic() - started
            print(f"[RESERVE] 🎉 訂單已保留 orderId={data['orderId']}"
                  f"（第 {attempt} 次，耗時 {spent:.1f}s，RTT {rtt:.0f}ms）")
            return {"orderId": data["orderId"], "raw": data, "reserve_s": spent}
        code = str(data.get("errCode"))
        if is_fatal(status, data):
            return {"error": f"errCode={code} {describe(code)}", "fatal": True}
        if code == "999":
            # 前端 axios catch 用的碼 → 網路/HTTP 層問題，不是票的問題，值得重試
            print(f"[RESERVE] {describe(code)}，{default_wait}s 後重試（第 {attempt} 次）")
            time.sleep(default_wait)
            continue
        if code in QUEUE_WAIT_CODES:
            # 官方前端在這裡是「回頭重新 enqueue」而不是拿舊 uuid 重打（errorHandler
            # 的 137 分支：`"reserve"===t ? enquene() : nextStep(s)`）。uuid 是排隊憑證，
            # 過期的憑證再打幾次都不會變成訂單，得重新排一次拿新的。
            wait = float(data.get("waitSecond") or default_wait)
            print(f"[RESERVE] ⏳ 還在排隊中… {wait}s 後重新排隊（第 {attempt} 次，"
                  f"RTT {rtt:.0f}ms）")
            time.sleep(wait)
            return {"requeue": True}
        if status != 200:
            wait = float(data.get("waitSecond") or default_wait)
            print(f"[RESERVE] HTTP {status}，{wait}s 後重試（第 {attempt} 次）")
            time.sleep(wait)
            continue
        return {"error": f"errCode={code} {describe(code)}",
                "soldout": code in SOLDOUT_ERR_CODES}
    return {"error": f"reserve 超過 {max_wait}s 未成功"}


def grab(session: cf_requests.Session, payload: dict, token: str,
         max_requeue: int = 5) -> dict:
    """enqueue → reserve 一條龍。回 {"orderId": …, "queue_s", "reserve_s"} 或 {"error": …}。
    reserve 回「還在排隊」時整個循環重來（重新 enqueue 拿新 uuid），最多 max_requeue 次。"""
    queue_s = 0.0
    for round_no in range(1, max_requeue + 1):
        queued = enqueue(session, payload, token)
        if queued.get("error"):
            return queued
        if queued.get("currentReservedOrderId"):
            return {"orderId": queued["currentReservedOrderId"], "resumed": True,
                    "queue_s": queue_s + queued.get("queue_s", 0.0), "reserve_s": 0.0}
        queue_s += queued.get("queue_s", 0.0)
        result = reserve(session, payload, queued["uuid"], token)
        if not result.get("requeue"):
            result["queue_s"] = queue_s
            return result
        print(f"[GRAB] 重新排隊（第 {round_no}/{max_requeue} 輪）")
    return {"error": f"重新排隊 {max_requeue} 輪仍未拿到訂單"}


def _to_epoch(value) -> float | None:
    """expiryTimestamp 可能是 epoch 毫秒或 ISO 字串（前端一律丟給 new Date()）。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e11 else float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize(raw: dict) -> list[str]:
    """從 reserve 回應擠出對人有用的資訊（結帳期限、座位）。抓不到就略過該行，
    **不要因為欄位名猜錯就炸掉** —— 票已經到手了，摘要只是加分。"""
    lines = []
    products = raw.get("products") or []

    expiry = _to_epoch(products[0].get("expiryTimestamp")) if products else None
    if expiry:
        left = expiry - time.time()
        lines.append(f"結帳期限 {time.strftime('%H:%M:%S', time.localtime(expiry))}"
                     f"（剩 {int(left // 60)} 分 {int(left % 60)} 秒）")

    seats = [s for p in products for s in (p.get("seats") or [])]
    if seats:
        # 2026-07-29 實測輸出過 "a000008654 工作表1 1 23 VVIP" —— ticketAreaId 因為含
        # "area" 被誤撿進來，且座位圖是從 Excel 匯的所以有 "工作表1" 這種雜訊欄位。
        # 對策：先砍掉一切 id 欄位，再照「區 → 排 → 號」的偏好順序組標籤。
        lines.append(f"座位 {' / '.join(_seat_label(s) for s in seats)}")
        lines.append(f"（座位欄位: {sorted(seats[0].keys())}）")
    return lines


_SEAT_KEY_ORDER = ("ticketareaname", "areaname", "zonename",
                   "rowname", "row", "rowno",
                   "seatname", "seatno", "seatnumber", "seat", "number")


def _seat_label(seat: dict) -> str:
    """座位 dict → 一句人看得懂的標籤。欄位名沒有官方文件，撿不到就整包壓字串。"""
    usable = {k.lower(): v for k, v in seat.items()
              if isinstance(v, (str, int)) and not k.lower().endswith("id")}
    picked = [str(usable[k]) for k in _SEAT_KEY_ORDER if k in usable]
    if picked:
        return " ".join(picked)
    return " ".join(str(v) for v in usable.values()) or str(seat)
