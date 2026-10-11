# -*- coding: utf-8 -*-
"""
보안관 알림 프로그램 — 배포 도구 (노트북 관리 본부용)

  python tools/release.py beta      시험배포: 검사 → 버전 올림 → release/beta 에 만들고 GitHub 에 올림
                                    (update_url 을 beta 로 둔 PC = 우리 마지 PC 만 업데이트됨)
  python tools/release.py stable    전체배포: 시험배포한 그대로를 release/stable 로 복사해서 올림
                                    (모든 업체 PC 가 6시간 안에 업데이트됨)
  python tools/release.py rollback  되돌리기: 직전 전체배포로 되돌려서 올림
  python tools/release.py check     검사만 (올리지 않음)
"""
import datetime as dt
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
TESTS = os.path.join(ROOT, "tests")
BETA = os.path.join(ROOT, "release", "beta")
STABLE = os.path.join(ROOT, "release", "stable")
PREV = os.path.join(ROOT, "release", "stable_prev")

# 업체 PC 에 필요한 부품 목록 ("설치이름=불러올이름"). 새 부품을 쓰게 되면 여기에 추가
# → 업데이트 때 그 PC 에 없는 부품만 자동 설치됨
PIP = ["requests", "beautifulsoup4=bs4", "playwright", "keyring", "psutil"]
EXCLUDE = {"config.ini", "state.json", "alive.json"}
EXT = (".py", ".bat", ".txt", ".md", ".example.ini")
SECRET_PATTERNS = [
    (re.compile(r"hooks\.slack\.com/services/T[0-9A-Z]{6,}/B"), "Slack Webhook 주소"),
    (re.compile(r"xox[bpa]-\d{5,}"), "Slack 토큰"),
    (re.compile(r"^[ \t]*password[ \t]*=[ \t]*\S+", re.M), "비밀번호"),
    (re.compile(r"^[ \t]*bot_token[ \t]*=[ \t]*\S+", re.M), "봇 토큰"),
]


def say(msg):
    print(msg, flush=True)


def stop(msg):
    say("\n[중단] " + msg)
    sys.exit(1)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def src_files():
    out = []
    for n in sorted(os.listdir(SRC)):
        p = os.path.join(SRC, n)
        if os.path.isfile(p) and n not in EXCLUDE and not n.startswith(".") and n.endswith(EXT):
            out.append(n)
    return out


