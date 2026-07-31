"""FSM runner：根據當前 state 派發 handler，handler 回 next URL → classify → 下個 state。

耦合規則（讓步驟模組保持乾淨的關鍵，改步驟時遵守）：
  - 步驟函式只收 (session, url, 明確參數)、回「落點 URL 或 None」，不讀 config、不認識彼此
  - config 只在這層（handler）讀，call-time 讀值所以 GUI override 一定生效
  - URL 語意判斷只有 state.classify 一份，步驟不自己解讀 redirect
  - 跨步驟共享狀態只走 Context（captcha prefetch 也掛這），沒有暗渡通道
  - transition 時 runner 把落點 URL 寫進對應 canonical 欄位（area_url / ticket_url / ...），
    handler 只讀自己的欄位，不猜別人留下什麼

典型 happy path:
  GAME → AREA → TICKET → QUEUE → CHECKOUT

被踢回場景（清票機制價值所在）:
  TICKET → submit 後 server 踢回 /ticket/area/... → classify=AREA → 重新挑區（cooldown 5s）
  AREA → 被導去 /ticket/verify/... → classify=VERIFY → handle_verify → 回 AREA（不吃 cooldown）
  QUEUE → 落到 /login → classify=LOGIN_FAIL → terminal，不再拿死 cookie 空轉

無 handler / UNKNOWN URL：fallback 回 GAME 從頭重來。
"""
import os
import itertools
import time
from dataclasses import dataclass
from typing import Callable, Optional

from curl_cffi import requests as cf_requests

import config
from LineBot import line_push
from tixcraftapi import BASE, alerts
from tixcraftapi.area import select_area
from tixcraftapi.captcha import CaptchaPrefetch
from tixcraftapi.errors import Blocked403
from tixcraftapi.game import select_game
from tixcraftapi.order import follow_order
from tixcraftapi.session import build_headers
from tixcraftapi.state import State, TERMINAL_BAD, TERMINAL_OK, classify
from tixcraftapi.submit import submit_ticket
from tixcraftapi.verify import handle_verify


@dataclass
class Context:
    """state transition 之間共享的 mutable context。
    URL 欄位由 runner 在 transition 時寫入（canonical），handler 只讀。"""
    session: cf_requests.Session
    slug: str
    area_url: Optional[str] = None
    ticket_url: Optional[str] = None
    queue_url: Optional[str] = None
    verify_url: Optional[str] = None
    result_url: Optional[str] = None
    captcha_prefetch: Optional[CaptchaPrefetch] = None
    area_html: Optional[str] = None     # polling 直接抓到的 area 頁（用一次就清）
    pool: Optional[object] = None       # session.WarmPool：並行請求用的常駐熱連線
    iter_n: int = 0


Handler = Callable[[Context], Optional[str]]


# ---------- handlers（唯一讀 config 的地方；把值注入步驟函式） ----------

def _h_game(ctx: Context) -> Optional[str]:
    headers = build_headers(referer=f"{BASE}/activity/detail/{ctx.slug}")
    return select_game(ctx.session, ctx.slug, headers, config.DATE_KEYWORD)


def _h_area(ctx: Context) -> Optional[str]:
    if not ctx.area_url:
        return None
    headers = build_headers(referer=ctx.area_url)
    # captcha prefetch 跟 area 頁 GET 並行跑，把 captcha GET+OCR 從 submit critical path 砍掉。
    # 從這裡到 submit POST 之間，session 不可再 GET /ticket/captcha（server 只認最後一張）。
    # 重複進 AREA 會建新的 prefetch 蓋掉舊的 — 跟「最後一張才算」的 server 行為一致。
    ctx.captcha_prefetch = CaptchaPrefetch(ctx.session, headers, pool=ctx.pool)
    # polling 若已把 area 頁抓回來就直接用，用完立刻清（重進 AREA 一定要拿新的頁面，
    # 舊 HTML 的餘票狀態早就過期了）
    prefetched, ctx.area_html = ctx.area_html, None
    return select_area(ctx.session, ctx.area_url, headers,
                       area_keyword=config.AREA_KEYWORD,
                       exclude_keyword=config.EXCLUDE_AREA_KEYWORD,
                       strategy=config.AREA_AUTO_SELECT_MODE,
                       prefetched_html=prefetched)


