"""kham_api.parsing 純函式測試（不連網、隨時可跑）。

資料取自對 kham.com.tw 非實名制場的真實觀察（2026-07 初版、2026-09-17 重抓）：
  票種 setType('P17IVGYH','原價-NT$3,280') / 座位 seatStr（fixtures/kham/seatstr_*.txt）：
  - seatstr_dome_side.txt   小巨蛋 2樓紅2B區，舞台在「左」→ x 軸是排，198 位剩 56
  - seatstr_dome_floor.txt  小巨蛋平面特1區，24 排 × 45 號、兩條 2 格寬走道，已售完
  - seatstr_tera.txt        Legacy TERA VIP區，中間一條走道，1/2 號在走道兩側
"""
import json
import os

from kham_api import parsing

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "kham")


def _load_seats(name, open_all=False):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        seats = parsing.parse_seat_str(f.read())
    if open_all:  # 模擬開賣瞬間：全部都還沒人買
        for s in seats:
            s["status"] = "0"
    return seats


# --------- parse_performance_links / select_performance ---------

PERF_HTML = (
    "<table><tr><td>2026/08/01 (六) 19:30</td><td>280、3880</td>"
    "<td><a href='javascript:;'><button class='red' onclick=\"top.location.href="
    "'UTK0204_.aspx?PERFORMANCE_ID=P17IDZEY&amp;PRODUCT_ID=P17IDZEW';return false;\">"
    "立即訂購</button></a></td></tr>"
    "<tr><td>2026/08/02 (日) 14:00</td><td>280、3880</td>"
    "<td><button onclick=\"top.location.href="
    "'UTK0204_.aspx?PERFORMANCE_ID=P17JC7MK&amp;PRODUCT_ID=P17IDZEW';return false;\">"
    "立即訂購</button></td></tr>"
    # 新版選票區頁（2026-09 實測 EUNHYUK / N.Flying 都是這種）
    "<tr><td>2026/08/02 (日) 14:00</td><td>【輪椅場】 1880</td>"
    "<td><button onclick=\"top.location.href="
    "'UTK0201_000.aspx?PERFORMANCE_ID=P1F8IEH5&PRODUCT_ID=P17IDZEW';return false;\">"
    "立即訂購</button></td></tr>"
    # 不劃位輸入張數（音樂節）
    "<tr><td>2026/08/03 (一) 13:00</td><td>一般區 雙日票 2980</td>"
    "<td><button onclick=\"top.location.href="
    "'UTK0202_.aspx?PERFORMANCE_ID=P1ET6G13&PRODUCT_ID=P17IDZEW';return false;\">"
    "立即訂購</button></td></tr>"
    "<tr><td>2026/08/04 (二) 19:30</td><td>請至套票專頁購買</td></tr></table>"
)


class TestPerformanceLinks:
    def test_parse(self):
        perfs = parsing.parse_performance_links(PERF_HTML)
        assert [p["performance_id"] for p in perfs] == ["P17IDZEY", "P17JC7MK", "P1F8IEH5", "P1ET6G13"]
        assert perfs[0]["url"] == "UTK0204_.aspx?PERFORMANCE_ID=P17IDZEY&PRODUCT_ID=P17IDZEW"
        assert "2026/08/01" in perfs[0]["label"]

    def test_kinds(self):
        perfs = parsing.parse_performance_links(PERF_HTML)
        assert [p["kind"] for p in perfs] == ["area", "area", "area", "quantity"]
        assert perfs[2]["url"] == "UTK0201_000.aspx?PERFORMANCE_ID=P1F8IEH5&PRODUCT_ID=P17IDZEW"
        assert perfs[3]["url"].startswith("UTK0202_.aspx?")

    def test_select_by_date_keyword(self):
        perfs = parsing.parse_performance_links(PERF_HTML)
        assert parsing.select_performance(perfs, "08/02")["performance_id"] == "P17JC7MK"

    def test_select_default_first(self):
        perfs = parsing.parse_performance_links(PERF_HTML)
        assert parsing.select_performance(perfs, "")["performance_id"] == "P17IDZEY"

    def test_exclude_skips_wheelchair_session(self):
        perfs = parsing.parse_performance_links(PERF_HTML)[1:3][::-1]  # 輪椅場排前面
        chosen = parsing.select_performance(perfs, "08/02", exclude="輪椅;身障")
        assert chosen["performance_id"] == "P17JC7MK"
        assert parsing.select_performance(perfs[:1], "08/02", exclude="輪椅") is None


