"""발송 — 기본은 파일(output/outbox/), SEND_MODE=smtp 일 때만 실제 메일.

안전장치
- 수신자는 keys.env 의 MAIL_ALLOWLIST(쉼표로 구분) 에 있는 주소만. 없으면 막고 기록만 남긴다
- 같은 thread_id 는 한 번만 발송 (outbox.jsonl 에 이미 있으면 건너뜀)
- 공개 모드(DEMO_PUBLIC=1) 또는 force_file=True(기준 검증·시험) 이면 SEND_MODE 와 상관없이 파일 모드
- SMTP 값(SMTP_HOST · SMTP_PORT · SMTP_USER · SMTP_APP_PASSWORD) 은 keys.env 에서 읽기만 하고 어디에도 찍지 않는다
"""
from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from graph import KEYS_ENV, OUT, ROOT

OUTBOX_DIR = OUT / "outbox"
OUTBOX_LOG = OUT / "outbox.jsonl"


def _env() -> dict:
    """keys.env · .env · 환경 변수 순으로 합친다 (뒤가 앞을 덮는다)."""
    vals: dict = {}
    try:
        from dotenv import dotenv_values
        for path in (KEYS_ENV, ROOT / ".env"):
            if Path(path).exists():
                vals.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    except ImportError:
        pass
    for k in ("SEND_MODE", "DEMO_PUBLIC", "MAIL_ALLOWLIST", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_APP_PASSWORD"):
        if os.getenv(k) is not None:
            vals[k] = os.getenv(k)
    return vals


def 허용목록() -> list[str]:
    return [a.strip().lower() for a in _env().get("MAIL_ALLOWLIST", "").split(",") if a.strip()]


def 모드(force_file: bool = False) -> str:
    """실제로 쓸 발송 모드와 그 이유. 'file' 또는 'smtp'."""
    e = _env()
    if force_file or e.get("DEMO_PUBLIC") == "1":
        return "file"
    return "smtp" if e.get("SEND_MODE", "file").strip().lower() == "smtp" else "file"


def 발송기록() -> list[dict]:
    if not OUTBOX_LOG.exists():
        return []
    return [json.loads(l) for l in OUTBOX_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]


def 이미발송(thread_id: str) -> dict | None:
    return next((r for r in 발송기록() if r["thread_id"] == thread_id and r["상태"] == "발송"), None)


def _기록(rec: dict) -> dict:
    OUT.mkdir(exist_ok=True)
    with OUTBOX_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _smtp(to: str, subject: str, body: str) -> None:
    e = _env()
    need = ("SMTP_HOST", "SMTP_USER", "SMTP_APP_PASSWORD")
    if missing := [k for k in need if not e.get(k)]:
        raise RuntimeError(f"keys.env 에 {', '.join(missing)} 가 없습니다")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = e["SMTP_USER"], to, subject
    msg.set_content(body)
    port = int(e.get("SMTP_PORT") or 465)
    if port == 465:
        with smtplib.SMTP_SSL(e["SMTP_HOST"], port, timeout=30) as s:
            s.login(e["SMTP_USER"], e["SMTP_APP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(e["SMTP_HOST"], port, timeout=30) as s:
            s.starttls()
            s.login(e["SMTP_USER"], e["SMTP_APP_PASSWORD"])
            s.send_message(msg)


def 발송(thread_id: str, 고객: str, 수신자: str, 제목: str, 본문: str, *, force_file: bool = False) -> dict:
    """보고서 한 건을 내보낸다. 결과 기록(dict)을 돌려주고 outbox.jsonl 에도 남긴다."""
    if prev := 이미발송(thread_id):
        return {**prev, "중복": True}                  # 두 번째 호출은 아무것도 보내지 않는다
    mode = 모드(force_file)
    base = {"thread_id": thread_id, "고객": 고객, "수신자": 수신자, "제목": 제목, "자수": len(본문),
            "모드": mode, "시각": datetime.now().isoformat(timespec="seconds")}

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)       # 파일은 모드와 상관없이 남긴다 — 무엇을 보냈는지의 증거
    path = OUTBOX_DIR / f"{thread_id}.md"
    path.write_text(f"받는 사람: {고객} <{수신자}>\n제목: {제목}\n\n{본문}\n", encoding="utf-8")
    base["파일"] = str(path.relative_to(ROOT))

    if mode == "smtp":
        if 수신자.strip().lower() not in 허용목록():
            return _기록({**base, "상태": "차단", "사유": "수신자가 허용 목록에 없음"})
        try:
            _smtp(수신자, 제목, 본문)
        except Exception as ex:                          # 비밀값이 섞이지 않게 예외 종류와 짧은 설명만
            return _기록({**base, "상태": "실패", "사유": f"{type(ex).__name__}: {str(ex)[:120]}"})
    return _기록({**base, "상태": "발송"})
