"""4단계 — 정답 라벨 (기준과 독립). 보고서 문장마다 인용 원문 대목을 붙여 gpt-4o 가 판정한다.

    python judge.py sample           # 297건에서 질문별 층화 표본 150건 → data/labels/sample.json (고정 시드)
    python judge.py prepare          # 표본 문장 → 인용 원문 대목 (임베딩, 약 0.01달러) + 비용 추산. 판정은 안 함
    python judge.py run [--limit N]  # 판정 (캐시 — 끊겨도 같은 명령으로 이어서)
    python judge.py label            # 문장 판정 → 보고서 라벨 두 가지(엄격 · 실무) → data/labels/llm.jsonl

보고서 라벨 = 고객에게 그대로 나가면 안 되는 오류가 하나라도 있는가 (4단계에서 승인한 정의):
  사실·수치가 인용 원문과 다르거나(모순) · 구체적 주장이 원문 대목에 없거나(근거없음) · 질문과 다른 대상(대상착오)
  표현이 투박하거나 덜 자세한 것은 오류가 아니다.
두 가지로 센다 (사용자 30건 판정과 더 맞는 쪽을 고른다):
  엄격 = 위 오류 판정이 하나라도 있으면
  실무 = 그 오류가 '구체' 문장(수치·기준·고유명사·연도처럼 고객이 그대로 옮겨 쓸 사실)일 때만
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time

import numpy as np

import criteria as C
import graph as G
from scan_criteria import 불러오기

WORK = G.OUT / "judge"                   # 임베딩(npz) — 다시 만들 수 있어 저장소에 올리지 않는다
CACHE = G.ROOT / "data" / "judge"         # 문장별 대목 · 판정 — 저장소에 올린다 (지난 기록 시연을 키 없이)
ITEMS, VERDICTS = CACHE / "items.jsonl", CACHE / "verdicts.jsonl"
LABELS = G.ROOT / "data" / "labels"
EMB_MODEL = "text-embedding-3-small"
JUDGE_MODEL = "gpt-4o"
PRICE = {"gpt-4o": (2.50, 10.00), EMB_MODEL: (0.02, 0.0)}     # 달러 / 100만 토큰 (입력, 출력)
조각길이, 대목수, 묶음 = 450, 3, 10
표본수, 시드 = 150, 20260928
SAMPLE = LABELS / "sample.json"
오류판정 = {"근거없음", "모순", "대상착오"}


def _client():
    from openai import OpenAI
    return OpenAI(api_key=G.파일키(), timeout=120, max_retries=3)


def _h(*xs) -> str:
    return hashlib.sha1("\x1f".join(xs).encode("utf-8")).hexdigest()[:16]


# ── 1. 원문 조각과 임베딩 ─────────────────────────────────────────────────────────
def 조각내기() -> list[tuple[str, str]]:
    out = []
    for title, text in G.DOCS.items():
        buf = ""
        for s in re.split(r"(?<=[.!?。다])\s+|\n+", text):
            if len(buf) + len(s) > 조각길이 and buf:
                out.append((title, buf.strip()))
                buf = buf[-80:]                     # 조각 경계에서 문장이 잘리지 않게 조금 겹친다
            buf += " " + s
        if buf.strip():
            out.append((title, buf.strip()))
    return out


def _embed(texts: list[str]) -> np.ndarray:
    cl, vecs = _client(), []
    for i in range(0, len(texts), 256):
        r = cl.embeddings.create(model=EMB_MODEL, input=texts[i:i + 256])
        vecs += [d.embedding for d in r.data]
    v = np.array(vecs, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def 조각임베딩():
    WORK.mkdir(parents=True, exist_ok=True)
    chunks = 조각내기()
    key = _h(EMB_MODEL, str(조각길이), *(t + c for t, c in chunks))
    f = WORK / f"chunks_{key}.npz"
    if f.exists():
        return chunks, np.load(f)["v"]
    v = _embed([c for _, c in chunks])
    np.savez_compressed(f, v=v)
    return chunks, v


# ── 2. 판정할 문장과 대목 ─────────────────────────────────────────────────────────
def 표본() -> list[dict]:
    """질문(qid)별로 고르게 — 11개 질문 × 27건에서 질문마다 13~14건. 조건(기본·끔·혼자)은 섞인 채로 무작위."""
    rows = 불러오기()
    if SAMPLE.exists():
        ids = set(json.loads(SAMPLE.read_text(encoding="utf-8"))["run_ids"])
        return [r for r in rows if r["run_id"] in ids]
    import random
    rng = random.Random(시드)
    by_q: dict[str, list] = {}
    for r in rows:
        by_q.setdefault(r["qid"], []).append(r)
    qs = sorted(by_q)
    quota = {q: 표본수 // len(qs) + (1 if i < 표본수 % len(qs) else 0) for i, q in enumerate(qs)}
    pick = [r for q in qs for r in rng.sample(by_q[q], quota[q])]
    LABELS.mkdir(parents=True, exist_ok=True)
    SAMPLE.write_text(json.dumps({"시드": 시드, "건수": len(pick), "run_ids": [r["run_id"] for r in pick]},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    return pick


def 문항들() -> list[dict]:
    """(질문, 문장) 이 같으면 한 번만 판정한다 — 반복 실행에서 같은 문장이 많이 나온다."""
    seen, items = set(), []
    for r in 표본():
        for s in C._본문(r["report"]):
            k = _h(r["질문"], s)
            if k not in seen:
                seen.add(k)
                items.append({"id": k, "qid": r["qid"], "질문": r["질문"], "문장": s, "인용": C.CITE.findall(s)})
    return items


def 대목붙이기(items: list[dict]) -> list[dict]:
    """items.jsonl 에 이미 있는 문장은 그대로 쓰고, 없는 문장만 대목을 찾아 덧붙인다 (검증용 표본 · 파이프라인 공용)."""
    have = 대목표()
    new = [x for x in items if x["id"] not in have]
    if new:
        _대목찾기(new)
        CACHE.mkdir(parents=True, exist_ok=True)
        with ITEMS.open("a", encoding="utf-8") as fh:
            for x in new:
                fh.write(json.dumps(x, ensure_ascii=False) + "\n")
                have[x["id"]] = x
    return [have[x["id"]] for x in items]


def 대목표() -> dict[str, dict]:
    return {x["id"]: x for x in map(json.loads, ITEMS.read_text(encoding="utf-8").splitlines())} if ITEMS.exists() else {}


def _대목찾기(items: list[dict]) -> None:
    chunks, cv = 조각임베딩()
    by_title: dict[str, list[int]] = {}
    for i, (t, _) in enumerate(chunks):
        by_title.setdefault(t, []).append(i)
    cited = [x for x in items if x["인용"]]
    sv = _embed([C.CITE.sub("", x["문장"]) for x in cited])
    for x, v in zip(cited, sv):
        idx = [i for t in dict.fromkeys(x["인용"]) for i in by_title.get(t, [])]
        if not idx:
            x["대목"] = []
            continue
        vals = [val for _, val in C.숫자들(C.CITE.sub("", x["문장"]))]
        score = cv[idx] @ v
        # 문장 속 숫자와 같은 값이 든 조각은 가산점 — 숫자 대조가 판정의 핵심이라
        bonus = np.array([0.15 if vals and any(C._같은값(a, {b for _, b in C.숫자들(chunks[i][1])}) for a in vals)
                          else 0.0 for i in idx])
        top = [idx[j] for j in np.argsort(-(score + bonus))[:대목수]]
        x["대목"] = [{"문서": chunks[i][0], "글": chunks[i][1]} for i in top]
    for x in items:
        x.setdefault("대목", [])


# ── 3. 판정 ──────────────────────────────────────────────────────────────────
SYSTEM = """당신은 컨설팅 보고서의 사실 검수자다. 보고서 문장 하나하나를, 그 문장이 인용한 원문 대목과 대조해 판정한다.
판정은 다섯 가지 중 하나:
- 뒷받침: 문장의 핵심 주장(수치·이름·연도·인과)이 대목에 있다. 번역·단위 표기 차이(50 million = 5000만)는 같은 것으로 본다.
- 일반서술: 구체적 사실 없이 흔한 설명·연결·요약·다짐이다 (예: "이 기술은 여러 산업에서 중요하다", "앞으로도 노력이 필요하다",
  "이 보고서는 ~를 다룬다"). 인용이 없어도 구체적 사실이 없으면 일반서술이다. 틀렸다고 할 것이 없다.
