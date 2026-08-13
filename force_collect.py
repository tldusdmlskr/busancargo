import os
os.environ["SERVICE_KEY_1"] = "a58d0e6bc4f6352a20578bfc8352853f83c9a85cc9d929761caca4e17e072bc8"  # 팀원 키 중 아무거나 값 직접 넣기

from collect_all import collect_all_items, save_to_csv, LINK_BASE_URL, AVI_BASE_URL

# 놓친 슬롯 라벨을 직접 지정 (예: 3시 슬롯이었다면)
slot_label = "20260730_1500"  # 실제 날짜_시간으로 수정
service_key = os.environ["SERVICE_KEY_1"]

link_items = collect_all_items(LINK_BASE_URL, service_key, "LINK")
save_to_csv(link_items, "link_traffic_all", slot_label)

avi_items = collect_all_items(AVI_BASE_URL, service_key, "AVI")
save_to_csv(avi_items, "avi_traffic_all", slot_label)

print("강제 수집 완료")