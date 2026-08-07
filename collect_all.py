import requests
import time
import csv
import os
from datetime import datetime, timezone, timedelta

# ============================================================
# 공통 설정
# ============================================================
KST = timezone(timedelta(hours=9))

KEYS = {
    1: os.environ.get("SERVICE_KEY_1"),
    2: os.environ.get("SERVICE_KEY_2"),
    3: os.environ.get("SERVICE_KEY_3"),
    4: os.environ.get("SERVICE_KEY_4"),
}

# 원래 목표 시각 그대로
TARGET_HOURS = [7, 8, 9, 12, 17, 18, 19, 21]

SLOT_KEY_MAP = {
    7: 1, 8: 2, 9: 3, 12: 4,
    17: 1, 18: 2, 19: 3, 21: 4,
}

# 각 목표 시각의 "-30분"부터 "+90분"까지만 수집 창으로 인정
# (그 목표 시각 전후로만 반응, 다른 목표들 사이 빈 시간엔 아무 것도 안 함)
WINDOW_BEFORE_MIN = 30
WINDOW_AFTER_MIN = 90

# 같은 슬롯 최대 재시도(재수집) 횟수
MAX_RETRIES_PER_SLOT = 2

END_DATE = None  # 예: "2026-09-30", None이면 날짜 제한 없음

NUM_OF_ROWS = 100
REQUEST_DELAY = 0.2

OUTPUT_DIR = "collected_data"
os.makedirs(OUTPUT_DIR, exist_ok=True)

LINK_BASE_URL = "https://apis.data.go.kr/6260000/BusanITSLINKTraffic/LINKTrafficList"
AVI_BASE_URL = "https://apis.data.go.kr/6260000/BusanITSAVI/AVIList"


# ============================================================
# 슬롯 판단: 각 목표 시각마다 독립적인 [-30분, +90분] 창 안에 있는지만 확인
# 여러 목표가 동시에 창 안에 들어오면(거의 없겠지만) 가장 가까운 목표 하나만 처리
# ============================================================
def check_collection_slot():
    now = datetime.now(KST)
    date_str = now.strftime("%Y-%m-%d")

    if END_DATE is not None and date_str > END_DATE:
        print(f"[스킵] {date_str}는 종료일({END_DATE}) 이후라 수집 대상 아님")
        return None, None

    best_target = None
    best_elapsed = None

    for target_hour in TARGET_HOURS:
        target_time = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        elapsed_min = (now - target_time).total_seconds() / 60

        # 이 목표 시각의 수집 창 [-30분, +90분] 안에 들어오는지 확인
        if -WINDOW_BEFORE_MIN <= elapsed_min <= WINDOW_AFTER_MIN:
            if best_target is None or abs(elapsed_min) < abs(best_elapsed):
                best_target = target_hour
                best_elapsed = elapsed_min

    if best_target is None:
        print(f"[스킵] 현재 {now.strftime('%H:%M')} — 어떤 목표 시각의 수집 창에도 해당 없음 "
              f"(목표: {TARGET_HOURS})")
        return None, None

    target_hour = best_target
    slot_label = f"{date_str.replace('-', '')}_{target_hour:02d}00"

    existing_count = sum(
        1 for f in os.listdir(OUTPUT_DIR)
        if f.startswith(f"link_traffic_all_{slot_label}")
    )
    if existing_count >= MAX_RETRIES_PER_SLOT:
        print(f"[스킵] {slot_label} 슬롯은 이미 {existing_count}번 수집됨 (최대 {MAX_RETRIES_PER_SLOT}회 제한)")
        return None, None

    key_no = SLOT_KEY_MAP[target_hour]
    service_key = KEYS.get(key_no)
    if not service_key:
        raise RuntimeError(f"KEY_{key_no}가 설정되지 않았습니다 (환경변수 확인 필요)")

    attempt_no = existing_count + 1
    save_label = slot_label if existing_count == 0 else f"{slot_label}_try{attempt_no}"

    print(f"[진행] 현재 {now.strftime('%H:%M')} — 목표 {target_hour}시로부터 {best_elapsed:.0f}분 "
          f"(담당 키: KEY_{key_no}, 라벨: {save_label}, {attempt_no}/{MAX_RETRIES_PER_SLOT}번째 시도)")
    return service_key, save_label


# ============================================================
# 공통 함수: 페이지 하나 호출 (재시도 강화)
# ============================================================
def fetch_page(base_url, service_key, page_no, max_retries=3, retry_delay=5):
    params = {
        "serviceKey": service_key,
        "pageNo": page_no,
        "numOfRows": NUM_OF_ROWS,
    }
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(base_url, params=params, headers={"accept": "application/json"}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("resultCode") != "00":
                raise RuntimeError(f"API 오류: {data.get('resultMsg')}")
            return data["content"]
        except Exception as e:
            last_error = e
            print(f"[경고] {page_no}페이지 {attempt}/{max_retries}번째 시도 실패: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
    raise last_error


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
        content = fetch_page(base_url, service_key, page)
        all_items.extend(content["items"])
        time.sleep(REQUEST_DELAY)

        if page % 20 == 0:
            print(f"[{label}] {page}/{total_pages} 페이지 완료")

    print(f"[{label}] 전체 수집 완료: {len(all_items)}건")
    return all_items


# ============================================================
# 저장 함수
# ============================================================
def save_to_csv(items, filename_prefix, save_label):
    filename = os.path.join(OUTPUT_DIR, f"{filename_prefix}_{save_label}.csv")
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
    service_key, save_label = check_collection_slot()
    if service_key is None:
        return

    print(f"=== [{save_label}] 통합 수집 시작 ===")

    link_items = collect_all_items(LINK_BASE_URL, service_key, "LINK")
    save_to_csv(link_items, "link_traffic_all", save_label)

    avi_items = collect_all_items(AVI_BASE_URL, service_key, "AVI")
    save_to_csv(avi_items, "avi_traffic_all", save_label)

    print(f"=== [{save_label}] 통합 수집 완료 ===\n")


if __name__ == "__main__":
    main()