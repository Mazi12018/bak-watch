# 가짜 일정표(실제 화면 구조를 흉내낸 것)로 파싱·변경감지 검증
import datetime as dt, sys
sys.path.insert(0, ".")
import bak_watch as bw

LABELS = ["1.오전 [9시~10시]", "2.오전 [10시~11시]", "3.오후 [12시~1시]", "4.오후 [1시~2시]",
          "5.오후 [2시~3시]", "6.오후 [3시~4시]", "7.오후 [4시~5시]"]

def card(addr, phone, tech, prod, bae=False):
    t = f'<span>{tech}</span>' if tech else '<img src="btn_bae.gif" onclick="javascript:popup_bae(\'order_bajung_pop.php?idx=9\')">'
    return f'''<table><tr><td colspan="3"><a href="#" title="{addr} / {prod} / 90만 카드">{addr}</a></td></tr>
<tr><td>&nbsp;{phone}</td><td>{t}</td><td></td></tr>
<tr><td colspan="3"><span style="padding:3px;">{prod} / 접수자</span></td></tr></table>'''

def reg(d, i):
    return f'<img src="btn_reg.gif" onclick="MM_openBrWindow(\'order_reg_pop.php?in_date={d}&mode=write&order_set_idx={i}\',\'\',\'\')">'

def board(days, data, layout="cell"):
    h = "<html><body><table>"
    for d in days:
        if layout == "cell":
            h += "<tr><td>날짜</td>"
            for i, lb in enumerate(LABELS, 1):
                h += f"<td><div>{lb} {reg(d,i)}</div>" + "".join(card(*c) for c in data.get((d, i), [])) + "</td>"
            h += "</tr>"
        else:  # 제목줄/카드줄 분리
            h += "<tr><td>날짜</td>" + "".join(f"<td>{lb} {reg(d,i)}</td>" for i, lb in enumerate(LABELS, 1)) + "</tr>"
            h += "<tr><td></td>" + "".join("<td>" + "".join(card(*c) for c in data.get((d, i), [])) + "</td>" for i in range(1, 8)) + "</tr>"
    return h + "</table></body></html>"

today = dt.date(2026, 10, 3)
days = [(today + dt.timedelta(n)).isoformat() for n in range(8)]
D0, D1 = days[0], days[1]
A = ("경기 성남시 은행동 1", "010-1111-1111", "박기사 설치", "콘18K")
B = ("경기 성남시 성남동 2", "010-2222-2222", "김기사 설치", "콘15K")
C = ("경기 광주시 태전동 3", "010-3333-3333", "", "콘27KQ")
E = ("경기 성남시 금광동 4", "010-4444-4444", "천기사 설치", "일16K")

base = {(D0, 1): [A], (D0, 3): [B], (D1, 2): [C], (D1, 5): [E]}
for layout in ("cell", "row"):
    p = bw.parse_board(board(days, base, layout))
    assert len(p["slots"]) == 56, len(p["slots"])
    assert len(p["orders"]) == 4, (layout, len(p["orders"]))
    o = {v["phone"]: v for v in p["orders"].values()}
    assert o["010-1111-1111"]["tech"] == "박기사 설치" and o["010-1111-1111"]["slot_idx"] == "1"
    assert o["010-3333-3333"]["tech"] == "미배정"
    assert o["010-2222-2222"]["product"].startswith("콘15K")
    print(layout, "파싱 OK")

    # 변경: A 기사변경, B 구간이동(3→4), C 삭제, 신규 F, E 내용수정
    A2 = (A[0], A[1], "황기사 설치", A[3])
    E2 = (E[0], E[1], E[2], "일16K+감압변")
    F = ("경기 성남시 수진동 5", "010-5555-5555", "박기사 설치", "콘22K")
    new = {(D0, 1): [A2], (D0, 4): [B], (D1, 5): [E2], (D1, 6): [F]}
    c = bw.parse_board(board(days, new, layout))
    ev = bw.diff(p, c, D0)
    kinds = sorted(e[0] for e in ev)
    assert kinds == sorted(["기사변경", "일정변경", "사라짐", "신규", "내용수정"]), kinds
    print(layout, "변경감지 OK:", kinds)

# 날짜가 넘어가는 경우: 어제 건은 조용히 빠지고 새로 들어온 날짜 건은 신규 알림 없음
p = bw.parse_board(board(days, base))
days2 = days[1:] + [(today + dt.timedelta(8)).isoformat()]
G = ("경기 성남시 태평동 6", "010-6666-6666", "김기사 설치", "콘18K")
c = bw.parse_board(board(days2, {(D1, 2): [C], (D1, 5): [E], (days2[-1], 1): [G]}))
ev = bw.diff(p, c, D1)
assert ev == [], ev
print("날짜 넘어감 OK (알림 0건)")

# 메시지 모양
p = bw.parse_board(board(days, base))
c = bw.parse_board(board(days, {(D0, 1): [("경기 성남시 은행동 1", "010-1111-1111", "황기사 설치", "콘18K")], (D0, 4): [B], (D1, 2): [C], (D1, 5): [E]}))
for e in bw.diff(p, c, D0):
    print("-----\n" + bw.fmt_event(*e))

class Cfg: empty_days = 3; capacity = 1; skip_sunday = True; empty_format = "table"
print("-----\n" + bw.fmt_empty(c, Cfg, dt.datetime(2026, 10, 3, 10, 5)))