- 근거없음: 구체적 주장(정의·수치·이름·연도·기준·인과)이 있는데 대목 어디에도 없다. 사실 자체는 맞아 보여도
  인용한 문서가 말하지 않으면 근거없음이다. 인용이 없는 문장의 구체적 주장도 여기.
- 모순: 대목과 다른 수치·사실을 말한다.
- 대상착오: 질문이 묻는 대상(예: 대한민국)과 다른 대상(예: EU·세계 일반)의 사실을 질문 대상의 것처럼 쓴다.
주의: 대목은 원문 일부만 보여 준다. 대목에 없다고 곧 틀린 것은 아니지만, 고객에게 근거를 댈 수 없으면 근거없음이다.
표현이 투박하거나 덜 자세한 것은 판정 대상이 아니다.
판정과 별도로 "구체" 를 표시한다: 문장에 수치·기준값·고유명사(제품·기관·인물·법령)·연도처럼 고객이 그대로 옮겨 쓸
사실이 있으면 true, 개념 정의나 설명뿐이면 false.
JSON 만 출력: {"판정": [{"i": 번호, "판정": "...", "구체": true/false, "이유": "20자 안팎"}]}"""


def _프롬프트(batch: list[dict]) -> str:
    parts = [f"질문: {batch[0]['질문']}\n"]
    for n, x in enumerate(batch, 1):
        parts.append(f"[{n}] 문장: {x['문장']}")
        if not x["인용"]:
            parts.append("    (인용 없음)")
        for d in x["대목"]:
            parts.append(f"    대목 «{d['문서']}»: {d['글']}")
    return "\n".join(parts)


def 묶기(items: list[dict]) -> list[list[dict]]:
    by_q: dict[str, list[dict]] = {}
    for x in items:
        by_q.setdefault(x["질문"], []).append(x)
    return [xs[i:i + 묶음] for xs in by_q.values() for i in range(0, len(xs), 묶음)]


def 추산(items: list[dict]) -> dict:
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")
    batches = 묶기(items)
    tin = sum(len(enc.encode(SYSTEM)) + len(enc.encode(_프롬프트(b))) + 10 for b in batches)
    tout = 35 * len(items)                        # 문장당 판정 한 줄 ≈ 35 토큰 (시험 호출로 확인)
    pin, pout = PRICE[JUDGE_MODEL]
    return {"문장": len(items), "호출": len(batches), "입력토큰": tin, "출력토큰(추정)": tout,
            "달러": round(tin / 1e6 * pin + tout / 1e6 * pout, 2)}


def _판정묶음들(todo: list[list[dict]], verbose: bool = True) -> dict:
    """묶음마다 gpt-4o 한 번. 판정은 verdicts.jsonl 에 바로 덧붙인다 — 끊겨도 다음 실행이 이어서 한다."""
    cl, tin, tout = _client(), 0, 0
    pin, pout = PRICE[JUDGE_MODEL]
    CACHE.mkdir(parents=True, exist_ok=True)
    for k, b in enumerate(todo, 1):
        r = cl.chat.completions.create(model=JUDGE_MODEL, temperature=0, response_format={"type": "json_object"},
                                       messages=[{"role": "system", "content": SYSTEM},
                                                 {"role": "user", "content": _프롬프트(b)}])
        tin += r.usage.prompt_tokens
        tout += r.usage.completion_tokens
        got = {v.get("i"): v for v in json.loads(r.choices[0].message.content).get("판정", [])}
        with VERDICTS.open("a", encoding="utf-8") as fh:
            for n, x in enumerate(b, 1):
                v = got.get(n, {"판정": "판정실패", "이유": "응답에 없음"})
                fh.write(json.dumps({"id": x["id"], "판정": v.get("판정"), "구체": bool(v.get("구체")),
                                     "이유": v.get("이유", "")},
                                    ensure_ascii=False) + "\n")
        if verbose and (k % 20 == 0 or k == len(todo)):
            print(f"  {k}/{len(todo)} · 토큰 {tin:,}+{tout:,} · {tin/1e6*pin + tout/1e6*pout:.2f}달러")
    return {"호출": len(todo), "입력토큰": tin, "출력토큰": tout, "달러": round(tin / 1e6 * pin + tout / 1e6 * pout, 4)}


def 판정하기(limit: int | None = None):
    items = 대목붙이기(문항들())
    done = set(판정표())
    todo = [b for b in 묶기(items) if any(x["id"] not in done for x in b)]
    if limit:
        todo = todo[:limit]
    print(f"남은 묶음 {len(todo)}개 (완료 문장 {len(done)})")
    _판정묶음들(todo)


def 판정표() -> dict[str, dict]:
    return {v["id"]: v for v in map(json.loads, VERDICTS.read_text(encoding="utf-8").splitlines())} \
        if VERDICTS.exists() else {}


def 검수(질문: str, report: str) -> dict:
    """파이프라인의 검수 노드가 부른다 — 보고서 한 건의 문장마다 판정. 이미 판정한 문장은 캐시에서 꺼내므로
    지난 기록(표본 150건)은 비용 0, 새 보고서는 약 2센트."""
    sents = C._본문(report)
    items = [{"id": _h(질문, s), "qid": None, "질문": 질문, "문장": s, "인용": C.CITE.findall(s)} for s in sents]
    uniq = list({x["id"]: x for x in items}.values())
    uniq = 대목붙이기(uniq)
    have = 판정표()
    todo = [x for x in uniq if x["id"] not in have]
    cost = _판정묶음들(묶기(todo), verbose=False) if todo else {"호출": 0, "달러": 0.0}
    have = 판정표() if todo else have
    full = {x["id"]: x for x in uniq}
    문장들 = [{"번호": n, "문장": x["문장"], "대목": full[x["id"]]["대목"],
             **{k: have.get(x["id"], {}).get(k) for k in ("판정", "구체", "이유")}}
            for n, x in enumerate(items, 1)]
    return {"문장": 문장들, "오류수": sum(1 for s in 문장들 if s["판정"] in 오류판정),
            "판정실패": sum(1 for s in 문장들 if s["판정"] not in 오류판정 | {"뒷받침", "일반서술"}),
            "비용": cost, "모델": JUDGE_MODEL}


def 보고서라벨():
    """문장 판정 → 보고서 라벨. 오류 문장이 하나라도 있으면 사람이 봤어야 하는 보고서."""
    vs = 판정표()
    LABELS.mkdir(parents=True, exist_ok=True)
    rows, miss = [], 0
    for r in 표본():
        sents = C._본문(r["report"])
        vv = [(s, vs.get(_h(r["질문"], s))) for s in sents]
        miss += sum(1 for _, v in vv if v is None)
        bad = [{"문장": s, "판정": v["판정"], "구체": v.get("구체", False), "이유": v["이유"]}
               for s, v in vv if v and v["판정"] in 오류판정]
        rows.append({"run_id": r["run_id"], "qid": r["qid"], "라벨": r["라벨"],
                     "엄격": bool(bad), "실무": any(b["구체"] for b in bad), "오류문장": bad, "판정분포": dict(__import__("collections").Counter(v["판정"] for _, v in vv if v))})
    with (LABELS / "llm.jsonl").open("w", encoding="utf-8") as fh:
        for x in rows:
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    for k in ("엄격", "실무"):
        n = sum(x[k] for x in rows)
        print(f"[{k}] 보고서 {len(rows)}건 중 사람이 봤어야 함 {n}건 ({100*n/len(rows):.1f}%)")
    print(f"판정 없는 문장 {miss}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sample", "prepare", "run", "label"])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.cmd == "sample":
        rs = 표본()
        from collections import Counter
        print(f"표본 {len(rs)}건 · 질문별 {dict(Counter(r['qid'] for r in rs))}")
        print(f"조건별 {dict(Counter(r['라벨'] for r in rs))}")
    elif a.cmd == "prepare":
        t0 = time.time()
        items = 대목붙이기(문항들())
        print(f"대목 준비 {time.time()-t0:.0f}초 · 인용 있는 문장 {sum(1 for x in items if x['인용'])} · "
              f"대목 없는 인용 문장 {sum(1 for x in items if x['인용'] and not x['대목'])}")
        print("비용 추산:", 추산(items))
    elif a.cmd == "run":
        판정하기(a.limit)
    else:
        보고서라벨()


if __name__ == "__main__":
    main()
