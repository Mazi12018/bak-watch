# -*- coding: utf-8 -*-
"""
보안관 ERP 설치 일정 감시 → Slack 알림

- 보안관 설치접수 일정표(/admin/shop/order.php)를 일정 간격으로 '조회만' 합니다.
  (로그인 외에는 어떤 저장/수정/삭제/문자발송도 하지 않습니다.)
- 이전 화면과 비교해 신규 / 기사변경 / 일정변경 / 내용수정 / 사라짐(취소·삭제 등)을 Slack으로 알립니다.
- 정해진 시각에 빈 구간(배정 가능한 구간) 목록을 Slack으로 보냅니다.

사용법
  python bak_watch.py          감시 시작 (계속 실행)
  python bak_watch.py --test   1회 로그인·조회 후 결과를 화면에 출력 (Slack 전송 없음)
"""
import configparser
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "bak_watch.log")
ALIVE_PATH = os.path.join(BASE_DIR, "alive.json")      # 감시 프로그램(watchdog)이 보는 '살아있음' 표시
KEYRING_SERVICE = "bak_watch"
DUMP_PATH = os.path.join(BASE_DIR, "board_dump.html")

VERSION = "2026.10.10"
BOARD_PATH = "/admin/shop/order.php"
WEEKDAYS = "월화수목금토일"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

log = logging.getLogger("bak_watch")


# ─────────────────────────── 설정 ───────────────────────────
class Config:
    def __init__(self, path=CONFIG_PATH):
        if not os.path.exists(path):
            sys.exit("config.ini 가 없습니다. config.example.ini 를 복사해 config.ini 로 만들고 값을 채워주세요.")
        cp = configparser.ConfigParser(interpolation=None)
        try:
            with open(path, encoding="utf-8-sig") as f:
                cp.read_file(f)
        except UnicodeDecodeError:
            with open(path, encoding="cp949") as f:
                cp.read_file(f)
        g = lambda s, k, d="": cp.get(s, k, fallback=d).strip()
        self.base_url = g("bak", "base_url", "http://bak-erp.com").rstrip("/")
        self.company_id = g("bak", "company_id")
        self.user_id = g("bak", "user_id")
        self.password = g("bak", "password")
        self.login_url = g("bak", "login_url")
        self.webhook = g("slack", "webhook_url")
        self.interval = max(60, int(g("watch", "interval_sec", "120")))
        self.work_start = _parse_hm(g("watch", "work_start", "07:30"))
        self.work_end = _parse_hm(g("watch", "work_end", "20:00"))
        self.empty_times = [_parse_hm(x) for x in g("empty", "send_times", "08:00,13:00").split(",") if x.strip()]
        self.empty_days = int(g("empty", "days", "3"))
        self.capacity = max(1, int(g("empty", "slot_capacity", "1")))
        self.skip_sunday = g("empty", "skip_sunday", "yes").lower() in ("yes", "y", "true", "1")
        self.empty_format = g("empty", "format", "table").lower()
        # 일정표 그림(캡처) 올리기 — Slack 봇 토큰 필요
        self.img_on = g("image", "enabled", "yes").lower() in ("yes", "y", "true", "1")
        self.bot_token = g("image", "bot_token")
        self.channel_id = g("image", "channel_id")
        self.img_days = int(g("image", "days", "3"))
        # table = 휴대폰용으로 새로 그린 표 / capture = 보안관 화면 캡처
        self.img_style = g("image", "style", "table").lower()
        self.img_delete_prev = g("image", "delete_previous", "yes").lower() in ("yes", "y", "true", "1")
        self.slack_api = g("image", "slack_api", "https://slack.com/api").rstrip("/")
        self.use_image = self.img_on and bool(self.bot_token) and bool(self.channel_id)
        # 원격 관리 (본사가 여러 대리점을 한 곳에서 지켜보고 업데이트)
        self.site = g("admin", "site_name", "") or self.company_id or "대리점"
        self.admin_webhook = g("admin", "admin_webhook_url")
        self.update_url = g("admin", "update_url")
        self.auto_update = g("admin", "auto_update", "yes").lower() in ("yes", "y", "true", "1")
        self.heartbeat = _parse_hm(g("admin", "heartbeat_time", "08:05"))
        if not self.user_id:
            sys.exit("config.ini 의 [bak] user_id 를 채워주세요.")

    def keyring_name(self):
        return f"{self.base_url}|{self.company_id}|{self.user_id}"

    def get_password(self):
        """비밀번호는 윈도우 자격 증명(Credential Manager)에서 먼저 찾고, 없으면 config.ini 값 사용.
        로그인할 때마다 새로 읽으므로 비밀번호를 바꿔도 프로그램을 다시 켤 필요가 없다."""
        try:
            import keyring
            pw = keyring.get_password(KEYRING_SERVICE, self.keyring_name())
            if pw:
                return pw
        except Exception:  # noqa
            log.debug("keyring 사용 불가", exc_info=True)
        if self.password:
            return self.password
        raise RuntimeError("보안관 비밀번호가 저장되어 있지 않습니다. 비밀번호_변경.bat 을 실행하세요.")


def _parse_hm(s):
    h, m = s.strip().split(":")
    return dt.time(int(h), int(m))


# ─────────────────────────── 보안관 접속 ───────────────────────────
def decode(resp):
    raw = resp.content
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


REG_RE = re.compile(r"order_reg_pop\.php[^'\"]*order_set_idx", re.I)


def is_board(html):
    return bool(REG_RE.search(html))


