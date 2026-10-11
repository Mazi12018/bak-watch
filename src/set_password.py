# -*- coding: utf-8 -*-
"""보안관 비밀번호를 윈도우 '자격 증명 관리자'에 저장합니다. 입력한 비밀번호는 화면에 보이지 않습니다.
저장 후 config.ini 에 비밀번호가 적혀 있으면 지웁니다(지우기 전에 비밀번호를 가린 백업본을 남김)."""
import datetime as dt
import getpass
import os
import re
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import bak_watch as bw  # noqa: E402

try:
    import keyring
except ImportError:
    sys.exit("keyring 부품이 없습니다. 1_install.bat 을 먼저 실행하세요.")

cfg = bw.Config()
print(f"보안관 계정: 대표아이디 [{cfg.company_id}] / 아이디 [{cfg.user_id}]")
print("새 비밀번호를 입력하세요. (입력해도 화면에 표시되지 않습니다)")
p1 = getpass.getpass("비밀번호: ")
p2 = getpass.getpass("한 번 더: ")
if not p1 or p1 != p2:
    sys.exit("두 번 입력한 비밀번호가 다르거나 비어 있습니다. 다시 실행하세요.")
keyring.set_password(bw.KEYRING_SERVICE, cfg.keyring_name(), p1)
if keyring.get_password(bw.KEYRING_SERVICE, cfg.keyring_name()) != p1:
    sys.exit("저장 확인 실패. 다시 실행하세요.")
print("윈도우 자격 증명에 저장했습니다.")

# config.ini 의 password 값 지우기 (백업본에는 비밀번호를 가려서 남김)
cp = os.path.join(BASE, "config.ini")
txt = open(cp, encoding="utf-8-sig").read()
pat = re.compile(r"^(password\s*=)[ \t]*(\S.*)$", re.M)
if pat.search(txt):
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    with open(os.path.join(BASE, f"config.ini.백업_{stamp}"), "w", encoding="utf-8") as f:
        f.write(pat.sub(r"\1 (가림)", txt))
    with open(cp, "w", encoding="utf-8") as f:
        f.write(pat.sub(r"\1", txt))
    print("config.ini 에 적혀 있던 비밀번호를 지웠습니다. (백업: config.ini.백업_" + stamp + ", 비밀번호는 가려짐)")

# 로그인 확인
try:
    bak = bw.Bak(cfg)
    html = bak.fetch_board()
    print("새 비밀번호로 보안관 로그인 확인 성공.")
except Exception as e:  # noqa
    print(f"[로그인 확인 실패] {e}\n보안관에서 비밀번호를 실제로 바꿨는지, 입력이 맞는지 확인하세요.")
print("실행 중인 알림 프로그램은 다음 로그인 때 새 비밀번호를 자동으로 씁니다. 다시 켤 필요 없습니다.")
