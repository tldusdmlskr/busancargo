"""
한국도로공사 구간교통량(VDS) 포털 자동 다운로드 스크립트 - Playwright 버전
https://data.ex.co.kr/portal/traffic/trafficVds

Selenium 버전에서 겪었던 두 가지 문제를 구조적으로 해결:
1. "다운로드되자마자 사라짐"
   -> Playwright의 page.expect_download()는 다운로드 스트림을 직접 캡처해서
      브라우저의 "위험한 파일" 자동 삭제 로직을 아예 거치지 않음 (CDP 트릭 불필요)
2. "데이터 없음 반복 (자동화 탐지 의심)"
   -> 실제 Chrome 바이너리 사용(channel="chrome"), navigator.webdriver 등
      자동화 흔적 제거, 사람처럼 랜덤 지연 추가

설치
----
pip install playwright
playwright install chrome

실행 전 필수 확인
------------------
아래 TARGET_ROUTES의 trafficLineCode 값(부산신항선=0105 등)과 구간 텍스트는
지난번 캡처한 실제 페이지 구조를 반영한 것. 혹시 사이트가 바뀌었으면
콘솔에 출력되는 "실제 옵션 목록"을 보고 맞춰서 수정할 것.
"""

import os
import re
import csv as csv_module
import random
import time
from datetime import date, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://data.ex.co.kr/portal/traffic/trafficVds"
DOWNLOAD_DIR = os.path.abspath("./vds_downloads")

TARGET_ROUTES = {
    "중앙선": {
        "code": "0550",
        "sections": [
            "대동IC-초정IC",
        ],
    },
}

START_DATE = date(2022, 1, 1)     # 필요시 더 과거로 조정
END_DATE = date(2026, 8, 8)       # 오늘 기준 확보 가능한 마지막 날
WINDOW_DAYS = 30                   # 사이트 제한(최대 30일)

NAV_TIMEOUT_MS = 20000
DOWNLOAD_TIMEOUT_MS = 30000


# ============================================================
# 사람처럼 보이게 하는 랜덤 대기
# ============================================================

def human_pause(a=0.6, b=1.6):
    time.sleep(random.uniform(a, b))


# ============================================================
# 30일/월 단위 구간 생성 (31일짜리 달은 30일+1일로 자동 분리)
# ============================================================

def date_windows(start: date, end: date, window_days: int):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        next_month_start = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        month_last_day = next_month_start - timedelta(days=1)

        window_end = min(cur + timedelta(days=window_days - 1), month_last_day, end)
        window_start = max(cur, start)
        if window_start <= window_end:
            yield window_start, window_end

        if window_end < month_last_day and window_end < end:
            remaining_start = window_end + timedelta(days=1)
            remaining_end = min(month_last_day, end)
            if remaining_start <= remaining_end:
                yield remaining_start, remaining_end

        cur = next_month_start


# ============================================================
# 페이지 조작 함수
# ============================================================

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""


def set_route(page, route_code: str):
    page.select_option("#trafficLineCode", value=route_code)
    human_pause(1.0, 1.8)  # 구간 드롭다운 AJAX 갱신 대기


def set_section_by_text(page, section_text: str) -> bool:
    options = page.eval_on_selector_all(
        "#trafficConzoneCode option", "els => els.map(e => e.textContent.trim())"
    )
    if section_text not in options:
        print(f"    ⚠️ 구간 '{section_text}' 못 찾음. 실제 옵션: {options}")
        return False
    page.select_option("#trafficConzoneCode", label=section_text)
    return True


