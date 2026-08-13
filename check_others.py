"""
부산광역시_스마트교차로 접근로 교통량 정보 - 본수집 스크립트
API: https://apis.data.go.kr/6260000/BusanITSSINTACR/ACRTrf

탐색용 스크립트(fetch_smart_intersection_list.py)로 확인된 신항 관련 6개 지점만
날짜×시간 전체를 돌면서 수집한다.

특징
----
- 하루 단위로 파일을 따로 저장(smart_YYYYMMDD.csv) -> 이미 받은 날짜는 자동 스킵(재개 가능)
- 하루 안에서 0~23시 전체 시간대 수집, 각 시간대마다 페이지네이션 처리
- 응답에서 6개 대상 교차로(ixrNm)만 필터링해서 저장 (용량 절약)
- 요청 실패 시 짧게 재시도 후 넘어감 (전체가 멈추지 않도록)
"""

import os
import time
import random
import datetime
import requests
import pandas as pd

# ============================================================
# CONFIG
# ============================================================

SERVICE_KEY = "dzDG4tcUUJoRrqZ5oubnx8XbqyBnBTFjXtG5QEBvgQ+HnMDPEqJFGgotwHV5HZAv7k7gqYUXdPLoKs9vWFULLQ=="
ENDPOINT_URL = "https://apis.data.go.kr/6260000/BusanITSSINTACR/ACRTrf"

OUTPUT_DIR = os.path.abspath("./smart_intersection_downloads")

# 수집 대상 6개 지점 (탐색 결과 확인된 정확한 이름 그대로)
TARGET_INTERSECTIONS = [
    "세산교차로",
    "르노삼성정문",
    "르노삼성동문",
    "르노삼성서문",
    "66호광장",
    "명지레포츠센터사거리",
]

# 수집 기간 (⚠️ 데이터가 실제로 존재하는 과거 범위를 아직 다 확인 못했으면,
#            처음엔 최근 1~2개월만 좁게 잡아서 테스트 후 넓히는 걸 추천)
START_DATE = datetime.date(2025, 1, 6)
END_DATE = datetime.date.today() - datetime.timedelta(days=1)  # 오늘 데이터는 아직 없을 수 있어 어제까지

HOURS = list(range(24))
# 24시간 전체 수집. (참고: 8개 대표 시간대만 쓰면 요청 수 1/3로 줄일 수 있지만,
#  요청에 따라 전체 시간대로 진행)

NUM_OF_ROWS = 100          # 100 -> 500으로 늘려서 한 시간대를 1페이지로 끝냄 (페이지네이션 최소화)
MAX_PAGES = 5               # 혹시 500으로도 안 끝나는 경우 대비한 안전장치
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
SLEEP_BETWEEN_CALLS = (0.3, 0.7)   # 랜덤 대기 범위(초)
SLEEP_BETWEEN_DAYS = (1.0, 2.0)

# --- 일일 API 호출 한도 안전장치 ---
# data.go.kr 활용신청 승인 단계별로 하루 호출 한도가 다름 (마이페이지 > 활용신청현황에서 확인 가능).
# 한도를 모르면 우선 보수적으로 잡아두고, 확인되면 정확한 값으로 바꿀 것.
DAILY_REQUEST_BUDGET = 900   # 하루 허용치보다 여유 있게 낮춰서 설정 (예: 한도가 1000이면 900)
_request_count = 0


def sleep_random(rng):
    time.sleep(random.uniform(*rng))


class BudgetExceeded(Exception):
    """하루 API 호출 한도에 도달했을 때 발생시켜 안전하게 중단하기 위한 예외."""
    pass


# ============================================================
# API 호출
# ============================================================

