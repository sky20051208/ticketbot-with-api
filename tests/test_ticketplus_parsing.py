"""ticketplus_api 純函式測試（不連網、隨時可跑）。

資料取自 2026-07-29 對 ticketplus.com.tw 活動 ac64dbcc…218e（男公館同樂會）的真實回應：
  S3 目錄 getS3?path=event/<加密id>/{sessions,ticketAreas,products}.json
  即時票況 config/api/v1/get?productId=…&ticketAreaId=…
"""
from ticketplus_api import crypto, parsing
from ticketplus_api.session import extract_token
from ticketplus_api.reserve import build_payload, is_auth_error


SESSIONS = [
    {"sessionId": "c03476164ea118ed9250804aedcf5d91", "name": "男公館同樂會",
     "date": "2026-08-23 ~ 2026-08-23", "time": "14:00 ~ 14:00",
     "location": "WESTAR", "ticketArea": True, "hidden": False, "sortedIndex": 1},
]

AREAS = [
    {"ticketAreaId": "a000008654", "sessionId": "c03476164ea118ed9250804aedcf5d91",
     "name": "VVIP", "price": 3880, "sortedIndex": 1, "hidden": False},
    {"ticketAreaId": "a000008655", "sessionId": "c03476164ea118ed9250804aedcf5d91",
     "name": "VIP", "price": 2880, "sortedIndex": 2, "hidden": False},
    {"ticketAreaId": "a000008656", "sessionId": "c03476164ea118ed9250804aedcf5d91",
     "name": "GA", "price": 2280, "sortedIndex": 3, "hidden": False},
    {"ticketAreaId": "a000008657", "sessionId": "c03476164ea118ed9250804aedcf5d91",
     "name": "身障席", "price": 1940, "sortedIndex": 4, "hidden": False},
]

PRODUCTS = [
    {"productId": "p000015682", "ticketAreaId": "a000008654", "name": "全票",
     "price": 3880, "sortedIndex": 3, "hidden": False},
    {"productId": "p000015684", "ticketAreaId": "a000008655", "name": "全票",
     "price": 2880, "sortedIndex": 3, "hidden": False},
    {"productId": "p000015686", "ticketAreaId": "a000008656", "name": "全票",
     "price": 2280, "sortedIndex": 3, "hidden": False},
    {"productId": "p000015687", "ticketAreaId": "a000008657", "name": "身障票",
     "price": 1940, "sortedIndex": 1, "hidden": False},
    {"productId": "p000015701", "ticketAreaId": "a000008657", "name": "身障陪同票",
     "price": 1940, "sortedIndex": 1, "hidden": False},
]

# 即時票況：注意 ticketArea 的 id 欄位叫 `id`，不叫 `ticketAreaId`
INFOS = {
    "product": [
        {"id": "p000015682", "status": "onsale", "productLimit": False, "count": 0,
         "purchaseLimit": 4, "seatAssignment": True},
        {"id": "p000015684", "status": "soldout", "productLimit": False, "count": 0,
         "purchaseLimit": 4, "seatAssignment": True},
        {"id": "p000015686", "status": "onsale", "productLimit": True, "count": 0,
         "purchaseLimit": 4, "seatAssignment": True},
    ],
    "ticketArea": [
        {"id": "a000008654", "status": "onsale", "ticketAreaLimit": True, "count": 999999},
        {"id": "a000008655", "status": "onsale", "ticketAreaLimit": True, "count": 999999},
        {"id": "a000008656", "status": "onsale", "ticketAreaLimit": True, "count": 0},
    ],
}


class TestCrypto:
    def test_decrypt_url_id(self):
        assert crypto.decrypt_id("ac64dbccc2c776702801bf33c82a218e") == "e000001451"

    def test_roundtrip(self):
        assert crypto.decrypt_id(crypto.encrypt_id("s000002136")) == "s000002136"

    def test_garbage_passthrough(self):
        assert crypto.decrypt_id("not-hex") == "not-hex"


class TestToken:
    def test_full_cookie_string(self):
        cookie = ('_ga=GA1.1.123; user=%7B%22access_token%22%3A%22eyJabc.def%22%2C'
                  '%22refresh_token%22%3A%22zzz%22%7D; other=1')
        assert extract_token(cookie) == "eyJabc.def"

    def test_raw_json_value(self):
        assert extract_token('{"access_token":"eyJabc.def"}') == "eyJabc.def"

    def test_bare_jwt(self):
        assert extract_token("eyJabc.def.ghi") == "eyJabc.def.ghi"

    def test_nothing(self):
        assert extract_token("_ga=GA1.1.123; foo=bar") == ""


