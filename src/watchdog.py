# -*- coding: utf-8 -*-
"""
보안관 알림 감시기 (watchdog)

bak_watch.py 를 대신 켜 주고 계속 지켜봅니다.
- 프로그램이 꺼지면            → 20초 뒤 다시 켬
- 켜져 있는데 응답이 없으면    → (alive.json 시각이 8분 넘게 멈춤) 강제 종료 후 다시 켬
- PC 가 절전/잠자기에서 깨어나면 → 기록하고 알림 (멈춤 원인 추적용)
- 다시 켤 때마다 Slack 에 "재시작됨 + 원인" 알림
- 30분 안에 5번 넘게 재시작되면 10분 쉬었다가 다시 시도 (알림 폭주 방지)

※ 이 파일은 bak_watch.py 를 import 하지 않습니다. bak_watch.py 가 망가져도 감시기는 살아 있어야 하므로.
"""
import configparser
import datetime as dt
import json
import logging
import os
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
CHILD = os.path.join(BASE, "bak_watch.py")
ALIVE = os.path.join(BASE, "alive.json")
LOG = os.path.join(BASE, "watchdog.log")
CHECK_SEC = int(os.environ.get("BAKWD_CHECK_SEC", "15"))
log = logging.getLogger("watchdog")


def setup_log():
    log.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s [감시기] %(message)s", "%H:%M:%S"))
    log.addHandler(sh)


def read_cfg():
    cp = configparser.ConfigParser(interpolation=None)
    p = os.path.join(BASE, "config.ini")
    for enc in ("utf-8-sig", "cp949"):
        try:
            with open(p, encoding=enc) as f:
                cp.read_file(f)
            break
        except UnicodeDecodeError:
            cp = configparser.ConfigParser(interpolation=None)
    g = lambda s, k, d="": cp.get(s, k, fallback=d).strip()
    interval = max(60, int(g("watch", "interval_sec", "120") or 120))
    return {
        "webhook": g("slack", "webhook_url"),
        "admin": g("admin", "admin_webhook_url"),
        "site": g("admin", "site_name") or g("bak", "company_id") or "대리점",
        # 한 번 조회(로그인+그림+업로드)는 길어도 3분 안쪽. 간격×3+2분 또는 8분 중 큰 값을 넘기면 '멈춤'
        "stall": int(os.environ.get("BAKWD_STALL_SEC", "0")) or max(480, interval * 3 + 120),
    }


def notify(cfg, text):
    """재시작·절전 알림: 관리 채널이 있으면 거기로, 없으면 일반 채널로"""
    url = cfg["admin"] or cfg["webhook"]
    if not url:
        return
    try:
        requests.post(url, json={"text": f"*[{cfg['site']}]* {text}"}, timeout=15)
    except Exception as e:  # noqa
        log.warning("Slack 알림 실패: %s", e)


def console_safe():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
    except Exception:  # noqa
        pass


def lock():
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sk.bind(("127.0.0.1", 47232))
        sk.listen(1)
        return sk
    except OSError:
        sk.close()
        return None


