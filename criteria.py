"""승인 기준 — 보고서 기록 하나 → 멈춘 이유 목록 (비어 있으면 자동 발송).

후보 7개(C1~C7)를 모두 계산할 수 있게 두고, 파이프라인은 그중 `켜진기준` 만 쓴다.
각 후보 함수는 걸린 증거(문장·문서·값) 목록을 돌려준다 — 비어 있으면 안 걸림.
받는 기록은 딥리서치 실행 기록 그대로이거나 hitl.간추리기 결과 — 둘 다 질문 · report · metrics · sections · 채택 을 가진다.
"""
from __future__ import annotations

import re

from graph import CORPUS, DOCS, 문장들
from metrics import 채택된절

CITE = re.compile(r"«([^»]+)»")
LANG: dict[str, str] = CORPUS["lang"]

# ── 숫자 읽기: 단위를 맞춰 값으로 바꾼다 (5000만 유로 = 50 million euro) ──────────────────
단위 = {"조": 1e12, "억": 1e8, "만": 1e4, "천": 1e3, "백": 1e2,
       "trillion": 1e12, "billion": 1e9, "million": 1e6, "thousand": 1e3}
# 5천만 = 5 × 천 × 만 처럼 한국어 단위는 두 개까지 겹친다
NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:(천|백)?(조|억|만)|(천)|(trillion|billion|million|thousand))?", re.I)
# 숫자처럼 보이지만 수치가 아닌 이름 — 뺀다
이름숫자 = re.compile(r"(?:Industry|인더스트리)\s*\d\.0|\d+\s*차\s*산업", re.I)


def 숫자들(글: str) -> list[tuple[str, float]]:
    """(보이는 글자, 값). 단위 없는 한 자리 정수(1절, 3가지 …)는 흔해서 뺀다."""
    out = []
    for m in NUM.finditer(이름숫자.sub(" ", 글)):
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        units = [u.lower() for u in m.groups()[1:] if u]
        if not units and "." not in raw and len(raw) < 2:
            continue
        for u in units:
            v *= 단위[u]
        out.append((m.group(0).strip(), v))
    return out


def _같은값(v: float, 후보: set[float]) -> bool:
    return any(abs(v - w) <= 1e-9 + 0.005 * abs(w) for w in 후보)


_원문값: dict[str, set[float]] = {}


def 원문값(title: str) -> set[float]:
    if title not in _원문값:
        _원문값[title] = {v for _, v in 숫자들(DOCS.get(title, ""))}
    return _원문값[title]


def _본문(report: str) -> list[str]:
    return [s for s in 문장들(report) if not s.lstrip().startswith("#")]


# ── 후보 7개 ────────────────────────────────────────────────────────────────
def C1_출처없는수치(rec: dict) -> list[str]:
    """숫자가 든 문장에 인용 표시가 하나도 없다."""
    return [s for s in _본문(rec["report"]) if 숫자들(CITE.sub("", s)) and not CITE.search(s)]


def C2_원문에없는숫자(rec: dict) -> list[dict]:
    """인용은 달았는데, 인용한 문서들 원문 어디에도 그 값이 없다 (단위를 맞춰 비교)."""
    out = []
    for s in _본문(rec["report"]):
        cites = CITE.findall(s)
        nums = 숫자들(CITE.sub("", s))
        if not cites or not nums:
            continue
        pool = set().union(*(원문값(c) for c in cites))
        if miss := [raw for raw, v in nums if not _같은값(v, pool)]:
            out.append({"문장": s, "없는숫자": miss, "인용": cites})
    return out


def C3_지표경보(rec: dict) -> list[str]:
    """딥리서처가 이미 재던 경보 7종 (지어낸 제목 인용 · 안 읽은 문서 인용 · 절 밖 인용 …)."""
    return [f"{k} {v}" for k, v in rec["metrics"]["경보"].items() if v]


def C4_근거율미달(rec: dict, 기준: float = 70.0) -> list[str]:
    g = rec["metrics"]["신호"].get("근거율")
    return [f"근거율 {g}% < {기준:g}%"] if g is not None and g < 기준 else []


def C5_부족한절남음(rec: dict) -> list[str]:
    """재위임까지 끝났는데 최종 채택본이 여전히 '부족'. 절이 없는 기록(대조군)은 해당 없음."""
    return [f"「{s['절']}」 — {s.get('부족') or '부족'}" for s in 채택된절(rec) if not s.get("충분", True)]


국내어 = re.compile(r"한국|대한민국|국내|우리나라")


해외어 = re.compile(r"EU|유럽|미국|영국|일본|중국|독일|호주|칠레|인도|캐나다|세계|국제|해외|다른 나라|국가마다|각국")


