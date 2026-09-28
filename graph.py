"""딥리서처 — 기획 → 배치 → 서브에이전트 → 점검 → 종합.

    python graph.py M2                    # questions.json 의 질문 ID 로 한 번 실행
    python graph.py "직접 쓴 질문"          # 아무 질문이나
    python graph.py M2 --set 재위임=false  # 스위치를 바꿔서 (역할·배정·구역·재위임, 절수·절예산 …)

실행 기록은 output/runs.jsonl 에 한 줄씩, 보고서는 output/reports/<run_id>.md 에 남는다.

설계 메모 (수업 코드에서 바꾼 것)
- 설정은 전역 변수가 아니라 State 안에 싣는다. 노드는 s["설정"] 만 본다 — 스위치를 껐는데
  코드 경로가 그대로인 사고를 막고, 한 프로세스에서 설정을 바꿔 가며 돌려도 섞이지 않는다.
- LLM 호출 기록(누가·무엇에·몇 자)을 State 의 calls 에 리듀서로 쌓는다 — 격리를 숫자로 보이는
  재료이고, 조사관 넷이 동시에 써도 안전하다.
- 코디네이터가 절마다 예산을 나눈다. 합계는 절수×절예산을 넘지 못한다(대조군과 같은 총 예산).
- 배정한 시작 문서가 목록에 없으면 아무 문서로 바꾸지 않고 '자율'로 둔 뒤 교정 기록을 남긴다.
- 재위임 원고는 근거가 줄지 않을 때만 채택한다 (원고_정책 = "근거비교").
"""
from __future__ import annotations

import argparse
import contextvars
import json
import operator
import threading
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CORPUS = json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))
DOCS: dict[str, str] = CORPUS["docs"]
LINKS: dict[str, list[str]] = CORPUS["links"]
QUESTIONS = {q["id"]: q for q in json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))["questions"]}
ROSTER: dict[str, str] = CFG["역할_명단"]
OUT = ROOT / "output"

KEYS_ENV = os.getenv("KEYS_ENV", r"C:\Users\lis29\projects\keys.env")   # 키는 저장소 밖에 둔다


# ── 공통 도구 ─────────────────────────────────────────────────────────────────

class Research(TypedDict):
    question: str
    설정:     dict                                # 이번 실행의 설정 (config.json + 덮어쓴 값)
    plan:     dict                                # ① 목차 · 역할 · 시작 문서 · 예산 · 배치 · 바퀴
    sections: Annotated[list, operator.add]       # 서브에이전트가 올린 원고 — 재위임본까지 전부 쌓인다
    visited:  Annotated[list, operator.add]       # (절 번호, 문서) — 누가 무엇을 읽었나
    calls:    Annotated[list, operator.add]       # LLM 호출 기록 — 격리율의 재료
    report:   str                                 # ④ 완성된 보고서
    log:      Annotated[list, operator.add]
    task:     dict                                # Send 로 서브에이전트 한 명에게만 가는 지시
    prior:    dict                                # 재위임일 때 지난 바퀴의 원고


_llms: dict[str, object] = {}
_llm_lock = threading.Lock()
# 웹 데모에서 방문자마다 자기 키를 쓰게 한다 — 키를 os.environ 에 넣으면 프로세스 전체가 공유해서
# 한 방문자의 키로 다른 방문자의 실행이 돈다. contextvar 는 LangGraph 가 병렬 노드 스레드로 넘겨준다.
_세션키: contextvars.ContextVar[str | None] = contextvars.ContextVar("세션키", default=None)


_보이지않는글자 = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿"), None)


def 키정리(key: str | None) -> tuple[str | None, str]:
    """붙여 넣은 키를 다듬는다 — 앞뒤 공백(전각 포함)·따옴표·보이지 않는 글자를 뗀다.
    그래도 영문·숫자·기호가 아닌 글자가 남으면 (None, 이유). 키는 HTTP 헤더('Bearer <키>')에 실리므로
    한 글자라도 비ASCII 면 UnicodeEncodeError 가 난다 — 한/영 키가 한글이던 경우가 흔하다."""
    if not key:
        return None, ""
    k = key.translate(_보이지않는글자).strip().strip("　").strip("\"'“”‘’`").strip()
    bad = [(i, c) for i, c in enumerate(k) if not (c.isascii() and c.isprintable()) or c.isspace()]
    if bad:
        i, c = bad[0]
        보기 = "한글" if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ" else f"'{c}'"
        return None, (f"키의 {i + 1}번째 글자가 {보기}입니다 — 영문·숫자만 있어야 합니다. "
                      "한/영 키가 한글로 되어 있었거나, 복사할 때 따옴표·공백이 딸려 왔을 수 있습니다.")
    if not k.startswith("sk-"):
        return None, "OpenAI 키는 sk- 로 시작합니다."
    return k, ""


def 키쓰기(key: str | None):
    """이 실행(현재 스레드·컨텍스트)에서 쓸 OpenAI 키를 정한다. 파일·기록·로그 어디에도 남기지 않는다."""
    return _세션키.set(키정리(key)[0] if key else None)


def 파일키() -> str | None:
    """이 PC 의 keys.env / .env 에 있는 키 — 명령줄 실행과, 데모에서 '이 PC 의 키'를 고른 경우에만 쓴다."""
    try:
        from dotenv import dotenv_values
        for path in (KEYS_ENV, ROOT / ".env"):
            if Path(path).exists() and (v := dotenv_values(path).get("OPENAI_API_KEY")):
                return v.strip()
    except ImportError:
        pass
    return os.getenv("OPENAI_API_KEY")


def llm():
    """키가 필요한 순간에만 만든다 — 지표만 다시 잴 때는 키 없이 import 되게. 키마다 클라이언트 하나."""
    key = _세션키.get() or 파일키()
    if not key:
        raise RuntimeError(f"OPENAI_API_KEY 가 없습니다 — {KEYS_ENV} 또는 .env 에 넣거나 데모 화면에 입력해 주세요.")
    with _llm_lock:
        if key not in _llms:
            from langchain_openai import ChatOpenAI
            _llms[key] = ChatOpenAI(model=CFG["모델"], temperature=0, timeout=90, max_retries=0, api_key=key)
        return _llms[key]


