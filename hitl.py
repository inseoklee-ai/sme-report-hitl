"""사람이 승인하는 보고서 발송 파이프라인.

    python hitl.py past [--n 20]              # 지난 기록(4단계 표본 150건 중 n건)을 의뢰로 투입 — 검수가 캐시돼 키 없음
    python hitl.py new M2 --고객 "가상고객 B"   # 새 의뢰 — 딥리서치를 실제로 돌린다(키 필요)
    python hitl.py list                       # 대기 건
    python hitl.py answer <의뢰ID> approve
    python hitl.py answer <의뢰ID> edit --방식 오류문장빼기        # 또는 --수정본 파일.md
    python hitl.py answer <의뢰ID> reject --사유 "수치 출처 확인 필요"
    python hitl.py answer <의뢰ID> redo --지시 "한국 기준은 중소기업 문서로 다시"
    python hitl.py sweep [--now 2026-10-02T09:00]   # 3일 규칙: 72시간 넘은 대기 건 → 상급자 이관 + 지연 안내

접수 → 조사 → 검수 → 판정 ─(안 걸림)──────────────────────────────→ 발송 → END
                ↑          └(걸림)→ 알림 → 검토[interrupt] ─approve───────→ 발송 → END
                │                                        ├edit──→ 수정반영 → 발송 → END
                │                                        ├reject(사유)──→ 반려기록 → END
                └─────────────────────── redo(지시, 2회 상한) ─┘

검수 = 문장마다 인용 원문 대목과 대조하는 AI 검수(judge.검수, gpt-4o). 판정 = criteria.켜진기준 (C2·C3·C6·C8).
원칙(1단계에서 확인): 검토 단계 안에는 interrupt 만 둔다. 조사·검수(비싸다)·알림·발송(바깥으로 나간다)은
모두 다른 단계 — 재개할 때 검토 단계가 처음부터 다시 실행되어도 아무것도 두 번 일어나지 않는다.
"""
from __future__ import annotations

import argparse
import difflib
import json
import operator
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import criteria
import graph as G
import judge
import mailer

DB = G.OUT / "checkpoints.sqlite"
NOTICES = G.OUT / "notices.jsonl"            # 고객·담당자에게 보낸 안내 (검토 중 · 지연)
MANUAL = G.OUT / "manual_queue.jsonl"        # 반려 → 담당자가 직접 처리할 목록
ESCALATIONS = G.OUT / "escalations.jsonl"    # 3일 규칙 → 상급자 이관
PAST = G.ROOT / "data" / "past_runs"
DEMO_TO = "customer@example.com"      # 지난 기록 의뢰의 가상 수신자 — 허용 목록에 없으니 smtp 모드에서도 막힌다
재조사_상한 = 2
대기_상한 = timedelta(hours=72)
결정들 = ("approve", "edit", "reject", "redo")


class 의뢰(TypedDict, total=False):
    id: str
    고객: str
    수신자: str
    질문: str
    qid: str | None
    입구: str            # "새 의뢰" | "지난 기록"
    past_run_id: str | None
    접수시각: str


class State(TypedDict, total=False):
    의뢰: 의뢰
    보고서: dict          # 조사 결과를 간추린 것 (run_id · 제목 · report · metrics · sections · 검수)
    멈춘이유: list[str]
    대기시작: str
    답: dict             # {"결정": approve|edit|reject|redo, "사유", "지시", "방식", "수정본", "담당자", "시각"}
    재조사횟수: int
    수정: dict           # 수정 후 승인의 기록 (방식 · 뺀 문장 · 바뀐 줄 수)
    발송본: str
    발송: dict
    결과: str            # "자동 발송" | "승인 발송" | "수정 발송" | "반려"
    이력: Annotated[list, operator.add]     # 담당자 답과 재조사 — 쌓이기만 한다


def _지금() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _기록(path, rec: dict) -> None:
    G.OUT.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 노드 ─────────────────────────────────────────────────────────────────────
def 접수(s: State) -> dict:
    return {"의뢰": {**s["의뢰"], "접수시각": s["의뢰"].get("접수시각") or _지금()}, "재조사횟수": 0}


