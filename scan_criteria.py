"""3단계 — 후보 기준 7개를 지난 실행 기록 297건에 걸어 본다 (키 불필요).

    python scan_criteria.py            # → docs/evidence/step3_scan.md
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import criteria as C
from graph import ROOT

PAST = ROOT / "data" / "past_runs"
OUT = ROOT / "docs" / "evidence" / "step3_scan.md"


def 불러오기() -> list[dict]:
    rows = []
    for f in ("ablation_runs.jsonl", "ablation2_runs.jsonl"):
        rows += [json.loads(l) for l in (PAST / f).read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows


def _짧게(x, n=160) -> str:
    s = x["문장"] + f"  ← 없는 숫자 {x['없는숫자']} · 인용 {x['인용']}" if isinstance(x, dict) else str(x)
    return s if len(s) <= n else s[:n] + "…"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    rows = 불러오기()
    hits = [(r, C.전부재기(r)) for r in rows]
    팀 = [h for h in hits if h[0]["sections"]]
    L = [f"# 3단계 — 후보 기준 7개를 지난 기록 {len(rows)}건에 걸어 본 결과", "",
         "키 없이 `python scan_criteria.py` 로 다시 만든다. 조건별 기록 수: "
         + ", ".join(f"{k} {v}" for k, v in Counter(r["라벨"] for r in rows).items()), "",
         "## 0. 걸어 보며 고친 것", "",
         "- **C2 단위 맞춤** — 글자 그대로 비교는 「5000만 유로」 와 원문 「50 million euro」 를 다르다고 봤고(헛멈춤), "
         "반대로 「300」 이 원문의 「300,000」 안에 들어 있다고 통과시켰다(놓침). 숫자를 단위까지 읽어 값으로 비교하도록 바꿨다. "
         "「5천만」 처럼 겹친 한국어 단위도 읽는다.",
         "- **이름 속 숫자 제외** — 「Industry 4.0」 · 「4차 산업혁명」 의 숫자는 수치가 아니라 이름이라 C1·C2 에서 뺐다.",
         "- **C6 계산식 변경** — 처음엔 '인용 문서 중 한국어 문서 비율 < 50%' 였는데, 국내 질문(S3) 27건이 모두 "
         "한·영 문서 1건씩(정확히 50%)이라 한 번도 걸리지 않았다. 문장 단위로 바꿨다: 숫자가 든 문장이 해외 문서만 인용하고 "
         "다른 나라 이름도 없으면(한국 이야기로 읽히면) 걸린다.",
         "- **남은 한계** — C2 는 값이 원문 어딘가에 있기만 하면 통과한다. 「2011년 … 클라우스 슈밥이 처음 제안」 처럼 "
         "연도는 맞고 주장이 틀린 문장은 못 잡는다.", "",
         "## 1. 기준별로 걸린 건수", "",
         "| 기준 | 걸린 기록 | 비율 | 팀 기록만 (절이 있는 것) |", "|---|---:|---:|---:|"]
    for k, (name, _) in C.후보.items():
        n = sum(1 for _, h in hits if h[k])
        nt = sum(1 for _, h in 팀 if h[k])
        L.append(f"| {k} {name} | {n} | {100*n/len(rows):.1f}% | {nt}/{len(팀)} |")

    L += ["", "## 2. 한 기록에 걸린 기준 수", "",
          "| 걸린 기준 수 | 기록 수 |", "|---:|---:|"]
    for c, n in sorted(Counter(sum(1 for v in h.values() if v) for _, h in hits).items()):
        L.append(f"| {c} | {n} |")

    L += ["", "## 3. 기준끼리 겹침 (둘 다 걸린 기록 수)", "",
          "| | " + " | ".join(C.후보) + " |", "|---|" + "---:|" * len(C.후보)]
    for a in C.후보:
        L.append(f"| {a} | " + " | ".join(str(sum(1 for _, h in hits if h[a] and h[b])) for b in C.후보) + " |")

    L += ["", "## 4. C4 근거율 문턱을 바꾸면", "", "| 문턱 | 걸린 기록 |", "|---:|---:|"]
    for t in (50, 60, 70, 80):
        L.append(f"| {t}% | {sum(1 for r in rows if C.C4_근거율미달(r, t))} |")

    L += ["", "## 5. C2 단위 맞춤 전후", "",
          "2단계의 글자 그대로 비교는 「5000만 유로」 와 원문 「50 million euro」 를 다르다고 봤다(헛멈춤)."]
    import re
    def 글자비교(r):
        n = 0
        for s in C._본문(r["report"]):
            cites = C.CITE.findall(s)
            nums = [m.strip().rstrip("%").rstrip(".,") for m in re.findall(r"\d[\d,.]*\s*%?", C.CITE.sub("", s))]
            nums = [x for x in nums if len(x.replace(",", "").replace(".", "")) >= 2]
            if cites and nums and any(x not in " ".join(C.DOCS.get(c, "") for c in cites) for x in nums):
                n += 1
        return n
    전 = sum(1 for r in rows if 글자비교(r))
    후 = sum(1 for _, h in hits if h["C2"])
    L += [f"- 글자 그대로 비교: {전}건 → 단위 맞춤: {후}건", ""]

    L += ["## 6. 기준마다 실제로 걸린 예 (최대 3건)", ""]
    for k, (name, _) in C.후보.items():
        L.append(f"### {k} {name}")
        ex = [(r, h[k]) for r, h in hits if h[k]][:3]
        if not ex:
            L.append("- (걸린 기록 없음)")
        for r, h in ex:
            L.append(f"- **{r['qid']} · {r['라벨']} · {r['run_id']}** — {r['질문'][:50]}")
            for x in h[:2]:
                L.append(f"  - {_짧게(x)}")
        L.append("")

    L += ["## 7. 질문별 C6 · C7", "", "| qid | 질문 | C6 걸림 | C7 걸림 | C7 주제어(가장 흔한 것) |", "|---|---|---:|---:|---|"]
    by_q: dict[str, list] = {}
    for r, h in hits:
        by_q.setdefault(r["qid"], []).append((r, h))
    for q, xs in sorted(by_q.items()):
        words = Counter(w for _, h in xs for w in h["C7"]).most_common(3)
        L.append(f"| {q} | {xs[0][0]['질문'][:34]}… | {sum(1 for _, h in xs if h['C6'])}/{len(xs)} | "
                 f"{sum(1 for _, h in xs if h['C7'])}/{len(xs)} | {', '.join(f'{w} {n}' for w, n in words)} |")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
