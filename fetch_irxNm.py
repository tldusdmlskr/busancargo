"""
부산광역시_스마트교차로 접근로 교통량 정보 API에서
전체 교차로명(ixrNm) 리스트를 뽑아내는 스크립트

- 날짜(yyyyMMdd) + 시간(hour) 파라미터가 필수라서,
  "설치된 교차로 전체 목록"을 한번에 주는 API가 아님
  → 아무 날짜/시간 하나 찍어서 호출하면, 그 시점에 데이터가 잡힌
    교차로들이 나옴 (설치된 전체 목록과 정확히 같지 않을 수 있음,
    다만 실제로 운영 중인 교차로 확인용으로는 충분)
- pageNo를 늘려가며 끝까지 수집 (totalCount 기준으로 반복 종료)

사용 전에 채워야 하는 것
------------------------
SERVICE_KEY : 공공데이터포털에서 발급받은 인증키
BASE_URL    : Swagger에서 확인한 실제 요청주소
              (서비스수준 API가 http://apis.data.go.kr/6260000/BusanITSSINTSRVC/SRVCLV 였으니
               접근로 교통량 API도 비슷한 패턴일 가능성이 높음 - 실제 주소는 활용신청 상세페이지에서 확인)
TARGET_DATE / TARGET_HOUR : 조회할 날짜/시간 (최근 날짜로 설정 권장)
"""

import time
import requests
import pandas as pd

SERVICE_KEY = "dzDG4tcUUJoRrqZ5oubnx8XbqyBnBTFjXtG5QEBvgQ+HnMDPEqJFGgotwHV5HZAv7k7gqYUXdPLoKs9vWFULLQ=="
ENDPOINT_URL = "https://apis.data.go.kr/6260000/BusanITSSINTACR/ACRTrf"

# ⚠️ 반드시 "실제 컴퓨터의 오늘 날짜" 기준으로 넣을 것
# (공공데이터포털 서버는 진짜 현실 날짜 기준으로 동작하므로, 미래 날짜를 넣으면
#  INVALID_REQUEST_PARAMETER_ERROR 또는 빈 결과가 돌아옴)
import datetime
_today = datetime.date.today()
TARGET_DATE = _today.strftime("%Y%m%d")   # 예: 오늘이면 자동으로 채워짐. 필요시 직접 "20250809"처럼 수정
TARGET_HOUR = 10           # 0~23, Swagger 예시와 동일하게 18로 우선 테스트
NUM_OF_ROWS = 100          # Swagger 예시값과 동일하게 100으로 복구 (10은 진단용이었음)
MAX_PAGES = 1             # 혹시 totalCount를 못 읽을 경우 대비한 안전장치
SLEEP_SEC = 0.3            # 페이지 사이 대기 (서버 부하 방지)


def fetch_all_items(date_str: str, hour: int) -> pd.DataFrame:
    """지정한 날짜·시간에 대해 전체 페이지를 순회하며 접근로 교통량 데이터를 모두 가져온다."""
    all_items = []
    page = 1
    total_count = None

    while True:
        params = {
            "serviceKey": SERVICE_KEY,
            "pageNo": str(page),
            "numOfRows": str(NUM_OF_ROWS),
            "yyyyMMdd": date_str,
            "hour": f"{int(hour):02d}",   # 2자리 고정 포맷으로 시도 (예: 17 -> "17", 7 -> "07")
        }
        res = requests.get(ENDPOINT_URL, params=params, timeout=15)
        if res.status_code != 200:
            print("=== 에러 응답 확인 ===")
            print("status:", res.status_code)
            print("요청 URL:", res.url)
            print("응답 본문:", res.text[:1000])
            print("======================")
        res.raise_for_status()
        data = res.json()

        # 진단용: 첫 페이지는 무조건 원본 응답을 보여줌
        if page == 1:
            print("=== 원본 응답 (진단용) ===")
            print(data)
            print("==========================")

        content = data.get("content", data)  # 혹시 content 래핑이 없는 응답 형태 대비
        items = content.get("items", [])

        # items가 리스트가 아니라 단일 dict로 오는 경우 대비
        if isinstance(items, dict):
            items = [items]

        if not items:
            break

        all_items.extend(items)

        if total_count is None:
            total_count = content.get("totalCount", None)

        print(f"  page {page}: {len(items)}건 수집 (누적 {len(all_items)}건)")

        page += 1
        if total_count is not None and len(all_items) >= total_count:
            break
        if page > MAX_PAGES:
            print("  MAX_PAGES 도달 - 중단 (필요시 MAX_PAGES 늘려서 재실행)")
            break

        time.sleep(SLEEP_SEC)

    return pd.DataFrame(all_items)


def main():
    print(f"{TARGET_DATE} {TARGET_HOUR}시 기준 접근로 교통량 데이터 수집 시작")
    df = fetch_all_items(TARGET_DATE, TARGET_HOUR)

    if df.empty:
        print("데이터 없음 - 날짜/시간/서비스키/BASE_URL 확인 필요")
        return

    df.to_csv("smart_intersection_raw.csv", index=False, encoding="utf-8-sig")
    print(f"\n원본 저장 완료: smart_intersection_raw.csv ({len(df)}행)")

    # 교차로명 컬럼 후보 (실제 응답 필드명 확인 후 필요시 수정)
    name_col_candidates = ["ixrNm", "교차로명", "intersectionNm"]
    name_col = next((c for c in name_col_candidates if c in df.columns), None)

    if name_col is None:
        print("\n교차로명 컬럼을 못 찾음. 실제 컬럼 목록:")
        print(df.columns.tolist())
        return

    ixr_list = sorted(df[name_col].dropna().unique())
    print(f"\n총 교차로 수: {len(ixr_list)}개")
    for name in ixr_list:
        print(" -", name)

    pd.Series(ixr_list, name="교차로명").to_csv(
        "smart_intersection_list.csv", index=False, encoding="utf-8-sig"
    )
    print("\n교차로명 리스트 저장 완료: smart_intersection_list.csv")

    # 신항 관련 키워드로 바로 필터링까지
    keywords = ["세산", "가락", "녹산", "신항", "낙동", "명지", "강서"]
    pattern = "|".join(keywords)
    matched = [n for n in ixr_list if any(k in n for k in keywords)]
    print(f"\n[신항 관련 키워드 매칭] {len(matched)}개")
    for name in matched:
        print(" ★", name)


if __name__ == "__main__":
    main()
    