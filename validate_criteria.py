"""4단계 — 정답과 기준 비교 (키 불필요).

    python validate_criteria.py      → docs/evidence/step4_validation.md

① 사람 30건 대 AI 판정(엄격 · 실무) 일치율 → 정답으로 쓸 정의를 고른다
② 그 정답으로, 표본 150건에서 기준 조합마다 개입률 · 놓침 · 헛멈춤
"""
from __future__ import annotations

import itertools
import json
import sys

import criteria as C
import judge as J

OUT = J.G.ROOT / "docs" / "evidence" / "step4_validation.md"


def _jsonl(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def 일치(a: list[bool], b: list[bool]) -> dict:
    tp = sum(x and y for x, y in zip(a, b))
    tn = sum((not x) and (not y) for x, y in zip(a, b))
    n = len(a)
    po = (tp + tn) / n if n else 0
    pa, pb = sum(a) / n if n else 0, sum(b) / n if n else 0
    pe = pa * pb + (1 - pa) * (1 - pb)
    return {"n": n, "일치율": round(100 * po, 1), "kappa": round((po - pe) / (1 - pe), 2) if pe < 1 else None,
            "둘다멈춤": tp, "둘다통과": tn, "사람만멈춤": sum((not x) and y for x, y in zip(a, b)),
            "AI만멈춤": sum(x and (not y) for x, y in zip(a, b))}


def 측정(rows, 정답: dict[str, bool], 걸림: dict[str, dict], combo: tuple[str, ...]) -> dict:
    stop = [any(걸림[r["run_id"]][k] for k in combo) for r in rows]
    truth = [정답[r["run_id"]] for r in rows]
    n = len(rows)
    return {"기준": "+".join(combo) or "없음(전부 자동)", "멈춤": sum(stop), "개입률": round(100 * sum(stop) / n, 1),
            "놓침": sum(t and not s for s, t in zip(stop, truth)),
            "헛멈춤": sum(s and not t for s, t in zip(stop, truth)),
            "잡음": sum(s and t for s, t in zip(stop, truth))}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    llm = {v["run_id"]: v for v in _jsonl(J.LABELS / "llm.jsonl")}
    human = {v["run_id"]: v for v in _jsonl(J.LABELS / "human.jsonl")}
    rows = [r for r in J.표본() if r["run_id"] in llm]
    for r in rows:
        r["검수"] = J.검수(r["질문"], r["report"])          # 캐시에서 꺼낸다 — API 호출 없음 (C8 계산용)
    L = [f"# 4단계 — 정답 라벨과 승인 기준 검증", "",
         f"표본 {len(rows)}건 (297건에서 질문별 층화, 시드 {J.시드}) · AI 판정 {J.JUDGE_MODEL} · 사람 판정 {len(human)}건", ""]

    # ① 일치율
    both = [i for i in human if i in llm]
    L += ["## 1. 사람 판정 대 AI 판정", "", f"둘 다 있는 보고서 {len(both)}건.", "",
          "| AI 정의 | 일치율 | 카파 | 둘 다 멈춤 | 둘 다 통과 | 사람만 멈춤 | AI만 멈춤 |", "|---|---:|---:|---:|---:|---:|---:|"]
    agree = {}
    for k in ("엄격", "실무"):
        a = 일치([llm[i][k] for i in both], [human[i]["should_stop"] for i in both])
        agree[k] = a
        L.append(f"| {k} | {a['일치율']}% | {a['kappa']} | {a['둘다멈춤']} | {a['둘다통과']} | {a['사람만멈춤']} | {a['AI만멈춤']} |")
    pick = max(agree, key=lambda k: (agree[k]["일치율"], agree[k]["kappa"] or -1)) if both else "실무"
    L += ["", f"→ 사람 판정과 더 맞는 **{pick}** 정의를 정답으로 쓴다. 사람이 판정한 건은 사람 판정을 정답으로 덮어쓴다.", ""]

    # 어긋난 건 — 사람 메모와 AI 오류 문장
    diff = [i for i in both if llm[i][pick] != human[i]["should_stop"]]
    if diff:
        L += [f"### 어긋난 {len(diff)}건", ""]
        for i in diff:
            h, a = human[i], llm[i]
            L.append(f"- **{a['qid']} · {i}** — 사람 {'멈춤' if h['should_stop'] else '통과'} / AI({pick}) "
                     f"{'멈춤' if a[pick] else '통과'}" + (f" · 사람 메모: {h['메모']}" if h.get("메모") else ""))
            for b in [b for b in a["오류문장"] if pick == "엄격" or b["구체"]][:2]:
                L.append(f"  - AI: {b['판정']} ({b['이유']}) — {b['문장'][:90]}")
            for s in h.get("문제문장글", [])[:2]:
                L.append(f"  - 사람이 짚은 문장: {s[:90]}")
        L.append("")

    정답 = {r["run_id"]: (human[r["run_id"]]["should_stop"] if r["run_id"] in human else llm[r["run_id"]][pick])
          for r in rows}
    n_true = sum(정답.values())
    걸림 = {r["run_id"]: {k: bool(v) for k, v in C.전부재기(r).items()} for r in rows}

    # ② 비교표
    L += [f"## 2. 기준별 개입률 · 놓침 · 헛멈춤 (정답: 사람이 봤어야 함 {n_true}/{len(rows)}건)", "",
          "- 개입률: 사람에게 온 비율 · 놓침: 봤어야 하는데 자동으로 나간 건 · 헛멈춤: 볼 필요 없는데 사람에게 온 건", "",
          "### 2-1. 기준 하나씩", "", "| 기준 | 멈춤 | 개입률 | 잡음 | 놓침 | 헛멈춤 |", "|---|---:|---:|---:|---:|---:|"]
    코드 = [k for k in C.후보 if k != "C8"]          # C8 은 AI 판정 정답과 같은 판정이라(순환) 여기서 빼고 4절에서 사람 판정으로 잰다
    singles = [()] + [(k,) for k in 코드] + [tuple(코드)]
    for combo in singles:
        m = 측정(rows, 정답, 걸림, combo)
        L.append(f"| {m['기준']} | {m['멈춤']} | {m['개입률']}% | {m['잡음']} | {m['놓침']} | {m['헛멈춤']} |")

    # 모든 조합 중 파레토(놓침·헛멈춤 둘 다 더 나은 조합이 없는 것)
    allm = [측정(rows, 정답, 걸림, c) for n in range(0, len(코드) + 1) for c in itertools.combinations(코드, n)]
    pareto = [m for m in allm if not any(o["놓침"] <= m["놓침"] and o["헛멈춤"] <= m["헛멈춤"]
                                          and (o["놓침"], o["헛멈춤"]) != (m["놓침"], m["헛멈춤"]) for o in allm)]
    seen, front = set(), []
    for m in sorted(pareto, key=lambda m: (m["놓침"], m["헛멈춤"], len(m["기준"]))):
        if (m["놓침"], m["헛멈춤"]) not in seen:
            seen.add((m["놓침"], m["헛멈춤"]))
            front.append(m)
    L += ["", f"### 2-2. 조합 {len(allm)}개 중 더 나은 대안이 없는 조합 (놓침↑ 헛멈춤↓ 순)", "",
          "같은 놓침·헛멈춤이면 기준이 적은 조합 하나만 적었다.", "",
          "| 조합 | 멈춤 | 개입률 | 놓침 | 헛멈춤 |", "|---|---:|---:|---:|---:|"]
    for m in front:
        L.append(f"| {m['기준']} | {m['멈춤']} | {m['개입률']}% | {m['놓침']} | {m['헛멈춤']} |")

    # 놓친 건의 AI 오류 종류 — 어떤 오류를 기준이 못 잡는가
    L += ["", "## 3. 기준 전부를 켜도 놓치는 건", ""]
    for r in rows:
        if 정답[r["run_id"]] and not any(걸림[r["run_id"]].values()):
            errs = [b for b in llm[r["run_id"]]["오류문장"] if pick == "엄격" or b["구체"]][:2]
            L.append(f"- **{r['qid']} · {r['라벨']} · {r['run_id']}**")
            for b in errs:
                L.append(f"  - {b['판정']} ({b['이유']}) — {b['문장'][:100]}")

    # ④ C8 을 더한 최종 기준 — 사람 판정 30건으로만 잰다 (AI 판정 정답과 C8 은 같은 판정이라 순환)
    hs = [r for r in rows if r["run_id"] in human]
    code = lambda r: any(C.전부재기(r)[k] for k in ("C2", "C3", "C6"))
    n_stop = sum(human[r["run_id"]]["should_stop"] for r in hs)
    L += ["", "## 4. C8 (AI 검수) 문턱 — 사람 판정 30건 기준", "",
          f"사람 판정: 멈춤 {n_stop} · 통과 {len(hs) - n_stop}. 정답이 AI 판정이면 C8 이 자기 자신과 비교되므로(순환) "
          "여기서는 사람 판정만 정답으로 쓴다. 사람 판정은 AI 검토 도구를 참고했다(AI 보조 사람 판정).", "",
          "| 기준 (C2·C3·C6 과 함께) | 사람 기준 놓침 | 사람 기준 헛멈춤 | 표본 150건 개입률 |", "|---|---:|---:|---:|"]
    for k in (1, 2, 3, 4, 5, 6, 8, 10):
        stop = lambda r: code(r) or bool(C.C8_AI검수(r, k))
        fn = sum(1 for r in hs if human[r["run_id"]]["should_stop"] and not stop(r))
        fp = sum(1 for r in hs if not human[r["run_id"]]["should_stop"] and stop(r))
        L.append(f"| C8 오류 ≥ {k}{' ← 채택' if k == C.C8_문턱 else ''} | {fn}/{n_stop} | {fp}/{len(hs) - n_stop} | "
                 f"{100 * sum(1 for r in rows if stop(r)) / len(rows):.0f}% |")
    fin = [r for r in rows if C.판정(r)]
    L += ["", f"**최종 기준 {' · '.join(C.켜진기준)}** — 표본 150건 중 {len(fin)}건 멈춤 (개입률 {100*len(fin)/len(rows):.0f}%), "
          f"자동 발송 {len(rows) - len(fin)}건.", "",
          "- 놓침을 막는 쪽을 골랐다(4단계 결정 A): 틀린 보고서가 고객에게 가면 되돌릴 수 없고, 헛멈춤은 검토 시간만 든다.",
          "- 사람 판정 🔴(중요 수정) 문장 75개 중 AI 검수가 잡은 것은 22개(29%) — AI 검수는 '인용 원문에 있는가' 를 보고, "
          "사람(과 참고한 AI 검토)은 배경지식으로 사실 자체를 봤다. 기준이 못 잡는 오류가 남는다는 뜻이다.", ""]

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
