"""kham_api.reserve 送單封包測試（不連網）。

GOLDEN 是拿官方 UTK0205.min.js 的 addShoppingCart 原封不動在 node 跑出來的 body
（jQuery 用 mock，2026-09-17）。三個真實選位頁比對過都逐字相同 —— 官方改版時重產這串。
"""
from kham_api import parsing, reserve

FIELDS = {
    "PERFORMANCE_ID": "P1D430L4", "PRODUCT_ID": "P1D3G65D", "PRODUCT_CATEGORY_ID": "205",
    "GROUP_ID": "14", "PLACE_ID": "P1D40WMK", "PRICE_AREA_ID": "P1D9ZT70", "LOGIN_ID": "",
    "ACTIVITY_GROUP_ID": "", "ACTIVITY_GROUP_ITEM_ID": "", "QUANTITY_LIMIT": "4", "NOT_MIX": "1",
    "CLOSE_3D": "0", "isMarketAmerica": "0", "NO_SELL_ON_WEBSITE": "0", "IS_NAME_BASED": "0",
    "DOC_MEMO": '{"P1D9ZT71":"AErN+ze4/q=="}',
}

GOLDEN = (
    "PERFORMANCE_ID=P1D430L4&PRODUCT_ID=P1D3G65D&PRODUCT_CATEGORY_ID=205&GROUP_ID=14"
    "&PLACE_ID=P1D40WMK&PERFORMANCE_PRICE_AREA_ID=P1D9ZT70&LOGIN_ID=&LOGIN_PWD=%E3%8E%9E"
    "&ACTIVITY_GROUP_ID=&ACTIVITY_GROUP_ITEM_ID=&QUANTITY_LIMIT=4&NOT_MIX=1&CLOSE_3D=0"
    "&isMarketAmerica=0&NO_SELL_ON_WEBSITE=0&IS_NAME_BASED=0&CHK_VERIFY=AB3D"
    '&DOC_MEMO={"P1D9ZT71":"AErN+ze4/q=="}'
    "&action=ADD_SHOPPING_CAR&SEATS=%5B%7B%22TYPE_ID%22%3A%22P1D9ZT71%22%2C%22TYPE_NAME%22"
    "%3A%22%E5%8E%9F%E5%83%B9%22%2C%22PRICE%22%3A%223%2C880%22%2C%22SEAT%22%3A%222%E6%A8%93"
    "%E7%B4%852B%E5%8D%80-2%E6%8E%92-16%E8%99%9F%22%7D%2C%7B%22TYPE_ID%22%3A%22P1D9ZT71%22"
    "%2C%22TYPE_NAME%22%3A%22%E5%8E%9F%E5%83%B9%22%2C%22PRICE%22%3A%223%2C880%22%2C%22SEAT"
    "%22%3A%222%E6%A8%93%E7%B4%852B%E5%8D%80-2%E6%8E%92-17%E8%99%9F%22%7D%5D"
    "&_INFO=%7B%22loadtime%22%3A%222026%2F09%2F17%2022%3A18%3A05%22%2C%22timestamp%22"
    "%3A1789654685123%7D&sender=jquery"
)


def test_body_matches_official_js():
    seats = parsing.build_add_cart_seats(
        [{"label": "2樓紅2B區-2排-16號"}, {"label": "2樓紅2B區-2排-17號"}],
        "P1D9ZT71", "原價-NT$3,880")
    info = parsing.build_info("2026/09/17 22:18:05", 1789654685123)
    assert reserve.build_body(FIELDS, "AB3D", seats, info) == GOLDEN


def test_doc_memo_is_not_encoded():
    # 官方就是原樣串進去（`+` 到伺服器會變空白），別幫它 encode
    body = reserve.build_body(FIELDS, "AB3D", "[]", "{}")
    assert '&DOC_MEMO={"P1D9ZT71":"AErN+ze4/q=="}&' in body


def test_redirect_target():
    assert reserve._redirect_target("top.location.href = 'UTK0206_.aspx';") == (
        "https://kham.com.tw/application/UTK02/UTK0206_.aspx")
    assert reserve._redirect_target("alert1('驗證碼錯誤');") is None


# --------- add_to_cart 流程（mock 掉頁內 fetch 跟驗證碼，不連網） ---------

SEAT_URL = "UTK0205_.aspx?PERFORMANCE_ID=P1D430L4&GROUP_ID=14&PERFORMANCE_PRICE_AREA_ID=P1D9ZT70"


