"""사람이 승인하는 보고서 발송 파이프라인.

    python hitl.py past                       # 지난 기록(질문 11건 '기본' 조건)을 의뢰로 투입 — 키 없음
    python hitl.py new M2 --고객 "가상고객 B"   # 새 의뢰 — 딥리서치를 실제로 돌린다(키 필요)
    python hitl.py list                       # 대기 건
    python hitl.py answer <의뢰ID> approve     # 담당자 답 → 멈춘 자리부터 이어감
    python hitl.py answer <의뢰ID> reject --사유 "수치 출처 확인 필요"

접수 → 조사 → 판정 ─(안 걸림)──────────────────────────→ 발송 → END
                   └(걸림)→ 알림 → 검토[interrupt] ─approve→ 발송 → END
                                                 └reject─→ 반려기록 → END

원칙(1단계에서 확인): 검토 단계 안에는 interrupt 만 둔다. 조사(비싸다)·알림·발송(바깥으로 나간다)은
모두 다른 단계 — 재개할 때 검토 단계가 처음부터 다시 실행되어도 아무것도 두 번 일어나지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import criteria
import graph as G
import mailer

DB = G.OUT / "checkpoints.sqlite"
NOTICES = G.OUT / "notices.jsonl"
PAST = G.ROOT / "data" / "past_runs"
DEMO_TO = "customer@example.com"      # 지난 기록 의뢰의 가상 수신자 — 허용 목록에 없으니 smtp 모드에서도 막힌다


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
    보고서: dict          # 조사 결과를 간추린 것 (run_id · 제목 · report · metrics · sections)
    멈춘이유: list[str]
    답: dict             # {"결정": "approve"|"reject", "사유": str, "담당자": str}
    발송: dict
    결과: str            # "자동 발송" | "승인 발송" | "반려"


# ── 노드 ─────────────────────────────────────────────────────────────────────
def 접수(s: State) -> dict:
    return {"의뢰": {**s["의뢰"], "접수시각": s["의뢰"].get("접수시각") or datetime.now().isoformat(timespec="seconds")}}


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
    q = s["의뢰"]
    if q["입구"] == "지난 기록":
        rec = 지난기록(q["past_run_id"])
    else:
        rec = G.run(q["질문"], qid=q.get("qid"), 라벨="HITL", save=True, verbose=False)
    return {"보고서": 간추리기(rec)}


def 판정(s: State) -> dict:
    return {"멈춘이유": criteria.판정(s["보고서"])}


def 알림(s: State) -> dict:
    """담당자·고객에게 '검토 중' 을 알린다 — 지금은 기록만. 검토와 떨어져 있어 재개해도 다시 실행되지 않는다."""
    q = s["의뢰"]
    with NOTICES.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"thread_id": q["id"], "고객": q["고객"], "내용": "보고서를 검토 중입니다",
                            "멈춘이유": s["멈춘이유"], "시각": datetime.now().isoformat(timespec="seconds")},
                           ensure_ascii=False) + "\n")
    return {}


def 검토(s: State) -> dict:
    q, r = s["의뢰"], s["보고서"]
    답 = interrupt({
        "의뢰ID": q["id"], "고객": q["고객"], "수신자": q["수신자"], "질문": q["질문"], "접수시각": q["접수시각"],
        "보고서제목": r["제목"], "보고서자수": len(r["report"]),
        "멈춘 이유": s["멈춘이유"],
        "통과시키면": f"{q['고객']} <{q['수신자']}> 에게 보고서 「{r['제목']}」({len(r['report']):,}자)가 발송됩니다",
    })
    if isinstance(답, str):
        답 = {"결정": 답}
    return {"답": 답}


def 발송(s: State) -> dict:
    q, r = s["의뢰"], s["보고서"]
    rec = mailer.발송(q["id"], q["고객"], q["수신자"], f"[SME 리서치] {r['제목']}", r["report"],
                    force_file=s["의뢰"].get("force_file", False))
    return {"발송": rec, "결과": "승인 발송" if s.get("답") else "자동 발송"}


def 반려기록(s: State) -> dict:
    return {"결과": "반려"}


def build(checkpointer):
    g = StateGraph(State)
    for name, fn in [("접수", 접수), ("조사", 조사), ("판정", 판정), ("알림", 알림),
                     ("검토", 검토), ("발송", 발송), ("반려기록", 반려기록)]:
        g.add_node(name, fn)
    g.add_edge(START, "접수")
    g.add_edge("접수", "조사")
    g.add_edge("조사", "판정")
    g.add_conditional_edges("판정", lambda s: "알림" if s["멈춘이유"] else "발송", ["알림", "발송"])
    g.add_edge("알림", "검토")
    g.add_conditional_edges("검토", lambda s: "발송" if s["답"].get("결정") == "approve" else "반려기록",
                            ["발송", "반려기록"])
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


def 투입(q: 의뢰) -> dict:
    """의뢰 한 건을 넣는다. 자동으로 끝나면 결과를, 멈추면 interrupt 내용을 돌려준다."""
    q = {**q, "id": q.get("id") or datetime.now().strftime("Q%m%d-%H%M%S-") + uuid.uuid4().hex[:4]}
    out = app().invoke({"의뢰": q}, cfg(q["id"]))
    return {"id": q["id"], "멈춤": "__interrupt__" in out, "결과": out.get("결과"),
            "화면": out["__interrupt__"][0].value if "__interrupt__" in out else None}


def 전체() -> list[str]:
    saver = app().checkpointer
    return sorted({c.config["configurable"]["thread_id"] for c in saver.list(None)})


def 대기목록() -> list[dict]:
    """다음 단계가 남아 있는 thread = 대기. 화면 내용은 interrupt 에 담아 둔 것을 그대로 꺼낸다."""
    out = []
    for t in 전체():
        snap = app().get_state(cfg(t))
        if snap.next and snap.tasks and snap.tasks[0].interrupts:
            out.append(snap.tasks[0].interrupts[0].value)
    return sorted(out, key=lambda v: v["접수시각"])


def 답하기(tid: str, 결정: str, 사유: str = "", 담당자: str = "담당자") -> dict:
    snap = app().get_state(cfg(tid))
    if not snap.next:
        raise ValueError(f"{tid} 는 대기 중이 아닙니다 (결과: {snap.values.get('결과')})")
    out = app().invoke(Command(resume={"결정": 결정, "사유": 사유, "담당자": 담당자,
                                       "시각": datetime.now().isoformat(timespec="seconds")}), cfg(tid))
    return {"id": tid, "결과": out.get("결과"), "발송": out.get("발송")}


def 상태(tid: str) -> dict:
    return app().get_state(cfg(tid)).values


# ── 지난 기록을 의뢰로 ────────────────────────────────────────────────────────────
가상고객 = ["가상고객 A (금속가공 20인)", "가상고객 B (식품제조 45인)", "가상고객 C (물류 12인)",
         "가상고객 D (사출성형 30인)", "가상고객 E (전자부품 60인)", "가상고객 F (섬유 25인)",
         "가상고객 G (자동차부품 80인)", "가상고객 H (화장품 OEM 35인)", "가상고객 I (가구 15인)",
         "가상고객 J (정밀기계 50인)", "가상고객 K (포장재 22인)"]


def 지난의뢰들(라벨: str = "기본", 반복: int = 1) -> list[의뢰]:
    rows = [json.loads(l) for l in (PAST / "ablation_runs.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r["라벨"] == 라벨 and r["실험"]["반복"] == 반복]
    return [{"id": f"P-{r['qid']}-{라벨}-{반복}", "고객": 가상고객[i % len(가상고객)], "수신자": DEMO_TO,
             "질문": r["질문"], "qid": r["qid"], "입구": "지난 기록", "past_run_id": r["run_id"]}
            for i, r in enumerate(rows)]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="사람이 승인하는 보고서 발송")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("past", help="지난 기록 투입 — 시험용이라 항상 파일 모드")
    p.add_argument("--라벨", default="기본"); p.add_argument("--반복", type=int, default=1)
    n = sub.add_parser("new"); n.add_argument("q"); n.add_argument("--고객", default="가상고객 Z")
    n.add_argument("--수신자", default=None, help="기본: 허용 목록 첫 주소, 없으면 가상 주소")
    n.add_argument("--file", action="store_true")
    sub.add_parser("list")
    a_ = sub.add_parser("answer"); a_.add_argument("id"); a_.add_argument("결정", choices=["approve", "reject"])
    a_.add_argument("--사유", default="")
    a = ap.parse_args()

    if a.cmd == "past":
        for q in 지난의뢰들(a.라벨, a.반복):
            if q["id"] in 전체():
                print(f"[{q['id']}] 이미 투입됨 — 건너뜀"); continue
            r = 투입({**q, "force_file": True})
            print(f"[{r['id']}] {q['질문'][:40]}…  → " +
                  (f"⏸ 대기 ({'; '.join(r['화면']['멈춘 이유'])})" if r["멈춤"] else f"✅ {r['결과']}"))
    elif a.cmd == "new":
        qd = G.QUESTIONS.get(a.q, {"id": None, "질문": a.q})
        to = a.수신자 or (mailer.허용목록() or [DEMO_TO])[0]
        print(f"[새 의뢰] {qd['질문']}  (발송 모드: {mailer.모드(a.file)})")
        r = 투입({"고객": a.고객, "수신자": to, "질문": qd["질문"], "qid": qd["id"], "입구": "새 의뢰",
                 "force_file": a.file})
        print(f"[{r['id']}] → " + (f"⏸ 대기 ({'; '.join(r['화면']['멈춘 이유'])})" if r["멈춤"] else f"✅ {r['결과']}"))
    elif a.cmd == "list":
        w = 대기목록()
        print(f"대기 {len(w)}건 (저장: {DB.relative_to(G.ROOT)})")
        for v in w:
            print(f" - {v['의뢰ID']}  {v['고객']}  접수 {v['접수시각']}\n     멈춘 이유: {'; '.join(v['멈춘 이유'])}"
                  f"\n     통과시키면: {v['통과시키면']}")
    elif a.cmd == "answer":
        try:
            r = 답하기(a.id, a.결정, a.사유)
        except ValueError as e:
            print(f"거절: {e}"); return
        d = r["발송"] or {}
        print(f"[{r['id']}] → {r['결과']}" + (f"  ({d.get('모드')} · {d.get('상태')} · {d.get('파일')})" if d else ""))


if __name__ == "__main__":
    main()
