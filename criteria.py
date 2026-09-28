"""승인 기준 — 보고서 기록 하나 → 멈춘 이유 목록 (비어 있으면 자동 발송).

2단계 현재는 임시 기준 3개. 3~4단계에서 후보를 늘리고 검증한 뒤 최종 기준으로 바꾼다.
"""
from __future__ import annotations

import re

from graph import DOCS, 문장들

NUM = re.compile(r"\d[\d,.]*\s*%?")
CITE = re.compile(r"«([^»]+)»")


def _수치들(문장: str) -> list[str]:
    """인용 표시를 뺀 문장에서 두 자리 이상 숫자. 한 자리(1절, 3가지 등)는 흔해서 뺀다."""
    out = []
    for n in NUM.findall(CITE.sub("", 문장)):
        n = n.strip().rstrip("%").strip().rstrip(".,")
        if len(n.replace(",", "").replace(".", "")) >= 2:
            out.append(n)
    return out


def 수치점검(report: str) -> dict:
    """숫자가 든 문장을 두 가지로 가른다: 출처 표시가 없는 것 · 인용한 문서 원문에 그 숫자가 없는 것."""
    무출처, 원문없음 = [], []
    for s in 문장들(report):
        if s.lstrip().startswith("#") or not (nums := _수치들(s)):
            continue
        cites = CITE.findall(s)
        if not cites:
            무출처.append(s)
            continue
        src = " ".join(DOCS.get(c, "") for c in cites)
        if miss := [n for n in nums if n not in src]:
            원문없음.append({"문장": s, "없는숫자": miss, "인용": cites})
    return {"무출처": 무출처, "원문없음": 원문없음}


def 판정(rec: dict) -> list[str]:
    """임시 기준: ① 지표 경보 ② 출처 없는 수치 ③ 인용 원문에 없는 숫자."""
    이유 = []
    if 울림 := {k: v for k, v in rec["metrics"]["경보"].items() if v}:
        이유.append("지표 경보 — " + ", ".join(f"{k} {v}" for k, v in 울림.items()))
    c = 수치점검(rec["report"])
    if c["무출처"]:
        이유.append(f"출처 표시 없는 수치 문장 {len(c['무출처'])}개")
    if c["원문없음"]:
        이유.append(f"인용한 문서 원문에 없는 숫자가 든 문장 {len(c['원문없음'])}개")
    return 이유
