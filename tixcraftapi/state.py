"""FSM state + URL classifier。

清票機制核心：搶票任何 step 失敗、被拓元 redirect 到某 URL 後，
classify(url) → State enum → runner 派發到該 state 對應 handler，自動繼續而非從頭。

新增「被踢去的頁面」處理只要：
  1. 在 State 加 enum
  2. 在 classify() 加 URL pattern 匹配
  3. 在 runner 的 HANDLERS 加 handler 函式
"""
from enum import Enum


class State(Enum):
    GAME = "GAME"             # /activity/game/{slug}
    AREA = "AREA"             # /ticket/area/{slug}/{game_id}
    VERIFY = "VERIFY"         # /ticket/verify/... 或 /activity/verify/...
    TICKET = "TICKET"         # /ticket/ticket/{slug}/{game}/{area}/{?}
                              # 最後那段不是票價也不是票區代碼（實測多個區共用同一個值），
                              # 只有 area 頁的 areaUrlList 給得出來，別想自己組
    QUEUE = "QUEUE"           # /ticket/order  (排隊頁)
    CHECKOUT = "CHECKOUT"     # /checkout  (terminal success)
    ACTIVITY = "ACTIVITY"     # /activity/detail/{slug}  (場次未開放 / 被踢回)
    LOGIN_FAIL = "LOGIN_FAIL" # /login  (terminal failure: cookie 失效)
    UNKNOWN = "UNKNOWN"


TERMINAL_OK: set[State] = {State.CHECKOUT}
TERMINAL_BAD: set[State] = {State.LOGIN_FAIL}


def classify(url: str) -> State:
    """URL pattern → State。順序重要：特定 pattern 先匹配避免被一般 pattern 吃掉。"""
    if "/checkout" in url:
        return State.CHECKOUT
    if "/login" in url:
        return State.LOGIN_FAIL
    if "/ticket/order" in url:
        return State.QUEUE
    if "/ticket/ticket/" in url:
        return State.TICKET
    if "/ticket/area/" in url:
        return State.AREA
    if "/verify/" in url or "/check-code/" in url:
        return State.VERIFY
    if "/activity/game/" in url:
        return State.GAME
    if "/activity/detail/" in url:
        return State.ACTIVITY
    return State.UNKNOWN
