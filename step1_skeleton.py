"""1단계 — 멈추고 이어가기 뼈대 (학습용). 가짜 보고서 3건, LLM·키 없음.

    python step1_skeleton.py start              # 3건 투입 → 1건 자동 발송, 2건 검토에서 멈춤
    python step1_skeleton.py list               # (새 프로세스) 파일에서 대기 건 찾기
    python step1_skeleton.py resume F2 approve  # (새 프로세스) 멈춘 자리부터 이어가기
    python step1_skeleton.py trap               # 멈춘 단계가 다시 실행되는 함정 재현 → 고친 구조

판정 ──(안 걸림)──────────────────→ 발송 → END
  └──(걸림)──→ 검토[interrupt] ──approve──→ 발송 → END
                               └─reject───→ 반려기록 → END
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
DB = OUT / "step1_checkpoints.sqlite"
OUTBOX = OUT / "step1_outbox.jsonl"

# 가짜 보고서 3건. 판정 기준도 2단계 전까지는 흉내만 낸다.
FAKE = {
    "F1": {"고객": "㈜한빛정밀", "질문": "OEE 는 무엇이고 어떻게 계산하나?",
           "보고서": "OEE 는 가용성·성능·품질을 곱한 값이다 «Overall equipment effectiveness».",
           "출처없는수치": 0, "경보": 0},
    "F2": {"고객": "㈜대성금속", "질문": "스마트공장 도입 효과는?",
           "보고서": "스마트공장을 도입하면 불량률이 평균 43% 줄어든다.",
           "출처없는수치": 1, "경보": 0},
    "F3": {"고객": "㈜푸른식품", "질문": "식품 공장의 이력 추적은 어떻게 하나?",
           "보고서": "RFID 로 원료를 추적한다 «Food Traceability Handbook».",
           "출처없는수치": 0, "경보": 1},
}


class State(TypedDict, total=False):
    건: dict
    멈춘이유: list[str]
    답: str
    결과: str


def 판정(state: State) -> dict:
    r = state["건"]
    이유 = []
    if r["출처없는수치"]:
        이유.append(f"출처 표시 없는 수치 문장 {r['출처없는수치']}개")
    if r["경보"]:
        이유.append(f"지표 경보 {r['경보']}개 (예: 읽지 않은 문서 인용)")
    return {"멈춘이유": 이유}


def 검토(state: State) -> dict:
    """사람에게 묻고 답이 올 때까지 멈춘다. 이 함수 안에서는 interrupt 앞에 바깥으로 나가는 일을 두지 않는다."""
    r = state["건"]
    답 = interrupt({
        "고객": r["고객"],
        "질문": r["질문"],
        "보고서": r["보고서"],
        "멈춘 이유": state["멈춘이유"],
        "통과시키면": f"{r['고객']} 에게 보고서({len(r['보고서'])}자)가 발송됩니다",
    })
    return {"답": 답}


def 발송(state: State) -> dict:
    r = state["건"]
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"고객": r["고객"], "보고서": r["보고서"]}, ensure_ascii=False) + "\n")
    print(f"   → 발송: {r['고객']}")
    return {"결과": "발송"}


def 반려기록(state: State) -> dict:
    print(f"   → 반려: {state['건']['고객']} (발송하지 않음)")
    return {"결과": "반려"}


def build(checkpointer):
    g = StateGraph(State)
    g.add_node("판정", 판정)
    g.add_node("검토", 검토)
    g.add_node("발송", 발송)
    g.add_node("반려기록", 반려기록)
    g.add_edge(START, "판정")
    g.add_conditional_edges("판정", lambda s: "검토" if s["멈춘이유"] else "발송", ["검토", "발송"])
    g.add_conditional_edges("검토", lambda s: "발송" if s["답"] == "approve" else "반려기록", ["발송", "반려기록"])
    g.add_edge("발송", END)
    g.add_edge("반려기록", END)
    return g.compile(checkpointer=checkpointer)


def open_app():
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB, check_same_thread=False)
    saver = SqliteSaver(conn)
    return build(saver), saver


def cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def 대기목록(app, saver) -> list[str]:
    """파일에 기록된 모든 thread_id 중, 다음 단계가 남아 있는 것 = 대기 중."""
    tids = sorted({c.config["configurable"]["thread_id"] for c in saver.list(None)})
    return [t for t in tids if app.get_state(cfg(t)).next]


def cmd_start():
    app, _ = open_app()
    for tid, r in FAKE.items():
        print(f"[{tid}] {r['고객']} 투입")
        res = app.invoke({"건": r}, cfg(tid))
        if "__interrupt__" in res:
            print(f"   ⏸ 멈춤 — 이유: {', '.join(res['__interrupt__'][0].value['멈춘 이유'])}")


def cmd_list():
    app, saver = open_app()
    대기 = 대기목록(app, saver)
    print(f"파일 {DB.name} 에서 찾은 대기 건: {len(대기)}건")
    for t in 대기:
        snap = app.get_state(cfg(t))
        화면 = snap.tasks[0].interrupts[0].value     # interrupt 에 담았던 내용이 그대로 저장되어 있다
        print(f" - {t}  다음 단계={snap.next}  고객={화면['고객']}  멈춘 이유={화면['멈춘 이유']}")
        print(f"        통과시키면: {화면['통과시키면']}")


def cmd_resume(tid: str, answer: str):
    app, _ = open_app()
    before = app.get_state(cfg(tid))
    if not before.next:
        print(f"{tid} 는 대기 중이 아니다 (결과: {before.values.get('결과')})")
        return
    print(f"[{tid}] 담당자 답: {answer}  (재개 전 다음 단계={before.next})")
    res = app.invoke(Command(resume=answer), cfg(tid))
    after = app.get_state(cfg(tid))
    print(f"   결과={res['결과']}  재개 후 다음 단계={after.next}  "
          f"쌓인 체크포인트={len(list(app.get_state_history(cfg(tid))))}개")


# ── 함정 재현: 멈춘 단계는 재개할 때 처음부터 다시 실행된다 ────────────────────────
def cmd_trap():
    보낸알림: list[str] = []

    def 검토_나쁜(state: State) -> dict:
        보낸알림.append("고객님, 보고서를 검토 중입니다")       # 바깥으로 나가는 일을 interrupt 앞에 둠
        return {"답": interrupt({"질문": "승인?"})}

    def 알림(state: State) -> dict:
        보낸알림.append("고객님, 보고서를 검토 중입니다")
        return {}

    def 검토_좋은(state: State) -> dict:
        return {"답": interrupt({"질문": "승인?"})}

    def run(label, nodes, edges):
        보낸알림.clear()
        g = StateGraph(State)
        for n, f in nodes:
            g.add_node(n, f)
        for a, b in edges:
            g.add_edge(a, b)
        app = g.compile(checkpointer=InMemorySaver())
        c = cfg(label)
        app.invoke({"건": FAKE["F2"]}, c)
        print(f"[{label}] 멈춘 직후 고객이 받은 알림: {len(보낸알림)}통")
        app.invoke(Command(resume="approve"), c)
        print(f"[{label}] 재개 후   고객이 받은 알림: {len(보낸알림)}통")

    run("나쁜 구조", [("검토", 검토_나쁜)], [(START, "검토"), ("검토", END)])
    run("고친 구조", [("알림", 알림), ("검토", 검토_좋은)], [(START, "알림"), ("알림", "검토"), ("검토", END)])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["resume"]:
        cmd_resume(a[1], a[2])
    else:
        {"start": cmd_start, "list": cmd_list, "trap": cmd_trap}[a[0]]()
