"""kham_api.parsing 純函式測試（不連網、隨時可跑）。

資料取自 2026-07 對 kham.com.tw 非實名制場 UTK0205 選位頁的真實觀察：
  票種 setType('P17IVGYH','原價-NT$3,280') / 座位 <td title="4樓A2區-1排-30號">。
"""
import json
import pytest

from kham_api import parsing


# --------- parse_performance_links / select_performance ---------

PERF_HTML = (
    "<table><tr><td>2026/08/01 (六) 19:30</td><td>280、3880</td>"
    "<td><a href='javascript:;'><button class='red' onclick=\"top.location.href="
    "'UTK0204_.aspx?PERFORMANCE_ID=P17IDZEY&amp;PRODUCT_ID=P17IDZEW';return false;\">"
    "立即訂購</button></a></td></tr>"
    "<tr><td>2026/08/02 (日) 14:00</td><td>280、3880</td>"
    "<td><button onclick=\"top.location.href="
    "'UTK0204_.aspx?PERFORMANCE_ID=P17JC7MK&amp;PRODUCT_ID=P17IDZEW';return false;\">"
    "立即訂購</button></td></tr></table>"
)


class TestPerformanceLinks:
    def test_parse(self):
        perfs = parsing.parse_performance_links(PERF_HTML)
        assert [p["performance_id"] for p in perfs] == ["P17IDZEY", "P17JC7MK"]
        assert perfs[0]["url"] == "UTK0204_.aspx?PERFORMANCE_ID=P17IDZEY&PRODUCT_ID=P17IDZEW"
        assert "2026/08/01" in perfs[0]["label"]

    def test_select_by_date_keyword(self):
        perfs = parsing.parse_performance_links(PERF_HTML)
        assert parsing.select_performance(perfs, "08/02")["performance_id"] == "P17JC7MK"

    def test_select_default_first(self):
        perfs = parsing.parse_performance_links(PERF_HTML)
        assert parsing.select_performance(perfs, "")["performance_id"] == "P17IDZEY"


# --------- parse_areas / select_area ---------

AREA_HTML = (
    '<tr class="status_tr" rel="a2" id="P17IVGY2" style="cursor:pointer;">'
    '<td><div class="colorblock"></div></td><td data-title="票區：">3880元</td>'
    '<td data-title="票價：">3,880</td><td data-title="空位：">41</td></tr>'
    '<tr class="status_tr" rel="a1" id="P17IVGYG" style="cursor:pointer;">'
    '<td><div class="colorblock"></div></td><td data-title="票區：">3280元</td>'
    '<td data-title="票價：">3,280</td><td data-title="空位：">94</td></tr>'
    '<tr class="status_tr" rel="a3" id="P17IVGZZ" style="cursor:pointer;">'
    '<td><div class="colorblock"></div></td><td data-title="票區：">2800元</td>'
    '<td data-title="票價：">2,800</td><td data-title="空位：">已售完</td></tr>'
)


class TestAreas:
    def test_parse(self):
        areas = parsing.parse_areas(AREA_HTML)
        assert [a["area_id"] for a in areas] == ["P17IVGY2", "P17IVGYG", "P17IVGZZ"]
        assert areas[1]["name"] == "3280元"
        assert areas[1]["price"] == "3,280"
        assert areas[1]["avail"] == 94
        assert areas[2]["sold_out"] is True
        assert areas[2]["avail"] == 0

    def test_select_skips_sold_out(self):
        areas = parsing.parse_areas(AREA_HTML)
        # keyword 命中已售完的 2800 → 該區無效，退回有票的第一個
        chosen = parsing.select_area(areas, keyword="2800", exclude="")
        assert chosen["sold_out"] is False

    def test_select_keyword(self):
        areas = parsing.parse_areas(AREA_HTML)
        assert parsing.select_area(areas, keyword="3280", exclude="")["area_id"] == "P17IVGYG"

    def test_select_exclude(self):
        areas = parsing.parse_areas(AREA_HTML)
        assert parsing.select_area(areas, keyword="", exclude="3880")["area_id"] == "P17IVGYG"


# --------- parse_hidden_inputs ---------

class TestHiddenInputs:
    HTML = (
        '<input type="hidden" id="PERFORMANCE_ID" value="P17IDZEY">'
        '<input type="hidden" id="GROUP_ID" value="1">'
        '<input type="hidden" id="PRICE_AREA_ID" value="P17IVGYG">'
        '<input type="hidden" id="ACTIVITY_GROUP_ID" value="">'
    )

    def test_reads_values(self):
        out = parsing.parse_hidden_inputs(self.HTML, ["PERFORMANCE_ID", "GROUP_ID", "PRICE_AREA_ID"])
        assert out == {"PERFORMANCE_ID": "P17IDZEY", "GROUP_ID": "1", "PRICE_AREA_ID": "P17IVGYG"}

    def test_empty_and_missing(self):
        out = parsing.parse_hidden_inputs(self.HTML, ["ACTIVITY_GROUP_ID", "NOPE"])
        assert out == {"ACTIVITY_GROUP_ID": "", "NOPE": ""}