def 키확인(key: str) -> tuple[bool, str]:
    """키가 살아 있는지 모델 목록 조회로 확인한다(토큰을 쓰지 않는다). (성공 여부, 한 줄 설명)."""
    key, 이유 = 키정리(key)
    if not key:
        return False, 이유 or "키가 비어 있습니다."
    try:
        from openai import OpenAI
        OpenAI(api_key=key, timeout=20).models.retrieve(CFG["모델"])
        return True, f"키 확인 — {CFG['모델']} 을 쓸 수 있습니다."
    except Exception as e:                        # 실패 이유를 화면까지 올린다 (키 문자열은 넣지 않는다)
        name = type(e).__name__
        if "Authentication" in name:
            return False, "키가 올바르지 않습니다(인증 실패)."
        if "NotFound" in name or "PermissionDenied" in name:
            return False, f"이 키로는 {CFG['모델']} 을 쓸 수 없습니다."
        return False, f"확인하지 못했습니다: {name}"


일시적오류 = ("RateLimit", "APIConnection", "Timeout", "InternalServer")


class 잔액소진(RuntimeError):
    """키의 잔액이 떨어졌다 — 기다려도 안 풀린다. 실험은 여기서 멈추고, 키를 바꾼 뒤 다시 실행하면 이어서 돈다."""


def ask(system: str, user: str, 누가: str, 용도: str, cap: int | None = None) -> tuple[str, dict]:
    """LLM 한 번 부르고 (답, 호출 기록) 을 돌려준다. 입력은 cap 자에서 자른다(창을 넘지 않게)."""
    cap = cap or CFG["읽기_입력상한"]
    body = user[:cap]
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": body}]
    t0 = time.time()
    for 시도 in range(7):
        try:
            res = llm().invoke(msgs)
            break
        except Exception as e:
            # 속도 제한과 잔액 소진은 둘 다 429 RateLimitError 로 온다 — 잔액 소진은 기다려도 안 풀리니 바로 멈춘다
            if "insufficient_quota" in str(e):
                raise 잔액소진(f"OpenAI 키 잔액이 소진됐습니다 — 키를 바꾸고 같은 명령을 다시 실행하면 이어서 돕니다. ({e})") from e
            if 시도 == 6 or not any(k in type(e).__name__ for k in 일시적오류):
                raise                             # 설정 오류(키 없음·모델명 등)는 기다려도 안 풀린다 — 바로 드러낸다
            time.sleep(min(2 ** 시도, 30))        # 잠깐 막힌 것만 다시: 1·2·4·8·16·30초
    usage = getattr(res, "usage_metadata", None) or {}
    rec = {"누가": 누가, "용도": 용도, "입력자수": len(body), "지시자수": len(system),
           "잘림": len(user) > cap, "입력토큰": usage.get("input_tokens", 0),
           "출력토큰": usage.get("output_tokens", 0), "초": round(time.time() - t0, 2)}
    if 용도.startswith("기획") or 용도 == "고르기":
        rec["답"] = res.content[:300]            # 나중에 '왜 그렇게 골랐나'를 의심할 수 있게
    return res.content, rec


def jload(raw: str, default):
    """모델이 JSON 앞뒤에 군더더기를 붙여도 {...} / [...] 만 오려 읽는다. 깨지면 default."""
    try:
        m = re.search(r"\[.*\]" if isinstance(default, list) else r"\{.*\}", raw, re.S)
        return json.loads(m.group(0))
    except Exception:
        return default


def 문장들(글: str) -> list[str]:
    """문장 분리 규칙은 여기 하나뿐이다 — metrics.py 도 이 함수를 쓴다(규칙이 갈라지면 지표가 흔들린다)."""
    return [x.strip() for x in re.split(r"(?<=[.!?。])\s+|\n", 글) if len(x.strip()) > 15]


def 인용들(글: str) -> list[str]:
    return re.findall(r"«([^»]+)»", 글)


def 인용형식교정(글: str) -> tuple[str, int]:
    """모델이 «제목» 대신 [제목] 으로 쓴 인용을 바로잡는다. 코퍼스에 있는 제목만, 고친 건수를 돌려준다.
    대조군(baseline.py)에도 똑같이 적용한다 — 한쪽만 고쳐 주면 비교가 기운다."""
    n = 0

    def fix(m):
        nonlocal n
        if m.group(1) in DOCS:
            n += 1
            return f"«{m.group(1)}»"
        return m.group(0)
    return re.sub(r"\[([^\[\]\n]{1,80})\]", fix, 글), n


def 근거문장수(sec: dict) -> int:
    """그 원고에서, 실제로 읽은 문서를 인용한 문장 수."""
    read = set(sec.get("읽은문서", []))
    return sum(1 for 문 in 문장들(sec.get("본문", "")) if any(c in read for c in 인용들(문)))


def 역할문장(t: dict) -> str:
    기본 = CFG["기본_역할"]
    return f"너는 {t['역할']}이다. {ROSTER.get(t['역할'], ROSTER[기본])}."


# ── ① 기획 ────────────────────────────────────────────────────────────────────