def _h_verify(ctx: Context) -> Optional[str]:
    """presale 驗證頁：驗過後回 area_url 重進 AREA（prev=VERIFY 不吃 AREA cooldown）。"""
    if not ctx.verify_url:
        return None
    # Referer 帶觸發 redirect 的 area 頁（跟真瀏覽器行為一致）
    headers = build_headers(referer=ctx.area_url or ctx.verify_url)
    if handle_verify(ctx.session, ctx.verify_url, headers,
                     presale_code=config.PRESALE_CODE):
        return ctx.area_url  # None 時自然 fallback GAME
    return None


def _h_ticket(ctx: Context) -> Optional[str]:
    if not ctx.ticket_url:
        return None
    headers = build_headers(referer=ctx.ticket_url)
    # prefetch 用過即清，避免下一個 ticket_url 誤用舊結果
    prefetch, ctx.captcha_prefetch = ctx.captcha_prefetch, None
    return submit_ticket(ctx.session, ctx.ticket_url, headers,
                         ticket_amount=config.TICKET_AMOUNT,
                         max_rounds=config.TICKET_CAPTCHA_RETRY,
                         prefetch=prefetch, pool=ctx.pool)


def _h_queue(ctx: Context) -> Optional[str]:
    if not ctx.queue_url:
        return None
    headers = build_headers(referer=ctx.queue_url)
    return follow_order(ctx.session, ctx.queue_url, headers,
                        poll_interval=config.ORDER_POLL_INTERVAL,
                        poll_max=config.ORDER_POLL_MAX)


def _h_activity(ctx: Context) -> Optional[str]:
    """被踢回活動詳情頁（場次未開放 / 被擋下）→ 觸發 fallback 回 GAME 重來。"""
    return None


DEFAULT_HANDLERS: dict[State, Handler] = {
    State.GAME: _h_game,
    State.AREA: _h_area,
    State.VERIFY: _h_verify,
    State.TICKET: _h_ticket,
    State.QUEUE: _h_queue,
    State.ACTIVITY: _h_activity,
}

# transition 時把落點 URL 寫進的 canonical 欄位
_URL_FIELD: dict[State, str] = {
    State.AREA: "area_url",
    State.TICKET: "ticket_url",
    State.QUEUE: "queue_url",
    State.VERIFY: "verify_url",
}


# ---------- main loop ----------

def _check_pause() -> None:
    """PAUSE：GUI 在 profiles/acc_{ACC_ID}/pause.lock 寫檔 → 在此阻塞到檔案被刪除。
    只在 FSM state 邊界（run() 迴圈頂端）呼叫，不進 handler 內的網路熱迴圈
    （poll_until_open / submit / follow_order），所以搶票熱路徑零影響。
    軟暫停：正在排隊/結帳途中暫停太久，server 端名額/session/prefetch 驗證碼可能過期，
    恢復後不保證搶得回；適合開搶前或清票重試空檔用。"""
    lock = os.path.join(config.BASE_DIR, "profiles", f"acc_{config.ACC_ID}", "pause.lock")
    if not os.path.exists(lock):
        return
    print("[PAUSE] 偵測到 pause.lock，暫停中…（GUI 按繼續 / 刪檔即恢復）")
    while os.path.exists(lock):
        time.sleep(0.3)
    print("[PAUSE] 已繼續")


