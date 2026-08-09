"""
한국도로공사 TCS 영업소별 교통량 자동 다운로드 스크립트 - Playwright

⚠️ TODO: BASE_URL을 실제 페이지 주소로 채울 것
    (브라우저에서 이 화면 열었을 때 주소창에 뜨는 URL 그대로 복사)

페이지 구조 요약
----------------
- 집계주기(collectCycle): 03=1시간, 04=1일
- 공급주기(supplyCycle) : 01=1일(달력에서 하루 선택), 02=1개월(연도+월 드롭다운)
  -> 이 스크립트는 02(1개월)로 고정해서 한 번에 한 달씩 받음
- 연도(dataSupplyYear) / 월(dataSupplyMonth) 드롭다운 (연도 선택 시 월 옵션이 JS로 갱신됨)
- 검색(btnSearch) 클릭 후, 다운로드 전에 설문 폼(소속/지역/성별/나이/활용목적)을 채워야 함
- 다운로드(fileDownload) 버튼 클릭

다운로드되는 파일은 영업소 필터링이 없고, 그 달의 전국 전체 영업소(약 470여 개) 데이터가
한꺼번에 들어있음 -> 이후 filter_tcs.py 등으로 우리가 필요한 17개 영업소만 걸러내야 함
(코드는 002, 626 처럼 3자리, 뒤에 공백 포함 - 지난번 확인된 형식 그대로)
"""

import os
import re
import csv as csv_module
import random
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://data.ex.co.kr/portal/fdwn/view?type=TCS&num=34&requestfrom=dataset"  # TODO
DOWNLOAD_DIR = os.path.abspath("./tcs_downloads")

# 수집 기간: (연도, 월) 리스트. 2003년부터 가능하다고 확인됐지만
# 우선 최근 10년 정도로 잡음 - 필요시 범위 조정
YEARS = range(2016, 2027)   # 2016 ~ 2026
MONTHS_ALL = [f"{m:02d}" for m in range(1, 13)]

# 설문 응답값 (요청받은 대로: 대학교·연구기관 / 서울특별시 / 여성 / 20대 / 연구 및 학술분석)
SURVEY = {
    "deptName": "4           ",     # 대학교·연구기관 (원본 옵션 value가 공백 패딩되어 있어 그대로 맞춤)
    "regionName": "KR-11       ",   # 서울특별시
    "gender": "1           ",       # 여성
    "userAgeName": "20          ",  # 20대
    "pouName": "0           ",      # 연구 및 학술분석
}

NAV_TIMEOUT_MS = 20000
DOWNLOAD_TIMEOUT_MS = 30000


def human_pause(a=0.6, b=1.6):
    time.sleep(random.uniform(a, b))


def scroll_to(page, selector: str):
    """요소를 화면 중앙으로 명시적으로 스크롤. Playwright의 자동 스크롤이
    안 먹히는 페이지(레이아웃 특이 케이스)를 위한 보강."""
    page.locator(selector).scroll_into_view_if_needed()
    human_pause(0.3, 0.6)


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""


# ============================================================
# 페이지 조작
# ============================================================

def set_collect_cycle_hourly(page):
    """집계주기를 1시간(03)으로 설정"""
    scroll_to(page, "#collectCycle")
    page.select_option("#collectCycle", value="03")
    human_pause(0.4, 0.8)


def set_supply_cycle_month(page):
    """공급주기를 1개월(02)로 설정 -> calMonth 영역이 보이게 됨.
    실제 <input type=radio>는 커스텀 스타일 때문에 화면에 숨겨져 있고
    보이는 건 <label>이라, label을 직접 클릭해야 함."""
    scroll_to(page, "label[for='supply_02']")
    page.click("label[for='supply_02']")
    human_pause(0.5, 1.0)

    # 제대로 선택됐는지 확인 (안 됐으면 강제로 input을 체크)
    checked = page.eval_on_selector("#supply_02", "el => el.checked")
    if not checked:
        print("    ⚠️ label 클릭으로 안 먹힘 - force check로 재시도")
        page.check("#supply_02", force=True)
        human_pause(0.3, 0.6)


def set_year_month(page, year: int, month: str):
    scroll_to(page, "#dataSupplyYear")
    page.select_option("#dataSupplyYear", value=str(year))
    human_pause(0.4, 0.8)  # 월 옵션이 JS로 갱신될 시간

    # 해당 연도에 그 달 옵션이 실제로 존재하는지 확인 (예: 2026년은 07월까지만 있을 수 있음)
    available_months = page.eval_on_selector_all(
        "#dataSupplyMonth option", "els => els.map(e => e.value)"
    )
    if month not in available_months:
        return False

    scroll_to(page, "#dataSupplyMonth")
    page.select_option("#dataSupplyMonth", value=month)
    human_pause(0.3, 0.6)
    return True


def fill_survey(page):
    for field_id, value in SURVEY.items():
        scroll_to(page, f"#{field_id}")
        try:
            page.select_option(f"#{field_id}", value=value)
        except Exception:
            # 원본 value가 공백 패딩 없이 등록되어 있을 수도 있어 trim해서 재시도
            page.select_option(f"#{field_id}", value=value.strip())
        human_pause(0.2, 0.4)


def has_no_data_message(page) -> bool:
    body_text = page.inner_text("body")
    markers = ["데이터가 없습니다", "조회된 데이터가 없습니다", "결과가 없습니다"]
    return any(m in body_text for m in markers)


# ============================================================
# 메인
# ============================================================

def run():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    log_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
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
            for year in YEARS:
                for month in MONTHS_ALL:
                    label = f"{year}-{month}"
                    print(f"[TCS 1시간 단위] {label}")

                    try:
                        page.goto(BASE_URL, wait_until="networkidle")
                        human_pause(0.8, 1.5)

                        set_collect_cycle_hourly(page)
                        set_supply_cycle_month(page)

                        ok = set_year_month(page, year, month)
                        if not ok:
                            print(f"  ⚠️ {label}: 해당 연/월 옵션 없음 (미래 시점이거나 범위 밖)")
                            log_rows.append([year, month, "MONTH_NOT_AVAILABLE"])
                            continue

                        scroll_to(page, "#btnSearch")
                        page.click("#btnSearch")
                        human_pause(1.5, 2.5)

                        if has_no_data_message(page):
                            print("  ⚠️ '데이터 없음' 메시지 감지")
                            log_rows.append([year, month, "NO_DATA"])
                            continue

                        fill_survey(page)
                        human_pause(0.5, 1.0)

                        try:
                            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                                scroll_to(page, "#fileDownload")
                                page.click("#fileDownload")
                            download = download_info.value
                        except PWTimeout:
                            print("  ⚠️ 다운로드 타임아웃")
                            log_rows.append([year, month, "DOWNLOAD_TIMEOUT"])
                            continue

                        new_name = f"TCS_1시간_{year}{month}.csv"
                        save_path = os.path.join(DOWNLOAD_DIR, new_name)
                        download.save_as(save_path)

                        print(f"  ✅ 저장: {new_name}")
                        log_rows.append([year, month, "OK"])

                    except Exception as e:
                        print(f"  ❌ 에러: {e}")
                        log_rows.append([year, month, f"ERROR: {e}"])

                    human_pause(2.5, 4.5)

        finally:
            context.close()
            browser.close()

            log_path = os.path.join(DOWNLOAD_DIR, "_collection_log.csv")
            with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv_module.writer(f)
                writer.writerow(["연도", "월", "상태"])
                writer.writerows(log_rows)
            print(f"\n로그 저장: {log_path}")


if __name__ == "__main__":
    run()