def cards() -> str:
    """코디네이터가 보는 것은 문서 전체가 아니라 카드뿐이다 — 제목 + 앞부분 + 문서의 절 제목 몇 개.

    앞부분만으로는 '디지털 트윈'이 설비 정비와, '텔레매틱스'가 차량 관리와 이어진다는 것이 안 보였다
    (둘 다 절 제목에 'Maintenance and service', 'Fleet management' 가 있다). 절 제목은 문서 한 건에
    평균 180자 남짓이라 코디네이터가 보는 양은 조금만 늘어난다."""
    n, k = CFG["카드_글자수"], CFG["카드_절제목수"]
    out = []
    for t, v in DOCS.items():
        heads = re.findall(r"^==+\s*(.+?)\s*==+\s*$", v, re.M)[:k]
        body = re.sub(r"^==+.*?==+\s*$", " ", v, flags=re.M)      # 앞부분에 절 제목 줄이 섞이지 않게
        line = f"- {t}: {re.sub(r'\s+', ' ', body[:n]).strip()}"
        if heads:
            line += f" [목차] {' · '.join(heads)}"
        out.append(line)
    return "\n".join(out)


def _제목맞추기(seed: str) -> str | None:
    """대소문자·공백만 다른 것은 같은 문서로 본다. 그 밖의 '그럴듯한 제목'은 인정하지 않는다."""
    if seed in DOCS:
        return seed
    key = re.sub(r"\s+", " ", seed).strip().casefold()
    return next((t for t in DOCS if t.casefold() == key), None)


def 카드(t: str) -> str:
    return next(l for l in cards().splitlines() if l.startswith(f"- {t}: "))


def 대상뽑기(question: str, 최대: int = 8) -> tuple[list, dict, list]:
    """기획 1단계 — 질문이 묻는 대상(방법·수단·기술·장벽 유형·이론)을 빠짐없이 늘어놓고, 대상마다 그것을
    가장 직접 다루는 카드 1~2건을 붙인다. 목차를 한 번에 짜게 하면 떠오르지 않는 대상을 흘리고
    측면(조건·전제) 절로 빈자리를 메웠다. (대상 목록, 호출 기록, 교정)."""
    raw, rec = ask(
        "너는 리서치 팀의 코디네이터다. 목차를 짜기 전에, 질문에 답하려면 다뤄야 할 **대상**을 모두 늘어놓는다.\n"
        "- 대상은 질문이 묻는 방법·수단·기술·장벽 유형·이론처럼 문서 카드 한두 건이 다루는 것이다. "
        "'조건·전제·장단점·효과·개요' 같은 측면은 대상이 아니다.\n"
        "- 질문에 이름이 나온 것은 반드시 넣는다. 질문이 '방법들·수단들'처럼 여럿을 물으면 카드 앞부분과 "
        "[목차] 에 적힌 절 제목을 보고, 질문이 묻는 쓰임새를 다루는 문서를 빠짐없이 찾는다. "
        "질문이 특정 집단·조직의 사정을 묻으면 그 집단·조직 자체를 다루는 카드도 대상으로 넣는다.\n"
        f"- 대상은 최대 {최대}개, 질문과 직접 관련이 큰 순서로 적는다.\n"
        "- 대상마다 '필수'를 정한다: 그 대상을 빼면 질문의 한 부분에 답할 수 없으면 true. 질문이 문서 한 건으로 "
        "답이 나오면 필수는 그 하나뿐이다.\n"
        "- 카드 제목은 카드에 적힌 그대로(영어 제목은 영어 그대로) 한 글자도 바꾸지 않는다.\n"
        'JSON으로만: {"대상":[{"이름":"한국어 대상 이름","카드":["카드 제목"],"이유":"질문의 어느 부분에 답하나 한 구절","필수":true}]}',
        f"[질문] {question}\n[읽을 수 있는 문서 카드]\n{cards()}", 누가="코디네이터", 용도="기획-대상")
    cands, 교정 = [], []
    for x in (jload(raw, {}).get("대상") or [])[:최대]:
        if not isinstance(x, dict):
            continue
        ok = [c for c in (_제목맞추기(str(t)) for t in (x.get("카드") or [])) if c]
        bad = [str(t) for t in (x.get("카드") or []) if not _제목맞추기(str(t))]
        if bad:
            교정.append({"절": str(x.get("이름")), "원래": ", ".join(bad), "사유": "대상 카드가 목록에 없음 → 뺌"})
        if ok:
            cands.append({"이름": str(x.get("이름") or ok[0]), "카드": list(dict.fromkeys(ok))[:2],
                          "이유": str(x.get("이유") or ""), "필수": x.get("필수") is True})
    return cands, rec, 교정


def 절고르기(cands: list, 절수: int) -> list:
    """어느 대상을 절로 삼을지는 코드가 정한다 — 필수 대상을 관련 순서대로, 최대 절수까지.
    필수가 하나도 없으면 첫 대상 하나. 첫 카드가 같은 대상(같은 문서로 시작하는 두 절)은 뒤의 것을 뺀다."""
    고른, 첫카드 = [], set()
    for c in [c for c in cands if c["필수"]] or cands[:1]:
        if c["카드"][0] in 첫카드:
            continue
        첫카드.add(c["카드"][0])
        고른.append(c)
        if len(고른) >= 절수:
            break
    return 고른