class TestSmallHelpers:
    def test_product_id_from(self):
        url = "https://kham.com.tw/application/UTK02/UTK0201_.aspx?PRODUCT_ID=P1D3G65D"
        assert parsing.product_id_from(url) == "P1D3G65D"
        assert parsing.product_id_from(url + "&tapeID=en04r3eA3-#") == "P1D3G65D"
        assert parsing.product_id_from(" p1d3g65d ") == "P1D3G65D"
        assert parsing.product_id_from("") == ""

    def test_product_id_from_rejects_url_without_product_id(self):
        # 套票頁只有 AGID，硬吃下去會拿整串網址當代碼去打
        assert parsing.product_id_from(
            "https://kham.com.tw/application/UTK02/UTK0201_040.aspx?AGID=P1A149QI") == ""

    def test_logged_in(self):
        assert parsing.logged_in("<script>\n\tvar _ul = '0';\n</script>") is False
        assert parsing.logged_in("<script>var _ul = '1';</script>") is True
        assert parsing.logged_in("top.location.href = '/x';") is None


# --------- parse_areas / select_area / seat_page_url ---------

AREA_HTML = (
    '<tr class="status_tr" rel="a2" id="P17IVGY2" style="cursor:pointer;">'
    '<td><div class="colorblock"></div></td><td data-title="票區：">搖滾A區3880元</td>'
    '<td data-title="票價：">3,880</td><td data-title="空位：">1</td></tr>'
    '<tr class="status_tr" rel="a1 a4" id="P17IVGYG" style="cursor:pointer;">'
    '<td><div class="colorblock"></div></td><td data-title="票區：">搖滾B區3880元</td>'
    '<td data-title="票價：">3,880</td><td data-title="空位：">94</td></tr>'
    '<tr class="status_tr Soldout" rel="a3" id="P17IVGZZ">'
    '<td><div class="colorblock"></div></td><td data-title="票區：">2樓2800元</td>'
    '<td data-title="票價：">2,800</td><td data-title="空位：">已售完</td></tr>'
    '<tr class="status_tr" rel="a5" id="P17IVH00">'
    '<td><div class="colorblock"></div></td><td data-title="票區：">輪椅席&nbsp;&nbsp; 僅限自備輪椅</td>'
    '<td data-title="票價：">1,980</td><td data-title="空位：">2</td></tr>'
)
EXCLUDE = "輪椅;身障;身心;障礙"


class TestAreas:
    def test_parse(self):
        areas = parsing.parse_areas(AREA_HTML)
        assert [a["area_id"] for a in areas] == ["P17IVGY2", "P17IVGYG", "P17IVGZZ", "P17IVH00"]
        assert areas[1]["name"] == "搖滾B區3880元"
        assert areas[1]["price"] == "3,880"
        assert areas[1]["avail"] == 94
        assert areas[1]["groups"] == ["1", "4"]

    def test_soldout_class_row_is_kept_and_marked(self):
        # 售完的列 class 是 "status_tr Soldout"，舊 regex 整列漏掉
        sold = parsing.parse_areas(AREA_HTML)[2]
        assert sold["sold_out"] is True and sold["avail"] == 0

    def test_entities_unescaped(self):
        assert parsing.parse_areas(AREA_HTML)[3]["name"] == "輪椅席 僅限自備輪椅"

    def test_select_skips_sold_out(self):
        areas = parsing.parse_areas(AREA_HTML)
        chosen = parsing.select_area(areas, keyword="2800", exclude=EXCLUDE)
        assert chosen["sold_out"] is False

    def test_exclude_wheelchair_even_if_only_one_left(self):
        # 一般區全賣完時，不能因為 fallback 就買到輪椅席
        areas = [a for a in parsing.parse_areas(AREA_HTML) if "輪椅" in a["name"] or a["sold_out"]]
        assert parsing.select_area(areas, keyword="", exclude=EXCLUDE) is None

    def test_prefers_area_with_enough_seats(self):
        areas = parsing.parse_areas(AREA_HTML)
        # A 區只剩 1 張，買 2 張要跳去同樣命中的 B 區
        assert parsing.select_area(areas, keyword="3880", exclude=EXCLUDE, amount=2)["area_id"] == "P17IVGYG"
        assert parsing.select_area(areas, keyword="3880", exclude=EXCLUDE, amount=1)["area_id"] == "P17IVGY2"

    def test_keyword_priority_and_and(self):
        areas = parsing.parse_areas(AREA_HTML)
        assert parsing.select_area(areas, keyword="搖滾C;B區", exclude="")["area_id"] == "P17IVGYG"
        assert parsing.select_area(areas, keyword="搖滾+B", exclude="")["area_id"] == "P17IVGYG"

    def test_strict_returns_none_when_no_match(self):
        areas = parsing.parse_areas(AREA_HTML)
        assert parsing.select_area(areas, keyword="VIP", exclude=EXCLUDE, strict=True) is None
        assert parsing.select_area(areas, keyword="VIP", exclude=EXCLUDE)["area_id"] == "P17IVGY2"

    def test_seat_page_url(self):
        area = parsing.parse_areas(AREA_HTML)[1]
        assert parsing.seat_page_url("P1D430L4", area) == (
            "UTK0205_.aspx?PERFORMANCE_ID=P1D430L4&GROUP_ID=1&PERFORMANCE_PRICE_AREA_ID=P17IVGYG")