def run(ctx: Context,
        handlers: dict[State, Handler] = DEFAULT_HANDLERS,
        initial: State = State.GAME,
        max_iter: Optional[int] = 30) -> Optional[str]:
    """跑 FSM 直到 terminal state 或 max_iter。回成功 URL 或 None。
    max_iter=None 表示無限制次數（只能靠 terminal state 或外部 stop 結束）。

    Cooldown 規則：
      - AREA state 第 1 次進入：0ms（第一拍黃金時間不能浪費）
      - AREA state 第 2+ 次進入：5s（被踢回 = 票剛被搶光，等 server 冷靜並讓票可能釋出）
        例外：從 VERIFY 回來、剛做完 403 cooldown → 不疊加
      - 其他 state 不加額外 cooldown（handler 失敗 / UNKNOWN 仍有 RETRY_INTERVAL）
    """
    AREA_RETRY_COOLDOWN = 5.0
    BLOCKED_COOLDOWN = 8.0  # 撞 403 時的 cooldown（比 AREA 5s 久，給 server / IP 喘息）
    visit_count: dict[State, int] = {}
    blocked_403_streak = 0
    just_did_403_cooldown = False  # 剛睡完 8s → 跳過下一輪 AREA 5s cooldown，避免疊加

    state = initial
    prev_state: Optional[State] = None
    iterator = itertools.count() if max_iter is None else range(max_iter)
    for i in iterator:
        _check_pause()  # state 邊界檢查暫停；熱迴圈不碰，不影響搶票速度
        ctx.iter_n = i + 1
        visit_count[state] = visit_count.get(state, 0) + 1

        # 清票場景：AREA 被重複進入時加 cooldown
        # （剛 403 過不疊加；從 VERIFY 驗證回來也不算「被踢回」，不能吃 cooldown）
        if (state == State.AREA and visit_count[state] > 1
                and not just_did_403_cooldown
                and prev_state != State.VERIFY):
            print(f"[FSM] AREA 重複進入第 {visit_count[state]} 次，cooldown {AREA_RETRY_COOLDOWN}s")
            time.sleep(AREA_RETRY_COOLDOWN)
        just_did_403_cooldown = False

        print(f"\n[FSM #{i + 1}] state={state.value}  (visit #{visit_count[state]})")

        handler = handlers.get(state)
        if handler is None:
            print(f"[FSM] no handler for {state.value}，fallback GAME")
            prev_state, state = state, State.GAME
            time.sleep(config.RETRY_INTERVAL)
            continue

        t_handler = time.perf_counter()
        try:
            next_url = handler(ctx)
            blocked_403_streak = 0  # handler 沒 raise 403 → reset streak
        except Blocked403 as e:
            blocked_403_streak += 1
            alerts.play_403(str(e))
            print(f"[FSM] {state.value} 撞 403 @{e}（連續第 {blocked_403_streak} 次），"
                  f"cooldown {BLOCKED_COOLDOWN}s 後同 state 重試")
            time.sleep(BLOCKED_COOLDOWN)
            just_did_403_cooldown = True
            continue
        except Exception as e:
            # 網路類錯誤（timeout, connection reset 等）→ 同 state 重試
            # 避免 GAME→AREA 整圈白繞、保住已抓到的 area_url / ticket_url
            exc_name = type(e).__name__
            is_network = ("Timeout" in exc_name) or ("Connection" in exc_name) or ("Curl" in exc_name)
            if is_network:
                print(f"[FSM] {state.value} 網路錯誤 ({exc_name})，同 state 重試 {config.RETRY_INTERVAL}s 後")
                time.sleep(config.RETRY_INTERVAL)
                continue
            # 邏輯類例外 → fallback GAME 重來
            print(f"[FSM] {state.value} handler 例外 ({exc_name}: {e})，fallback GAME")
            prev_state, state = state, State.GAME
            time.sleep(config.RETRY_INTERVAL)
            continue
        handler_ms = (time.perf_counter() - t_handler) * 1000

        if next_url is None:
            print(f"[FSM] {state.value} handler 失敗 ({handler_ms:.0f}ms)，fallback GAME")
            prev_state, state = state, State.GAME
            time.sleep(config.RETRY_INTERVAL)
            continue

        new_state = classify(next_url)
        print(f"[FSM] {state.value} → {new_state.value}  ({handler_ms:.0f}ms)  {next_url}")

        if new_state in TERMINAL_OK:
            alerts.play_checkout()
            ctx.result_url = next_url
            if config.ENABLE_LINE_NOTIFY and config.LINE_USER_ID:
                line_push.notify_grabbed(config.LINE_USER_ID,
                                         slug=ctx.slug,
                                         amount=config.TICKET_AMOUNT,
                                         fee=config.TICKET_FEE)
            return next_url
        if new_state in TERMINAL_BAD:
            print(f"[FSM] terminal failure: {new_state.value}")
            return None
        if new_state == State.UNKNOWN:
            print(f"[FSM] UNKNOWN URL → fallback GAME")
            prev_state, state = state, State.GAME
            time.sleep(config.RETRY_INTERVAL)
            continue

        # 落點 URL 寫進 canonical 欄位，下個 handler 直接讀（不猜舊值）
        field = _URL_FIELD.get(new_state)
        if field:
            setattr(ctx, field, next_url)

        prev_state, state = state, new_state

    # max_iter=None 時不會走到這（迴圈無限），只有 max_iter 達上限才會到這
    print(f"[FSM] max_iter {max_iter} 達到，放棄")
    return None