def fetch_hour(date_str: str, hour: int) -> list:
    """지정 날짜·시간의 전체 페이지를 순회하며 원본 items를 모두 반환."""
    global _request_count
    all_items = []
    page = 1
    total_count = None

    while True:
        if _request_count >= DAILY_REQUEST_BUDGET:
            print(f"  🛑 일일 요청 한도({DAILY_REQUEST_BUDGET}회) 도달 - 여기서 중단")
            raise BudgetExceeded()

        params = {
            "serviceKey": SERVICE_KEY,
            "pageNo": str(page),
            "numOfRows": str(NUM_OF_ROWS),
            "yyyyMMdd": date_str,
            "hour": hour,
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                res = requests.get(ENDPOINT_URL, params=params, timeout=REQUEST_TIMEOUT)
                _request_count += 1
                res.raise_for_status()
                data = res.json()
                break
            except Exception as e:
                if attempt == MAX_RETRIES:
                    print(f"    ⚠️ {date_str} {hour}시 page{page} 요청 실패(재시도 소진): {e}")
                    return all_items
                time.sleep(1.0 * attempt)

        if data.get("resultCode") not in ("00", None):
            # 정상 아님 (에러 코드) - 로그만 남기고 이 시간대는 스킵
            print(f"    ⚠️ {date_str} {hour}시: resultCode={data.get('resultCode')} msg={data.get('resultMsg')}")
            return all_items

        content = data.get("content", {})
        items = content.get("items", [])
        if isinstance(items, dict):
            items = [items]
        if not items:
            break

        all_items.extend(items)

        if total_count is None:
            total_count = content.get("totalCount")

        page += 1
        if total_count is not None and len(all_items) >= total_count:
            break
        if page > MAX_PAGES:
            break

        sleep_random(SLEEP_BETWEEN_CALLS)

    return all_items


def collect_day(date_str: str) -> pd.DataFrame:
    """하루치(0~23시) 전체 수집 후, 대상 6개 교차로만 필터링해서 반환."""
    day_items = []
    for hour in HOURS:
        items = fetch_hour(date_str, hour)
        day_items.extend(items)
        sleep_random(SLEEP_BETWEEN_CALLS)

    if not day_items:
        return pd.DataFrame()

    df = pd.DataFrame(day_items)
    if "ixrNm" not in df.columns:
        print("    ⚠️ ixrNm 컬럼이 없음 - 실제 컬럼:", df.columns.tolist())
        return pd.DataFrame()

    filtered = df[df["ixrNm"].isin(TARGET_INTERSECTIONS)].copy()
    return filtered


# ============================================================
# 메인
# ============================================================

def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    est_calls_per_day = len(HOURS) * 1  # numOfRows=500이면 대부분 1페이지로 끝남
    est_days_per_budget = DAILY_REQUEST_BUDGET // max(est_calls_per_day, 1)
    print(f"시간대 {len(HOURS)}개 x 약 1페이지 = 하루당 약 {est_calls_per_day}회 요청 예상")
    print(f"일일 한도({DAILY_REQUEST_BUDGET}회) 기준 한 번 실행에 약 {est_days_per_budget}일치 수집 가능 (대략적인 추정)\n")

    cur = START_DATE
    while cur <= END_DATE:
        date_str = cur.strftime("%Y%m%d")
        out_path = os.path.join(OUTPUT_DIR, f"smart_{date_str}.csv")

        if os.path.exists(out_path):
            print(f"[{date_str}] 이미 수집됨 - 스킵")
            cur += datetime.timedelta(days=1)
            continue

        print(f"[{date_str}] 수집 시작")
        try:
            df = collect_day(date_str)
        except BudgetExceeded:
            print(f"\n오늘 요청 한도를 다 써서 중단함. 총 {_request_count}회 호출.")
            print(f"'{date_str}' 이전 날짜까지는 저장 완료. 스크립트를 나중에(예: 내일) 다시 실행하면")
            print("이미 받은 날짜는 자동 스킵하고 이어서 진행됨.")
            break

        if df.empty:
            print(f"  -> 데이터 없음 (빈 파일로 표시해서 재수집 방지)")
            # 빈 파일이라도 만들어둬야 다음 실행 때 다시 이 날짜를 스킵함
            pd.DataFrame(columns=["ixrNm"]).to_csv(out_path, index=False, encoding="utf-8-sig")
        else:
            df.to_csv(out_path, index=False, encoding="utf-8-sig")
            print(f"  ✅ 저장: smart_{date_str}.csv ({len(df)}행)")

        cur += datetime.timedelta(days=1)
        sleep_random(SLEEP_BETWEEN_DAYS)

    # 전체 병합
    all_files = sorted(
        f for f in os.listdir(OUTPUT_DIR) if f.startswith("smart_") and f.endswith(".csv")
    )
    frames = []
    for f in all_files:
        try:
            d = pd.read_csv(os.path.join(OUTPUT_DIR, f))
            if not d.empty:
                frames.append(d)
        except Exception:
            pass

    if frames:
        merged = pd.concat(frames, ignore_index=True)
        merged_path = os.path.join(OUTPUT_DIR, "_merged_all.csv")
        merged.to_csv(merged_path, index=False, encoding="utf-8-sig")
        print(f"\n전체 병합 완료: {merged_path} (총 {len(merged)}행)")
    else:
        print("\n병합할 데이터가 없음")


if __name__ == "__main__":
    run()