def check():
    say("1) 검사 시작")
    if os.path.exists(os.path.join(SRC, "config.ini")):
        stop("src 폴더에 config.ini 가 있습니다. 비밀번호·토큰이 들어 있는 파일이라 지워야 합니다.")
    for n in src_files():
        p = os.path.join(SRC, n)
        text = open(p, encoding="utf-8", errors="replace").read()
        for pat, what in SECRET_PATTERNS:
            if pat.search(text):
                stop(f"{n} 안에 {what}로 보이는 값이 있습니다. 지우고 다시 하세요. (공개 저장소라 누구나 볼 수 있음)")
        if n.endswith(".py"):
            try:
                py_compile.compile(p, doraise=True)
            except py_compile.PyCompileError as e:
                stop(f"{n} 문법 오류:\n{e}")
    say("   - 비밀정보 없음, 문법 이상 없음")
    tf = os.path.join(TESTS, "test_fixture.py")
    if os.path.exists(tf):
        r = subprocess.run([sys.executable, tf], cwd=SRC, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            stop("자동 시험 실패:\n" + (r.stdout + r.stderr)[-1500:])
        say(f"   - 자동 시험 통과 ({r.stdout.count('OK')}개 항목)")
    shutil.rmtree(os.path.join(SRC, "__pycache__"), ignore_errors=True)


def bump():
    p = os.path.join(SRC, "bak_watch.py")
    s = open(p, encoding="utf-8").read()
    m = re.search(r'VERSION = "([^"]+)"', s)
    if not m:
        stop("bak_watch.py 에서 VERSION 을 찾지 못했습니다.")
    today = dt.date.today().strftime("%Y.%m.%d")
    old = m.group(1)
    n = int(old.split(".")[3]) + 1 if old.startswith(today) and old.count(".") == 3 else 1
    new = f"{today}.{n}"
    s = s.replace(m.group(0), f'VERSION = "{new}"', 1)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    return old, new


def build(dest, version):
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    files = []
    for n in src_files():
        shutil.copy2(os.path.join(SRC, n), os.path.join(dest, n))
        files.append({"path": n, "sha256": sha(os.path.join(dest, n))})
    man = {
        "version": version,
        "built": dt.datetime.now().isoformat(timespec="seconds"),
        "files": files,
        "pip": PIP,
        # 예전 버전(10.08~10.10) 프로그램은 아래 두 값만 읽음 → bak_watch.py 를 먼저 받고, 새 버전이 나머지를 받음
        "file": "bak_watch.py",
        "sha256": sha(os.path.join(dest, "bak_watch.py")),
    }
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    return man


def verify(folder):
    mp = os.path.join(folder, "manifest.json")
    if not os.path.exists(mp):
        return None
    man = json.load(open(mp, encoding="utf-8"))
    for f in man["files"]:
        p = os.path.join(folder, f["path"])
        if not os.path.exists(p) or sha(p) != f["sha256"]:
            stop(f"{folder} 의 {f['path']} 가 manifest 와 다릅니다. 시험배포를 다시 하세요.")
    return man


def git(*args, ok_fail=False):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0 and not ok_fail:
        stop("git " + " ".join(args) + " 실패:\n" + (r.stdout + r.stderr)[-1500:])
    return r


def push(msg):
    say("3) GitHub 에 올리는 중...")
    git("add", "-A")
    r = git("commit", "-m", msg, ok_fail=True)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        stop("git commit 실패:\n" + (r.stdout + r.stderr)[-800:])
    git("push")
    say("   - 올림 완료")


def main():
    mode = (sys.argv[1:] or ["check"])[0]
    if mode == "check":
        check()
        say("\n검사 통과. (올리지는 않았습니다)")
    elif mode == "beta":
        check()
        old, new = bump()
        say(f"2) 버전 {old} → {new}, release/beta 만드는 중")
        man = build(BETA, new)
        say(f"   - 파일 {len(man['files'])}개")
        push(f"시험배포 v{new}")
        say(f"\n시험배포 완료 v{new}\n"
            "→ 우리 마지 PC(시험판 주소 사용)가 6시간 안에 업데이트됩니다. 바로 받게 하려면 마지 PC 의 검은 창을 닫고 3_run.bat 다시 실행.\n"
            "→ 하루 써 보고 문제없으면 '전체배포.bat'")
    elif mode == "stable":
        man = verify(BETA)
        if not man:
            stop("시험배포가 아직 없습니다. 먼저 '시험배포.bat' 을 실행하세요.")
        cur = verify(STABLE)
        if cur and cur["version"] == man["version"]:
            stop(f"이미 전체배포된 버전입니다 (v{man['version']}).")
        say(f"1) 시험판 v{man['version']} 확인 완료")
        if cur:
            shutil.rmtree(PREV, ignore_errors=True)
            shutil.copytree(STABLE, PREV)
            say(f"2) 직전 전체배포 v{cur['version']} 를 되돌리기용으로 보관")
        shutil.rmtree(STABLE, ignore_errors=True)
        shutil.copytree(BETA, STABLE)
        push(f"전체배포 v{man['version']}")
        say(f"\n전체배포 완료 v{man['version']} → 모든 업체 PC 가 6시간 안에 업데이트되고 관리 채널에 '업데이트 완료'가 옵니다.")
    elif mode == "rollback":
        prev = verify(PREV)
        if not prev:
            stop("되돌릴 직전 버전이 없습니다.")
        cur = verify(STABLE)
        shutil.rmtree(STABLE, ignore_errors=True)
        shutil.copytree(PREV, STABLE)
        push(f"되돌리기 v{cur['version'] if cur else '?'} → v{prev['version']}")
        say(f"\n되돌리기 완료 → 모든 업체 PC 가 6시간 안에 v{prev['version']} 로 돌아갑니다.\n"
            "※ 우리 마지 PC 는 시험판 주소를 쓰므로, 고친 뒤 다시 '시험배포.bat' 하세요.")
    else:
        stop("알 수 없는 명령: " + mode)


if __name__ == "__main__":
    main()
