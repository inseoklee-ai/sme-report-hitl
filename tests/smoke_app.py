"""웹 데모 시험 — 키 없이 지난 기록으로 대기함 · 답(승인 · 빼고 발송 · 반려) · 3일 규칙을 눌러 본다.

    python tests/smoke_app.py          (시연 저장소를 초기화한다)
"""
import os
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.update(KEYS_ENV=str(ROOT / "없음.env"), OPENAI_API_KEY="", SEND_MODE="file")   # 키 · 실제 발송 없이

import hitl as H  # noqa: E402

H.초기화()


def app():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def btn(at, label):
    return next(b for b in at.button if b.label == label)


at = app()
print("처음:", at.caption[0].value)

# ② 지난 기록 12건 흘려 보내기
at.slider[0].set_value(12).run()
btn(at, "불러와서 흘려 보내기").click().run()
assert not at.exception, [e.value for e in at.exception]
print("불러오기:", [s.value for s in at.success])

at = app()
print("대기함:", at.tabs[0].label, "· 첫 건 질문:", next(m.value for m in at.markdown if m.value.startswith("#### 질문")))
print("통과시키면:", at.info[0].value[:80], "…")

# 승인
first = at.selectbox[0].value
btn(at, "승인하고 발송").click().run()
assert not at.exception, [e.value for e in at.exception]
print("승인:", first, "→", H.상태(first)["결과"])

# 빼고 발송
at = app()
second = at.selectbox[0].value
btn(at, "빼고 발송").click().run()
assert not at.exception, [e.value for e in at.exception]
print("빼고 발송:", second, "→", H.상태(second)["결과"], H.상태(second)["수정"])

# 반려 — 사유 없이는 거절, 사유를 넣으면 반려
at = app()
third = at.selectbox[0].value
btn(at, "반려").click().run()
print("사유 없는 반려:", [e.value for e in at.error])
at.text_input[0].set_value("근거 없는 비교가 많음 — 직접 재작성").run()
btn(at, "반려").click().run()
assert not at.exception, [e.value for e in at.exception]
print("반려:", third, "→", H.상태(third)["결과"])

# 3일 규칙
at = app()
at.sidebar.number_input[0].set_value(4).run()
btn(at, "대기 건 점검 (sweep)").click().run()
print("sweep:", [s.value for s in at.sidebar.success] or [i.value for i in at.sidebar.info])

at = app()
print("끝:", at.caption[0].value)
print("③ 처리 기록 표 수:", len(at.dataframe), "· ④ 기준 표:", len(at.table))