# --------- parse_hidden_inputs ---------

class TestHiddenInputs:
    HTML = (
        '<input type="hidden" id="PERFORMANCE_ID" value="P17IDZEY">'
        '<input type="hidden" id="GROUP_ID" value="1">'
        '<input type="hidden" id="PRICE_AREA_ID" value="P17IVGYG">'
        '<input type="hidden" id="ACTIVITY_GROUP_ID" value="">'
        '<input type="hidden" id="DOC_MEMO" value="{&quot;P1DBFS3Z&quot;:&quot;AErN+ze4/q==&quot;}">'
    )

    def test_reads_values(self):
        out = parsing.parse_hidden_inputs(self.HTML, ["PERFORMANCE_ID", "GROUP_ID", "PRICE_AREA_ID"])
        assert out == {"PERFORMANCE_ID": "P17IDZEY", "GROUP_ID": "1", "PRICE_AREA_ID": "P17IVGYG"}

    def test_empty_and_missing(self):
        out = parsing.parse_hidden_inputs(self.HTML, ["ACTIVITY_GROUP_ID", "NOPE"])
        assert out == {"ACTIVITY_GROUP_ID": "", "NOPE": ""}

    def test_doc_memo_unescaped(self):
        # 瀏覽器 .value 讀到的是解碼後的 JSON，送單要跟它一樣
        out = parsing.parse_hidden_inputs(self.HTML, ["DOC_MEMO"])
        assert out["DOC_MEMO"] == '{"P1DBFS3Z":"AErN+ze4/q=="}'


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

    def test_area_keyword_does_not_hijack_type(self):
        # AREA_KEYWORD 是給票區用的，對不上任何票種時照樣拿原價
        types = parsing.parse_ticket_types(TYPE_HTML)
        assert parsing.select_ticket_type(types, keyword="搖滾A;3280", exclude=EXCLUDE)["type_id"] == "P17IVGYH"

    def test_select_exclude(self):
        types = parsing.parse_ticket_types(TYPE_HTML)
        # 預設排除詞會把身障票、陪同票都刷掉
        assert [t["type_id"] for t in types
                if parsing.select_ticket_type([t], keyword="", exclude=EXCLUDE)] == ["P17IVGYH"]

    def test_select_none_when_all_excluded(self):
        types = parsing.parse_ticket_types(TYPE_HTML)
        assert parsing.select_ticket_type(types, keyword="", exclude="原;身") is None


# --------- parse_seat_str / parse_seat_page ---------

SEAT_PAGE_HTML = (
    "<script>var _info = {'loadtime':'2026/09/17 22:18:05'};</script>"
    '<input type="hidden" name="ctl00$ContentPlaceHolder1$PERFORMANCE_ID" id="PERFORMANCE_ID" value="P1D430L4" />'
    '<input type="hidden" id="QUANTITY_LIMIT" value="4" />'
    '<input name="ctl00$ContentPlaceHolder1$LOGIN_ID" type="text" maxlength="20" id="LOGIN_ID" placeholder="帳號" />'
    "<button class=\"green\" onclick=\"setType('P1D9ZT71','原價-NT$3,880');return false;\">原價</button>"
    "<script>//<![CDATA[\n$('#mapdata').load('x.map', '', function(){loadMapSettings()});;"
    "xMax = 9;yMax = 22;seatStr = '左:0:1,15,,2樓紅2B區-2排-16號.2,12,,2樓紅2B區-3排-13號"
    "\t左:1:0,0,,2樓紅2B區-1排-1號';maxWaitTime = 0;document.body.style.display = '';</script>"
)