def set_date_range(page, start_d: date, end_d: date):
    """jQuery UI datepicker API를 JS로 직접 호출. 종료일을 먼저 설정해야
    시작일이 활성화되는 사이트 특성 반영. 하루짜리 구간은 사이트가 자동으로
    전날로 밀 수 있어 결과를 로그로 남긴다."""

    page.evaluate(
        """([y, m, d]) => {
            $('#searchDay').datepicker('setDate', new Date(y, m - 1, d));
            $('#searchDay').trigger('change');
        }""",
        [end_d.year, end_d.month, end_d.day],
    )
    human_pause(0.3, 0.6)

    page.evaluate(
        """([y, m, d]) => {
            $('#searchDayFrom').datepicker('setDate', new Date(y, m - 1, d));
            $('#searchDayFrom').trigger('change');
        }""",
        [start_d.year, start_d.month, start_d.day],
    )
    human_pause(0.3, 0.6)

    actual_end = page.input_value("#searchDay")
    actual_start = page.input_value("#searchDayFrom")
    print(f"    날짜 설정 결과: 시작={actual_start}, 종료={actual_end} (요청: {start_d} ~ {end_d})")
    return actual_start, actual_end


def has_no_data_message(page) -> bool:
    body_text = page.inner_text("body")
    markers = ["데이터가 없습니다", "조회된 데이터가 없습니다", "결과가 없습니다"]
    return any(m in body_text for m in markers)


# ============================================================
# 메인 크롤링 로직
# ============================================================

def run():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    log_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",       # 번들 chromium 대신 실제 설치된 크롬 사용 (핑거프린트 더 자연스러움)
            headless=True,         # 처음엔 눈으로 확인 권장. 안정화되면 True로 변경
        )
        context = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1400, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        try:
            for route_name, route_info in TARGET_ROUTES.items():
                route_code = route_info["code"]

                for section_name in route_info["sections"]:
                    for start_d, end_d in date_windows(START_DATE, END_DATE, WINDOW_DAYS):
                        print(f"[{route_name}] {section_name} | {start_d} ~ {end_d}")

                        try:
                            page.goto(BASE_URL, wait_until="networkidle")
                            human_pause(0.8, 1.5)

                            set_route(page, route_code)

                            if not set_section_by_text(page, section_name):
                                log_rows.append([route_name, section_name, start_d, end_d, "SECTION_NOT_FOUND"])
                                continue

                            set_date_range(page, start_d, end_d)

                            page.click("#btnSearch")
                            human_pause(1.5, 2.5)

                            if has_no_data_message(page):
                                print("    ⚠️ '데이터 없음' 메시지 감지")
                                log_rows.append([route_name, section_name, start_d, end_d, "NO_DATA_OR_BLOCKED"])
                                continue

                            # CSV 다운로드: Playwright가 다운로드 스트림을 직접 캡처
                            try:
                                with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                                    page.click("#btnCsv")
                                download = download_info.value
                            except PWTimeout:
                                print("    ⚠️ 다운로드 타임아웃")
                                log_rows.append([route_name, section_name, start_d, end_d, "DOWNLOAD_TIMEOUT"])
                                continue

                            safe_route = re.sub(r"[\\/]", "-", route_name)
                            safe_section = re.sub(r"[\\/]", "-", section_name)
                            new_name = f"VDS_{safe_route}_{safe_section}_{start_d:%Y%m%d}_{end_d:%Y%m%d}.csv"
                            save_path = os.path.join(DOWNLOAD_DIR, new_name)
                            download.save_as(save_path)

                            print(f"  ✅ 저장: {new_name}")
                            log_rows.append([route_name, section_name, start_d, end_d, "OK"])

                        except Exception as e:
                            print(f"  ❌ 에러: {e}")
                            log_rows.append([route_name, section_name, start_d, end_d, f"ERROR: {e}"])

                        human_pause(2.5, 4.5)  # 요청 간 사람처럼 랜덤 대기

        finally:
            context.close()
            browser.close()

            log_path = os.path.join(DOWNLOAD_DIR, "_collection_log.csv")
            with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv_module.writer(f)
                writer.writerow(["노선", "구간", "시작일", "종료일", "상태"])
                writer.writerows(log_rows)
            print(f"\n로그 저장: {log_path}")


if __name__ == "__main__":
    run()