def plan(s: dict) -> dict:
    """기획 — 1단계 대상 뽑기(모델) → 절 고르기(코드) → 2단계 절마다 제목·지시·역할·예산(모델).
    절의 대상과 시작 문서는 코드가 정하므로, 모델이 필수 대상을 빼먹거나 측면 절을 만들 수 없다."""
    st = s["설정"]
    총예산 = st["절수"] * st["절예산"]
    cands, rec1, 교정 = 대상뽑기(s["question"])
    if not cands:
        교정.append({"절": "-", "원래": rec1.get("답", "")[:80], "사유": "대상 뽑기 실패 → 질문 하나를 한 절로"})
    고른 = 절고르기(cands, st["절수"]) if cands else [{"이름": "개요", "카드": [], "이유": "", "필수": True}]
    roster = "\n".join(f"- {k}: {v}" for k, v in ROSTER.items())
    절글 = "\n".join(f"{i}. {c['이름']} — {c['이유']}\n" + "\n".join(f"   {카드(t)}" for t in c["카드"])
                    for i, c in enumerate(고른))
    raw, rec2 = ask(
        "너는 리서치 팀의 코디네이터다. 보고서의 절은 아래처럼 정해져 있다. 절마다 제목·지시·조사관·예산을 정한다.\n"
        "- 절 제목은 그 대상의 이름으로 한다. 질문이 묻는 측면(데이터·조건·전제·효과 등)은 지시 안에 넣는다.\n"
        f"- 절마다 읽을 문서 수(예산)를 1~{st['절예산_최대']} 사이로 정한다. 모든 절의 합은 {총예산} 이하. "
        "근거가 여러 문서에 흩어진 절에 더 준다.\n"
        f"- 절의 성격에 맞는 조사관을 명단에서 고른다.\n[조사관 명단]\n{roster}\n"
        "- 보고서 제목·절 제목·지시는 한국어로 쓴다.\n"
        'JSON으로만: {"제목":"보고서 제목","목차":[{"번호":0,"절":"절 제목","지시":"이 절에서 밝혀야 할 것 한두 문장",'
        '"역할":"명단의 이름 그대로","예산":3}]}',
        f"[질문] {s['question']}\n[정해진 절]\n{절글}", 누가="코디네이터", 용도="기획-목차")
    obj = jload(raw, {})
    답 = {}
    for item in (obj.get("목차") or []):
        try:
            답.setdefault(int(item.get("번호")), item)
        except (TypeError, ValueError):
            pass

    toc, taken = [], set()
    for i, c in enumerate(고른):
        item = 답.get(i, {})
        if not item:
            교정.append({"절": c["이름"], "원래": "", "사유": "2단계 답에 이 절이 없음 → 기본값"})
        절 = str(item.get("절") or c["이름"])
        seed = next((t for t in c["카드"] if t not in taken), None) if st["배정"] else None   # 스위치: 배정
        if seed:
            taken.add(seed)
        role = str(item.get("역할") or "")
        if not st["역할"]:                        # 스위치: 모두 같은 범용 조사관
            role = CFG["기본_역할"]
        elif role not in ROSTER:
            교정.append({"절": 절, "원래": role, "사유": "명단에 없는 역할 → 기본 역할"})
            role = CFG["기본_역할"]
        try:
            budget = int(item.get("예산") or st["절예산"])
        except (TypeError, ValueError):
            budget = st["절예산"]
        toc.append({"절": 절, "대상": c["이름"], "지시": str(item.get("지시") or s["question"]), "역할": role,
                    "시작문서": seed or "", "예산": max(1, min(budget, st["절예산_최대"]))})

    while sum(t["예산"] for t in toc) > 총예산:    # 합계 상한 — 가장 큰 절부터 하나씩 깎는다
        max(toc, key=lambda t: t["예산"])["예산"] -= 1

    p = {"제목": str(obj.get("제목") or s["question"]), "목차": toc, "대상후보": cands,
         "배치": list(range(len(toc))), "바퀴": 1, "교정": 교정}
    seeds = " · ".join(f"{t['역할']}→«{t['시작문서'] or '자율'}»×{t['예산']}" for t in toc)
    note = f" · 교정 {len(교정)}건" if 교정 else ""
    필수 = sum(1 for c in cands if c["필수"])
    return {"plan": p, "calls": [rec1, rec2],
            "log": [f"① 기획   대상 후보 {len(cands)}개(필수 {필수}) → 목차 {len(toc)}절 · {seeds}{note}"]}


# ── ② 배치 ────────────────────────────────────────────────────────────────────

def dispatch(s: dict) -> dict:
    p = s["plan"]
    return {"log": [f"② 배치   {p.get('바퀴', 1)}바퀴 · 서브에이전트 {len(p.get('배치', []))}명 동시 파견"]}


def fanout(s: dict):
    """배치 뒤 갈림길 — 절마다 Send 하나. 남의 구역(다른 절의 시작 문서 + 이미 읽힌 문서)을 알려 준다."""
    idxs = s["plan"].get("배치", [])
    if not idxs:
        return "review"
    toc = s["plan"]["목차"]
    adopted = 채택원고(s["sections"], s["설정"]["원고_정책"])[0]
    sends = []
    for i in idxs:
        남의구역 = {t["시작문서"] for j, t in enumerate(toc) if j != i and t["시작문서"]}
        남의구역 |= {d for j, sec in adopted.items() if j != i for d in sec["읽은문서"]}
        sends.append(Send("researcher", {
            "question": s["question"], "설정": s["설정"],
            "task": {**toc[i], "번호": i, "바퀴": s["plan"].get("바퀴", 1),
                     "피하기": sorted(남의구역) if s["설정"]["구역"] else []},
            "prior": adopted.get(i, {})}))
    return sends


# ── ③ 서브에이전트 ─────────────────────────────────────────────────────────────

def read_one(title: str, brief: str) -> tuple[str, dict]:
    """격리의 경계선 — 원문은 여기서만 읽히고, 밖으로는 간추린 메모만 나간다."""
    return ask("너는 자료 조사 담당이다. 아래 문서를 읽고 지시에 관련된 내용만 한국어로 여섯 문장 이내로 "
               "간추려라. 수치·고유명사는 원문 그대로 옮긴다. 관련 없으면 '관련 없음'이라고만 답하라.\n"
               f"[지시] {brief}",
               f"[문서: {title}]\n{DOCS[title]}", 누가="조사관", 용도="읽기")


def 후보목록(read: list, 피하기: list) -> list:
    """읽은 문서의 링크가 다음 후보다. 링크가 막히면 전체에서, 그것도 막히면 구역을 푼다."""
    쓴것 = set(read) | set(피하기)
    frontier = sorted({x for v in read for x in LINKS.get(v, []) if x in DOCS} - 쓴것)
    return frontier or [x for x in DOCS if x not in 쓴것] or [x for x in DOCS if x not in read]