class TestSeatPage:
    def test_parse_seat_str(self):
        seats = parsing.parse_seat_str("左:0:1,15,,A-2排-16號.2,12,,A-3排-13號\t:1:0,0,,A-1排-1號")
        assert seats == [
            {"x": 1, "y": 15, "status": "0", "dir": "左", "label": "A-2排-16號"},
            {"x": 2, "y": 12, "status": "0", "dir": "左", "label": "A-3排-13號"},
            {"x": 0, "y": 0, "status": "1", "dir": "上", "label": "A-1排-1號"},  # 空方向 = 上
        ]

    def test_parse_seat_str_skips_garbage(self):
        assert parsing.parse_seat_str("") == []
        assert parsing.parse_seat_str("上:0:,,,\t壞掉") == []

    def test_parse_seat_page(self):
        page = parsing.parse_seat_page(SEAT_PAGE_HTML)
        assert page["loadtime"] == "2026/09/17 22:18:05"
        assert page["max_wait_ms"] == 0
        assert page["fields"]["PERFORMANCE_ID"] == "P1D430L4"
        assert page["fields"]["QUANTITY_LIMIT"] == "4"
        assert page["fields"]["LOGIN_ID"] == ""
        assert [t["type_id"] for t in page["types"]] == ["P1D9ZT71"]
        assert [s["label"] for s in page["seats"]] == [
            "2樓紅2B區-2排-16號", "2樓紅2B區-3排-13號", "2樓紅2B區-1排-1號"]

    def test_real_fixture_parses(self):
        seats = _load_seats("seatstr_dome_floor.txt")
        assert len(seats) == 24 * 45
        assert {s["dir"] for s in seats} == {"上"}


# --------- pick_seats ---------

def _row(y, xs, status="0", direction="上", label="A區-{r}排-{x}號"):
    return [{"x": x, "y": y, "status": status, "dir": direction,
             "label": label.format(r=y + 1, x=x)} for x in xs]


def _labels(picked):
    return [s["label"] for s in picked]