def C6_국내질문_해외근거(rec: dict) -> list[str]:
    """한국 사정을 묻는 질문에서, 숫자가 든 문장이 해외(비한국어) 문서만 인용하면서 다른 나라 이름도 없다
    → 해외 수치가 한국 이야기처럼 읽힌다.

    처음엔 '인용 문서 중 한국어 문서 비율 < 50%' 로 재었는데, S3 27건이 모두 한·영 1건씩(정확히 50%)이라
    한 번도 걸리지 않았다 — 문서 비율이 아니라 문장 단위로 바꿨다 (docs/evidence/step3_scan.md).
    """
    if not 국내어.search(rec["질문"]):
        return []
    out = []
    for s in _본문(rec["report"]):
        cites = CITE.findall(s)
        if (cites and 숫자들(CITE.sub("", s)) and all(LANG.get(c) != "ko" for c in cites)
                and not 해외어.search(CITE.sub("", s))):
            out.append(s)
    return out


고위험어 = ["법률", "법적", "법령", "과세", "세금", "세액", "세제", "보조금", "지원금", "융자", "대출",
         "인증", "개인정보", "과태료", "벌금", "처벌", "계약", "투자"]


def C7_고위험주제(rec: dict) -> list[str]:
    """틀리면 고객이 돈이나 법적 책임을 지는 주제어가 질문이나 보고서에 있다."""
    글 = rec["질문"] + "\n" + rec["report"]
    return [w for w in 고위험어 if w in 글]


# C8 문턱. 4단계에서 4 로 정했다가, 6단계에서 절 이름표 줄을 검수에서 빼고 다시 재니 k=4 의 놓침이 2→4/25 로 늘어
# 결정 A(놓침을 막는다)의 취지대로 3 으로 낮췄다 (docs/evidence/step4_validation.md 4절).
C8_문턱 = 3


def C8_AI검수(rec: dict, k: int = C8_문턱) -> list[str]:
    """AI 검수(judge.검수 — 문장마다 인용 원문 대목과 대조, gpt-4o)에서 근거없음·모순·대상착오 문장이 k개 이상.
    검수 결과가 없는 기록(검수 전)은 계산하지 않는다. 검수에 실패한 문장이 있으면 확인할 수 없으니 멈춘다."""
    g = rec.get("검수")
    if not g:
        return []
    bad = [f"[{s['번호']}] {s['판정']} — {s['이유']}" for s in g["문장"] if s["판정"] in ("근거없음", "모순", "대상착오")]
    fail = [f"AI 검수 실패 문장 {g['판정실패']}개 — 확인할 수 없어 사람에게"] if g["판정실패"] else []
    return fail + bad if (fail or len(bad) >= k) else []


후보 = {
    "C1": ("출처 없는 수치", C1_출처없는수치),
    "C2": ("인용 원문에 없는 숫자", C2_원문에없는숫자),
    "C3": ("지표 경보", C3_지표경보),
    "C4": ("근거율 70% 미만", C4_근거율미달),
    "C5": ("부족한 절 남음", C5_부족한절남음),
    "C6": ("국내 질문에 해외 근거", C6_국내질문_해외근거),
    "C7": ("고위험 주제어", C7_고위험주제),
    "C8": (f"AI 검수 오류 문장 {C8_문턱}개 이상", C8_AI검수),
}

# 4단계에서 확정 (docs/evidence/step4_validation.md). 놓침을 막는 쪽(A)을 골랐다 —
# C2·C3·C6 은 걸리면 거의 확실히 진짜 문제(헛멈춤 ≈ 0)라 싸고 빠른 1차 관문, C8 은 문장을 원문과 대조해 읽어야만 잡히는 오류.
# C1(표본에서 0건) · C4 · C5 · C7(헛멈춤이 크거나 C8 과 겹친다) 은 끈다.
켜진기준 = ["C2", "C3", "C6", "C8"]


def 전부재기(rec: dict) -> dict[str, list]:
    """C1~C8. C8 은 rec 에 '검수' 가 있을 때만 값이 있다 (4단계 검증은 validate_criteria 가 따로 붙인다)."""
    return {k: fn(rec) for k, (_, fn) in 후보.items()}


def 판정(rec: dict, 켜진: list[str] | None = None) -> list[str]:
    이유 = []
    for k in 켜진 or 켜진기준:
        이름, fn = 후보[k]
        if not (hits := fn(rec)):
            continue
        if k == "C8":
            g = rec["검수"]
            이유.append(f"C8 AI 검수 — 오류 문장 {g['오류수']}개" + (f" · 검수 실패 {g['판정실패']}개" if g["판정실패"] else ""))
        else:
            이유.append(f"{k} {이름} — {len(hits)}건")
    return 이유