def 다음문서(t: dict, read: list, 피하기: list) -> tuple[str | None, dict]:
    """다음에 읽을 문서 하나. 예산이 남아 있으면 반드시 고른다 — 대조군도 예산을 끝까지 쓰므로
    같은 조건을 맞춘다. 모델이 후보에 없는 제목을 내면 후보 첫 번째로 바꾸고 기록한다."""
    cand = 후보목록(read, 피하기)
    if not cand:
        return None, [{"누가": "조사관", "용도": "고르기", "입력자수": 0, "지시자수": 0, "잘림": False,
                       "입력토큰": 0, "출력토큰": 0, "초": 0, "답": "(후보 없음)"}]
    return 고르기(역할문장(t) + " 맡은 절을 쓰기 위해", f"[맡은 절] {t['절']}\n[지시] {t['지시']}", read, cand)


def 고르기(누구: str, 맥락: str, read: list, cand: list, 누가: str = "조사관",
          찾을것: bool = False) -> tuple[str, list]:
    """후보 중 하나를 모델이 고른다. 이미 읽었거나 후보에 없는 제목이면 번호 목록으로 한 번 더 묻고,
    그래도 안 되면 후보 첫 번째로 바꾼다(대체 — 경보로 센다). 대조군도 이 함수를 쓴다.
    찾을것=True 면 같은 호출에서 '이 문서에서 찾을 것' 한 문장도 받아 recs[-1]["찾을것"] 에 둔다
    (대조군 (나) — 혼자서도 문서마다 읽기 지시를 좁힐 수 있게. 추가 호출 없음)."""
    덧 = ',"찾을것":"이 문서에서 찾을 내용 한 문장"' if 찾을것 else ""
    recs = []
    # 1차 — 제목으로 고른다
    raw, rec = ask(f"{누구} 다음에 읽을 문서를 후보 중에서 정확히 하나 고른다.\n"
                   'JSON으로만: {"문서":"후보에 있는 제목 그대로"' + 덧 + '}',
                   f"{맥락}\n[이미 읽음] {', '.join(read) or '없음'}\n[후보] {', '.join(cand[:60])}",
                   누가=누가, 용도="고르기")
    recs.append(rec)
    obj = jload(raw, {})
    got = str(obj.get("문서") or "")
    pick = _제목맞추기(got)
    if pick not in cand:                          # "A, B" 처럼 여러 제목을 한 칸에 적으면 후보에 있는 첫 제목으로
        pick = next((p for p in (_제목맞추기(x) for x in re.split(r"\s*[,/|·]\s*", got)) if p in cand), pick)
    if pick and pick in cand:
        if 찾을것:
            rec["찾을것"] = str(obj.get("찾을것") or "")
        return pick, recs
    # 2차 — 번호로 고른다. 모델이 [이미 읽음] 목록의 제목을 베껴 오는 일이 잦아서, 후보만 번호로 보여 준다
    번호판 = "\n".join(f"{k}. {t}" for k, t in enumerate(cand[:60]))
    raw, rec = ask(f"{누구} 다음에 읽을 문서를 아래 번호 목록에서 정확히 하나 고른다. 방금 고른 «{pick or got[:40]}» 는 "
                   "이미 읽었거나 후보가 아니다.\n"
                   'JSON으로만: {"번호":0' + 덧 + '}',
                   f"{맥락}\n[후보]\n{번호판}", 누가=누가, 용도="고르기")
    recs.append(rec)
    obj = jload(raw, {})
    if 찾을것:
        rec["찾을것"] = str(obj.get("찾을것") or "")
    try:
        k = int(obj.get("번호"))
        if 0 <= k < len(cand[:60]):
            return cand[k], recs
    except (TypeError, ValueError):
        pass
    recs[-1]["대체"] = True                       # 두 번 다 못 써서 후보 첫 번째로 — 경보로 센다
    return cand[0], recs


def 탐색(t: dict, prior: dict) -> tuple[list, list, list, list]:
    """예산만큼 새로 읽는다. 배정된 시작 문서가 1순위. (읽은 문서, 메모, 호출 기록, 이번에 새로 읽은 것)."""
    read, notes = list(prior.get("읽은문서", [])), [list(n) for n in prior.get("메모", [])]
    피하기 = t.get("피하기") or []
    recs, 새로 = [], []
    다음 = t.get("시작문서") if t.get("시작문서") in DOCS else None
    for _ in range(t["예산"]):                   # 코드가 정한 상한 — 모델이 어길 수 없다
        if 다음 is None or 다음 in read:
            다음, rs = 다음문서(t, read, 피하기)
            recs += rs
        if 다음 is None:
            break
        memo, rec = read_one(다음, t["지시"])
        recs.append(rec)
        notes.append([다음, memo])
        read.append(다음)
        새로.append(다음)
        다음 = None
    return read, notes, recs, 새로


집필_형식 = ('JSON으로만: {"문장":[{"글":"한국어 문장 하나","근거":["자료 제목"]}, ...],'
            '"충분":true,"부족":""}\n'
            '- "근거" 에는 그 문장의 근거가 된 자료 제목을 자료 목록의 [ ] 안 제목 그대로(영어 제목은 영어 그대로, '
            '번역하지 말고) 적는다. 근거가 없는 문장은 쓰지 않는다.')


def 문장조립(문장: list) -> tuple[str, int]:
    """[{"글","근거"}] → '… 이다 «제목».' 인용을 마침표 앞에 둔다 — 마침표 뒤에 두면 문장 분리기가
    인용을 다음 문장에 붙인다(수업 코드의 숨은 문제). (본문, 근거 칸이 빈 문장 수)."""
    out, 빈근거 = [], 0
    for x in 문장:
        if not isinstance(x, dict):
            continue
        글 = re.sub(r"\s+", " ", str(x.get("글") or "")).strip()
        if not 글:
            continue
        근거 = [str(c).strip() for c in (x.get("근거") or []) if str(c).strip()]
        근거 = [_제목맞추기(c) or c for c in 근거]   # 대소문자만 다른 건 맞추고, 지어낸 제목은 그대로 둔다(→ 허위 인용으로 잡힌다)
        if not 근거:
            빈근거 += 1
        끝 = 글[-1] if 글[-1] in ".!?" else "."
        몸 = 글[:-1].rstrip() if 글[-1] in ".!?" else 글
        out.append(f"{몸} {' '.join(f'«{c}»' for c in dict.fromkeys(근거))}{끝}" if 근거 else f"{몸}{끝}")
    return " ".join(out), 빈근거


