import requests
import time
import csv
import os
from datetime import datetime, timezone, timedelta

# ============================================================
# 공통 설정
# ============================================================
KST = timezone(timedelta(hours=9))

# GitHub Secrets 등 환경변수에서 4개 키 로드 (하드코딩 금지)
KEYS = {
    1: os.environ.get("SERVICE_KEY_1"),
    2: os.environ.get("SERVICE_KEY_2"),
    3: os.environ.get("SERVICE_KEY_3"),
    4: os.environ.get("SERVICE_KEY_4"),
}

# 시간대별 담당 키 매핑 (키 1개당 하루 2개 시간대 = 186회/일, 500회 한도 내)
SLOT_KEY_MAP = {
    7: 1, 8: 2, 9: 3, 12: 4,
    17: 1, 18: 2, 19: 3, 21: 4,
}

# 수집 대상 날짜 (KST 기준, 7/29~8/4)
TARGET_DATES = {"2026-07-29", "2026-07-30", "2026-07-31",
                 "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"}

NUM_OF_ROWS = 100
REQUEST_DELAY = 0.2

OUTPUT_DIR = "collected_data"
os.makedirs(OUTPUT_DIR, exist_ok=True)

LINK_BASE_URL = "https://apis.data.go.kr/6260000/BusanITSLINKTraffic/LINKTrafficList"
AVI_BASE_URL = "https://apis.data.go.kr/6260000/BusanITSAVI/AVIList"


# ============================================================
# 시간대 가드: 지금이 수집 대상 시각이 아니면 즉시 종료 (API 호출 자체를 안 함)
# ============================================================
def check_collection_slot():
    now = datetime.now(KST)
    date_str = now.strftime("%Y-%m-%d")
    hour = now.hour

    if date_str not in TARGET_DATES:
        print(f"[스킵] {date_str}는 수집 대상 날짜가 아님")
        return None
    if hour not in SLOT_KEY_MAP:
        print(f"[스킵] {hour}시는 수집 대상 시간이 아님")
        return None

    key_no = SLOT_KEY_MAP[hour]
    service_key = KEYS.get(key_no)
    if not service_key:
        raise RuntimeError(f"KEY_{key_no}가 설정되지 않았습니다 (환경변수 확인 필요)")

    print(f"[진행] {date_str} {hour}시 수집 시작 — 담당 키: KEY_{key_no}")
    return service_key


# ============================================================
# 공통 함수: 페이지 하나 호출
# ============================================================
def fetch_page(base_url, service_key, page_no):
    params = {
        "serviceKey": service_key,
        "pageNo": page_no,
        "numOfRows": NUM_OF_ROWS,
    }
    resp = requests.get(base_url, params=params, headers={"accept": "application/json"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("resultCode") != "00":
        raise RuntimeError(f"API 오류: {data.get('resultMsg')}")

    return data["content"]


# ============================================================
# 공통 함수: 전체 페이지 순회 (필터링 없이 전부 수집)
# ============================================================
def collect_all_items(base_url, service_key, label):
    first = fetch_page(base_url, service_key, 1)
    total_count = first["totalCount"]
    total_pages = (total_count + NUM_OF_ROWS - 1) // NUM_OF_ROWS

    print(f"[{label}] 총 {total_count}건, {total_pages}페이지 조회 예정")

    all_items = list(first["items"])

    for page in range(2, total_pages + 1):
        try:
            content = fetch_page(base_url, service_key, page)
            all_items.extend(content["items"])
        except Exception as e:
            print(f"[{label}][경고] {page}페이지 호출 실패: {e} — 재시도 1회")
            time.sleep(1)
            try:
                content = fetch_page(base_url, service_key, page)
                all_items.extend(content["items"])
            except Exception as e2:
                print(f"[{label}][오류] {page}페이지 재시도도 실패, 건너뜀: {e2}")
        time.sleep(REQUEST_DELAY)

        if page % 20 == 0:
            print(f"[{label}] {page}/{total_pages} 페이지 완료")

    print(f"[{label}] 전체 수집 완료: {len(all_items)}건")
    return all_items


# ============================================================
# 저장 함수: 필터링 없이 전체 그대로 CSV 저장
# ============================================================
def save_to_csv(items, filename_prefix, collection_time_label):
    filename = os.path.join(OUTPUT_DIR, f"{filename_prefix}_{collection_time_label}.csv")
    if not items:
        print(f"[경고] {filename_prefix}: 저장할 데이터가 없습니다.")
        return

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=items[0].keys())
        writer.writeheader()
        writer.writerows(items)

    print(f"저장 완료: {filename} ({len(items)}건)")


# ============================================================
# 메인
# ============================================================
def main():
    service_key = check_collection_slot()
    if service_key is None:
        return  # 수집 대상 시각이 아니면 API 호출 없이 바로 종료

    now_label = datetime.now(KST).strftime("%Y%m%d_%H%M")
    print(f"=== [{now_label}] 통합 수집 시작 ===")

    link_items = collect_all_items(LINK_BASE_URL, service_key, "LINK")
    save_to_csv(link_items, "link_traffic_all", now_label)

    avi_items = collect_all_items(AVI_BASE_URL, service_key, "AVI")
    save_to_csv(avi_items, "avi_traffic_all", now_label)

    print(f"=== [{now_label}] 통합 수집 완료 ===\n")


if __name__ == "__main__":
    main()