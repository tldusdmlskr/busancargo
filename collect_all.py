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

# ------------------------------------------------------------
# 원래 계획한 목표 시각 그대로 사용 (실행 시각 역산 방식 폐기)
# ------------------------------------------------------------
TARGET_HOURS = [7, 8, 9, 12, 17, 18, 19, 21]

SLOT_KEY_MAP = {
    7: 1, 8: 2, 9: 3, 12: 4,
    17: 1, 18: 2, 19: 3, 21: 4,
}

TARGET_DATES = {"2026-07-29", "2026-07-30", "2026-07-31",
                 "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"}

# 목표 시각이 지난 뒤 이 시간(분) 안에만 캐치업 인정
# 워크플로가 15분마다 깨어나므로, 최대 지연을 넉넉히 흡수하도록 90분으로 설정
CATCHUP_WINDOW_MIN = 90

NUM_OF_ROWS = 100
REQUEST_DELAY = 0.2

OUTPUT_DIR = "collected_data"
os.makedirs(OUTPUT_DIR, exist_ok=True)

LINK_BASE_URL = "https://apis.data.go.kr/6260000/BusanITSLINKTraffic/LINKTrafficList"
AVI_BASE_URL = "https://apis.data.go.kr/6260000/BusanITSAVI/AVIList"


# ============================================================
# 슬롯 판단: "실행 시각"이 아니라 "목표 시각으로부터 경과 시간"으로 판단
# ============================================================
def check_collection_slot():
    now = datetime.now(KST)
    date_str = now.strftime("%Y-%m-%d")

    if date_str not in TARGET_DATES:
        print(f"[스킵] {date_str}는 수집 대상 날짜가 아님")
        return None, None

    for target_hour in TARGET_HOURS:
        target_time = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        elapsed_min = (now - target_time).total_seconds() / 60

        # 아직 목표 시각이 안 됐으면 이 target은 건너뛰고 다음 target 확인
        if elapsed_min < 0:
            continue
        # 목표 시각이 지났지만 캐치업 윈도우를 넘었으면 이 target은 영구 소실 → 다음 target 확인
        if elapsed_min > CATCHUP_WINDOW_MIN:
            continue

        # 여기 도달하면 "목표 시각을 막 지났고, 아직 캐치업 윈도우 안"인 target
        slot_label = f"{date_str.replace('-', '')}_{target_hour:02d}00"
        expected_file = os.path.join(OUTPUT_DIR, f"link_traffic_all_{slot_label}.csv")

        if os.path.exists(expected_file):
            # 이미 수집된 슬롯이면 다음 target 확인 (혹시 여러 target이 캐치업 윈도우에 겹칠 수 있으니 계속 순회)
            continue

        key_no = SLOT_KEY_MAP[target_hour]
        service_key = KEYS.get(key_no)
        if not service_key:
            raise RuntimeError(f"KEY_{key_no}가 설정되지 않았습니다 (환경변수 확인 필요)")

        print(f"[진행] 현재 {now.strftime('%H:%M')} — 목표 {target_hour}시로부터 {elapsed_min:.0f}분 경과, "
              f"아직 미수집 → 지금 수집 (담당 키: KEY_{key_no}, 라벨: {slot_label})")
        return service_key, slot_label

    print(f"[스킵] 현재 {now.strftime('%H:%M')} — 수집 대기 중이거나 이미 완료된 슬롯뿐임")
    return None, None


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
# 저장 함수: 필터링 없이 전체 그대로 CSV 저장 (target_hour 기준 라벨)
# ============================================================
def save_to_csv(items, filename_prefix, slot_label):
    filename = os.path.join(OUTPUT_DIR, f"{filename_prefix}_{slot_label}.csv")
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
    service_key, slot_label = check_collection_slot()
    if service_key is None:
        return

    print(f"=== [{slot_label}] 통합 수집 시작 ===")

    link_items = collect_all_items(LINK_BASE_URL, service_key, "LINK")
    save_to_csv(link_items, "link_traffic_all", slot_label)

    avi_items = collect_all_items(AVI_BASE_URL, service_key, "AVI")
    save_to_csv(avi_items, "avi_traffic_all", slot_label)

    print(f"=== [{slot_label}] 통합 수집 완료 ===\n")


if __name__ == "__main__":
    main()