class TestSelection:
    def test_select_session_by_keyword(self):
        assert parsing.select_session(SESSIONS, "08-23")["sessionId"].startswith("c034")

    def test_select_session_fallback(self):
        assert parsing.select_session(SESSIONS, "12-31") is SESSIONS[0]

    def test_areas_sorted_by_index(self):
        areas = parsing.areas_of_session(AREAS, "c03476164ea118ed9250804aedcf5d91")
        assert [a["name"] for a in areas] == ["VVIP", "VIP", "GA", "身障席"]

    def test_rank_excludes_and_prioritises(self):
        ranked = parsing.rank_areas(AREAS, keyword="GA", exclude="身障;輪椅",
                                    strategy="關鍵字優先")
        assert [a["name"] for a in ranked] == ["GA", "VVIP", "VIP"]

    def test_rank_bottom_up(self):
        ranked = parsing.rank_areas(AREAS, exclude="身障", strategy="由下而上")
        assert [a["name"] for a in ranked] == ["GA", "VIP", "VVIP"]

    def test_keyword_and_condition(self):
        ranked = parsing.rank_areas(AREAS, keyword="VIP+2880", strategy="關鍵字優先")
        assert ranked[0]["name"] == "VIP"


class TestAvailability:
    def test_index_infos_uses_id_field(self):
        products, areas = parsing.index_infos(INFOS)
        assert "p000015682" in products and "a000008654" in areas

    def test_unlimited_product_with_zero_count_is_buyable(self):
        # productLimit=False → count 0 不代表售完（前端也是這樣判的）
        assert parsing.product_ready(INFOS["product"][0]) is True

    def test_soldout_status(self):
        assert parsing.product_ready(INFOS["product"][1]) is False

    def test_limited_product_with_zero_count_is_out(self):
        assert parsing.product_ready(INFOS["product"][2]) is False

    def test_limited_area_with_zero_count_is_out(self):
        assert parsing.area_ready(INFOS["ticketArea"][2]) is False

    def test_sale_open_true_when_any_onsale(self):
        # INFOS 裡有 onsale 的票種 → 已開賣（走清票節奏，不是等開賣）
        products, _ = parsing.index_infos(INFOS)
        assert parsing.sale_open(products) is True

    def test_sale_open_false_when_none_onsale(self):
        # 開賣前的真實 status = "pending"（2026-08 實測未開賣活動）→ 等 T-0，全速輪詢
        infos = {"product": [
            {"id": "p1", "status": "pending", "count": None, "saleStart": "2026-08-01T12:00:00+08:00"},
            {"id": "p2", "status": "pending", "count": None},
        ], "ticketArea": []}
        products, _ = parsing.index_infos(infos)
        assert parsing.sale_open(products) is False

    def test_sale_open_false_when_empty(self):
        assert parsing.sale_open({}) is False


class TestPickTarget:
    def setup_method(self):
        self.products, self.areas = parsing.index_infos(INFOS)

    def _targets(self, areas, **kw):
        return parsing.rank_targets(PRODUCTS, areas, **kw)

    def test_skips_soldout_and_picks_next_priority(self):
        # 優先序 GA > VVIP > VIP，但 GA 票區已滿、VIP 票種售完 → 只剩 VVIP
        targets = self._targets(AREAS, keyword="GA", exclude="身障",
                                strategy="關鍵字優先")
        area, product, count = parsing.pick_target(
            targets, self.products, self.areas, amount=2)
        assert (area["name"], product["productId"], count) == ("VVIP", "p000015682", 2)

    def test_clamps_to_purchase_limit(self):
        targets = self._targets(AREAS, exclude="身障")
        _, _, count = parsing.pick_target(
            targets, self.products, self.areas, amount=10)
        assert count == 4

    def test_exclude_applies_to_product_name(self):
        # 身障席票區沒被排除，但票種名稱含排除詞 → 仍要跳過（且該區沒票況資料）
        targets = self._targets([AREAS[3]])
        assert parsing.pick_target(targets, self.products, self.areas,
                                   amount=1, exclude="身障") is None

    def test_no_ticket_returns_none(self):
        targets = self._targets([AREAS[2]])   # GA：票區已滿
        assert parsing.pick_target(targets, self.products, self.areas, amount=1) is None


