"""웹 데모 — 사람이 승인하는 SME 리서치 보고서 발송.

    python -m streamlit run app.py

화면 설계 목표: 담당자가 한 건을 10초 안에 판단한다 (학습자료 섹션 4).
맨 위에 질문 · 멈춘 이유 · 통과시키면 일어나는 일 → AI 검수가 짚은 문장과 인용 원문 → 답 네 가지.
딥리서치 내부 지표 · 조사 로그 · 토큰 수 · 통과한 문장의 판정은 판단에 쓰이지 않아 보여 주지 않는다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import streamlit as st

import criteria as C
import graph as G
import hitl as H
import judge as J
import mailer

st.set_page_config(page_title="보고서 발송 승인", layout="wide")

기준설명 = {
    "C2": ("인용 원문에 없는 숫자", "틀린 수치가 출처를 달고 고객에게 나가는 것"),
    "C3": ("지표 경보", "지어낸 제목 · 읽지 않은 문서를 인용하는 것"),
    "C6": ("국내 질문에 해외 근거", "해외 기준이 한국 기준처럼 읽히는 것"),
    "C8": ("AI 검수 오류 문장 4개 이상", "인용 문서가 뒷받침하지 않는 주장 (가장 흔한 오류)"),
}
판정색 = {"근거없음": "orange", "모순": "red", "대상착오": "violet"}


def _시간(iso: str) -> str:
    h = (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    return f"{h:.0f}시간" if h >= 1 else f"{h * 60:.0f}분"


def _안전(t: str) -> str:
    """보고서 글을 Streamlit 색 표시 문법 안에 넣을 때 깨지지 않게."""
    return t.replace("[", "［").replace("]", "］").replace("$", "＄")


def _메일모드() -> str:
    return mailer.모드()


# ── 사이드바: 운영 ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("운영")
    공개 = mailer._env().get("DEMO_PUBLIC") == "1"
    st.markdown(f"**발송 설정**: `{_메일모드()}`" + (" (공개 모드 — 실제 발송 꺼짐)" if 공개 else ""))
    st.caption("smtp 설정이어도 화면에서 「실제 메일로 보내기」 를 켠 건만 실제로 나갑니다. 수신자는 허용 목록 주소만.")
    st.markdown(f"**OpenAI 키**: {'있음' if G.파일키() else '없음'} (새 의뢰 · 다시 판정에만 필요)")
    st.divider()
    st.subheader("3일 규칙")
    st.caption("72시간 넘게 기다린 건은 자동 승인·반려 없이 상급 담당자에게 넘기고 고객에게 지연 안내를 남깁니다.")
    days = st.number_input("시연: 며칠 뒤라고 치고 실행", 0, 10, 0)
    if st.button("대기 건 점검 (sweep)"):
        done = H.sweep(datetime.now() + timedelta(days=int(days)))
        if done:
            st.success(f"이관 {len(done)}건")
        else:
            st.info("이관할 건 없음")
    st.divider()
    if st.button("시연 초기화", help="대기 건 저장소와 발송·알림·이관 기록(output/)을 지웁니다"):
        st.session_state["초기화확인"] = True
    if st.session_state.get("초기화확인"):
        st.warning("정말 지울까요? 되돌릴 수 없습니다.")
        c1, c2 = st.columns(2)
        if c1.button("지우기", type="primary"):
            H.초기화()
            st.session_state.pop("초기화확인")
            st.rerun()
        if c2.button("취소"):
            st.session_state.pop("초기화확인")
            st.rerun()

st.title("SME 리서치 보고서 — 발송 승인")
대기 = H.대기목록()
끝 = H.끝난목록()
자동 = sum(1 for x in 끝 if x["결과"] == "자동 발송")
st.caption(f"대기 {len(대기)}건 · 처리 완료 {len(끝)}건 (자동 발송 {자동} · 사람 처리 {len(끝) - 자동}) · "
           f"대기 건은 {H.DB.relative_to(G.ROOT)} 에 저장되어 앱을 껐다 켜도 남습니다")

탭 = st.tabs([f"① 승인 대기함 ({len(대기)})", "② 새 의뢰", f"③ 처리 기록 ({len(끝)})", "④ 기준 설명"])

# ── ① 승인 대기함 ─────────────────────────────────────────────────────────────────
with 탭[0]:
    if not 대기:
        st.info("대기 중인 건이 없습니다. 「② 새 의뢰」 에서 지난 기록을 불러오거나 새로 의뢰하세요.")
    else:
        rows = [{"의뢰ID": v["의뢰ID"], "고객": v["고객"], "질문": v["질문"][:28] + "…",
                 "멈춘 이유": " · ".join(x.split(" ")[0] for x in v["멈춘 이유"]),
                 "오류 문장": len(v["검수 오류 문장"]), "대기": _시간(v["대기시작"]),
                 "재조사": v["재조사"], "이관": "⚠ 이관" if v["이관"] else ""} for v in 대기]
        st.dataframe(rows, hide_index=True)
        ids = [v["의뢰ID"] for v in 대기]
        if st.session_state.get("선택") not in ids:
            st.session_state["선택"] = ids[0]
        tid = st.selectbox("열어 볼 건", ids, key="선택",
                           format_func=lambda i: next(f"{v['의뢰ID']} · {v['고객']} · {v['질문'][:30]}…" for v in 대기
                                                      if v["의뢰ID"] == i))
        v = next(x for x in 대기 if x["의뢰ID"] == tid)
        보고서 = H.상태(tid)["보고서"]

        st.divider()
        # 맨 위 세 줄 — 10초 판단의 재료
        st.markdown(f"#### 질문: {v['질문']}")
        st.markdown(f"**고객** {v['고객']} · **대기** {_시간(v['대기시작'])} · **재조사** {v['재조사']}"
                    + (f" · :red[**⚠ 상급자 이관됨 ({v['이관']['대기시간']} 대기)**]" if v["이관"] else ""))
        st.markdown("**멈춘 이유** " + "  ".join(f":orange-background[{_안전(x)}]" for x in v["멈춘 이유"]))
        st.info(f"**통과시키면** — {v['통과시키면']}")

        bad = v["검수 오류 문장"]
        st.markdown(f"##### AI 검수가 짚은 문장 {len(bad)}개")
        st.caption("문장마다 인용한 원문 대목과 대조한 결과입니다. 대목에 없다고 곧 틀린 것은 아니지만, 고객에게 근거를 댈 수 없는 문장입니다.")
        for x in bad:
            col = 판정색.get(x["판정"], "gray")
            with st.expander(f"[{x['번호']}] {x['판정']} — {x['이유']}  ·  {x['문장'][:70]}…"):
                st.markdown(f":{col}-background[{_안전(x['문장'])}]")
                if not x["대목"]:
                    st.caption("(인용 없음)")
                for d in x["대목"]:
                    st.markdown(f"> «{d['문서']}» {d['글']}")
        other = [x for x in v["멈춘 이유"] if not x.startswith("C8")]
        if other:
            hits = C.전부재기(보고서)
            with st.expander("C2 · C3 · C6 에 걸린 내용"):
                for k in ("C2", "C3", "C6"):
                    for h in hits[k]:
                        st.markdown(f"- **{k}** " + (_안전(f"{h['문장']} ← 원문에 없는 숫자 {h['없는숫자']}")
                                                     if isinstance(h, dict) else _안전(str(h))))

        with st.expander(f"보고서 전체 ({v['보고서자수']:,}자) — 짚은 문장은 색으로 표시"):
            글 = 보고서["report"]
            for x in bad:
                글 = 글.replace(x["문장"], f":{판정색.get(x['판정'], 'gray')}-background[{_안전(x['문장'])}]")
            st.markdown(글)

        st.divider()
        st.markdown("##### 답")
        실제 = st.checkbox("실제 메일로 보내기", value=False, disabled=_메일모드() != "smtp",
                         help="끄면 이 건은 파일(output/outbox/)로만 냅니다. smtp 설정이 아니면 켤 수 없습니다.")
        빠진본 = H.빼기(보고서["report"], [x["문장"] for x in bad])
        a1, a2, a3, a4 = st.tabs(["승인", "수정 후 승인", "반려", "다시 판정"])

        def 보내기(결정, **kw):
            try:
                r = H.답하기(tid, 결정, 파일만=not 실제, **kw)
            except ValueError as e:
                st.error(str(e))
                return
            st.session_state["방금"] = f"{tid} → {r['결과'] or '다시 대기'}" + \
                (f" ({r['발송']['모드']} · {r['발송']['상태']})" if r.get("발송") else "")
            st.session_state.pop("선택", None)
            st.rerun()

        with a1:
            st.caption(f"그대로 발송합니다 — {v['보고서자수']:,}자, AI 검수가 짚은 문장 {len(bad)}개가 포함된 채로.")
            if st.button("승인하고 발송", type="primary", key="ok"):
                보내기("approve")
        with a2:
            방식 = st.radio("방식", ["AI 검수가 짚은 문장 빼고 발송", "직접 수정"], horizontal=True)
            if 방식.startswith("AI"):
                st.caption(f"{len(bad)}문장을 빼고 {len(빠진본):,}자로 발송합니다 (원본 {v['보고서자수']:,}자).")
                with st.expander("발송본 미리보기"):
                    st.markdown(빠진본)
                if st.button("빼고 발송", type="primary", key="edit1"):
                    보내기("edit", 방식="오류문장빼기")
            else:
                본 = st.text_area("발송할 본문", 보고서["report"], height=300)
                st.caption("고친 본문을 그대로 발송합니다. 바뀐 줄 수가 기록에 남습니다.")
                if st.button("고친 본문 발송", type="primary", key="edit2"):
                    보내기("edit", 수정본=본)
        with a3:
            사유 = st.text_input("반려 사유 (필수)", placeholder="예: 한국 기준 수치의 출처가 없음 — 직접 재작성")
            st.caption("발송하지 않고 「담당자 직접 처리」 목록으로 넘깁니다. 사유는 기준을 고칠 재료가 됩니다.")
            if st.button("반려", key="rej"):
                보내기("reject", 사유=사유)
        with a4:
            가능 = "redo" in v["가능한 답"]
            지시 = st.text_input("AI 에게 줄 지시 (필수)", placeholder="예: 한국 기준은 「중소기업」 문서로만 다시",
                               disabled=not 가능)
            st.caption(("지시를 붙여 실제로 다시 조사 → 검수 → 판정합니다 (키 필요, 약 3~5센트, 30~60초). "
                        "다시 걸리면 이 대기함으로 돌아옵니다.") if 가능 else
                       f"다시 판정은 {H.재조사_상한}회까지입니다 — 승인·수정·반려 중에서 골라 주세요.")
            if st.button("다시 판정", key="redo", disabled=not 가능):
                with st.spinner("다시 조사하는 중…"):
                    보내기("redo", 지시=지시)

    if msg := st.session_state.pop("방금", None):
        st.toast(msg)

# ── ② 새 의뢰 ─────────────────────────────────────────────────────────────────────
with 탭[1]:
    st.markdown("##### 지난 기록 불러오기 (키 없음 · 비용 0)")
    st.caption("4단계 검증에 쓴 표본 150건에서 질문이 골고루 섞이게 불러옵니다. 검수 결과가 캐시에 있어 API 를 부르지 않고, 발송은 항상 파일입니다.")
    n = st.slider("몇 건", 1, 150, 20)
    if st.button("불러와서 흘려 보내기"):
        있음, 결과 = set(H.전체()), {"자동 발송": 0, "대기": 0, "건너뜀": 0}
        bar = st.progress(0.0)
        qs = H.지난의뢰들(n)
        for i, q in enumerate(qs, 1):
            if q["id"] in 있음:
                결과["건너뜀"] += 1
            else:
                r = H.투입({**q, "force_file": True})
                결과["대기" if r["멈춤"] else "자동 발송"] += 1
            bar.progress(i / len(qs))
        st.success(" · ".join(f"{k} {v}" for k, v in 결과.items()))

    st.divider()
    st.markdown("##### 새 의뢰 (딥리서치를 실제로 실행)")
    키 = bool(G.파일키())
    st.caption("조사 약 1.5센트 + 검수 약 2센트, 30~60초. " + ("" if 키 else "⚠ 이 PC 에 OpenAI 키가 없어 실행할 수 없습니다."))
    with st.form("new"):
        qopt = st.selectbox("질문", ["(직접 입력)"] + [f"{k} · {q['질문']}" for k, q in G.QUESTIONS.items()], index=1)
        직접 = st.text_input("직접 입력한 질문", disabled=False)
        고객 = st.text_input("고객", "가상고객 Z (시연)")
        허용 = mailer.허용목록()
        수신자 = st.text_input("수신자", (허용 or [H.DEMO_TO])[0],
                            help="실제 메일은 허용 목록 주소로만 나갑니다. 다른 주소는 막히고 기록만 남습니다.")
        실제새 = st.checkbox("자동 발송될 때 실제 메일로 보내기", value=False, disabled=_메일모드() != "smtp")
        go = st.form_submit_button("의뢰하기", type="primary", disabled=not 키)
    if go:
        if qopt == "(직접 입력)":
            qd = {"id": None, "질문": 직접.strip()}
        else:
            qd = G.QUESTIONS[qopt.split(" · ")[0]]
        if not qd["질문"]:
            st.error("질문을 입력하세요")
        else:
            with st.spinner("조사 → 검수 → 판정 중…"):
                r = H.투입({"고객": 고객, "수신자": 수신자, "질문": qd["질문"], "qid": qd["id"], "입구": "새 의뢰",
                          "force_file": not 실제새})
            if r["멈춤"]:
                st.warning(f"{r['id']} — 멈춤: {'; '.join(r['화면']['멈춘 이유'])} → 「① 승인 대기함」 에서 확인하세요")
            else:
                st.success(f"{r['id']} — {r['결과']} ({r['발송']['모드']} · {r['발송']['상태']})")

# ── ③ 처리 기록 ───────────────────────────────────────────────────────────────────
with 탭[2]:
    def _jsonl(p):
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    st.markdown("##### 끝난 건")
    st.dataframe([{"의뢰ID": x["의뢰ID"], "고객": x["고객"], "결과": x["결과"], "재조사": x["재조사횟수"],
                   "수정": (f"{x['수정']['방식']} · 바뀐 줄 {x['수정']['바뀐줄']}" if x["수정"] else ""),
                   "발송": (f"{x['발송']['모드']} · {x['발송']['상태']}" if x["발송"] else ""),
                   "멈춘 이유": " · ".join(y.split(" ")[0] for y in x["멈춘이유"]) or "(자동)",
                   "담당자 답": " → ".join(e.get("결정", e.get("사건", "")) for e in x["이력"])}
                  for x in 끝], hide_index=True)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### 담당자 직접 처리 (반려)")
        st.dataframe([{k: x[k] for k in ("thread_id", "고객", "사유", "시각")} for x in _jsonl(H.MANUAL)],
                     hide_index=True)
        st.markdown("##### 상급자 이관 (3일 규칙)")
        st.dataframe(_jsonl(H.ESCALATIONS), hide_index=True)
    with c2:
        st.markdown("##### 발송함")
        st.dataframe([{k: x.get(k, "") for k in ("thread_id", "고객", "수신자", "모드", "상태", "사유", "자수", "시각")}
                      for x in mailer.발송기록()], hide_index=True)
        st.markdown("##### 고객 안내")
        st.dataframe([{k: x.get(k, "") for k in ("thread_id", "종류", "내용", "시각")} for x in _jsonl(H.NOTICES)],
                     hide_index=True)

# ── ④ 기준 설명 ───────────────────────────────────────────────────────────────────
with 탭[3]:
    st.markdown("##### 어떤 건에서 멈추는가 — 켜진 기준 " + " · ".join(C.켜진기준))
    st.table([{"기준": f"{k} {기준설명[k][0]}", "막으려는 위험": 기준설명[k][1]} for k in C.켜진기준])
    st.markdown("""