def read_alive():
    try:
        with open(ALIVE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return None


def kill_tree(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception as e:  # noqa
        log.warning("강제 종료 실패: %s", e)
    try:
        proc.wait(timeout=20)
    except Exception:  # noqa
        pass


def leftover_browsers(kill=False):
    """그림용 크롬(ms-playwright 폴더의 크롬)이 남아 쌓였는지 확인. 사용자가 쓰는 일반 크롬은 건드리지 않음."""
    try:
        import psutil
    except ImportError:
        return 0
    n = 0
    for p in psutil.process_iter(["exe", "cmdline"]):
        try:
            path = (p.info.get("exe") or "") + " " + " ".join(p.info.get("cmdline") or [])
            if "ms-playwright" in path or "pw-browsers" in path:
                n += 1
                if kill:
                    p.kill()
        except Exception:  # noqa
            continue
    return n


def start_child():
    kw = {"cwd": BASE}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    # 시작 전 alive 표시를 지금 시각으로 (예전 파일 때문에 바로 '멈춤' 판정되지 않게)
    try:
        with open(ALIVE, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "at": dt.datetime.now().strftime("%H:%M:%S"), "step": "켜는 중"}, f,
                      ensure_ascii=False)
    except Exception:  # noqa
        pass
    return subprocess.Popen([sys.executable, CHILD, "--child"], **kw)


def main():
    setup_log()
    console_safe()
    lk = lock()
    if lk is None:
        log.error("감시기가 이미 다른 창에서 실행 중입니다. 이 창은 종료합니다.")
        time.sleep(5)
        sys.exit(9)
    cfg = read_cfg()
    log.info("감시기 시작 (응답 없음 기준 %d분)", cfg["stall"] // 60)
    left = leftover_browsers(kill=True)
    if left:
        log.warning("남아 있던 그림용 크롬 %d개 정리", left)

    restarts = []
    reason = None
    first = True
    quiet = False            # 재시작이 반복되는 중엔 개별 알림을 멈춤(알림 폭주 방지)
    last_storm_note = 0
    while True:
        child = start_child()
        child_start = time.time()
        log.info("bak_watch 실행 (pid %s)", child.pid)
        if first:
            notify(cfg, "프로그램 시작됨 (자동 재시작 감시 켜짐)")
            first = False
        elif reason and not quiet:
            notify(cfg, f"*재시작됨* — 원인: {reason}")
        reason = None
        last_tick = time.time()
        last_br_log = time.time()

        while True:
            time.sleep(CHECK_SEC)
            now = time.time()
            gap = now - last_tick
            last_tick = now
            if gap > CHECK_SEC * 8:          # 감시기 자신도 멈춰 있었음 = PC 가 잠들었었음
                mins = gap / 60
                log.warning("PC 절전/잠자기에서 깨어남 (약 %.0f분 멈춤)", mins)
                notify(cfg, f"PC가 약 {mins:.0f}분 동안 절전/잠자기 상태였습니다. 그동안 알림이 멈췄습니다. "
                            f"(윈도우 절전 설정 확인 필요)")
                # 깨어난 직후엔 자식도 막 깨어난 상태 → alive 시각을 기다려 준다
                try:
                    a = read_alive() or {}
                    a["t"] = now
                    with open(ALIVE, "w", encoding="utf-8") as f:
                        json.dump(a, f, ensure_ascii=False)
                except Exception:  # noqa
                    pass

            code = child.poll()
            if code is not None:
                if code == 3:
                    log.info("업데이트 후 재시작")
                    reason = None
                elif code == 4:
                    log.info("감시기도 업데이트됨 → 감시기를 끄고 3_run.bat 이 새 감시기로 다시 켬")
                    sys.exit(0)
                elif code == 9:
                    reason = "프로그램이 '이미 실행 중'이라며 꺼짐 — 3_run 창이 2개 떠 있는지 확인"
                else:
                    reason = f"프로그램이 꺼짐 (종료코드 {code}) — bak_watch.log 마지막 부분 확인"
                log.warning("bak_watch 종료: 코드 %s", code)
                break

            a = read_alive()
            if a and now - a.get("t", now) > cfg["stall"]:
                mins = (now - a["t"]) / 60
                reason = f"응답 없음 {mins:.0f}분 (멈춘 단계: {a.get('step', '?')}, 마지막 신호 {a.get('at', '?')})"
                log.error("멈춤 감지: %s → 강제 종료", reason)
                kill_tree(child)
                break

            if quiet and now - child_start > 1800:
                quiet = False
                restarts = []
                log.info("30분 이상 정상 동작 → 반복 재시작 상태 해제")
                notify(cfg, "다시 안정적으로 동작 중입니다 (30분 이상 멈춤 없음)")

            if now - last_br_log > 3600:
                n = leftover_browsers()
                log.info("상태 확인: 그림용 크롬 프로세스 %d개", n)
                if n > 10:
                    log.warning("그림용 크롬이 %d개 쌓임 → 정리", n)
                    leftover_browsers(kill=True)
                last_br_log = now

        left = leftover_browsers(kill=True)
        if left:
            log.warning("남은 그림용 크롬 %d개 정리", left)
            if reason:
                reason += f" · 남은 크롬 {left}개 정리"

        restarts = [t for t in restarts if time.time() - t < 1800] + [time.time()]
        if len(restarts) >= 5:
            log.error("30분 안에 %d번 재시작 → 10분 쉬고 다시 시도", len(restarts))
            if time.time() - last_storm_note > 3600:          # 이 알림은 1시간에 1번만
                notify(cfg, f"*재시작이 계속 반복됨* — 10분 간격으로 계속 다시 시도합니다. 고쳐지면 '다시 안정' 알림이 옵니다. "
                            f"마지막 원인: {reason or '업데이트'}  (bak_watch.log 확인 필요)")
                last_storm_note = time.time()
            quiet = True
            time.sleep(int(os.environ.get("BAKWD_BACKOFF_SEC", "600")))
            restarts = []
        else:
            time.sleep(int(os.environ.get("BAKWD_RESTART_SEC", "20")))
        cfg = read_cfg()     # 그새 config.ini 를 고쳤으면 반영


if __name__ == "__main__":
    main()