def 문장건지기(raw: str) -> list[dict]:
    """JSON 이 조금 깨져서 통째로 못 읽을 때, {"글": "...", "근거": [...]} 조각을 하나씩 건진다.
    팀 집필과 대조군 집필이 같이 쓴다 — 한쪽만 건져 주면 비교가 기운다."""
    out = []
    for 글, 근거 in re.findall(r'\{\s*"글"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"근거"\s*:\s*\[([^\]]*)\]', raw):
        try:
            글 = json.loads(f'"{글}"')
        except ValueError:
            pass
        out.append({"글": 글, "근거": re.findall(r'"((?:[^"\\]|\\.)*)"', 근거)})
    return out


def 집필(t: dict, notes: list) -> tuple[dict, dict]:
    """모은 메모만 근거로 절 원고를 쓰고 충분/부족을 스스로 신고한다(같은 호출 — 추가 비용 0).
    문장별 구조로 받아 코드가 인용 표기를 붙인다 — 자유 서술에 «» 를 맡기면 gpt-4o-mini 가 자주 빠뜨린다."""
    자료 = "\n\n".join(f"[{d}]\n{n}" for d, n in notes) or "(읽은 자료 없음)"
    raw, rec = ask(f"{역할문장(t)} 아래 자료만 근거로 보고서의 한 절을 한국어로 쓴다. "
                   "문장 여섯 개 이상 열두 개 이하, 모두 합쳐 600자 이상. 자료에 없는 내용은 쓰지 마라. "
                   "다 쓴 뒤 스스로 신고한다 — 충분/부족은 문장 수나 글자 수가 아니라 **지시가 묻는 내용을 자료로 "
                   "채웠는가**로만 판단한다. 못 채웠으면 충분을 false 로 하고, 자료에 없어서 비어 있는 내용이 "
                   "무엇인지 한 문장으로 적는다(다음 조사관이 그 빈 칸을 겨냥해 새 문서를 찾는다).\n"
                   + 집필_형식,
                   f"[맡은 절] {t['절']}\n[지시] {t['지시']}\n[모은 자료]\n{자료}", 누가="조사관", 용도="집필")
    obj = jload(raw, {})
    rec["답"] = raw[:6000]                        # 구조가 깨졌을 때 나중에 다시 건질 수 있게 원답을 남긴다
    문장 = obj.get("문장") or []
    if not 문장:
        문장 = 문장건지기(raw)
        if 문장:
            rec["구조실패"] = True                # 건졌어도 형식 실패는 경보로 센다
    text, 빈근거 = 문장조립(문장)
    교정수 = 0
    if len(text) < 100:                          # 그래도 못 건졌으면 원문에서라도 건지고 [제목] 을 «제목» 으로
        text = re.sub(r'^\s*\{.*?"(본문|글)"\s*:\s*"?', "", raw, flags=re.S)[:2500].strip()
        text, 교정수 = 인용형식교정(text)
        rec["구조실패"] = True
    rec["인용형식교정"] = 교정수
    return {"본문": text, "충분": bool(obj.get("충분")), "부족": str(obj.get("부족") or ""),
            "교정": 교정수, "빈근거": 빈근거}, rec


def researcher(s: dict) -> dict:
    """절 하나를 통째로 맡는다 — 탐색하고, 쓰고, 원고만 올린다(원문·메모 원본은 위로 안 간다)."""
    t = s["task"]
    read, notes, recs, 새로 = 탐색(t, s.get("prior") or {})
    원고, rec = 집필(t, notes)
    인용 = 인용들(원고["본문"])
    sec = {"번호": t["번호"], "절": t["절"], "역할": t["역할"], "바퀴": t["바퀴"], "지시": t["지시"],
           "시작문서": t.get("시작문서", ""), "예산": t["예산"], "피하기": t.get("피하기", []),
           "본문": 원고["본문"], "읽은문서": read, "새로읽음": 새로, "메모": notes, "인용": 인용,
           "허위인용": sorted({c for c in 인용 if c not in read}),
           "충분": 원고["충분"], "부족": 원고["부족"], "인용형식교정": 원고["교정"], "근거없는문장": 원고["빈근거"],
           "대체선택": sum(1 for r in recs if r.get("대체"))}
    sec["근거문장수"] = 근거문장수(sec)
    sec["부족_출처"] = "" if sec["충분"] else "자기신고"
    if s["설정"].get("부족_판정") == "자기신고+코드" and sec["충분"]:
        # 실험 1 에서 자기신고가 96절 중 한 번도 '부족'을 말하지 않아 재위임 루프가 열리지 않았다 —
        # 코드가 셀 수 있는 두 가지 빈 칸 신호를 더한다
        새메모 = [m for d, m in notes if d in 새로]
        무관 = sum(1 for m in 새메모 if m.strip().startswith("관련 없음"))
        if 새메모 and 무관 / len(새메모) >= CFG["코드판정_관련없음비율"]:
            sec.update(충분=False, 부족=f"이번에 읽은 {len(새메모)}건 중 {무관}건이 지시와 관련 없었다",
                       부족_출처=f"코드: 관련 없음 {무관}/{len(새메모)}")
        elif sec["근거문장수"] < CFG["코드판정_최소근거문장"]:
            sec.update(충분=False, 부족=f"근거가 붙은 문장이 {sec['근거문장수']}개뿐이다",
                       부족_출처=f"코드: 근거문장 {sec['근거문장수']}")
    mark = "충분" if sec["충분"] else f"부족({sec['부족_출처']}: {sec['부족'][:20]})"
    return {"sections": [sec], "calls": recs + [rec],
            "visited": [[t["번호"], d] for d in 새로],
            "log": [f"   ③ {t['역할']} «{t['절'][:16]}» 새로 {len(새로)}/{t['예산']}건 (누적 {len(read)}) · "
                    f"{len(원고['본문'])}자 · 인용 {len(인용)}곳 · 근거문장 {sec['근거문장수']} · {mark}"]}