- **C2 · C3 · C6** 은 코드로 즉시 계산한다. 걸리면 거의 확실히 진짜 문제다 (4단계 검증에서 헛멈춤 ≈ 0) — 싸고 빠른 1차 관문.
- **C8** 은 문장마다 인용 원문 대목과 대조하는 AI 검수(gpt-4o)다. 가장 흔한 오류인 '인용한 문서가 그 말을 하지 않는 문장' 은 이렇게 읽어야만 잡힌다.
- 검수에 실패하면(키 없음 · 한도 초과) 통과가 아니라 사람에게 보낸다.

**검증 (4단계, 표본 150건 · 사람 판정 30건)**
- 개입률 **86%** (150건 중 129건 멈춤, 21건 자동 발송) · 사람 판정 기준 놓침 **2/25** · 헛멈춤 3/5
- 놓침을 막는 쪽을 골랐다. 틀린 보고서가 고객에게 가면 되돌릴 수 없고, 헛멈춤은 검토 시간만 든다.
- 개입률이 높은 이유는 기준이 아니라 **생성기(딥리서처)** 에 있다 — 문장의 13% 가 근거없음·모순·대상착오였다. 그래서 개입률 대신 **건당 검토 시간**을 줄이는 쪽(짚은 문장부터 보여 주기 · 빼고 발송 버튼)으로 보완했다.

**자동으로 나가는 건은 왜 괜찮은가** — C2 · C3 · C6 에 걸리지 않고 AI 검수 오류 문장이 3개 이하인 보고서다. 사람 판정 30건에서 이 조건을 통과한 건 중 사람이 멈췄어야 한 건은 2건이었다(놓침). 0 은 아니다 — 한계로 REPORT 에 적었다.

자세한 표: `docs/evidence/step4_validation.md`
""")