# --------- parse_ticket_types / select_ticket_type ---------

TYPE_HTML = (
    "<button onclick=\"setType('P17IVGYH','原價-NT$3,280');return false;\">原價</button>"
    "<button onclick=\"setType('P17IW8CR','身心障礙票-NT$1,640');return false;\">身障</button>"
    "<button onclick=\"setType('P17IW8D7','身障陪同票-NT$1,640');return false;\">陪同</button>"
)


class TestTicketTypes:
    def test_parse(self):
        types = parsing.parse_ticket_types(TYPE_HTML)
        assert [t["type_id"] for t in types] == ["P17IVGYH", "P17IW8CR", "P17IW8D7"]
        assert types[0]["name"] == "原價"
        assert types[0]["price"] == "3,280"
        assert types[1]["name"] == "身心障礙票"

    def test_select_default_first(self):
        types = parsing.parse_ticket_types(TYPE_HTML)
        assert parsing.select_ticket_type(types, keyword="", exclude="")["type_id"] == "P17IVGYH"

    def test_select_keyword(self):
        types = parsing.parse_ticket_types(TYPE_HTML)
        assert parsing.select_ticket_type(types, keyword="原價", exclude="")["type_id"] == "P17IVGYH"

    def test_select_exclude(self):
        types = parsing.parse_ticket_types(TYPE_HTML)
        # 排除身障相關 → 只剩原價
        chosen = parsing.select_ticket_type(types, keyword="", exclude="身心;身障")
        assert chosen["type_id"] == "P17IVGYH"

    def test_select_none_when_all_excluded(self):
        types = parsing.parse_ticket_types(TYPE_HTML)
        assert parsing.select_ticket_type(types, keyword="", exclude="原;身") is None


# --------- pick_available_seats ---------

def _seats():
    # 真實格式：S 是字串 "0"(空位) / 非"0"(售出)，欄位 S/T/I/A/Y/Z
    return {
        "p0000": {"S": "0", "T": "上", "A": "", "I": "4樓A2區-1排-30號", "Y": "", "Z": ""},
        "p0100": {"S": "3", "T": "上", "A": "", "I": "4樓A2區-1排-29號", "Y": "", "Z": ""},  # 售出
        "p0200": {"S": "0", "T": "上", "A": "", "I": "4樓A2區-1排-28號", "Y": "", "Z": ""},
        "p0300": {"S": "0", "T": "上", "A": "", "I": "", "Y": "", "Z": ""},                  # 無 id，跳過
        "p0400": {"S": "0", "T": "上", "A": "", "I": "4樓A2區-1排-27號", "Y": "", "Z": ""},
    }


class TestPickSeats:
    def test_picks_available_in_order(self):
        picked = parsing.pick_available_seats(_seats(), 2)
        assert [s["I"] for s in picked] == ["4樓A2區-1排-30號", "4樓A2區-1排-28號"]

    def test_skips_booked_and_idless(self):
        picked = parsing.pick_available_seats(_seats(), 10)
        assert [s["I"] for s in picked] == [
            "4樓A2區-1排-30號", "4樓A2區-1排-28號", "4樓A2區-1排-27號"]

    def test_amount_at_least_one(self):
        assert len(parsing.pick_available_seats(_seats(), 0)) == 1


# --------- build_add_cart_seats ---------

class TestBuildSeats:
    def test_payload_shape(self):
        picked = [{"I": "4樓A2區-1排-30號"}, {"I": "4樓A2區-1排-28號"}]
        s = parsing.build_add_cart_seats(picked, "P17IVGYH", "原價-NT$3,280")
        arr = json.loads(s)
        assert arr == [
            {"TYPE_ID": "P17IVGYH", "TYPE_NAME": "原價", "PRICE": "3,280", "SEAT": "4樓A2區-1排-30號"},
            {"TYPE_ID": "P17IVGYH", "TYPE_NAME": "原價", "PRICE": "3,280", "SEAT": "4樓A2區-1排-28號"},
        ]

    def test_key_order_matches_js(self):
        s = parsing.build_add_cart_seats([{"I": "x"}], "T", "名-NT$100")
        # JS 是 TYPE_ID, TYPE_NAME, PRICE, SEAT 的插入序
        assert s.index("TYPE_ID") < s.index("TYPE_NAME") < s.index("PRICE") < s.index("SEAT")
