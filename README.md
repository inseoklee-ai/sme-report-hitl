# SME 딥리서치 보고서 — 고객 발송 전 사람 승인 (HITL)

모두의연구소(AIFFEL) Main Quest 05 「사람이 승인하는 에이전트 만들기」 과제 저장소. 작성 중.

## 가져온 코드

아래 파일은 [sme-deep-research](https://github.com/inseoklee-ai/sme-deep-research) 커밋 `b54bd76` 에서 **고치지 않고** 가져왔다. 새 코드는 이것을 `import` 해서 쓴다.

| 파일 | 원본에서의 역할 |
|---|---|
| `graph.py` · `metrics.py` · `config.json` | 딥리서치 에이전트 본체 · 정답표 없는 지표 |
| `data/corpus.json` · `data/questions.json` | 위키백과 45건 코퍼스 · 질문 11건 |
| `data/past_runs/*.jsonl` | 원본의 실험 1·2 실행 기록 297건 (원본 경로 `output/`) |