def _seat_html(seat_str="上:0:0,0,,A區-1排-2號.1,0,,A區-1排-1號", types=None):
    types = types if types is not None else [("P1D9ZT71", "原價-NT$3,880")]
    return (
        "<script>var _ul = '1'; var _info = {'loadtime':'2026/09/17 22:18:05'};</script>"
        '<input type="hidden" id="PERFORMANCE_ID" value="P1D430L4" />'
        '<input type="hidden" id="QUANTITY_LIMIT" value="4" />'
        + "".join(f"<button onclick=\"setType('{i}','{z}');return false;\"></button>" for i, z in types)
        + f"<script>xMax = 2;yMax = 1;seatStr = '{seat_str}';maxWaitTime = 0;</script>"
    )


class _Fake:
    """記錄每一發 POST；responses 依序吐回應，captchas 依序吐辨識結果。"""

    def __init__(self, monkeypatch, responses, captchas):
        self.posts = []
        self._responses = list(responses)
        self._captchas = list(captchas)

        async def fake_captcha_bytes(tab, referrer):
            assert referrer == SEAT_URL
            return b"img"

        async def fake_fetch(tab, url, **kw):
            self.posts.append({"url": url, **kw})
            return {"ok": True, "status": 200, "text": self._responses.pop(0)}

        monkeypatch.setattr(reserve, "_get_captcha_bytes", fake_captcha_bytes)
        monkeypatch.setattr(reserve, "page_fetch", fake_fetch)
        monkeypatch.setattr(reserve.captcha, "recognize", lambda b: self._captchas.pop(0))


def _run(html, **kw):
    import asyncio
    args = dict(amount=2, keyword="", exclude="輪椅;身障;身心")
    args.update(kw)
    return asyncio.run(reserve.add_to_cart(None, SEAT_URL, html, **args))


def test_success_after_wrong_captcha(monkeypatch):
    fake = _Fake(monkeypatch,
                 responses=["alert1('驗證碼錯誤！');", "top.location.href = 'UTK0206_.aspx';"],
                 captchas=["AB3", "WRNG", "AB3D"])  # 第一張長度不對 → 不送、直接換
    cart, reason = _run(_seat_html())
    assert cart == "https://kham.com.tw/application/UTK02/UTK0206_.aspx"
    assert reason == reserve.REASON_OK
    assert len(fake.posts) == 2
    post = fake.posts[-1]
    assert post["method"] == "POST" and post["referrer"] == SEAT_URL
    assert "&CHK_VERIFY=AB3D&" in post["body"]
    assert "timestamp" in reserve.urllib.parse.unquote(post["body"])


def test_no_free_seat_is_retryable(monkeypatch):
    fake = _Fake(monkeypatch, responses=[], captchas=[])
    assert _run(_seat_html(seat_str="上:1:0,0,,A區-1排-2號")) == (None, reserve.REASON_NO_SEAT)
    assert fake.posts == []


def test_no_ticket_type_is_not_retryable(monkeypatch):
    _Fake(monkeypatch, responses=[], captchas=[])
    html = _seat_html(types=[("P1DBFS01", "身心障礙票-NT$1,940")])
    assert _run(html) == (None, reserve.REASON_NO_TYPE)


def test_require_full_amount(monkeypatch):
    _Fake(monkeypatch, responses=[], captchas=[])
    html = _seat_html(seat_str="上:0:0,0,,A區-1排-2號")
    assert _run(html, require_full=True) == (None, reserve.REASON_NOT_ENOUGH)


def test_unknown_response_is_retryable(monkeypatch):
    # 沒見過的訊息不能直接放棄：開賣瞬間回「尚未開賣」就是這一類，
    # 由 __main__ 的清票迴圈限制連續次數（MAX_UNKNOWN_ROUNDS）
    fake = _Fake(monkeypatch, responses=["alert1('您選擇的座位已被訂購');"], captchas=["AB3D"])
    assert _run(_seat_html()) == (None, reserve.REASON_UNKNOWN)
    assert len(fake.posts) == 1


def test_captcha_retries_exhausted(monkeypatch):
    fake = _Fake(monkeypatch, responses=["alert1('驗證碼錯誤');"] * 6, captchas=["WRNG"] * 6)
    assert _run(_seat_html()) == (None, reserve.REASON_CAPTCHA)
    assert len(fake.posts) == 6
