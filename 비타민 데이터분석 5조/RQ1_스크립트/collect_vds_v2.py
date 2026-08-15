"""
한국도로공사 구간교통량(VDS) 자동 다운로드 스크립트 v2 - Playwright
https://data.ex.co.kr/portal/traffic/trafficVds

v1(collect_vds.py) 대비 달라진 점
----------------------------------
1. RQ1에 필요한 4개 노선(부산신항선/남해2지선/남해선(순천-부산)/중앙선)의
   전체 대상 구간을 TARGET_ROUTES에 한 번에 정의. (v1은 중앙선 1개 구간만 있었음)
2. trafficLineCode를 더 이상 숫자 코드로 하드코딩하지 않음.
   -> 실행 시 페이지의 #trafficLineCode 드롭다운을 읽어서 노선명(텍스트)으로 코드를
      자동으로 찾는다(get_route_code_by_text). 코드값이 바뀌거나 몰라도 안전함.
3. **이미 수집된 부분은 건너뛴다.**
   -> DOWNLOAD_DIR에 이미 있는 "VDS_노선_구간_시작일_종료일.csv" 파일들을 스캔해서
      (노선, 구간)별로 이미 커버된 날짜 집합을 만들고, 이번에 돌려고 하는 30일 윈도우가
      이미 100% 커버돼 있으면 그 윈도우는 통째로 스킵한다.
   -> 부분적으로만 겹치는 경우(예: 예전엔 1일짜리 파일만 있었던 경우)는 안전하게
      그냥 다시 받는다 (사이트가 어차피 30일 제한이라 재요청 비용이 크지 않음).
4. 스킵된 윈도우도 로그(_collection_log.csv)에 SKIPPED_ALREADY_COVERED로 남긴다.

사용법
------
- 필요하면 END_DATE만 조정 (기본값: 오늘)
- python collect_vds_v2.py
- 중간에 끊겨도 다시 실행하면 이미 받은 부분은 자동으로 건너뛰고 이어서 받는다.
"""

import os
import re
import csv as csv_module
import random
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://data.ex.co.kr/portal/traffic/trafficVds"
DOWNLOAD_DIR = r"C:\Users\USER\busancargo\비타민 데이터분석 5조\교통데이터_원본\vds_downloads"

# RQ1(물동량-통행량 상관분석)에 필요한 전체 대상.
# 구간명은 실제 사이트 드롭다운에 표시되는 텍스트 그대로 넣어야 한다.
# (아래 값들은 그동안 실제로 다운로드된 파일명에서 확인된 표기 그대로임)
TARGET_ROUTES = {
    "부산신항선": [
        "진해IC-남문대교",
        "남진례IC-대청IC",
        "대청IC-진해IC",
        "진례JC-남진례IC",   # <- 지금까지 2021-02-28 이후로 수집이 끊겨있던 구간. 최우선으로 채워야 함
    ],
    "남해2지선": [
        "가락IC-서부산TG",
        "서부산IC-가락IC",
    ],
    "남해선(순천-부산)": [
        "김해JC-동김해IC",
        "북부산TG-김해JC",
    ],
    "중앙선": [
        "대동IC-초정IC",
        "대감JC-대동IC",
    ],
}

START_DATE = date(2022, 1, 1)     # 필요시 더 과거로 조정
END_DATE = date.today()           # 오늘 기준 확보 가능한 마지막 날까지
WINDOW_DAYS = 30                  # 사이트 제한(최대 30일)

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
# 이미 수집된 부분 스캔
# ============================================================