# ── 두 번째 원고를 어떻게 할까 ──────────────────────────────────────────────────

def 채택원고(sections: list, 정책: str) -> tuple[dict, list]:
    """절 번호별로 채택된 원고 하나와, 재위임 원고를 받았는지/버렸는지 판정 기록을 돌려준다.

    "근거비교": 새 원고는 ① 허위 인용이 없고 ② 읽은 문서를 인용한 문장 수가 이전 원고 이상일 때만
               채택한다. 무조건 덮어쓰면 인용을 빠뜨린 재작성본이 멀쩡한 원고를 지운다.
    "덮어쓰기": 수업 코드와 같다 — 늘 마지막 원고.
    """
    adopted, 판정 = {}, []
    for sec in sorted(sections, key=lambda x: (x["번호"], x.get("바퀴", 1))):
        i = sec["번호"]
        old = adopted.get(i)
        if old is None or 정책 == "덮어쓰기":
            adopted[i] = sec
            if old is not None:
                판정.append({"번호": i, "바퀴": sec["바퀴"], "결과": "채택(덮어쓰기)"})
            continue
        ok = not sec["허위인용"] and sec["근거문장수"] >= old["근거문장수"]
        판정.append({"번호": i, "바퀴": sec["바퀴"], "결과": "채택" if ok else "기각",
                     "근거문장": f"{old['근거문장수']}→{sec['근거문장수']}", "허위인용": sec["허위인용"]})
        if ok:
            adopted[i] = sec
    return adopted, 판정


# ── ③ 점검 ────────────────────────────────────────────────────────────────────

def review(s: dict) -> dict:
    """LLM 을 부르지 않는다 — 조사관이 스스로 적은 충분/부족만 모아 누구를 다시 보낼지 정한다."""
    st, p = s["설정"], s["plan"]
    toc, wheel = p["목차"], p.get("바퀴", 1)
    adopted, 판정 = 채택원고(s["sections"], st["원고_정책"])
    이번판정 = [x for x in 판정 if x["바퀴"] == wheel]
    판정글 = "".join(f" · {toc[x['번호']]['절'][:10]} 재위임본 {x['결과']}({x.get('근거문장', '')})" for x in 이번판정)

    if not st["재위임"] or wheel >= st["바퀴_상한"]:    # 예산 쪽 상한을 먼저 본다 — 무한 루프 차단
        gaps = []
    else:
        gaps = [i for i in range(len(toc)) if not adopted.get(i, {}).get("충분", False)]

    if not gaps:
        why = "재위임 끔" if not st["재위임"] else ("바퀴 소진" if wheel >= st["바퀴_상한"] else "빈 칸 없음")
        남은부족 = sum(1 for i in range(len(toc)) if not adopted.get(i, {}).get("충분", False))
        return {"plan": {**p, "배치": [], "종료": why},
                "log": [f"④ 점검   {len(toc)}절 중 부족 {남은부족}개 — 종합으로 ({why}){판정글}"]}

    newtoc = [dict(t) for t in toc]
    for i in gaps:
        prev = adopted.get(i, {})
        newtoc[i]["지시"] = (f"{toc[i]['지시']} (재위임: 지난번에 «{'», «'.join(prev.get('읽은문서', [])) or '없음'}» 를 "
                             f"읽었지만 '{prev.get('부족') or '근거가 모자랐다'}'. 그 빈 칸을 겨냥해 아직 안 본 문서를 찾아라)")
    return {"plan": {**p, "목차": newtoc, "배치": gaps, "바퀴": wheel + 1},
            "log": [f"④ 점검   {len(toc)}절 중 부족 {len(gaps)}개 — "
                    f"«{'», «'.join(toc[i]['절'][:12] for i in gaps)}» 재위임{판정글}"]}


def route(s: dict) -> str:
    return "more" if s["plan"].get("배치") else "done"


# ── ④ 종합 ────────────────────────────────────────────────────────────────────

def synthesize(s: dict) -> dict:
    """편집자는 절 본문을 고치지 않는다 — 제목과 첫머리만 보고 머리말·맺음말을 쓴다(인용을 지킨다)."""
    adopted, _ = 채택원고(s["sections"], s["설정"]["원고_정책"])
    order = [adopted[i] for i in sorted(adopted)]
    n = CFG["종합_첫머리_글자수"]
    outline = "\n".join(f"{k+1}. {x['절']}: {x['본문'][:n]}…" for k, x in enumerate(order))
    raw, rec = ask("너는 보고서를 마무리하는 편집자다. 아래는 조사관들이 각자 쓴 절의 제목과 첫머리다. "
                   "본문은 고치지 말고, 전체를 여는 머리말과 닫는 맺음말만 한국어로 써라. 각각 세 문장 이내.\n"
                   'JSON으로만: {"머리말":"...","맺음말":"..."}',
                   f"[질문] {s['question']}\n[보고서 제목] {s['plan']['제목']}\n[절 목록]\n{outline}",
                   누가="편집자", 용도="종합")
    obj = jload(raw, {})
    parts = [f"# {s['plan']['제목']}", str(obj.get("머리말") or "")]
    for k, x in enumerate(order):
        parts.append(f"## {k+1}. {x['절']}  _({x['역할']})_\n\n{x['본문']}")
    parts.append(f"## 맺음말\n\n{obj.get('맺음말') or ''}")
    report = "\n\n".join(p for p in parts if p.strip())
    return {"report": report, "calls": [rec],
            "log": [f"⑤ 종합   {len(order)}절 이어붙임 → 보고서 {len(report):,}자 (편집자는 절 첫머리 {n}자씩만 봤다)"]}