def 간추리기(rec: dict) -> dict:
    return {"run_id": rec["run_id"], "제목": (rec.get("plan") or {}).get("제목") or rec["질문"], "질문": rec["질문"],
            "report": rec["report"], "metrics": rec["metrics"], "채택": rec.get("채택") or {},
            "sections": [{k: x.get(k) for k in ("번호", "바퀴", "절", "본문", "인용", "읽은문서", "충분", "부족")}
                         for x in rec.get("sections", [])]}


def 지난기록(run_id: str) -> dict:
    for f in PAST.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if f'"run_id": "{run_id}"' in line[:60]:
                return json.loads(line)
    raise KeyError(run_id)


def 조사(s: State) -> dict:
    """처음에는 의뢰대로. 담당자가 '다시 판정' 을 주면 그 지시를 질문 뒤에 붙여 실제로 다시 조사한다(키 필요).
    다시 조사에 실패하면 이전 보고서를 그대로 두고 실패를 멈춘 이유에 남긴다 — 다시 사람에게 간다."""
    q, 답 = s["의뢰"], s.get("답") or {}
    if 답.get("결정") == "redo":
        n = s.get("재조사횟수", 0) + 1
        try:
            rec = G.run(f"{q['질문']}\n(담당자 지시: {답['지시']})", qid=q.get("qid"), 라벨=f"HITL-재조사{n}",
                        save=True, verbose=False)
            return {"보고서": 간추리기(rec), "재조사횟수": n,
                    "이력": [{"사건": "재조사", "회차": n, "지시": 답["지시"], "시각": _지금()}]}
        except Exception as e:
            return {"보고서": {**s["보고서"], "재조사실패": f"{type(e).__name__}: {str(e)[:40]}"}, "재조사횟수": n,
                    "이력": [{"사건": "재조사 실패", "회차": n, "지시": 답["지시"], "시각": _지금()}]}
    rec = 지난기록(q["past_run_id"]) if q["입구"] == "지난 기록" else \
        G.run(q["질문"], qid=q.get("qid"), 라벨="HITL", save=True, verbose=False)
    return {"보고서": 간추리기(rec)}


def 검수(s: State) -> dict:
    """AI 검수. 실패하면(키 없음·한도 초과 등) 통과시키지 않고 '확인 못 함' 으로 남긴다 — C8 이 사람에게 보낸다."""
    r = s["보고서"]
    try:
        g = judge.검수(r["질문"], r["report"])
    except Exception as e:                        # 키 문자열이 섞이지 않게 예외 종류와 짧은 설명만
        n = len(criteria._본문(r["report"]))
        g = {"문장": [], "오류수": 0, "판정실패": n or 1, "비용": {"호출": 0, "달러": 0.0},
             "오류": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"보고서": {**r, "검수": g}}


def 판정(s: State) -> dict:
    이유 = criteria.판정(s["보고서"])
    if f := s["보고서"].get("재조사실패"):
        이유 = [f"재조사 실패 — {f}"] + 이유
    return {"멈춘이유": 이유}


def 알림(s: State) -> dict:
    """담당자·고객에게 '검토 중' 을 알린다 — 지금은 기록만. 검토와 떨어져 있어 재개해도 다시 실행되지 않는다."""
    q = s["의뢰"]
    _기록(NOTICES, {"thread_id": q["id"], "고객": q["고객"], "종류": "검토 중", "내용": "보고서를 검토 중입니다",
                  "멈춘이유": s["멈춘이유"], "시각": _지금()})
    return {"대기시작": _지금()}


def 검토(s: State) -> dict:
    q, r = s["의뢰"], s["보고서"]
    n = s.get("재조사횟수", 0)
    답 = interrupt({
        "의뢰ID": q["id"], "고객": q["고객"], "수신자": q["수신자"], "질문": q["질문"], "접수시각": q["접수시각"],
        "대기시작": s["대기시작"], "입구": q["입구"],
        "보고서제목": r["제목"], "보고서자수": len(r["report"]),
        "멈춘 이유": s["멈춘이유"],
        "검수 오류 문장": [x for x in r.get("검수", {}).get("문장", []) if x["판정"] in judge.오류판정],
        "재조사": f"{n}/{재조사_상한}회",
        "가능한 답": [d for d in 결정들 if d != "redo" or n < 재조사_상한],
        "통과시키면": f"{q['고객']} <{q['수신자']}> 에게 보고서 「{r['제목']}」({len(r['report']):,}자)가 발송됩니다",
    })
    if isinstance(답, str):
        답 = {"결정": 답}
    return {"답": 답, "이력": [{"사건": "담당자 답", **답}]}


