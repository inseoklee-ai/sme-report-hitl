"""지표 — 정답표도 판정 모델도 쓰지 않는다. 완성된 보고서 문자열과 실제로 읽은 문서만 본다.

    python metrics.py                     # output/runs.jsonl 전체를 다시 잰다
    python metrics.py output/dev/x.jsonl  # 다른 기록 파일
    python metrics.py --test              # 문장 분리·인용 규칙 점검 (규칙이 바뀌면 여기서 깨진다)

저장된 기록만으로 다시 계산한다 — 키가 필요 없고, 같은 기록이면 늘 같은 값이 나온다.
문장 분리는 graph.문장들 하나만 쓴다(팀·대조군·지표가 같은 규칙).

┌ 신호 (높낮이를 견준다) ───────────────────────────────────────────────────────┐
│ 근거율        인용이 붙은 문장 ÷ 전체 문장 (# 머리글 줄은 분모에서 뺀다)   ← 집필·예산·재위임 │
│ 인용편중      가장 많이 인용된 문서 ÷ 전체 인용                            ← 배정·구역      │
│ 중복읽기율    두 절 이상이 읽은 문서 ÷ 읽은 문서 (팀 전용)                 ← 구역           │
│ 읽고안쓴비율  읽었지만 인용하지 않은 문서 ÷ 읽은 문서                      ← 탐색·배정 품질 │
│ 격리율        코디네이터·편집자가 본 글자 ÷ 전체가 본 글자                 ← 격리 설계      │
├ 경보 (0 이어야 한다) ────────────────────────────────────────────────────────┤
│ 지어낸제목인용  코퍼스에 없는 제목을 인용                                  ← 집필           │
│ 안읽은문서인용  읽은 적 없는 문서를 인용                                   ← 집필           │
│ 절밖인용        그 절 조사관이 읽지 않은 문서를 그 절에서 인용 (팀 전용)   ← 격리·집필      │
│ 예산미사용      읽은 수가 배정 예산보다 적음                               ← 탐색·대조군 공정성 │
│ 고르기대체      모델이 두 번 다 엉뚱한 문서를 골라 후보 첫 번째로 바뀜     ← 탐색           │
│ 집필형식문제    문장별 구조 실패 + 인용 형식을 코드가 고친 곳              ← 집필 파이프라인 │
│ 빈절·입력잘림   본문 100자 미만인 절 + 6만 자 상한에서 잘린 읽기           ← 파이프라인     │
└──────────────────────────────────────────────────────────────────────────────┘
참고(순위에 쓰지 않는다): 보고서 글자 수 · 인용된 문서 수 · LLM 호출 · 토큰 · 시간
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from graph import DOCS, OUT, 문장들, 인용들

신호 = ("근거율", "인용편중", "중복읽기율", "읽고안쓴비율", "격리율")
경보 = ("지어낸제목인용", "안읽은문서인용", "절밖인용", "예산미사용", "고르기대체", "집필형식문제", "빈절·입력잘림")


def 본문문장(report: str) -> list[str]:
    """# 로 시작하는 줄(보고서 제목·절 머리글)은 문장이 아니라 이름표라 뺀다. 머리말·맺음말은 남긴다 —
    편집자가 쓴 근거 없는 문장도 독자가 읽는 산출물이다."""
    return 문장들("\n".join(l for l in report.split("\n") if not l.lstrip().startswith("#")))


def _비율(a: int, b: int) -> float | None:
    return round(100 * a / b, 1) if b else None


def 채택된절(run: dict) -> list[dict]:
    """재위임본까지 쌓인 원고 중 최종 채택본만. 대조군 기록에는 절이 없다."""
    pick = {int(k): v for k, v in (run.get("채택") or {}).items()}
    return [s for s in run.get("sections", []) if pick.get(s["번호"]) == s.get("바퀴", 1)]


def 재기(run: dict) -> dict:
    """실행 기록 하나 → {"신호", "경보", "참고", "경보목록"}. 팀 기록과 대조군 기록 모두 받는다."""
    report = run.get("report", "")
    calls = run.get("calls", [])
    팀 = bool(run.get("sections"))

    # 읽은 것 — visited 는 (절 번호, 문서). 대조군은 절 번호 자리에 "(혼자)"
    읽기 = [(str(i), d) for i, d in run.get("visited", [])]
    읽은 = {d for _, d in 읽기}

    # 인용 — 보고서 문자열에서 다시 뽑는다(절 기록을 믿지 않고 최종 산출물 기준)
    문장 = 본문문장(report)
    근거문장 = [x for x in 문장 if any(c in DOCS for c in 인용들(x))]
    모든인용 = 인용들(report)
    유효 = [c for c in 모든인용 if c in DOCS]
    쓰임 = Counter(유효)

    # 두 절 이상이 읽은 문서 (팀 전용)
    누가읽음: dict[str, set] = {}
    for i, d in 읽기:
        누가읽음.setdefault(d, set()).add(i)
    겹침 = [d for d, who in 누가읽음.items() if len(who) > 1]

    # 절 밖 인용 (팀 전용): 채택된 절의 인용 중 그 절이 읽지 않은 코퍼스 문서
    절밖 = []
    for s in 채택된절(run):
        mine = set(s.get("읽은문서", []))
        절밖 += [f"{s['절']}→{c}" for c in dict.fromkeys(인용들(s.get("본문", ""))) if c in DOCS and c not in mine]

    # 예산 미사용: 팀은 조사관 파견마다 (배정 예산 - 새로 읽은 수), 대조군은 (예산 - 읽은 수)
    if 팀:
        미사용 = sum(max(0, s.get("예산", 0) - len(s.get("새로읽음", []))) for s in run["sections"])
    else:
        미사용 = max(0, int(run.get("예산", 0)) - len(읽기))

    빈절 = [s["절"] for s in 채택된절(run) if len(s.get("본문", "")) < 100]
    잘림 = sum(1 for c in calls if c.get("용도") == "읽기" and c.get("잘림"))

    위 = sum(c["입력자수"] for c in calls if c["누가"] in ("코디네이터", "편집자"))
    아래 = sum(c["입력자수"] for c in calls if c["누가"] == "조사관")

    a = {"지어낸제목인용": sorted(set(모든인용) - set(DOCS)),
         "안읽은문서인용": sorted(set(유효) - 읽은),
         "절밖인용": 절밖 if 팀 else None,
         "예산미사용": 미사용,
         "고르기대체": sum(1 for c in calls if c.get("대체")),
         "집필형식문제": sum(1 for c in calls if c.get("구조실패")) + sum(c.get("인용형식교정", 0) for c in calls),
         "빈절·입력잘림": len(빈절) + 잘림}
    센경보 = {k: (len(v) if isinstance(v, list) else v) for k, v in a.items()}
    return {
        "신호": {"근거율": _비율(len(근거문장), len(문장)),
                 "인용편중": _비율(max(쓰임.values()), sum(쓰임.values())) if 쓰임 else None,
                 "중복읽기율": _비율(len(겹침), len(읽은)) if 팀 else None,
                 "읽고안쓴비율": _비율(len(읽은 - set(쓰임)), len(읽은)),
                 "격리율": _비율(위, 위 + 아래)},
        "경보": 센경보,
        "경보목록": {k: v for k, v in a.items() if isinstance(v, list) and v},
        "참고": {"문장수": len(문장), "근거문장수": len(근거문장), "보고서자수": len(report),
                 "인용수": len(유효), "인용문서수": len(쓰임), "읽은문서수": len(읽은), "읽기횟수": len(읽기),
                 "겹친문서": sorted(겹침), "읽고안쓴": sorted(읽은 - set(쓰임)),
                 "LLM호출": len(calls), "입력토큰": sum(c.get("입력토큰", 0) for c in calls),
                 "출력토큰": sum(c.get("출력토큰", 0) for c in calls), "초": run.get("초")},
    }


def 불러오기(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _칸(v) -> str:
    return "  -  " if v is None else f"{v:5.1f}"


def 표(runs: list[dict]) -> str:
    head = f"{'qid':<4} {'라벨':<14} {'근거율':>6} {'편중':>6} {'중복':>6} {'안씀':>6} {'격리':>6}  경보"
    out = [head, "-" * len(head.encode("euc-kr", "replace"))]
    for r in runs:
        m = r.get("metrics") or 재기(r)
        s = m["신호"]
        울림 = ", ".join(f"{k} {v}" for k, v in m["경보"].items() if v) or "없음"
        out.append(f"{str(r.get('qid')):<4} {str(r.get('라벨'))[:14]:<14} {_칸(s['근거율'])}  {_칸(s['인용편중'])}  "
                   f"{_칸(s['중복읽기율'])}  {_칸(s['읽고안쓴비율'])}  {_칸(s['격리율'])}   {울림}")
    return "\n".join(out)


def 점검() -> None:
    """규칙이 바뀌면 깨지는 확인 — 지표가 좋아진 줄 알았는데 문장 분리가 바뀐 사고를 막는다."""
    글 = "예지 정비는 설비 상태를 보고 정비 시점을 정한다 «Predictive maintenance». " \
         "상태 감시는 진동과 온도를 계속 잰다 «Condition monitoring».\n" \
         "## 2. 머리글 줄은 문장으로 세지 않는다 _(기술 담당)_\n" \
         "근거가 없는 편집자의 연결 문장도 분모에 들어간다."
    s = 본문문장(글)
    assert len(s) == 3, s                                   # 머리글 빼고 세 문장
    assert 인용들(s[0]) == ["Predictive maintenance"], s    # 인용이 제 문장에 붙어 있다
    assert 인용들(s[1]) == ["Condition monitoring"], s
    assert not 인용들(s[2])
    # 마침표 뒤에 인용을 두면 다음 문장으로 넘어간다 — graph.문장조립 이 마침표 앞에 두는 이유
    뒤 = 문장들("앞 문장은 여기서 끝나고 인용은 뒤에 붙었다. «Cobot» 다음 문장이 인용을 가져간다.")
    assert 인용들(뒤[0]) == [] and 인용들(뒤[1]) == ["Cobot"], 뒤
    # 지어낸 제목은 조용히 버리지 않고 경보로 드러난다
    m = 재기({"report": "없는 문서를 인용한 문장이다 «예지 정비 개론».", "visited": [], "calls": []})
    assert m["경보"]["지어낸제목인용"] == 1 and m["신호"]["근거율"] == 0.0, m
    print("점검 통과: 문장 분리 · 머리글 제외 · 인용 위치 · 지어낸 제목 경보")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="실행 기록을 다시 잰다")
    ap.add_argument("path", nargs="?", default=str(OUT / "runs.jsonl"))
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        점검()
        sys.exit(0)
    runs = 불러오기(Path(a.path))
    for r in runs:
        r["metrics"] = 재기(r)
    print(표(runs))
    for r in runs:
        if r["metrics"]["경보목록"]:
            print(f"\n[{r.get('qid')} {r.get('run_id')}] 경보 내용: {r['metrics']['경보목록']}")
