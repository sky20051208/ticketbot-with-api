"""跨步驟共用的控制流例外 + response 檢查 helper（零依賴）。

步驟模組撞 403 一律 `raise_if_blocked(res, "位置label")`；
音效 / cooldown / 重試策略全部由 runner 的 except Blocked403 統一處理，
步驟模組不需要知道 403 之後會發生什麼事。
"""


class Blocked403(Exception):
    """Server 回 403 → runner 攔下做 cooldown + 同 state 重試（不 fallback GAME）。
    str(exc) = 觸發位置 label（GAME / AREA / TICKET GET / TICKET POST / ORDER / VERIFY）。"""


def raise_if_blocked(res, label: str):
    """res.status_code == 403 時 raise Blocked403(label)，其餘放行。"""
    if res.status_code == 403:
        raise Blocked403(label)