def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def load_existing_coverage(download_dir: str, target_routes: dict):
    """download_dir 안의 기존 VDS_*.csv 파일명을 파싱해서
    (노선, 구간) -> 이미 커버된 날짜(date) 집합 을 돌려준다.

    파일명 형식: VDS_{route}_{section}_{YYYYMMDD}_{YYYYMMDD}.csv
    route/section 자체에 '_'가 들어있지 않다는 전제 하에, 알고 있는
    (route, section) 조합의 접두어와 정확히 매칭시켜서 애매함을 없앤다.

    한글 파일명이 NFC(완성형)/NFD(자모분리형) 두 가지로 섞여 저장돼 있을 수 있어서
    ("가락" 같은 글자가 macOS/클라우드 동기화를 거치면 자모분리형으로 저장되는 경우가 실제로 있었음)
    비교 전에 양쪽 다 NFC로 정규화해서 매칭한다. 이걸 안 하면 파일이 분명히 있는데도
    "이미 수집된 부분 0건"으로 잘못 판정된다.
    """
    coverage = defaultdict(set)
    if not os.path.isdir(download_dir):
        print(f"⚠️ DOWNLOAD_DIR가 존재하지 않습니다: {download_dir}")
        print("   -> os.makedirs로 방금 새로 만들어졌다면, 기존 vds_downloads 폴더와")
        print("      다른 위치에서 스크립트를 실행하고 있다는 뜻입니다. 스크립트를")
        print("      기존 collect_vds.py가 있던 폴더로 옮겨서 다시 실행해보세요.")
        return coverage

    existing_files = os.listdir(download_dir)
    csv_files = [f for f in existing_files if f.lower().endswith(".csv")]
    print(f"[진단] DOWNLOAD_DIR = {download_dir}")
    print(f"[진단] 그 안에서 발견한 .csv 파일 수: {len(csv_files)}개")
    if csv_files:
        print(f"[진단] 예시 파일명: {csv_files[:3]}")

    # 파일명 -> NFC 정규화된 파일명 매핑 (한 번만 정규화)
    existing_nfc = {_nfc(f): f for f in csv_files}
    date_pat = re.compile(r"^(\d{8})_(\d{8})\.csv$")

    matched_files = 0
    for route, sections in target_routes.items():
        for section in sections:
            prefix = _nfc(f"VDS_{route}_{section}_")
            for fname_nfc, fname_orig in existing_nfc.items():
                if not fname_nfc.startswith(prefix):
                    continue
                rest = fname_nfc[len(prefix):]
                m = date_pat.match(rest)
                if not m:
                    continue
                matched_files += 1
                s, e = m.groups()
                try:
                    sd = datetime.strptime(s, "%Y%m%d").date()
                    ed = datetime.strptime(e, "%Y%m%d").date()
                except ValueError:
                    continue
                d = sd
                while d <= ed:
                    coverage[(route, section)].add(d)
                    d += timedelta(days=1)

    print(f"[진단] TARGET_ROUTES와 매칭된 기존 파일 수: {matched_files}개 "
          f"(0인데 위 .csv 파일 수는 0이 아니면 노선/구간명 표기가 실제 파일명과 다르다는 뜻)")
    if matched_files == 0 and csv_files:
        print("[진단] 매칭 실패 예시 비교를 위해 기존 파일명 몇 개를 그대로 출력합니다:")
        for f in csv_files[:5]:
            print("   ", repr(f))

    return coverage


def window_fully_covered(covered_days: set, start_d: date, end_d: date) -> bool:
    d = start_d
    while d <= end_d:
        if d not in covered_days:
            return False
        d += timedelta(days=1)
    return True


# ============================================================
# 페이지 조작 함수
# ============================================================

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""


def get_route_code_by_text(page, route_name: str):
    """#trafficLineCode 드롭다운을 읽어서 노선명(텍스트)에 해당하는 value를 찾는다.
    코드를 몰라도/바뀌어도 동작하도록 하드코딩 대신 텍스트 매칭을 사용."""
    options = page.eval_on_selector_all(
        "#trafficLineCode option",
        "els => els.map(e => ({value: e.value, text: e.textContent.trim()}))",
    )
    for opt in options:
        if opt["text"] == route_name:
            return opt["value"]
    print(f"    ⚠️ 노선 '{route_name}' 못 찾음. 실제 옵션: {options}")
    return None


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

    coverage = load_existing_coverage(DOWNLOAD_DIR, TARGET_ROUTES)
    for (route, section), days in coverage.items():
        print(f"[기존 수집 현황] {route} / {section}: {len(days)}일 이미 보유")

    # 이번 실행에서 실제로 받아야 할 (노선, 구간, 시작일, 종료일) 목록을 먼저 계산
    jobs = []
    skipped = 0
    for route_name, sections in TARGET_ROUTES.items():
        for section_name in sections:
            covered_days = coverage.get((route_name, section_name), set())
            for start_d, end_d in date_windows(START_DATE, END_DATE, WINDOW_DAYS):
                if window_fully_covered(covered_days, start_d, end_d):
                    skipped += 1
                    log_rows.append([route_name, section_name, start_d, end_d, "SKIPPED_ALREADY_COVERED"])
                    continue
                jobs.append((route_name, section_name, start_d, end_d))

    print(f"\n총 {len(jobs)}건 다운로드 예정 / {skipped}건은 이미 수집되어 스킵\n")

    if not jobs:
        print("받을 게 없습니다. 종료.")
        _write_log(log_rows)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
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

        # 노선명 -> 코드 캐시 (노선이 바뀔 때만 다시 조회하면 되므로)
        route_code_cache = {}

        try:
            current_route = None
            for route_name, section_name, start_d, end_d in jobs:
                print(f"[{route_name}] {section_name} | {start_d} ~ {end_d}")

                try:
                    page.goto(BASE_URL, wait_until="networkidle")
                    human_pause(0.8, 1.5)

                    if route_name not in route_code_cache:
                        code = get_route_code_by_text(page, route_name)
                        if code is None:
                            log_rows.append([route_name, section_name, start_d, end_d, "ROUTE_NOT_FOUND"])
                            continue
                        route_code_cache[route_name] = code
                    route_code = route_code_cache[route_name]

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
            _write_log(log_rows)


def _write_log(log_rows):
    log_path = os.path.join(DOWNLOAD_DIR, "_collection_log.csv")
    # 기존 로그가 있으면 이어붙이기 (append) - 헤더는 없을 때만 쓴다
    file_exists = os.path.isfile(log_path)
    with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv_module.writer(f)
        if not file_exists:
            writer.writerow(["노선", "구간", "시작일", "종료일", "상태"])
        writer.writerows(log_rows)
    print(f"\n로그 저장(추가): {log_path}")


if __name__ == "__main__":
    run()