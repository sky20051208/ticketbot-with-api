"""FSM runner：根據當前 state 派發 handler，handler 回 next URL → classify → 下個 state。

當 step 被踢去非預期頁面（清票場景），runner 自動進對應 state 處理而非硬性從 GAME 重來。

典型 happy path:
  GAME → AREA → TICKET → QUEUE → CHECKOUT

被踢回場景（清票機制價值所在）:
  TICKET → submit 後 server 把你踢回 /ticket/area/...
    → classify = AREA → AREA handler 重新挑區
    → TICKET → submit 再來

  TICKET → submit 後 redirect 是 /activity/game/... (場次被關)
    → classify = GAME → 重新選場次

無 handler / UNKNOWN URL：fallback 回 GAME 從頭重來。
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

from curl_cffi import requests as cf_requests

import config
from tixcraftapi import BASE
from tixcraftapi.area import select_area
from tixcraftapi.game import select_game
from tixcraftapi.order import follow_order
from tixcraftapi.session import build_headers
from tixcraftapi.state import State, TERMINAL_BAD, TERMINAL_OK, classify
from tixcraftapi.submit import submit_ticket


@dataclass
class Context:
    """state transition 之間共享的 mutable context。"""
    session: cf_requests.Session
    slug: str
    area_url: Optional[str] = None
    ticket_url: Optional[str] = None
    redirect_url: Optional[str] = None
    result_url: Optional[str] = None
    iter_n: int = 0


Handler = Callable[[Context], Optional[str]]


# ---------- handlers ----------

def _h_game(ctx: Context) -> Optional[str]:
    headers = build_headers(referer=f"{BASE}/activity/detail/{ctx.slug}")
    area_url = select_game(ctx.session, ctx.slug, headers, config.DATE_KEYWORD)
    if area_url:
        ctx.area_url = area_url
    return area_url


def _h_area(ctx: Context) -> Optional[str]:
    """進 area page → 挑區 → 回 ticket_url。
    若 ctx.area_url 不存在（FSM 直接 transition 過來但沒帶 URL），fallback GAME。"""
    if not ctx.area_url:
        return None
    headers = build_headers(referer=ctx.area_url)
    ticket_url = select_area(ctx.session, ctx.area_url, headers, config.AREA_KEYWORD)
    if ticket_url:
        ctx.ticket_url = ticket_url
    return ticket_url


def _h_ticket(ctx: Context) -> Optional[str]:
    """進 ticket page → submit → 回 redirect URL（可能是 /ticket/order、/checkout、/ticket/area、...）。"""
    if not ctx.ticket_url:
        return None
    headers = build_headers(referer=ctx.ticket_url)
    redirect_url = submit_ticket(ctx.session, ctx.ticket_url, headers)
    if redirect_url:
        ctx.redirect_url = redirect_url
    return redirect_url


def _h_queue(ctx: Context) -> Optional[str]:
    """進排隊頁 → 輪詢 /ticket/check 等到 checkout。"""
    if not ctx.redirect_url:
        return None
    headers = build_headers(referer=ctx.redirect_url)
    return follow_order(ctx.session, ctx.redirect_url, headers)


def _h_activity(ctx: Context) -> Optional[str]:
    """被踢回活動詳情頁（場次未開放 / 被擋下）→ 觸發 fallback 回 GAME 重來。"""
    return None


def _h_verify(ctx: Context) -> Optional[str]:
    """落到 verify 頁通常已被 select_area 內部處理。若 FSM 直接到這代表流程亂了，重來。"""
    return None


DEFAULT_HANDLERS: dict[State, Handler] = {
    State.GAME: _h_game,
    State.AREA: _h_area,
    State.TICKET: _h_ticket,
    State.QUEUE: _h_queue,
    State.ACTIVITY: _h_activity,
    State.VERIFY: _h_verify,
}


# ---------- main loop ----------

def run(ctx: Context,
        handlers: dict[State, Handler] = DEFAULT_HANDLERS,
        initial: State = State.GAME,
        max_iter: int = 30) -> Optional[str]:
    """跑 FSM 直到 terminal state 或 max_iter。回成功 URL 或 None。

    Cooldown 規則：
      - AREA state 第 1 次進入：0ms（第一拍黃金時間不能浪費）
      - AREA state 第 2+ 次進入：5s（被踢回 = 票剛被搶光，等 server 冷靜並讓票可能釋出）
      - 其他 state 不加額外 cooldown（handler 失敗 / UNKNOWN 仍有 RETRY_INTERVAL）
    """
    AREA_RETRY_COOLDOWN = 5.0
    visit_count: dict[State, int] = {}

    state = initial
    for i in range(max_iter):
        ctx.iter_n = i + 1
        visit_count[state] = visit_count.get(state, 0) + 1

        # 清票場景：AREA 被重複進入時加 cooldown
        if state == State.AREA and visit_count[state] > 1:
            print(f"[FSM] AREA 重複進入第 {visit_count[state]} 次，cooldown {AREA_RETRY_COOLDOWN}s")
            time.sleep(AREA_RETRY_COOLDOWN)

        print(f"\n[FSM #{i + 1}] state={state.value}  (visit #{visit_count[state]})")

        handler = handlers.get(state)
        if handler is None:
            print(f"[FSM] no handler for {state.value}，fallback GAME")
            state = State.GAME
            time.sleep(config.RETRY_INTERVAL)
            continue

        next_url = handler(ctx)
        if next_url is None:
            print(f"[FSM] {state.value} handler 失敗，fallback GAME")
            state = State.GAME
            time.sleep(config.RETRY_INTERVAL)
            continue

        new_state = classify(next_url)
        print(f"[FSM] {state.value} → {new_state.value}  ({next_url})")

        if new_state in TERMINAL_OK:
            ctx.result_url = next_url
            return next_url
        if new_state in TERMINAL_BAD:
            print(f"[FSM] terminal failure: {new_state.value}")
            return None
        if new_state == State.UNKNOWN:
            print(f"[FSM] UNKNOWN URL → fallback GAME")
            state = State.GAME
            time.sleep(config.RETRY_INTERVAL)
            continue

        state = new_state

    print(f"[FSM] max_iter {max_iter} 達到，放棄")
    return None
