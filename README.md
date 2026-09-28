# SME 리서치 보고서 — 고객 발송 전 사람 승인 (HITL)

모두의연구소(AIFFEL) Main Quest 05 「사람이 승인하는 에이전트 만들기」 과제 저장소.

딥리서치 에이전트([sme-deep-research](https://github.com/inseoklee-ai/sme-deep-research))가 쓴 보고서를 **고객(중소기업 대표)에게 보내기 직전**에 멈춰 세운다. 위험한 건만 사람에게 보내고 나머지는 자동으로 발송한다. 멈춘 건은 파일(SQLite)에 보관해 두었다가, 사람이 답하면 **멈춘 자리부터** 이어서 실행한다.

- 설계 · 기준 · 실행 결과 · 회고: **[REPORT.md](REPORT.md)**
- 증거 자료: [docs/evidence/](docs/evidence/) · 화면 캡처: [docs/screenshots/](docs/screenshots/)

```
접수 → 조사 → 검수 → 판정 ─(안 걸림)──────────────────────────────→ 발송 → END
                ↑          └(걸림)→ 알림 → 검토[interrupt] ─승인──────────→ 발송 → END
                │                                        ├수정 후 승인─→ 수정반영 → 발송 → END
                │                                        ├반려(사유)───→ 반려기록 → END
                └──────────────────────── 다시 판정(지시, 2회 상한) ─┘
```

**멈춤 기준** (하나라도 걸리면 사람에게):

| 기준 | 막으려는 위험 |
|---|---|
| C2 인용 원문에 없는 숫자 | 틀린 수치가 출처를 달고 나가는 것 |
| C3 지표 경보 | 지어낸 제목 · 읽지 않은 문서를 인용하는 것 |
| C6 국내 질문에 해외 근거 | 해외 기준이 한국 기준처럼 읽히는 것 |
| C8 AI 검수 치명 문장 ≥ 1 | 고객이 옮겨 쓸 구체 사실(수치·기준·고유명사)이 근거 없이 · 틀리게 · 다른 대상의 것으로 나가는 것 |

표본 150건에서 **개입률 65%**(98건 멈춤, 52건 자동 발송). 기준을 어떻게 정하고 고쳐 왔는지는 REPORT 4장.

## 빠른 시작

Python 3.12 기준.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

### 웹 데모 (키 없이 됨)

```bash
.venv\Scripts\python -m streamlit run app.py
```

1. **② 새 의뢰 → 「지난 기록 불러오기」** 로 표본 보고서를 흘려 보낸다 (기본 20건). 검수 결과가 저장소에 캐시돼 있어 **API 키도 비용도 필요 없다.**
2. **① 승인 대기함** 에서 한 건을 열면 맨 위에 질문 · 멈춘 이유 · 「통과시키면 일어나는 일」, 그 아래 AI 검수가 짚은 치명 문장과 인용 원문 대목이 보인다.
3. 답은 네 가지 — **승인 · 수정 후 승인**(치명 문장 빼고 발송 / 직접 수정) **· 반려**(사유 필수) **· 다시 판정**(지시 필수, 2회까지).
4. 앱을 껐다 켜도 대기 건은 `output/checkpoints.sqlite` 에 남아 있다.
5. 사이드바 **3일 규칙** — 「며칠 뒤라고 치고」 실행하면 72시간 넘은 대기 건을 자동 승인·반려 없이 상급자에게 넘기고 고객 지연 안내를 남긴다.
6. **③ 처리 기록** 에 끝난 건 · 발송함 · 직접 처리 목록 · 이관 · 고객 안내가 모인다.

### 명령줄

```bash
.venv\Scripts\python hitl.py past --n 20
```

| 명령 | 하는 일 |
|---|---|
| `hitl.py past [--n 20]` | 지난 기록 n건 투입 (키 없음, 항상 파일 발송) |
| `hitl.py new S3 --고객 "가상고객 Z" [--file]` | 새 의뢰 — 딥리서치를 실제로 돌린다 (키 필요, 약 3~5센트) |
| `hitl.py list` | 대기 건 |
| `hitl.py answer <ID> approve` | 승인 |
| `hitl.py answer <ID> edit --방식 오류문장빼기` | 치명 문장 빼고 발송 (`--수정본 파일.md` 로 직접 수정본) |
| `hitl.py answer <ID> reject --사유 "..."` | 반려 |
| `hitl.py answer <ID> redo --지시 "..."` | 다시 판정 (키 필요) |
| `hitl.py sweep [--now 2026-10-02T09:00]` | 3일 규칙 |
| `validate_criteria.py` | 기준 검증표 다시 만들기 → `docs/evidence/step4_validation.md` (키 없음) |
| `scan_criteria.py` | 후보 기준 7개를 지난 기록 297건에 걸기 → `docs/evidence/step3_scan.md` |
| `judge.py sample / prepare / run / label` | AI 검수 정답 라벨 (gpt-4o, 표본 150건 약 3.7달러 — 결과는 이미 `data/judge/` 에 있음) |
| `label_app.py` | 사람 판정지 (`python -m streamlit run label_app.py`) |

### 시험

```bash
.venv\Scripts\python tests/test_mailer.py
```

| 파일 | 확인하는 것 |
|---|---|
| `tests/test_mailer.py` | 발송 안전장치 7개 — 기본 파일 · 허용 목록 · 한 건 한 번 · 시험·공개 모드는 파일 고정 (가짜 SMTP) |
| `tests/test_resend.py` | 발송 실패를 끝난 건으로 치지 않음 · 재발송 (가짜 SMTP) |
| `tests/smoke_app.py` | 웹 데모를 키 없이 눌러 본다 — **시연 저장소(output/)를 초기화한다** |
| `tests/scenario_step5.py` | 네 가지 응답 · 다시 판정 상한 · 3일 규칙을 명령마다 새 프로세스로 |

## 키와 발송 설정

- **OpenAI 키**는 저장소 밖 `C:\Users\lis29\projects\keys.env`(또는 프로젝트 `.env`)의 `OPENAI_API_KEY` 에서 읽는다. 둘 다 `.gitignore`. 다른 경로는 환경 변수 `KEYS_ENV`.
- 키가 필요한 것은 **새 의뢰 · 다시 판정** 뿐이다. 지난 기록 시연 · 기준 검증 · 시험은 키 없이 된다.
- **발송**은 기본이 파일(`output/outbox/`)이다. 실제 메일은 아래를 모두 갖췄을 때만 나간다.
  - `keys.env` 에 `SEND_MODE=smtp` · `SMTP_HOST` · `SMTP_PORT` · `SMTP_USER` · `SMTP_APP_PASSWORD`(Gmail 은 앱 비밀번호) · `MAIL_ALLOWLIST`
  - 수신자가 `MAIL_ALLOWLIST` 에 있을 것 — 아니면 막고 「발송 차단」 으로 남긴다
  - 웹 화면에서는 그 건의 **「실제 메일로 보내기」 를 켰을 것** (기본 꺼짐)
  - 공개 모드(`DEMO_PUBLIC=1`)가 아닐 것 — 공개 모드는 항상 파일
- 같은 의뢰는 한 번만 발송된다. 발송이 실패·차단되면 끝난 것으로 치지 않고 직접 처리 목록에 올려 **재발송** 할 수 있다.

## 저장소 구조

```
hitl.py              파이프라인 (LangGraph · SqliteSaver) · CLI · 웹 데모 공용 함수
criteria.py          승인 기준 후보 C1~C8 · 켜진 기준
judge.py             AI 검수 (문장 ↔ 인용 원문 대목, gpt-4o) · 정답 라벨
mailer.py            발송 (파일 / SMTP) · 안전장치
app.py               웹 데모 (Streamlit)
label_app.py         사람 판정지
scan_criteria.py     3단계 — 후보 기준 7개 스캔
validate_criteria.py 4단계 — 정답 라벨로 기준 검증
step1_skeleton.py    1단계 — 멈추고 이어가기 뼈대 (학습용)
graph.py metrics.py config.json   ← sme-deep-research b54bd76 에서 고치지 않고 가져옴
data/corpus.json questions.json   ← 〃 (위키백과 45건 · 질문 11건)
data/past_runs/      ← 〃 실행 기록 297건
data/judge/          AI 검수 캐시 (문장별 원문 대목 · 판정) — 지난 기록 시연을 키 없이
data/labels/         표본 150건 · AI 라벨 · 사람 판정 30건
docs/evidence/       단계별 실행 출력 · 검증표
docs/screenshots/    화면 캡처 (scripts/screenshots.py)
tests/               시험
output/              실행하면 생긴다 (대기 건 DB · 발송함 · 알림) — 저장소에 올리지 않는다
```

## 가져온 코드

`graph.py` · `metrics.py` · `config.json` · `data/corpus.json` · `data/questions.json` · `data/past_runs/*.jsonl` 은 [sme-deep-research](https://github.com/inseoklee-ai/sme-deep-research) 커밋 `b54bd76` 에서 **고치지 않고** 가져왔다. 새 코드는 이것을 `import` 해서 쓴다 — 딥리서처는 그대로 두고 그 뒤에 승인 단계를 붙인 구조다.
