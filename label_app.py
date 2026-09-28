"""4단계 — 사람 판정지. 표본 150건 중 30건을 담당자가 직접 판정한다 (LLM 판정·승인 기준은 보여 주지 않는다).

    python -m streamlit run label_app.py         → data/labels/human.jsonl
"""
from __future__ import annotations

import json
import random
from datetime import datetime

import streamlit as st

import criteria as C
import judge as J

사람수, 시드 = 30, 20260928
HUMAN = J.LABELS / "human.jsonl"
PICK = J.LABELS / "human_pick.json"


def 고르기() -> list[dict]:
    """표본 150건에서 30건. 기준에 걸린 것 15 · 안 걸린 것 15 — 두 부류가 다 있어야 일치율을 잴 수 있다.
    고른 목록은 파일로 고정해 다시 열어도 같은 30건이 나온다."""
    rows = {r["run_id"]: r for r in J.표본()}
    if PICK.exists():
        return [rows[i] for i in json.loads(PICK.read_text(encoding="utf-8"))["run_ids"]]
    rng = random.Random(시드)
    걸림 = [r for r in rows.values() if any(C.전부재기(r).values())]
    안걸림 = [r for r in rows.values() if r not in 걸림]
    pick = rng.sample(걸림, 사람수 // 2) + rng.sample(안걸림, 사람수 - 사람수 // 2)
    rng.shuffle(pick)
    PICK.write_text(json.dumps({"시드": 시드, "run_ids": [r["run_id"] for r in pick]}, indent=1), encoding="utf-8")
    return pick


@st.cache_data
def 대목표() -> dict:
    return J.대목표()


def 판정들() -> dict:
    if not HUMAN.exists():
        return {}
    out = {}
    for l in HUMAN.read_text(encoding="utf-8").splitlines():
        v = json.loads(l)
        out[v["run_id"]] = v                      # 같은 건을 다시 판정하면 마지막 것이 남는다
    return out


st.set_page_config(page_title="사람 판정지", layout="wide")
rows, done, items = 고르기(), 판정들(), 대목표()
st.title("사람 판정지 — 이 보고서를 고객에게 그대로 보내도 되는가")
st.caption(f"{len(done)}/{len(rows)}건 판정 완료 · 저장: data/labels/human.jsonl · AI 판정과 승인 기준은 일부러 보여 주지 않습니다")
st.progress(len(done) / len(rows))

with st.sidebar:
    st.markdown("**판정 기준** (4단계에서 정한 정의)\n\n고객에게 그대로 나가면 안 되는 오류가 **하나라도** 있으면 "
                "「사람이 봤어야 함」.\n- 사실과 다른 수치·주장\n- 인용한 문서가 그 주장을 뒷받침하지 않음\n"
                "- 질문과 다른 대상 (예: 한국을 물었는데 해외 기준을 한국 것처럼)\n\n표현이 투박하거나 덜 자세한 것은 오류가 아닙니다."
                "\n\n애매하면 **대표님이 실제로 고객에게 보내기 전에 한 번 보고 싶었을지**로 정해 주세요.")
    names = [f"{'✅' if r['run_id'] in done else '⬜'} {i+1:02d}. {r['qid']} · {r['질문'][:18]}…" for i, r in enumerate(rows)]
    if "_goto" in st.session_state:                  # 저장 직후 다음 미판정 건으로 (위젯이 그려지기 전에만 바꿀 수 있다)
        st.session_state["k"] = st.session_state.pop("_goto")
    st.session_state.setdefault("k", next((i for i, r in enumerate(rows) if r["run_id"] not in done), 0))
    k = st.radio("보고서", range(len(rows)), key="k", format_func=lambda i: names[i])

r = rows[k]
st.subheader(f"{k+1:02d}. 질문: {r['질문']}")
st.caption(f"run_id {r['run_id']}")

sents = C._본문(r["report"])
for n, s in enumerate(sents, 1):
    it = items.get(J._h(r["질문"], s), {})
    c1, c2 = st.columns([3, 2])
    c1.markdown(f"**[{n}]** {s}")
    if it.get("대목"):
        with c2.expander(f"인용 원문 대목 {len(it['대목'])}개"):
            for d in it["대목"]:
                st.markdown(f"«{d['문서']}» — {d['글']}")
    elif not it.get("인용"):
        c2.caption("(인용 없음)")

st.divider()
prev = done.get(r["run_id"], {})
with st.form(f"f{k}"):
    결정 = st.radio("판정", ["그대로 발송해도 됨", "사람이 봤어야 함"], horizontal=True,
                  index=None if not prev else (1 if prev["should_stop"] else 0))
    문제 = st.multiselect("문제 문장 번호 (선택)", list(range(1, len(sents) + 1)), default=prev.get("문제문장", []))
    메모 = st.text_input("메모 (선택 — 왜 그렇게 판정했는지 한 줄)", value=prev.get("메모", ""))
    if st.form_submit_button("저장하고 다음", type="primary"):
        if 결정 is None:
            st.error("판정을 골라 주세요")
        else:
            J.LABELS.mkdir(parents=True, exist_ok=True)
            with HUMAN.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"run_id": r["run_id"], "qid": r["qid"], "should_stop": 결정 == "사람이 봤어야 함",
                                    "문제문장": 문제, "문제문장글": [sents[i - 1] for i in 문제], "메모": 메모,
                                    "시각": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")
            남은 = [i for i, x in enumerate(rows) if x["run_id"] not in done and i != k]
            st.session_state["_goto"] = next((i for i in 남은 if i > k), 남은[0] if 남은 else k)
            st.rerun()
