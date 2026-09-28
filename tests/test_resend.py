"""발송 실패 → 끝난 것으로 치지 않음 → 재발송 시험. 임시 폴더에서, 가짜 SMTP 로 (실제 메일 없음).

    python tests/test_resend.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
tmp = Path(tempfile.mkdtemp())
os.environ.update(KEYS_ENV=str(tmp / "없음.env"), SEND_MODE="smtp", MAIL_ALLOWLIST="me@test.kr", DEMO_PUBLIC="0")

import hitl as H  # noqa: E402
import mailer  # noqa: E402

H.DB, H.MANUAL = tmp / "cp.sqlite", tmp / "manual.jsonl"
mailer.OUTBOX_DIR, mailer.OUTBOX_LOG, mailer.ROOT = tmp / "outbox", tmp / "outbox.jsonl", tmp
보낸 = []


def 실패(to, subject, body):
    raise RuntimeError("인증 실패 (시험)")


mailer._smtp = 실패
q = next(x for x in H.지난의뢰들(150) if x["id"] == "P-M3-5164")          # 기준에 안 걸려 자동 발송되는 건
r = H.투입({**q, "id": "T1", "수신자": "me@test.kr"})
assert r["결과"].startswith("발송 실패"), r["결과"]
assert "T1" in H.MANUAL.read_text(encoding="utf-8")
print("1) 실패하면:", r["결과"], "· 직접 처리 목록에 올라감")

mailer._smtp = lambda to, subject, body: 보낸.append(to)
rec = H.재발송("T1")
assert rec["상태"] == "발송" and 보낸 == ["me@test.kr"], rec
print("2) 재발송:", rec["모드"], rec["상태"], "→ 결과", H.상태("T1")["결과"])

try:
    H.재발송("T1")
    raise AssertionError("두 번째 재발송이 막혀야 한다")
except ValueError as e:
    print("3) 다시 재발송:", "거절 —", e)
assert 보낸 == ["me@test.kr"]
print("통과")