# ── 그래프 ────────────────────────────────────────────────────────────────────

def build():
    g = StateGraph(Research)
    for name, fn in (("plan", plan), ("dispatch", dispatch), ("researcher", researcher),
                     ("review", review), ("synthesize", synthesize)):
        g.add_node(name, fn)
    g.add_edge(START, "plan")
    g.add_edge("plan", "dispatch")
    g.add_conditional_edges("dispatch", fanout, ["researcher", "review"])          # ②⇉③ 팬아웃
    g.add_edge("researcher", "review")
    g.add_conditional_edges("review", route, {"more": "dispatch", "done": "synthesize"})   # 재위임 루프
    g.add_edge("synthesize", END)
    return g.compile()


def 설정만들기(덮어쓸: dict | None = None) -> dict:
    st = {k: CFG[k] for k in ("절수", "절예산", "절예산_최대", "바퀴_상한", "원고_정책", "부족_판정")} | dict(CFG["스위치"])
    for k, v in (덮어쓸 or {}).items():
        if k not in st:
            raise KeyError(f"모르는 설정: {k} (가능: {', '.join(st)})")
        st[k] = v
    return st


def 격리(calls: list) -> dict:
    """코디네이터·편집자가 본 글자 수와 조사관이 본 글자 수를 따로 센다."""
    위 = sum(c["입력자수"] for c in calls if c["누가"] in ("코디네이터", "편집자"))
    아래 = sum(c["입력자수"] for c in calls if c["누가"] == "조사관")
    return {"코디네이터가_본_글자": 위, "조사관이_본_글자": 아래,
            "격리율": round(100 * 위 / max(위 + 아래, 1), 1), "LLM호출": len(calls),
            "입력토큰": sum(c["입력토큰"] for c in calls), "출력토큰": sum(c["출력토큰"] for c in calls),
            "고르기_대체": sum(1 for c in calls if c.get("대체")),
            "인용형식교정": sum(c.get("인용형식교정", 0) for c in calls),
            "집필_구조실패": sum(1 for c in calls if c.get("구조실패"))}


def run(question: str, 덮어쓸: dict | None = None, qid: str | None = None, 라벨: str = "기본",
        save: bool = True, verbose: bool = True, on_log=None) -> dict:
    """on_log 를 주면 노드가 끝날 때마다 새 로그 줄을 넘겨준다(웹 데모의 실시간 진행 표시)."""
    st = 설정만들기(덮어쓸)
    t0 = time.time()
    init = {"question": question, "설정": st, "plan": {}, "sections": [], "visited": [],
            "calls": [], "report": "", "log": [], "task": {}, "prior": {}}
    if on_log is None:
        final = build().invoke(init, {"recursion_limit": 50})
    else:
        final, 본것 = init, 0
        for state in build().stream(init, {"recursion_limit": 50}, stream_mode="values"):
            final = state
            for line in state["log"][본것:]:
                on_log(line)
            본것 = len(state["log"])
    adopted, 판정 = 채택원고(final["sections"], st["원고_정책"])
    rec = {"run_id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
           "시각": datetime.now().isoformat(timespec="seconds"), "qid": qid, "질문": question, "라벨": 라벨,
           "설정": st, "초": round(time.time() - t0, 1), "plan": final["plan"],
           "sections": final["sections"], "채택": {str(k): v["바퀴"] for k, v in adopted.items()},
           "원고판정": 판정, "visited": final["visited"], "calls": final["calls"],
           "격리": 격리(final["calls"]), "report": final["report"], "log": final["log"]}
    from metrics import 재기                      # 지표는 metrics.py 한 곳에서만 계산한다
    rec["metrics"] = 재기(rec)
    if verbose:
        for line in final["log"]:
            print(line)
        g = rec["격리"]
        print(f"   · 격리: 코디네이터·편집자 {g['코디네이터가_본_글자']:,}자 / 조사관 {g['조사관이_본_글자']:,}자 "
              f"→ 격리율 {g['격리율']}% · LLM {g['LLM호출']}회 · 토큰 {g['입력토큰']:,}+{g['출력토큰']:,} · {rec['초']}초\n"
              f"   · 고르기 대체 {g['고르기_대체']}회 · 집필 구조 실패 {g['집필_구조실패']}회 · 인용 형식 교정 {g['인용형식교정']}곳")
        m = rec["metrics"]
        print("⑥ 측정   " + " · ".join(f"{k} {v}%" for k, v in m["신호"].items() if v is not None)
              + " · 경보 " + (", ".join(f"{k} {v}" for k, v in m["경보"].items() if v) or "없음"))
    if save:
        (OUT / "reports").mkdir(parents=True, exist_ok=True)
        with open(OUT / "runs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        (OUT / "reports" / f"{rec['run_id']}.md").write_text(final["report"], encoding="utf-8")
        if verbose:
            print(f"   · 저장: output/runs.jsonl · output/reports/{rec['run_id']}.md")
    return rec


def _값(v: str):
    return {"true": True, "false": False}.get(v.lower(), int(v) if v.isdigit() else v)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="딥리서처 한 번 실행")
    ap.add_argument("q", help="questions.json 의 ID(예: M2) 또는 질문 문장")
    ap.add_argument("--set", nargs="*", default=[], help="설정 덮어쓰기, 예: 재위임=false 절예산=2")
    ap.add_argument("--label", default="기본")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()
    q = QUESTIONS.get(a.q, {"id": None, "질문": a.q})
    over = dict(kv.split("=", 1) for kv in a.set)
    print(f"[{q['id'] or '직접'}] {q['질문']}")
    run(q["질문"], {k: _값(v) for k, v in over.items()}, qid=q["id"], 라벨=a.label, save=not a.no_save)
