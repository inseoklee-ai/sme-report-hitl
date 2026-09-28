"""판정지 화면이 오류 없이 뜨는지 (저장은 하지 않는다).   python tests/smoke_label_app.py"""
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.stdout.reconfigure(encoding="utf-8")
at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "label_app.py"), default_timeout=60).run()
assert not at.exception, [e.value for e in at.exception]
print("제목:", at.title[0].value)
print("진행:", at.caption[0].value)
print("보고서 수:", len(at.sidebar.radio[0].options), "· 첫 화면 문장 수:", sum(1 for m in at.markdown if m.value.startswith("**[")))
