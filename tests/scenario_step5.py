"""5단계 시나리오 — 네 가지 응답 · 다시 판정 상한 · 3일 규칙. 명령마다 새 프로세스(재시작해도 이어지는지).

    python tests/scenario_step5.py > docs/evidence/step5_output.txt

키를 쓰지 않는다: 다시 판정은 KEYS_ENV 를 없는 파일로 돌려 '재조사 실패' 경로로 확인한다 (실제 재조사는 7단계).
발송은 지난 기록 입구라 항상 파일 모드.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
sys.stdout.reconfigure(encoding="utf-8")


def run(*args, nokey=False):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if nokey:
        env.update(KEYS_ENV=str(ROOT / "없음.env"), OPENAI_API_KEY="")
    print("$ python hitl.py " + " ".join(f'"{a}"' if " " in a else a for a in args) + ("   # 키 없음" if nokey else ""))
    r = subprocess.run([PY, "hitl.py", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
    print((r.stdout + r.stderr).rstrip() + "\n")


for f in ("checkpoints.sqlite", "outbox.jsonl", "notices.jsonl", "manual_queue.jsonl", "escalations.jsonl"):
    for p in (ROOT / "output").glob(f + "*"):
        p.unlink()
import shutil
shutil.rmtree(ROOT / "output" / "outbox", ignore_errors=True)

run("past", "--n", "12")
run("list")
edited = ROOT / "output" / "edited_P-S1.md"
edited.write_text("# OEE 보고서 (담당자 수정본)\n\nOEE 는 가용성 × 성능 × 품질로 계산한다 «Overall equipment effectiveness».\n",
                  encoding="utf-8")
run("answer", "P-M1-c224", "approve")
run("answer", "P-M2-35cc", "edit", "--방식", "오류문장빼기")
run("answer", "P-S1-bc7e", "edit", "--수정본", str(edited))
run("answer", "P-M4-5d69", "reject")                                     # 사유 없음 → 거절
run("answer", "P-M4-5d69", "reject", "--사유", "근거 없는 수단 비교가 많음 — 직접 재작성")
run("answer", "P-S2-35e5", "redo")                                       # 지시 없음 → 거절
run("answer", "P-S2-35e5", "redo", "--지시", "TAM 원 논문의 두 변수만 정확히", nokey=True)
run("answer", "P-S2-35e5", "redo", "--지시", "한 번 더", nokey=True)
run("answer", "P-S2-35e5", "redo", "--지시", "세 번째")                   # 상한 2회 → 거절
run("answer", "P-M1-c224", "approve")                                    # 이미 끝난 건 → 거절
run("list")
later = (datetime.now() + timedelta(days=4)).isoformat(timespec="seconds")
run("sweep", "--now", later)
run("sweep", "--now", later)                                             # 같은 건은 한 번만
run("list")
for name in ("manual_queue.jsonl", "escalations.jsonl"):
    print(f"# output/{name}")
    print((ROOT / "output" / name).read_text(encoding="utf-8"))
print("# output/notices.jsonl 종류별")
import json
from collections import Counter
print(Counter(json.loads(l)["종류"] for l in (ROOT / "output" / "notices.jsonl").read_text(encoding="utf-8").splitlines()))