def find_login_form(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        if form.find("input", attrs={"type": re.compile("^password$", re.I)}):
            return form, urljoin(page_url, form.get("action") or page_url)
    return None, None


def js_redirect(html, page_url):
    m = re.search(r"location\.(?:href\s*=|replace\()\s*['\"]([^'\"]+)['\"]", html)
    return urljoin(page_url, m.group(1)) if m else None


class Bak:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.board_url = cfg.base_url + BOARD_PATH

    def _get(self, url):
        r = self.s.get(url, timeout=20)
        r.raise_for_status()
        return r, decode(r)

    def login(self, hint_html=None, hint_url=None):
        """로그인 폼을 찾아 아이디/비밀번호를 넣고 제출합니다. (보안관에 대한 유일한 POST)"""
        candidates = []
        if self.cfg.login_url:
            candidates.append(self.cfg.login_url)
        if hint_html is not None:
            candidates.append(("__html__", hint_html, hint_url))
        candidates += [self.cfg.base_url + "/admin/", self.cfg.base_url + "/"]

        tried = set()
        for c in candidates:
            if isinstance(c, tuple):
                _, html, url = c
            else:
                if c in tried:
                    continue
                tried.add(c)
                r, html = self._get(c)
                url = r.url
            form, action = find_login_form(html, url)
            if form is None:
                nxt = js_redirect(html, url)
                if nxt and nxt not in tried:
                    tried.add(nxt)
                    r, html = self._get(nxt)
                    url = r.url
                    form, action = find_login_form(html, url)
            if form is None:
                continue

            data, texts = {}, []
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                t = (inp.get("type") or "text").lower()
                if t == "password":
                    data[name] = self.cfg.get_password()
                elif t in ("text", "email", "tel"):
                    texts.append(name)
                    data[name] = inp.get("value", "")
                elif t == "hidden":
                    data[name] = inp.get("value", "")
                elif t in ("checkbox", "radio") and inp.has_attr("checked"):
                    data[name] = inp.get("value", "on")
            if not texts:
                continue
            # 보안관 로그인: [대표아이디] [아이디] [비밀번호] 순서
            if len(texts) >= 2:
                if not self.cfg.company_id:
                    raise RuntimeError("로그인 화면에 대표아이디 칸이 있습니다. config.ini 의 company_id 를 채워주세요.")
                data[texts[0]] = self.cfg.company_id
                data[texts[1]] = self.cfg.user_id
            else:
                data[texts[0]] = self.cfg.user_id
            method = (form.get("method") or "post").lower()
            log.info("로그인 시도: %s", action.split("?")[0])
            if method == "get":
                self.s.get(action, params=data, timeout=20)
            else:
                self.s.post(action, data=data, timeout=20, headers={"Referer": url})
            return True
        raise RuntimeError("로그인 화면(비밀번호 입력칸)을 찾지 못했습니다. config.ini 에 login_url 을 지정해 주세요.")

    def fetch_board(self):
        r, html = self._get(self.board_url)
        if is_board(html):
            return html
        self.login(hint_html=html, hint_url=r.url)
        r, html = self._get(self.board_url)
        if is_board(html):
            return html
        raise RuntimeError("로그인 후에도 일정표가 보이지 않습니다. 아이디/비밀번호 또는 권한을 확인하세요.")


# ─────────────────────────── 일정표 해석 ───────────────────────────
PHONE_RE = re.compile(r"0\d{1,2}-\d{3,4}-\d{4}")
DATE_RE = re.compile(r"in_date=(\d{4}-\d{2}-\d{2})")
SLOT_RE = re.compile(r"order_set_idx=(\d+)")
LABEL_RE = re.compile(r"(\d+)\s*\.\s*(오전|오후)\s*\[([^\]]+)\]")


def _is_reg(tag):
    oc = tag.get("onclick") or ""
    return bool(REG_RE.search(oc))


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def parse_board(html):
    """반환: {"slots": [...], "orders": {key: order}}"""
    soup = BeautifulSoup(html, "html.parser")
    regs = soup.find_all(_is_reg)
    slots, orders = [], {}
    seen_cards = set()
    for el in regs:
        oc = el.get("onclick", "")
        dm, sm = DATE_RE.search(oc), SLOT_RE.search(oc)
        if not dm or not sm:
            continue
        date, slot_idx = dm.group(1), sm.group(1)

        # 등록 버튼이 1개만 들어있는 가장 큰 상위 요소 = 해당 구간 칸
        box = el
        while box.parent is not None and box.parent.name != "[document]" \
                and len(box.parent.find_all(_is_reg)) == 1:
            box = box.parent

        lm = LABEL_RE.search(box.get_text(" ", strip=True))
        if lm:
            label = f"{lm.group(1)}구간 {lm.group(2)} {lm.group(3)}"
            no = int(lm.group(1))
        else:
            label, no = f"구간{slot_idx}", 0
        start_hour = _slot_start_hour(lm)
        slot = {"date": date, "slot_idx": slot_idx, "label": label, "no": no,
                "start_hour": start_hour, "count": 0}
        slots.append(slot)

        for a in box.find_all("a", title=True):
            card = a.find_parent("table") or a.parent
            if id(card) in seen_cards:
                continue
            seen_cards.add(id(card))
            o = _parse_card(card, a)
            o.update(date=date, slot_idx=slot_idx, slot_label=label)
            key = f'{o["phone"]}|{_norm(o["title"].split(" / ")[0])}'
            base, n = key, 2
            while key in orders:
                key, n = f"{base}#{n}", n + 1
            orders[key] = o
            slot["count"] += 1

    # 예비 방식: 구간 제목줄과 접수카드가 서로 다른 줄(tr)에 있는 화면 구조일 때
    slot_by_reg = {}
    for s, el in zip(slots, [r for r in regs if DATE_RE.search(r.get("onclick", "")) and SLOT_RE.search(r.get("onclick", ""))]):
        slot_by_reg[id(el)] = s
    for a in soup.find_all("a", title=True):
        card = a.find_parent("table") or a.parent
        if id(card) in seen_cards or a.get("href") != "#":
            continue
        s = _slot_by_column(card, slot_by_reg)
        if s is None:
            continue
        seen_cards.add(id(card))
        o = _parse_card(card, a)
        o.update(date=s["date"], slot_idx=s["slot_idx"], slot_label=s["label"])
        key = f'{o["phone"]}|{_norm(o["title"].split(" / ")[0])}'
        base, n = key, 2
        while key in orders:
            key, n = f"{base}#{n}", n + 1
        orders[key] = o
        s["count"] += 1
    return {"slots": slots, "orders": orders}


def _slot_by_column(card, slot_by_reg):
    for td in card.find_parents("td"):
        tr = td.find_parent("tr")
        if tr is None:
            continue
        tds = tr.find_all("td", recursive=False)
        if td not in tds:
            continue
        i = tds.index(td)
        prev = tr.find_previous_sibling("tr")
        while prev is not None:
            ptds = prev.find_all("td", recursive=False)
            if i < len(ptds):
                regs = ptds[i].find_all(_is_reg)
                if len(regs) == 1 and id(regs[0]) in slot_by_reg:
                    return slot_by_reg[id(regs[0])]
            if prev.find(_is_reg):
                break
            prev = prev.find_previous_sibling("tr")
    return None


def _slot_start_hour(lm):
    if not lm:
        return None
    m = re.match(r"\s*(\d+)", lm.group(3))
    if not m:
        return None
    h = int(m.group(1))
    if lm.group(2) == "오후" and h < 12:
        h += 12
    return h


def _parse_card(card, a):
    title = _norm(a.get("title"))
    text = card.get_text(" ", strip=True)
    pm = PHONE_RE.search(text)
    tech, product = "", ""
    for sp in card.find_all("span"):
        t = _norm(sp.get_text(" ", strip=True))
        if not t:
            continue
        if "padding" in (sp.get("style") or "") and not product:
            product = t
        elif not tech and not PHONE_RE.search(t):
            tech = t
    if not tech:
        tech = "미배정"
    return {"title": title, "phone": pm.group(0) if pm else "", "tech": tech, "product": product}


# ─────────────────────────── 변경 감지 ───────────────────────────
def diff(prev, cur, today):
    """이전/현재 스냅샷 비교 → 이벤트 목록"""
    events = []
    prev_o, cur_o = prev["orders"], cur["orders"]
    prev_dates = {s["date"] for s in prev["slots"]}
    cur_dates = {s["date"] for s in cur["slots"]}

    for k, o in cur_o.items():
        if k in prev_o:
            p = prev_o[k]
            moved = (p["date"], p["slot_idx"]) != (o["date"], o["slot_idx"])
            if moved:
                events.append(("일정변경", o, p))
            if p["tech"] != o["tech"]:
                events.append(("기사변경", o, p))
            if not moved and p["tech"] == o["tech"] and \
                    (p["title"], p["product"]) != (o["title"], o["product"]):
                events.append(("내용수정", o, p))
        elif o["date"] in prev_dates:          # 새 날짜가 화면에 들어온 경우는 조용히 기준만 저장
            events.append(("신규", o, None))

    gone = []
    for k, p in prev_o.items():
        if k in cur_o:
            continue
        if p["date"] < today or p["date"] not in cur_dates:
            continue                           # 지난 날짜가 화면에서 빠진 것
        gone.append(("사라짐", None, p))
    return events + gone


def fmt_date(d):
    y, m, dd = map(int, d.split("-"))
    w = WEEKDAYS[dt.date(y, m, dd).weekday()]
    return f"{m}/{dd}({w})"


def fmt_event(kind, o, p):
    x = o or p
    head = f"*[{kind}]* {fmt_date(x['date'])} {x['slot_label']}"
    lines = [head]
    if kind == "일정변경":
        lines.append(f"일정: {fmt_date(p['date'])} {p['slot_label']} → {fmt_date(o['date'])} {o['slot_label']}")
    if kind == "기사변경":
        lines.append(f"기사: {p['tech']} → {o['tech']}")
    elif kind != "사라짐":
        lines.append(f"기사: {x['tech']}")
    if kind == "내용수정":
        lines.append(f"이전: {p['title']}")
    lines.append(x["title"])
    tail = " / ".join(v for v in (x["phone"], x["product"]) if v)
    if tail:
        lines.append(tail)
    if kind == "사라짐":
        lines.append(f"(기사: {p['tech']}) 일정표에서 빠졌습니다 — 취소·삭제 또는 완료처리 여부 확인 필요")
    return "\n".join(lines)


def fmt_empty(cur, cfg, now):
    if cfg.empty_format == "text":
        return fmt_empty_text(cur, cfg, now)
    return fmt_empty_table(cur, cfg, now)


def fmt_empty_table(cur, cfg, now):
    """Slack 코드블록(고정폭 글꼴) 안에 날짜×구간 표. 정렬이 깨지지 않도록 표 안에는 영문·숫자만 사용."""
    today = now.date()
    dates = [d for d in sorted({s["date"] for s in cur["slots"]})
             if 0 <= (dt.date.fromisoformat(d) - today).days < cfg.empty_days
             and not (cfg.skip_sunday and dt.date.fromisoformat(d).weekday() == 6)]
    if not dates:
        return None
    cols = sorted({(s["no"] or int(s["slot_idx"]), s["start_hour"]) for s in cur["slots"]})
    head = "       " + "".join(f"{(h if h is not None else n):>4}" for n, h in cols) + "  (시)"
    rows = [head]
    for d in dates:
        dd = dt.date.fromisoformat(d)
        cells = []
        for n, h in cols:
            s = next((x for x in cur["slots"] if x["date"] == d and (x["no"] or int(x["slot_idx"])) == n), None)
            if s is None:
                c = "?"
            elif dd == today and h is not None and h <= now.hour:
                c = "-"
            elif s["count"] == 0:
                c = "O"
            else:
                c = str(s["count"])
            cells.append(f"{c:>4}")
        rows.append(f"{dd.month:>2}/{dd.day:<2}  " + "".join(cells) + f"   {WEEKDAYS[dd.weekday()]}")
    cap = "" if cfg.capacity == 1 else f" · 구간당 {cfg.capacity}건 기준"
    return ("*[빈 구간 안내]*  O = 비어있음 · 숫자 = 접수 건수 · - = 지난 시간" + cap +
            "\n```\n" + "\n".join(rows) + "\n```")


def fmt_empty_text(cur, cfg, now):
    today = now.date()
    dates = sorted({s["date"] for s in cur["slots"]})
    out = []
    for d in dates:
        dd = dt.date.fromisoformat(d)
        if dd < today or (dd - today).days >= cfg.empty_days:
            continue
        if cfg.skip_sunday and dd.weekday() == 6:
            continue
        frees = []
        for s in sorted((s for s in cur["slots"] if s["date"] == d), key=lambda s: (s["no"], s["slot_idx"])):
            if dd == today and s["start_hour"] is not None and s["start_hour"] <= now.hour:
                continue
            if s["count"] < cfg.capacity:
                short = f'{s["no"] or s["slot_idx"]}구간({s["label"].split(" ")[-1]})'
                frees.append(short if cfg.capacity == 1 else f'{short} {s["count"]}/{cfg.capacity}건')
        out.append(f"{fmt_date(d)}: " + (", ".join(frees) if frees else "빈 구간 없음"))
    title = "*[빈 구간 안내]*" + ("" if cfg.capacity == 1 else f" (구간당 {cfg.capacity}건 기준)")
    return title + "\n" + "\n".join(out) if out else None


# ─────────────────────────── Slack ───────────────────────────
def slack(cfg, text):
    if not cfg.webhook:
        log.warning("Slack webhook_url 이 비어 있어 알림 %d자를 보내지 못함", len(text))
        return
    chunks, buf = [], ""
    for block in text.split("\n\n"):
        if len(buf) + len(block) > 3500:
            chunks.append(buf)
            buf = ""
        buf += ("\n\n" if buf else "") + block
    if buf:
        chunks.append(buf)
    for c in chunks:
        r = requests.post(cfg.webhook, json={"text": c}, timeout=15)
        if r.status_code != 200:
            log.error("Slack 전송 실패 %s %s", r.status_code, r.text[:200])


# ─────────────────────────── 상태 저장 ───────────────────────────
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


# ─────────────────────────── 실행 ───────────────────────────
def in_work_hours(cfg, now):
    return cfg.work_start <= now.time() <= cfg.work_end


# ─────────────────────────── 일정표 캡처 ───────────────────────────
CROP_JS = r"""
(dates) => {
  const isReg = el => /order_reg_pop\.php[^'"]*order_set_idx/i.test(el.getAttribute('onclick') || '');
  const regs = [...document.querySelectorAll('[onclick]')].filter(isReg);
  if (!regs.length) return null;
  const dateOf = el => ((el.getAttribute('onclick') || '').match(/in_date=(\d{4}-\d{2}-\d{2})/) || [])[1];
  // 모든 등록 버튼을 품는 가장 작은 요소 = 일정표 전체
  let box = regs[0];
  while (box.parentElement && regs.some(r => !box.contains(r))) box = box.parentElement;
  const all = box.getBoundingClientRect();
  // 날짜별로 그 날짜 등록버튼 7개만 품는 요소(= 그 날짜 줄)의 위쪽 좌표
  const rowTop = d => {
    const mine = regs.filter(r => dateOf(r) === d);
    if (!mine.length) return null;
    let e = mine[0];
    while (e.parentElement && mine.some(r => !e.contains(r))) e = e.parentElement;
    return e.getBoundingClientRect().top + window.scrollY;
  };
  const order = [...new Set(regs.map(dateOf))].sort();
  const want = dates.length ? dates : order.slice(0, 3);
  const top = rowTop(want[0]);
  const nextDate = order[order.indexOf(want[want.length - 1]) + 1];
  const bottom = nextDate ? rowTop(nextDate) : all.bottom + window.scrollY;
  if (top == null || bottom == null) return null;
  return {x: Math.max(0, all.left + window.scrollX - 4), y: Math.max(0, top - 6),
          width: all.width + 8, height: bottom - top + 2};
}
"""


def capture_board(cfg, bak, dates):
    """보안관 일정표를 실제 브라우저로 열어 앞쪽 N일만 잘라 PNG로 반환"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(timeout=60000)
        try:
            ctx = browser.new_context(viewport={"width": 1720, "height": 1200}, device_scale_factor=1.5)
            ctx.add_cookies([{"name": c.name, "value": c.value, "url": cfg.base_url + "/"}
                             for c in bak.s.cookies])
            page = ctx.new_page()
            page.goto(bak.board_url, wait_until="networkidle", timeout=30000)
            if not is_board(page.content()):
                raise RuntimeError("캡처용 브라우저에서 일정표가 열리지 않았습니다(로그인 쿠키 문제).")
            clip = page.evaluate(CROP_JS, dates)
            if clip:
                return page.screenshot(clip=clip, full_page=True)
            log.warning("일정표 자르기 실패 → 전체 화면 캡처")
            return page.screenshot(full_page=True)
        finally:
            browser.close()


def slack_api(cfg, method, **data):
    r = requests.post(f"{cfg.slack_api}/{method}", data=data, timeout=30,
                      headers={"Authorization": f"Bearer {cfg.bot_token}"})
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Slack {method} 실패: {j.get('error')}")
    return j


def slack_image(cfg, png, comment):
    """그림 업로드 → 채널 공유. 파일 id 반환"""
    name = dt.datetime.now().strftime("설치일정_%m%d_%H%M.png")
    j = slack_api(cfg, "files.getUploadURLExternal", filename=name, length=str(len(png)))
    up = requests.post(j["upload_url"], files={"file": (name, png, "image/png")}, timeout=60)
    if up.status_code != 200:
        raise RuntimeError(f"Slack 그림 업로드 실패: HTTP {up.status_code}")
    slack_api(cfg, "files.completeUploadExternal",
              files=json.dumps([{"id": j["file_id"], "title": "설치 일정"}], ensure_ascii=False),
              channel_id=cfg.channel_id, initial_comment=comment)
    return j["file_id"]


DONG_RE = re.compile(r"([가-힣0-9]+동)(?=[\s,])")


def short_event(kind, o, p):
    x = o or p
    dm = DONG_RE.search(x["title"])
    where = dm.group(1) if dm else x["title"].split(" / ")[0][-12:]
    if kind == "기사변경":
        who = f"{p['tech']} → {o['tech']}"
    elif kind == "일정변경":
        who = f"{fmt_date(p['date'])} {p['slot_label'].split(' ')[0]}에서 이동 · {x['tech']}"
    else:
        who = x["tech"]
    return f"*[{kind}]* {fmt_date(x['date'])} {x['slot_label'].split(' ')[0]} · {who} · {where}"


# ─────────────────────────── 휴대폰용 표 그리기 ───────────────────────────
TABLE_CSS = """
*{box-sizing:border-box;margin:0}
body{background:#fff;font-family:"Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",sans-serif;color:#1d1c1d}
#t{width:360px;padding:10px;background:#fff}
.ttl{display:flex;justify-content:space-between;align-items:baseline;margin:0 2px 6px}
.ttl b{font-size:15px}.ttl span{font-size:11px;color:#666}
table{width:100%;border-collapse:separate;border-spacing:4px 6px;table-layout:fixed}
th{font-size:12px;padding:5px 2px;border-radius:6px;background:#0a5eb5;color:#fff}
th.today{background:#e60000}
th small{display:block;font-weight:400;font-size:10.5px;opacity:.9}
td.t{width:40px;font-size:12px;font-weight:800;color:#444;text-align:center;vertical-align:middle;background:#eef0f3;border-radius:6px}
td.c{min-height:50px;height:50px;border-radius:6px;padding:4px 5px;vertical-align:top;font-size:10.5px;line-height:1.3;position:relative}
td.ok{background:#e9f7ec;color:#1e7b34;text-align:center;vertical-align:middle;font-weight:800;font-size:12px}
td.past{background:#f1f1f1;color:#aaa;text-align:center;vertical-align:middle;font-size:11px}
td.job{background:#eaf2fd;border:1px solid #c8dcf7}
td.un{background:#fdecec;border:1px solid #f3b9b9}
td.chg{outline:3px solid #ffb800;outline-offset:-2px}
.o + .o{border-top:1px dashed #b7cdee;margin-top:3px;padding-top:2px}
.n{font-size:13px;font-weight:800;display:block}
.o.u .n{color:#c62828}
.d,.m{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.d{color:#333}.m{color:#666}
.bd{position:absolute;right:-3px;top:-7px;background:#ffb800;color:#000;font-size:9px;font-weight:800;padding:0 3px;border-radius:3px}
.lg{display:flex;flex-wrap:wrap;gap:8px;margin:6px 2px 0;font-size:10.5px;color:#555}
.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:3px;vertical-align:-1px}
"""

ADDR_RE = re.compile(r"([가-힣0-9]+동)[,\s]+([^/,]+)")


def _esc(x):
    return (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tech_short(t):
    return re.sub(r"\s*(설치|AS)$", "", t).strip() or t


def _place(o):
    m = ADDR_RE.search(o["title"])
    if m:
        return f"{m.group(1)} {m.group(2).split()[0]}"
    dm = DONG_RE.search(o["title"])
    return dm.group(1) if dm else o["title"].split(" / ")[0][-10:]


def _model(o):
    parts = o["title"].split(" / ")
    if len(parts) > 1 and len(parts[1]) <= 16:
        return parts[1]
    return (o["product"] or "").split(" / ")[0]


def table_dates(cur, cfg, now):
    out = []
    for d in sorted({s["date"] for s in cur["slots"]}):
        dd = dt.date.fromisoformat(d)
        if dd < now.date() or (cfg.skip_sunday and dd.weekday() == 6):
            continue
        out.append(d)
    return out[:cfg.img_days]


def table_html(cur, cfg, now, events):
    dates = table_dates(cur, cfg, now)
    changed = {}
    for kind, o, p in events:
        if o is not None:
            changed[(o["date"], o["slot_idx"], o["title"])] = "신규" if kind == "신규" else "변경"
    slots = {}
    for s in cur["slots"]:
        slots.setdefault(s["slot_idx"], s)
    order = sorted(slots.values(), key=lambda s: (s["no"] or int(s["slot_idx"])))
    h = [f'<div id="t"><div class="ttl"><b>설치 일정</b><span>마지막 갱신 {now.month}/{now.day} {now:%H:%M}</span></div><table><tr><td class="t" style="background:none"></td>']
    for d in dates:
        dd = dt.date.fromisoformat(d)
        top = "오늘" if dd == now.date() else f"{dd.month}/{dd.day}"
        sub = f"{dd.month}/{dd.day} {WEEKDAYS[dd.weekday()]}" if dd == now.date() else WEEKDAYS[dd.weekday()]
        h.append(f'<th class="{"today" if dd == now.date() else ""}">{top}<small>{sub}</small></th>')
    h.append("</tr>")
    for s in order:
        hour = (s["label"].split("[")[-1].split("~")[0] if "[" in s["label"] else s["label"].split(" ")[-1].split("~")[0])
        h.append(f'<tr><td class="t">{_esc(hour)}</td>')
        for d in dates:
            jobs = [o for o in cur["orders"].values() if o["date"] == d and o["slot_idx"] == s["slot_idx"]]
            dd = dt.date.fromisoformat(d)
            past = dd == now.date() and s["start_hour"] is not None and s["start_hour"] <= now.hour
            if not jobs:
                h.append('<td class="c past">지남</td>' if past else '<td class="c ok">가능</td>')
                continue
            un = all(o["tech"] == "미배정" for o in jobs)
            badge = next((changed[(o["date"], o["slot_idx"], o["title"])] for o in jobs
                          if (o["date"], o["slot_idx"], o["title"]) in changed), None)
            cls = "c " + ("un" if un else "job") + (" chg" if badge else "")
            inner = "".join(
                f'<div class="o{" u" if o["tech"] == "미배정" else ""}"><span class="n">{_esc(_tech_short(o["tech"]))}</span>'
                f'<span class="d">{_esc(_place(o))}</span>'
                + (f'<span class="m">{_esc(_model(o))}</span>' if len(jobs) == 1 else "") + "</div>"
                for o in jobs)
            h.append(f'<td class="{cls}">' + (f'<span class="bd">{badge}</span>' if badge else "") + inner + "</td>")
        h.append("</tr>")
    h.append('</table><div class="lg"><span><i style="background:#e9f7ec;border:1px solid #9bd3a8"></i>가능</span>'
             '<span><i style="background:#eaf2fd;border:1px solid #c8dcf7"></i>배정</span>'
             '<span><i style="background:#fdecec;border:1px solid #f3b9b9"></i>미배정</span>'
             '<span><i style="background:#fff;outline:2px solid #ffb800"></i>방금 바뀜</span></div></div>')
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{TABLE_CSS}</style></head><body>{"".join(h)}</body></html>', dates


def render_table_png(cur, cfg, now, events):
    from playwright.sync_api import sync_playwright
    html, dates = table_html(cur, cfg, now, events)
    with sync_playwright() as p:
        browser = p.chromium.launch(timeout=60000)
        try:
            page = browser.new_page(viewport={"width": 380, "height": 800}, device_scale_factor=3)
            page.set_content(html, timeout=30000)
            return page.locator("#t").screenshot(timeout=30000), dates
        finally:
            browser.close()


def make_image(cfg, bak, cur, now, events):
    if cfg.img_style == "capture":
        dates = [d for d in sorted({s["date"] for s in cur["slots"]}) if d >= now.date().isoformat()][:cfg.img_days]
        return capture_board(cfg, bak, dates), dates
    return render_table_png(cur, cfg, now, events)


def status_line(cfg, st, now, repost=False):
    """채널 맨 아래 상태줄 1개: '정상 작동 중 · 마지막 확인 HH:MM'. 2분마다 글만 고침(알림 안 울림).
    프로그램이 멈추면 이 시각이 더 이상 안 바뀌므로 직원들이 멈춘 걸 알 수 있다."""
    if not cfg.use_image:
        return
    text = f"● 정상 작동 중 · 마지막 확인 {now.month}/{now.day} {now:%H:%M}  _(2분마다 확인 · 이 시각이 멈춰 있으면 사무실에 알려주세요)_"
    ts = st.get("status_ts")
    try:
        if ts and not repost and st.get("status_day") == str(now.date()):
            slack_api(cfg, "chat.update", channel=cfg.channel_id, ts=ts, text=text)
            return
        if ts:
            try:
                slack_api(cfg, "chat.delete", channel=cfg.channel_id, ts=ts)
            except Exception as e:  # noqa
                log.info("이전 상태줄 삭제 못함: %s", e)
        j = slack_api(cfg, "chat.postMessage", channel=cfg.channel_id, text=text)
        st["status_ts"], st["status_day"] = j.get("ts"), str(now.date())
    except Exception as e:  # noqa
        log.warning("상태줄 갱신 실패: %s", e)
        if "message_not_found" in str(e) or "cant_update" in str(e):
            st.pop("status_ts", None)


def post_board_image(cfg, bak, st, cur, now, events):
    png, dates = make_image(cfg, bak, cur, now, events)
    if events:
        head = "\n".join(short_event(*e) for e in events)
    else:
        head = f"*[설치 일정]* {fmt_date(dates[0])}~{fmt_date(dates[-1])} 현황"
    head += f"\n_마지막 갱신 {now.month}/{now.day} {now:%H:%M}_"
    fid = slack_image(cfg, png, head)
    prev = st.get("last_image")
    if cfg.img_delete_prev and prev:
        try:
            slack_api(cfg, "files.delete", file=prev)
        except Exception as e:  # noqa
            log.warning("이전 그림 삭제 실패: %s", e)
    st["last_image"] = fid
    st["last_image_day"] = str(now.date())
    status_line(cfg, st, now, repost=True)          # 상태줄을 새 그림 아래로 옮김
    log.info("일정표 그림 전송 (변경 %d건)", len(events))


def run_once(cfg, bak, st, now):
    html = bak.fetch_board()
    cur = parse_board(html)
    if not cur["slots"]:
        raise RuntimeError("일정표 구간을 하나도 읽지 못했습니다(화면 구조 변경 가능성).")

    prev = st.get("snapshot")
    if prev is None:
        slack(cfg, f"*[보안관 감시 시작]* 일정 {len({s['date'] for s in cur['slots']})}일 · "
                   f"{len(cur['slots'])}개 구간 · 접수 {len(cur['orders'])}건 확인. 이후 변경분부터 알립니다.")
    else:
        events = diff(prev, cur, now.date().isoformat())
        gone = [e for e in events if e[0] == "사라짐"]
        if len(gone) >= 5 and len(gone) > len(prev["orders"]) * 0.5:
            # 대량으로 사라진 경우: 오작동 가능성이 높으므로 개별 알림 대신 경고 1건
            slack(cfg, f"*[확인 필요]* 한 번에 {len(gone)}건이 일정표에서 사라졌습니다. "
                       f"보안관 화면을 직접 확인해 주세요. (개별 알림 생략)")
            events = [e for e in events if e[0] != "사라짐"]
        need_daily = cfg.use_image and st.get("last_image_day") != str(now.date())
        if events or need_daily:
            sent_img = False
            if cfg.use_image:
                try:
                    post_board_image(cfg, bak, st, cur, now, events)
                    sent_img = True
                except Exception as e:  # noqa
                    log.exception("일정표 그림 전송 실패 → 글 알림으로 대신 보냄")
                    if not st.get("img_err_notified"):
                        slack(cfg, f"*[그림 전송 실패]* {e}\n당분간 글 알림으로 보냅니다.")
                        admin(cfg, f"그림 전송 실패 → 글 알림으로 대체 중: {e}")
                        st["img_err_notified"] = True
            if sent_img:
                st["img_err_notified"] = False
            elif events:
                slack(cfg, "\n\n".join(fmt_event(*e) for e in events))
            log.info("변경 %d건 전송 (%s)", len(events), "그림" if sent_img else "글")

    # 빈 구간 안내 (정해진 시각 이후 하루 1회씩)
    sent = st.setdefault("empty_sent", {})
    for t in cfg.empty_times:
        k = f"{now.date()} {t.strftime('%H:%M')}"
        late = (dt.datetime.combine(now.date(), t) + dt.timedelta(minutes=30)).time()
        if t <= now.time() <= late and k not in sent:
            msg = fmt_empty(cur, cfg, now)
            if msg:
                slack(cfg, msg)
            sent[k] = True
    st["empty_sent"] = {k: v for k, v in sent.items() if k[:10] >= str(now.date() - dt.timedelta(days=2))}

    st["snapshot"] = cur
    return cur


def setup_logging(console=True):
    log.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(sh)


def test_mode(cfg):
    bak = Bak(cfg)
    html = bak.fetch_board()
    with open(DUMP_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    cur = parse_board(html)
    dates = sorted({s["date"] for s in cur["slots"]})
    print(f"\n로그인·조회 성공: {len(dates)}일, {len(cur['slots'])}개 구간, 접수 {len(cur['orders'])}건\n")
    for d in dates[:cfg.empty_days]:           # 오늘부터 3일치만, 접수 있는 구간만
        busy = [s for s in cur["slots"] if s["date"] == d and s["count"]]
        print(f"■ {fmt_date(d)}  " + ("접수 없음" if not busy else ""))
        for s in busy:
            names = [o["tech"] for o in cur["orders"].values()
                     if o["date"] == d and o["slot_idx"] == s["slot_idx"]]
            print(f"   {s['label']:<22} {s['count']}건  {', '.join(names)}")
    rest = sum(1 for o in cur["orders"].values() if o["date"] not in dates[:cfg.empty_days])
    if rest:
        print(f"\n(그 이후 날짜 접수 {rest}건은 생략)")
    print("\n" + (fmt_empty(cur, cfg, dt.datetime.now()) or "(빈 구간 안내 대상 없음)").replace("*", ""))
    if not cfg.use_image:
        why = [n for n, v in (("enabled = yes", cfg.img_on), ("bot_token", cfg.bot_token), ("channel_id", cfg.channel_id)) if not v]
        print("\n[그림 알림 꺼짐] config.ini [image] 칸에서 비어 있거나 꺼진 값: " + ", ".join(why))
    elif not cfg.bot_token.startswith("xoxb-") or not cfg.channel_id.upper().startswith(("C", "G")):
        print("\n[확인 필요] bot_token 은 xoxb- 로, channel_id 는 C 로 시작해야 합니다. 괄호나 설명 글이 남아 있는지 보세요.")
    if cfg.use_image or "--shot" in sys.argv:
        try:
            png, _ = make_image(cfg, bak, cur, dt.datetime.now(), [])
            path = os.path.join(BASE_DIR, "capture_test.png")
            with open(path, "wb") as f:
                f.write(png)
            print(f"\n일정표 그림 만들기 성공 → {os.path.basename(path)} 를 열어서 확인하세요.")
            if cfg.use_image:
                slack_api(cfg, "auth.test")
                print("Slack 봇 토큰 확인 성공.")
        except Exception as e:  # noqa
            print(f"\n[캡처/Slack 봇 확인 실패] {e}")
    print(f"\n※ 화면 원본을 {os.path.basename(DUMP_PATH)} 에 저장했습니다(고객정보 포함 — 확인 후 삭제하세요).")


# ─────────────────────────── 원격 관리: 상태 보고 · 자동 업데이트 ───────────────────────────
def admin(cfg, text, fallback=False):
    """본사 관리 채널로 상태 보고. 고객정보는 절대 넣지 않는다(건수·오류 문구만).
    fallback=True 이면 관리 채널이 없을 때 일반 채널로 보낸다(정상 작동·재시작 알림)."""
    url = cfg.admin_webhook or (cfg.webhook if fallback else "")
    if not url:
        return
    try:
        requests.post(url, json={"text": f"*[{cfg.site}]* {text}  _(v{VERSION})_"}, timeout=15)
    except Exception:  # noqa
        log.exception("상태 보고 실패")


def heartbeat(cfg, st, now):
    k = str(now.date())
    if st.get("hb_day") == k or now.time() < cfg.heartbeat:
        return
    snap = st.get("snapshot") or {"orders": {}}
    today = now.date().isoformat()
    in3 = (now.date() + dt.timedelta(days=3)).isoformat()
    n_today = sum(1 for o in snap["orders"].values() if o["date"] == today)
    n_3 = sum(1 for o in snap["orders"].values() if today <= o["date"] < in3)
    n_un = sum(1 for o in snap["orders"].values() if today <= o["date"] < in3 and o["tech"] == "미배정")
    last = st.get("last_ok", "-")
    admin(cfg, f"정상 작동 중 · 오늘 {n_today}건 · 3일 {n_3}건(미배정 {n_un}) · 마지막 조회 {last} · "
               f"그림알림 {'켜짐' if cfg.use_image else '꺼짐'}", fallback=True)
    st["hb_day"] = k
    log.info("하루 1회 정상 작동 보고 전송")


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


UPDATE_PROTECTED = {"config.ini", "state.json", "3_run.bat", "alive.json"}   # 업데이트로 절대 바꾸지 않는 파일
UPDATE_EXT = (".py", ".bat", ".txt", ".md", ".example.ini")
SAFE_NAME = re.compile(r"^[0-9A-Za-z가-힣_.\-]+$")


def _sha(path):
    import hashlib
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check_update(cfg, st, now, force=False):
    """update_url 의 manifest.json 을 6시간마다 확인.
    manifest 의 파일 목록과 지문(sha256)을 내 파일과 비교해서 다른 파일만 내려받아 교체한다.
    - 모든 파일을 먼저 내려받아 지문·문법 검사를 다 통과해야 교체 시작 (하나라도 틀리면 아무것도 안 바꿈)
    - 바꾸기 전 파일은 backup 폴더(시각별)에 보관
    - config.ini / state.json / 3_run.bat 은 절대 안 바꿈
    - 교체 후 종료: 코드 3 = 프로그램만 재시작, 코드 4 = 감시기(watchdog)도 재시작"""
    if not (cfg.update_url and cfg.auto_update):
        return
    last = st.get("upd_checked")
    if not force and last and (now - dt.datetime.fromisoformat(last)).total_seconds() < 6 * 3600:
        return
    st["upd_checked"] = now.isoformat(timespec="seconds")
    import hashlib
    try:
        m = requests.get(cfg.update_url, timeout=20, headers={"Cache-Control": "no-cache"}).json()
        files = m.get("files") or [{"path": m.get("file", "bak_watch.py"), "sha256": m["sha256"]}]
        changed = []
        for f in files:
            name = f["path"]
            if not SAFE_NAME.match(name) or name in UPDATE_PROTECTED or not name.endswith(UPDATE_EXT):
                continue
            if _sha(os.path.join(BASE_DIR, name)) != f["sha256"].lower():
                changed.append(f)
        if not changed:
            log.info("업데이트 확인: 최신 상태 (v%s)", m.get("version", "?"))
            return
        bodies = {}
        for f in changed:                                        # 1) 전부 내려받아 검사
            body = requests.get(urljoin(cfg.update_url, f["path"]), timeout=60,
                                headers={"Cache-Control": "no-cache"}).content
            if hashlib.sha256(body).hexdigest() != f["sha256"].lower():
                raise RuntimeError(f"{f['path']} 지문(sha256)이 맞지 않음 — 업데이트 중단(아무것도 안 바꿈)")
            if f["path"].endswith(".py"):
                compile(body.decode("utf-8"), f["path"], "exec")
            bodies[f["path"]] = body
        missing = []                                              # 2) 없는 부품만 먼저 설치
        import importlib.util
        for item in m.get("pip") or []:
            pkg, _, mod = item.partition("=")                     # 예: "beautifulsoup4=bs4"
            if importlib.util.find_spec(mod or pkg) is None:
                missing.append(pkg)
        if missing:
            import subprocess
            log.info("새 부품 설치: %s", ", ".join(missing))
            r = subprocess.run([sys.executable, "-m", "pip", "install", *missing],
                               capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                raise RuntimeError("필요한 부품 설치 실패 — 업데이트 중단(아무것도 안 바꿈): " + (r.stderr or "")[-300:])
        bdir = os.path.join(BASE_DIR, "backup", f"{now:%Y%m%d_%H%M%S}_v{VERSION}")   # 3) 백업
        os.makedirs(bdir, exist_ok=True)
        for name in bodies:
            p = os.path.join(BASE_DIR, name)
            if os.path.exists(p):
                with open(p, "rb") as src, open(os.path.join(bdir, name), "wb") as dst:
                    dst.write(src.read())
        for name, body in bodies.items():                         # 4) 교체
            p = os.path.join(BASE_DIR, name)
            with open(p + ".new", "wb") as f:
                f.write(body)
            os.replace(p + ".new", p)
        save_state(st)
        names = ", ".join(bodies)
        admin(cfg, f"업데이트 완료 v{VERSION} → v{m.get('version', '?')} ({names}) · 잠시 뒤 재시작")
        log.info("업데이트 %s → %s: %s", VERSION, m.get("version"), names)
        sys.exit(4 if "watchdog.py" in bodies else 3)
    except SystemExit:
        raise
    except Exception as e:  # noqa
        log.exception("업데이트 확인 실패")
        if st.get("upd_err_day") != str(now.date()):        # 하루 1번만 보고
            admin(cfg, f"업데이트 확인 실패: {e}")
            st["upd_err_day"] = str(now.date())


def alive(step):
    """감시 프로그램(watchdog)에게 '아직 살아있음 + 지금 하는 일'을 알림. 멈추면 이 시각이 멈춘다."""
    try:
        tmp = ALIVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "at": dt.datetime.now().strftime("%H:%M:%S"),
                       "step": step, "pid": os.getpid()}, f, ensure_ascii=False)
        os.replace(tmp, ALIVE_PATH)
    except Exception:  # noqa
        log.debug("alive 기록 실패", exc_info=True)


def console_safe():
    """윈도우 검은 창을 마우스로 클릭하면(빠른 편집 모드) 프로그램이 통째로 멈추는 문제를 막는다."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)   # QUICK_EDIT 끄기, EXTENDED_FLAGS 켜기
        k.SetThreadExecutionState(0x80000000 | 0x00000001)          # 실행 중에는 '절전 모드' 진입 막기(화면 꺼짐은 허용)
    except Exception:  # noqa
        log.debug("콘솔 설정 실패", exc_info=True)


def log_env(cfg):
    import platform
    log.info("시작 v%s · Python %s · %s · 그림알림 %s · 관리채널 %s · 자동업데이트 %s",
             VERSION, platform.python_version(), platform.platform(),
             "켜짐" if cfg.use_image else "꺼짐", "있음" if cfg.admin_webhook else "없음",
             "켜짐" if (cfg.update_url and cfg.auto_update) else "꺼짐")


def single_instance(port=47231):
    """같은 PC에서 두 번 실행되면 알림이 2개씩 가므로, 두 번째 실행은 막는다."""
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sk.bind(("127.0.0.1", port))
        sk.listen(1)
        return sk
    except OSError:
        sk.close()
        return None


def main():
    cfg = Config()
    if "--test" in sys.argv:
        setup_logging(console=False)
        test_mode(cfg)
        return
    setup_logging()
    lock = single_instance()
    if lock is None:
        log.error("이미 다른 창에서 실행 중입니다. 이 창은 종료합니다. (알림 중복 방지)")
        time.sleep(5)
        sys.exit(9)
    console_safe()
    log.info("보안관 감시 시작 v%s (간격 %d초, 업무시간 %s~%s)", VERSION, cfg.interval, cfg.work_start, cfg.work_end)
    log_env(cfg)
    alive("시작")
    bak = Bak(cfg)
    st = load_state()
    if "--child" not in sys.argv:          # watchdog 없이 직접 켠 경우에만 (watchdog 은 따로 알림)
        admin(cfg, "프로그램 시작")
    check_update(cfg, st, dt.datetime.now(), force=True)
    fails = 0
    last_loop = time.time()
    while True:
        now = dt.datetime.now()
        gap = time.time() - last_loop
        if gap > cfg.interval * 3 + 60:
            log.warning("공백 %.0f분 감지 — PC 절전/잠자기 또는 프로그램 멈춤이 있었음", gap / 60)
        last_loop = time.time()
        alive("업데이트 확인")
        check_update(cfg, st, now)
        if in_work_hours(cfg, now):
            try:
                t0 = time.time()
                alive("보안관 조회·알림")
                cur = run_once(cfg, bak, st, now)
                st["last_ok"] = now.strftime("%m/%d %H:%M")
                alive("상태줄 갱신")
                status_line(cfg, st, now)
                heartbeat(cfg, st, now)
                save_state(st)
                log.info("조회 OK · 접수 %d건 · %.1f초", len(cur["orders"]), time.time() - t0)
                if fails >= 3:
                    slack(cfg, "*[보안관 감시 복구]* 다시 정상적으로 조회하고 있습니다.")
                    admin(cfg, "복구됨 — 다시 정상 조회 중")
                fails = 0
            except Exception as e:  # noqa
                fails += 1
                log.exception("조회 실패 (%d회 연속)", fails)
                if fails == 3:
                    try:
                        slack(cfg, f"*[보안관 감시 오류]* 3회 연속 실패했습니다: {e}\n복구되면 다시 알려드립니다.")
                        admin(cfg, f"오류 — 3회 연속 조회 실패: {e}")
                    except Exception:
                        log.exception("오류 알림 전송 실패")
                bak = Bak(cfg)  # 세션 새로 만들기
        else:
            log.debug("업무시간 외 대기")
        alive("대기")
        time.sleep(cfg.interval)


if __name__ == "__main__":
    main()