class TestPickSeats:
    def test_contiguous_beats_front_row_split(self):
        # 第 1 排只剩被隔開的兩個，第 3 排有連號 → 要一起坐的連號優先
        seats = (_row(0, [0, 2, 3, 4, 6], status="1") + _row(0, [1, 5])
                 + _row(2, [2, 3]))
        picked, how = parsing.pick_seats(seats, 2)
        assert how == "連號"
        assert _labels(picked) == ["A區-3排-2號", "A區-3排-3號"]

    def test_front_row_first_then_center(self):
        seats = _row(0, [0, 1]) + _row(1, [0, 1, 2, 3, 4, 5, 6, 7, 8]) + _row(5, [0, 1, 2, 3, 4, 5, 6, 7, 8])
        picked, how = parsing.pick_seats(seats, 2)
        assert how == "連號" and _labels(picked) == ["A區-1排-0號", "A區-1排-1號"]
        picked, _ = parsing.pick_seats(seats, 3)
        # 第 1 排不夠 3 個 → 第 2 排、挑正中間（格子 0~8 中線是 4）
        assert _labels(picked) == ["A區-2排-3號", "A區-2排-4號", "A區-2排-5號"]

    def test_aisle_breaks_contiguity(self):
        # 格子 2、3 是走道：1 跟 4 雖然在清單裡相鄰，不能算連號
        seats = _row(0, [0, 1, 4, 5]) + _row(3, [0, 1, 2, 3])
        picked, how = parsing.pick_seats(seats, 2)
        assert how == "連號" and picked[0]["y"] == 0 and picked[1]["x"] - picked[0]["x"] == 1

        picked, how = parsing.pick_seats(_row(0, [0, 3]) + _row(4, [9]), 2)
        assert how == "同排" and _labels(picked) == ["A區-1排-0號", "A區-1排-3號"]

    def test_too_wide_same_row_falls_back_to_scattered(self):
        seats = _row(0, [0, 20]) + _row(1, [5])
        picked, how = parsing.pick_seats(seats, 2)
        assert how == "散位"
        assert len(picked) == 2 and picked[0]["y"] == 0

    def test_returns_what_is_left(self):
        picked, how = parsing.pick_seats(_row(3, [7]), 4)
        assert how == "散位" and _labels(picked) == ["A區-4排-7號"]

    def test_only_status_zero_and_labelled(self):
        seats = _row(0, [0, 1], status="4") + _row(0, [2], status="1") + [
            {"x": 3, "y": 0, "status": "0", "dir": "上", "label": ""}] + _row(2, [0])
        picked, _ = parsing.pick_seats(seats, 1)
        assert _labels(picked) == ["A區-3排-0號"]

    def test_exclude_applies_to_seat_label(self):
        seats = _row(0, [0], label="輪椅席-{r}排-{x}號") + _row(4, [0])
        picked, _ = parsing.pick_seats(seats, 1, exclude=EXCLUDE)
        assert _labels(picked) == ["A區-5排-0號"]

    def test_stage_on_right_uses_x_as_row(self):
        # 舞台在右：x 越大越前排，y 才是同排的方向
        seats = [{"x": x, "y": y, "status": "0", "dir": "右", "label": f"{x},{y}"}
                 for x in (0, 9) for y in (0, 1)]
        picked, how = parsing.pick_seats(seats, 2)
        assert how == "連號" and _labels(picked) == ["9,0", "9,1"]

    def test_empty(self):
        assert parsing.pick_seats([], 2) == ([], "")

    def test_real_side_section_stage_left(self):
        seats = _load_seats("seatstr_dome_side.txt")
        # 2排 只剩單一個 → 買 1 張就拿它；買 2、4 張要往後找能連號的排
        assert _labels(parsing.pick_seats(seats, 1)[0]) == ["2樓紅2B區-2排-16號"]
        assert _labels(parsing.pick_seats(seats, 2)[0]) == ["2樓紅2B區-4排-16號", "2樓紅2B區-4排-17號"]
        picked, how = parsing.pick_seats(seats, 4)
        assert how == "連號"
        assert _labels(picked) == ["2樓紅2B區-5排-16號", "2樓紅2B區-5排-17號",
                                   "2樓紅2B區-5排-18號", "2樓紅2B區-5排-19號"]

    def test_real_floor_at_open_picks_front_center(self):
        # 開賣瞬間全空：第 1 排正中間（寬宏中間是 1/2 號，兩側單雙號遞增）
        seats = _load_seats("seatstr_dome_floor.txt", open_all=True)
        assert _labels(parsing.pick_seats(seats, 2)[0]) == ["平面特1區-1排-2號", "平面特1區-1排-1號"]

    def test_real_tera_number_gap_is_still_adjacent(self):
        # 37、39 號號碼差 2，但格子相鄰 → 就是連號
        seats = _load_seats("seatstr_tera.txt")
        picked, how = parsing.pick_seats(seats, 2)
        assert how == "連號" and _labels(picked) == ["VIP區-12排-37號", "VIP區-12排-39號"]


# --------- build_add_cart_seats / build_info ---------

class TestBuildSeats:
    def test_payload_shape(self):
        picked = [{"label": "4樓A2區-1排-30號"}, {"label": "4樓A2區-1排-28號"}]
        s = parsing.build_add_cart_seats(picked, "P17IVGYH", "原價-NT$3,280")
        arr = json.loads(s)
        assert arr == [
            {"TYPE_ID": "P17IVGYH", "TYPE_NAME": "原價", "PRICE": "3,280", "SEAT": "4樓A2區-1排-30號"},
            {"TYPE_ID": "P17IVGYH", "TYPE_NAME": "原價", "PRICE": "3,280", "SEAT": "4樓A2區-1排-28號"},
        ]

    def test_matches_json_stringify(self):
        # JSON.stringify：鍵照插入序、不加空白、中文不跳脫
        s = parsing.build_add_cart_seats([{"label": "x"}], "T", "名-NT$100")
        assert s == '[{"TYPE_ID":"T","TYPE_NAME":"名","PRICE":"100","SEAT":"x"}]'

    def test_build_info(self):
        assert parsing.build_info("2026/09/17 22:18:05", 1789654685123) == (
            '{"loadtime":"2026/09/17 22:18:05","timestamp":1789654685123}')


