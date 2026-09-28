"""REPORT · README 용 화면 캡처 — 떠 있는 데모(localhost:8501)를 이 PC 의 Edge 로 열어 찍는다 (키·비용 없음).

    python -m streamlit run app.py        # 먼저 데모를 띄운다 (대기 건이 있어야 한다)
    python scripts/screenshots.py         # → docs/screenshots/*.png

playwright 는 캡처에만 쓰므로 requirements.txt 에 넣지 않았다 (pip install playwright).
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
URL = "http://localhost:8501"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.goto(URL)
        page.wait_for_selector("text=SME 리서치 보고서", timeout=30000)
        page.wait_for_timeout(3000)

        def 찍기(name, full=False):
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
            print("저장", OUT / f"{name}.png")

        찍기("01_queue")
        # 상세: 통과시키면 상자까지 보이게
        page.get_by_text("통과시키면").first.scroll_into_view_if_needed()
        page.mouse.wheel(0, -250)
        찍기("02_detail_top")
        # 짚은 문장 하나 펼치기
        exp = page.locator('[data-testid="stExpander"]').first
        exp.locator("summary").click()
        exp.scroll_into_view_if_needed()
        찍기("03_flagged_sentence")
        # 답 탭들
        for i, label in enumerate(["승인", "수정 후 승인", "반려", "다시 판정"], 4):
            t = page.get_by_role("tab", name=label, exact=True)
            t.click()
            t.scroll_into_view_if_needed()
            page.mouse.wheel(0, 250)
            찍기(f"0{i}_answer_{label.replace(' ', '')}")
        for i, label in enumerate(["② 새 의뢰", "③ 처리 기록", "④ 기준 설명"], 8):
            page.mouse.wheel(0, -10000)
            page.get_by_role("tab", name=label).click()
            찍기(f"{i:02d}_{label[2:].replace(' ', '')}", full=True)
        browser.close()


if __name__ == "__main__":
    main()