def 빼기(report: str, 문장들: list[str]) -> str:
    """보고서에서 문장을 지운다. 줄이 통째로 비면 그 줄도 없앤다."""
    out = report
    for x in 문장들:
        out = out.replace(x, "")
    return "\n".join(l.rstrip() for l in out.split("\n") if l.strip() or not l).replace("\n\n\n", "\n\n").strip() + "\n"


def 수정반영(s: State) -> dict:
    r, 답 = s["보고서"], s["답"]
    if 답.get("방식") == "오류문장빼기":
        뺄 = [x["문장"] for x in r.get("검수", {}).get("문장", []) if criteria.치명(x)]     # 일반 서술은 남긴다 (C8 v2)
        본 = 빼기(r["report"], 뺄)
        기록 = {"방식": "AI 검수 치명 문장 빼기", "뺀문장": len(뺄)}
    else:
        본 = 답["수정본"]
        기록 = {"방식": "직접 수정"}
    바뀐 = [l for l in difflib.unified_diff(r["report"].splitlines(), 본.splitlines(), lineterm="", n=0)
          if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return {"발송본": 본, "수정": {**기록, "바뀐줄": len(바뀐), "원본자수": len(r["report"]), "발송본자수": len(본)}}


def 발송(s: State) -> dict:
    q, r = s["의뢰"], s["보고서"]
    본 = s.get("발송본") or r["report"]
    rec = mailer.발송(q["id"], q["고객"], q["수신자"], f"[SME 리서치] {r['제목']}", 본,
                    force_file=q.get("force_file", False) or (s.get("답") or {}).get("파일만", False))
    결정 = (s.get("답") or {}).get("결정")
    결과 = {"approve": "승인 발송", "edit": "수정 발송"}.get(결정, "자동 발송")
    if rec["상태"] != "발송":
        # 7단계에서 찾았다: SMTP 인증 실패인데 '자동 발송' 으로 끝나 보냈다고 착각할 뻔했다.
        # 나가지 않은 보고서는 끝난 것으로 치지 않고 직접 처리 목록에 올린다 — 재발송() 으로 다시 보낸다.
        결과 = f"발송 {rec['상태']} ({결과} 시도)"
        _기록(MANUAL, {"thread_id": q["id"], "고객": q["고객"], "질문": q["질문"],
                      "사유": f"발송 {rec['상태']} — {rec.get('사유', '')[:80]}", "멈춘이유": s.get("멈춘이유", []),
                      "시각": _지금()})
    return {"발송": rec, "결과": 결과}


def 반려기록(s: State) -> dict:
    q = s["의뢰"]
    _기록(MANUAL, {"thread_id": q["id"], "고객": q["고객"], "질문": q["질문"], "사유": s["답"].get("사유", ""),
                  "멈춘이유": s["멈춘이유"], "시각": _지금()})
    return {"결과": "반려"}


def _검토뒤(s: State) -> str:
    return {"approve": "발송", "edit": "수정반영", "reject": "반려기록", "redo": "조사"}[s["답"]["결정"]]


def build(checkpointer):
    g = StateGraph(State)
    for name, fn in [("접수", 접수), ("조사", 조사), ("검수", 검수), ("판정", 판정), ("알림", 알림),
                     ("검토", 검토), ("수정반영", 수정반영), ("발송", 발송), ("반려기록", 반려기록)]:
        g.add_node(name, fn)
    g.add_edge(START, "접수")
    g.add_edge("접수", "조사")
    g.add_edge("조사", "검수")
    g.add_edge("검수", "판정")
    g.add_conditional_edges("판정", lambda s: "알림" if s["멈춘이유"] else "발송", ["알림", "발송"])
    g.add_edge("알림", "검토")
    g.add_conditional_edges("검토", _검토뒤, ["발송", "수정반영", "반려기록", "조사"])
    g.add_edge("수정반영", "발송")
    g.add_edge("발송", END)
    g.add_edge("반려기록", END)
    return g.compile(checkpointer=checkpointer)


# ── 바깥에서 쓰는 함수 (CLI · 웹 데모 공용) ───────────────────────────────────────
_app = None


def app():
    global _app
    if _app is None:
        G.OUT.mkdir(exist_ok=True)
        _app = build(SqliteSaver(sqlite3.connect(DB, check_same_thread=False)))
    return _app


def cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def _요약(tid: str, out: dict) -> dict:
    return {"id": tid, "멈춤": "__interrupt__" in out, "결과": out.get("결과"), "발송": out.get("발송"),
            "수정": out.get("수정"), "화면": out["__interrupt__"][0].value if "__interrupt__" in out else None}


def 투입(q: 의뢰) -> dict:
    """의뢰 한 건을 넣는다. 자동으로 끝나면 결과를, 멈추면 interrupt 내용을 돌려준다."""
    q = {**q, "id": q.get("id") or datetime.now().strftime("Q%m%d-%H%M%S-") + uuid.uuid4().hex[:4]}
    return _요약(q["id"], app().invoke({"의뢰": q}, cfg(q["id"])))


def 전체() -> list[str]:
    saver = app().checkpointer
    return sorted({c.config["configurable"]["thread_id"] for c in saver.list(None)})


def _이관표() -> dict[str, dict]:
    if not ESCALATIONS.exists():
        return {}
    return {v["thread_id"]: v for v in map(json.loads, ESCALATIONS.read_text(encoding="utf-8").splitlines())}


def 대기목록() -> list[dict]:
    """다음 단계가 남아 있는 thread = 대기. 화면 내용은 interrupt 에 담아 둔 것을 그대로 꺼낸다."""
    out, 이관 = [], _이관표()
    for t in 전체():
        snap = app().get_state(cfg(t))
        if snap.next and snap.tasks and snap.tasks[0].interrupts:
            v = snap.tasks[0].interrupts[0].value
            out.append({**v, "이관": 이관.get(t)})
    return sorted(out, key=lambda v: v["대기시작"])


def 끝난목록() -> list[dict]:
    out = []
    for t in 전체():
        snap = app().get_state(cfg(t))
        if not snap.next and snap.values.get("결과"):
            v = snap.values
            out.append({"의뢰ID": t, "고객": v["의뢰"]["고객"], "질문": v["의뢰"]["질문"], "결과": v["결과"],
                        "멈춘이유": v.get("멈춘이유", []), "이력": v.get("이력", []), "수정": v.get("수정"),
                        "발송": v.get("발송"), "재조사횟수": v.get("재조사횟수", 0)})
    return out


def 답하기(tid: str, 결정: str, 사유: str = "", 담당자: str = "담당자", 지시: str = "",
         방식: str = "", 수정본: str = "", 파일만: bool = False) -> dict:
    """파일만=True 면 SEND_MODE 가 smtp 여도 이 건은 파일로만 낸다 (웹 데모의 '실제 메일로 보내기' 가 꺼진 경우)."""
    """담당자 답을 검사한 뒤 멈춘 자리부터 이어간다. 규칙에 어긋난 답은 그래프에 넣지 않고 거절한다."""
    snap = app().get_state(cfg(tid))
    if not (snap.next and snap.tasks and snap.tasks[0].interrupts):
        raise ValueError(f"{tid} 는 대기 중이 아닙니다 (결과: {snap.values.get('결과')})")
    화면 = snap.tasks[0].interrupts[0].value
    if 결정 not in 결정들:
        raise ValueError(f"결정은 {결정들} 중 하나")
    if 결정 not in 화면["가능한 답"]:
        raise ValueError(f"다시 판정은 {재조사_상한}회까지입니다 — 승인·수정·반려 중에서 골라 주세요")
    if 결정 == "reject" and not 사유.strip():
        raise ValueError("반려에는 사유가 필요합니다 (기준을 고칠 재료가 됩니다)")
    if 결정 == "redo" and not 지시.strip():
        raise ValueError("다시 판정에는 지시가 필요합니다 (예: '한국 기준은 중소기업 문서로 다시')")
    if 결정 == "edit" and 방식 != "오류문장빼기" and not 수정본.strip():
        raise ValueError("수정 후 승인은 --방식 오류문장빼기 또는 수정본이 필요합니다")
    답 = {"결정": 결정, "사유": 사유, "지시": 지시, "방식": 방식, "수정본": 수정본, "담당자": 담당자, "시각": _지금(),
         "파일만": 파일만}
    return _요약(tid, app().invoke(Command(resume=답), cfg(tid)))


def 상태(tid: str) -> dict:
    return app().get_state(cfg(tid)).values


def 재발송(tid: str, 파일만: bool = False) -> dict:
    """발송 실패·차단으로 끝난 건을 다시 보낸다 (같은 thread 는 '발송' 이 한 번 성공하면 더 나가지 않는다)."""
    v = app().get_state(cfg(tid)).values
    if not str(v.get("결과", "")).startswith("발송 "):
        raise ValueError(f"{tid} 는 발송 실패 건이 아닙니다 (결과: {v.get('결과')})")
    q, r = v["의뢰"], v["보고서"]
    rec = mailer.발송(tid, q["고객"], q["수신자"], f"[SME 리서치] {r['제목']}", v.get("발송본") or r["report"],
                    force_file=파일만)
    if rec["상태"] == "발송":
        app().update_state(cfg(tid), {"발송": rec, "결과": v["결과"].split("(")[-1].replace(" 시도)", "") + " (재발송)",
                                      "이력": [{"사건": "재발송", "모드": rec["모드"], "시각": _지금()}]})
    return rec


def 초기화() -> None:
    """시연 초기화 — 대기 건 저장소와 발송·알림·이관 기록을 지운다 (output/ 안만)."""
    global _app
    if _app is not None:
        _app.checkpointer.conn.close()
        _app = None
    for p in list(G.OUT.glob("checkpoints.sqlite*")) + [NOTICES, MANUAL, ESCALATIONS, mailer.OUTBOX_LOG]:
        p.unlink(missing_ok=True)
    import shutil
    shutil.rmtree(mailer.OUTBOX_DIR, ignore_errors=True)


def sweep(now: datetime | None = None) -> list[dict]:
    """3일 규칙. 72시간 넘게 기다린 건은 자동 승인도 자동 반려도 하지 않는다 —
    자동 승인은 놓침을 막는다는 결정(4단계 A)과 어긋나고, 자동 반려는 고객 요청을 조용히 없앤다.
    대신 상급 담당자에게 넘기고(이관 기록) 고객에게 지연 안내를 남긴다. 한 건에 한 번만."""
    now = now or datetime.now()
    이관, done = _이관표(), []
    for v in 대기목록():
        waited = now - datetime.fromisoformat(v["대기시작"])
        if waited < 대기_상한 or v["의뢰ID"] in 이관:
            continue
        rec = {"thread_id": v["의뢰ID"], "고객": v["고객"], "대기시간": f"{waited.total_seconds() / 3600:.0f}시간",
               "이관대상": "상급 담당자", "시각": now.isoformat(timespec="seconds")}
        _기록(ESCALATIONS, rec)
        _기록(NOTICES, {"thread_id": v["의뢰ID"], "고객": v["고객"], "종류": "지연 안내",
                      "내용": "보고서 검토가 길어지고 있습니다. 담당자가 확인 중이며 곧 안내드리겠습니다.",
                      "시각": now.isoformat(timespec="seconds")})
        done.append(rec)
    return done


# ── 지난 기록을 의뢰로 ────────────────────────────────────────────────────────────
가상고객 = ["가상고객 A (금속가공 20인)", "가상고객 B (식품제조 45인)", "가상고객 C (물류 12인)",
         "가상고객 D (사출성형 30인)", "가상고객 E (전자부품 60인)", "가상고객 F (섬유 25인)",
         "가상고객 G (자동차부품 80인)", "가상고객 H (화장품 OEM 35인)", "가상고객 I (가구 15인)",
         "가상고객 J (정밀기계 50인)", "가상고객 K (포장재 22인)"]


def 지난의뢰들(n: int | None = None) -> list[의뢰]:
    """4단계 표본 150건(검수 판정이 캐시에 있어 비용 0)을 질문이 골고루 섞이게 줄 세운다 — 앞에서 n건."""
    by_q: dict[str, list] = {}
    for r in judge.표본():
        by_q.setdefault(r["qid"], []).append(r)
    order = [r for i in range(max(map(len, by_q.values()))) for q in sorted(by_q) if i < len(by_q[q])
             for r in [by_q[q][i]]]
    return [{"id": f"P-{r['qid']}-{r['run_id'][-4:]}", "고객": 가상고객[i % len(가상고객)], "수신자": DEMO_TO,
             "질문": r["질문"], "qid": r["qid"], "입구": "지난 기록", "past_run_id": r["run_id"]}
            for i, r in enumerate(order[:n])]


def _한줄(r: dict) -> str:
    if r["멈춤"]:
        return f"⏸ 대기 ({'; '.join(r['화면']['멈춘 이유'])})"
    d, m = r.get("발송") or {}, r.get("수정")
    return f"✅ {r['결과']}" + (f"  ({d.get('모드')} · {d.get('상태')} · {d.get('파일')})" if d else "") + \
        (f"  [수정: {m['방식']} · 바뀐 줄 {m['바뀐줄']} · {m['원본자수']:,}→{m['발송본자수']:,}자]" if m else "")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="사람이 승인하는 보고서 발송")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("past", help="지난 기록 투입 — 시험용이라 항상 파일 모드")
    p.add_argument("--n", type=int, default=20, help="표본 150건 중 몇 건 (기본 20)")
    n = sub.add_parser("new"); n.add_argument("q"); n.add_argument("--고객", default="가상고객 Z")
    n.add_argument("--수신자", default=None, help="기본: 허용 목록 첫 주소, 없으면 가상 주소")
    n.add_argument("--file", action="store_true")
    sub.add_parser("list")
    a_ = sub.add_parser("answer"); a_.add_argument("id"); a_.add_argument("결정", choices=결정들)
    a_.add_argument("--사유", default=""); a_.add_argument("--지시", default="")
    a_.add_argument("--방식", default="", choices=["", "오류문장빼기"])
    a_.add_argument("--수정본", default="", help="직접 고친 보고서 파일 경로")
    w = sub.add_parser("sweep"); w.add_argument("--now", default=None, help="시연용 현재 시각 (ISO)")
    a = ap.parse_args()

    if a.cmd == "past":
        for q in 지난의뢰들(a.n):
            if q["id"] in 전체():
                print(f"[{q['id']}] 이미 투입됨 — 건너뜀"); continue
            r = 투입({**q, "force_file": True})
            print(f"[{r['id']}] {q['질문'][:40]}…  → {_한줄(r)}")
    elif a.cmd == "new":
        qd = G.QUESTIONS.get(a.q, {"id": None, "질문": a.q})
        to = a.수신자 or (mailer.허용목록() or [DEMO_TO])[0]
        print(f"[새 의뢰] {qd['질문']}  (발송 모드: {mailer.모드(a.file)})")
        r = 투입({"고객": a.고객, "수신자": to, "질문": qd["질문"], "qid": qd["id"], "입구": "새 의뢰",
                 "force_file": a.file})
        print(f"[{r['id']}] → {_한줄(r)}")
    elif a.cmd == "list":
        w = 대기목록()
        print(f"대기 {len(w)}건 (저장: {DB.relative_to(G.ROOT)})")
        for v in w:
            print(f" - {v['의뢰ID']}  {v['고객']}  대기 시작 {v['대기시작']}  재조사 {v['재조사']}"
                  + (f"  ⚠ 이관됨({v['이관']['대기시간']})" if v["이관"] else "")
                  + f"\n     멈춘 이유: {'; '.join(v['멈춘 이유'])}\n     통과시키면: {v['통과시키면']}")
    elif a.cmd == "answer":
        수정본 = open(a.수정본, encoding="utf-8").read() if a.수정본 else ""
        try:
            r = 답하기(a.id, a.결정, a.사유, 지시=a.지시, 방식=a.방식, 수정본=수정본)
        except ValueError as e:
            print(f"거절: {e}"); return
        print(f"[{r['id']}] → {_한줄(r)}")
    elif a.cmd == "sweep":
        done = sweep(datetime.fromisoformat(a.now) if a.now else None)
        print(f"이관 {len(done)}건" + "".join(f"\n - {d['thread_id']} {d['고객']} ({d['대기시간']} 대기)" for d in done))


if __name__ == "__main__":
    main()
