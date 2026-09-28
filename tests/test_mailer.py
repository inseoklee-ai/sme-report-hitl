"""발송 안전장치 시험 — 실제 메일은 절대 보내지 않는다 (_smtp 를 가짜로 바꿔 호출만 센다).

    python -m pytest tests -q        또는        python tests/test_mailer.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mailer  # noqa: E402

보낸것: list[str] = []


def _setup(**env):
    tmp = Path(tempfile.mkdtemp())
    mailer.OUTBOX_DIR, mailer.OUTBOX_LOG = tmp / "outbox", tmp / "outbox.jsonl"
    mailer.ROOT = tmp
    mailer.KEYS_ENV = str(tmp / "없음.env")         # 이 PC 의 keys.env(실제 SMTP 설정)를 읽지 않는다
    mailer._smtp = lambda to, subject, body: 보낸것.append(to)
    보낸것.clear()
    for k in ("SEND_MODE", "DEMO_PUBLIC", "MAIL_ALLOWLIST"):
        os.environ.pop(k, None)
    os.environ.update({"DEMO_PUBLIC": "0", **env})


def _send(tid="T1", to="me@test.kr", **kw):
    return mailer.발송(tid, "가상고객", to, "제목", "본문", **kw)


def test_기본은_파일():
    _setup()
    r = _send()
    assert r["모드"] == "file" and r["상태"] == "발송" and 보낸것 == []
    assert (mailer.OUTBOX_DIR / "T1.md").exists()


def test_smtp_허용목록_안이면_발송():
    _setup(SEND_MODE="smtp", MAIL_ALLOWLIST="me@test.kr")
    r = _send(to="ME@test.kr")
    assert r["모드"] == "smtp" and r["상태"] == "발송" and 보낸것 == ["ME@test.kr"]


def test_smtp_허용목록_밖이면_차단():
    _setup(SEND_MODE="smtp", MAIL_ALLOWLIST="me@test.kr")
    r = _send(to="customer@example.com")
    assert r["상태"] == "차단" and 보낸것 == []


def test_같은_thread는_한번만():
    _setup(SEND_MODE="smtp", MAIL_ALLOWLIST="me@test.kr")
    _send()
    r = _send()
    assert r.get("중복") and 보낸것 == ["me@test.kr"]
    assert sum(1 for x in mailer.발송기록() if x["thread_id"] == "T1") == 1


def test_차단된_건은_중복으로_치지_않음():
    _setup(SEND_MODE="smtp", MAIL_ALLOWLIST="me@test.kr")
    _send(to="x@example.com")
    r = _send(to="me@test.kr")
    assert r["상태"] == "발송" and 보낸것 == ["me@test.kr"]


def test_시험실행은_파일로_고정():
    _setup(SEND_MODE="smtp", MAIL_ALLOWLIST="me@test.kr")
    r = _send(force_file=True)
    assert r["모드"] == "file" and 보낸것 == []


def test_공개모드는_실제발송_끔():
    _setup(SEND_MODE="smtp", MAIL_ALLOWLIST="me@test.kr", DEMO_PUBLIC="1")
    r = _send()
    assert r["모드"] == "file" and 보낸것 == []


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"통과 {name}")