# --------- 無劃位活動（ticketArea=False，ticketAreas.json 空陣列）---------
# 真實資料：27556b3a…ab73「我們搖滾——無妄合作社十週年演唱會」2026-07-29 觀察
NO_AREA_SESSIONS = [
    {"sessionId": "f84cb356ab1cec2efab7cce48489f7c0", "name": "【台北場】我們搖滾",
     "date": "2026-08-15 ~ 2026-08-15", "location": "台北  Legacy Tera",
     "ticketArea": False, "hidden": False, "sortedIndex": 1},
    {"sessionId": "9f17aed7373546416a117a02a8877316", "name": "【高雄場】我們搖滾",
     "date": "2026-08-29 ~ 2026-08-29", "location": "高雄  後台Backstage Live",
     "ticketArea": False, "hidden": False, "sortedIndex": 2},
]

# 注意：這些票種**沒有 ticketAreaId 欄位**，而且兩個場次的票種混在同一個 products.json
NO_AREA_PRODUCTS = [
    {"productId": "p000015624", "sessionId": "f84cb356ab1cec2efab7cce48489f7c0",
     "name": "單人預售票", "price": 1500, "sortedIndex": 1, "hidden": False},
    {"productId": "p000015625", "sessionId": "f84cb356ab1cec2efab7cce48489f7c0",
     "name": "雙人預售票", "price": 1400, "sortedIndex": 2, "hidden": False},
    {"productId": "p000015626", "sessionId": "9f17aed7373546416a117a02a8877316",
     "name": "單人預售票", "price": 1500, "sortedIndex": 1, "hidden": False},
    {"productId": "p000015627", "sessionId": "9f17aed7373546416a117a02a8877316",
     "name": "雙人預售票", "price": 1400, "sortedIndex": 2, "hidden": False},
]

NO_AREA_INFOS = {
    "product": [
        {"id": "p000015624", "status": "onsale", "productLimit": True,
         "count": 999999, "purchaseLimit": 4, "seatAssignment": False},
        {"id": "p000015625", "status": "onsale", "productLimit": True,
         "count": 0, "purchaseLimit": 4, "seatAssignment": False},
    ],
    "ticketArea": [],
}


class TestNoAreaEvent:
    def setup_method(self):
        self.products, self.areas = parsing.index_infos(NO_AREA_INFOS)
        self.taipei = NO_AREA_SESSIONS[0]["sessionId"]
        self.mine = [p for p in NO_AREA_PRODUCTS if p["sessionId"] == self.taipei]

    def test_empty_area_list_still_yields_targets(self):
        # 這就是原本的 bug：票區空陣列被當成「沒票可搶」
        targets = parsing.rank_targets(self.mine, [])
        assert [p["productId"] for _, p in targets] == ["p000015624", "p000015625"]
        assert all(area is None for area, _ in targets)

    def test_keyword_matches_product_name(self):
        targets = parsing.rank_targets(self.mine, [], keyword="雙人",
                                       strategy="關鍵字優先")
        assert targets[0][1]["productId"] == "p000015625"

    def test_picks_the_one_with_stock(self):
        # 單人有票(999999)、雙人售完(count=0 且 productLimit=True)
        targets = parsing.rank_targets(self.mine, [])
        area, product, count = parsing.pick_target(
            targets, self.products, self.areas, amount=2)
        assert area is None
        assert (product["productId"], count) == ("p000015624", 2)

    def test_other_session_products_are_not_mixed_in(self):
        assert [p["productId"] for p in self.mine] == ["p000015624", "p000015625"]

    def test_select_session_by_city(self):
        got = parsing.select_session(NO_AREA_SESSIONS, "高雄")
        assert got["sessionId"] == "9f17aed7373546416a117a02a8877316"

    def test_label_without_area(self):
        assert parsing.target_label(None, self.mine[0]) == "單人預售票"
        assert parsing.target_label({"name": "VVIP"}, self.mine[0]) == "VVIP/單人預售票"


class TestPayload:
    def test_seat_flags(self):
        payload = build_payload("p000015682", 2, seat_assignment=True)
        assert payload == {"products": [{"productId": "p000015682", "count": 2}],
                           "reserveSeats": True, "consecutiveSeats": False,
                           "finalizedSeats": True}

    def test_no_seat_no_flags(self):
        assert build_payload("p1", 1) == {"products": [{"productId": "p1", "count": 1}]}

    def test_serial_number(self):
        assert build_payload("p1", 1, serial_number="ABC")["serialNumber"] == "ABC"

    def test_auth_error_detection(self):
        assert is_auth_error(200, {"errCode": "103", "errMsg": "Invalid Token"}) is True
        assert is_auth_error(401, {}) is True
        assert is_auth_error(200, {"errCode": "00"}) is False
