# -*- coding: utf-8 -*-
"""
VDS 원본(vds_downloads, 30일 단위 다운로드) -> 1차정리 구조로 재구성 + 화물차환산 통행량 산출
================================================================================================

입력: 교통데이터_원본/vds_downloads/VDS_{노선}_{구간}_{시작일}_{종료일}.csv
      (CP949 인코딩, 컬럼: 구간,일자,교통량  <- 이미 "일자별 총교통량"으로 집계된 값,
       시간대별 데이터 아님)

출력 1 (원본 정리): 교통데이터_1차정리/시간대별_화물차추정통행량/VDS_구간별_시간대별/
                    {노선}/{구간}/{연도}/{YYYYMM}_{구간}.csv
                    컬럼: 일자,교통량
                    (여러 번 받은 30일 윈도우가 겹칠 수 있어서 날짜 기준으로 중복 제거함.
                     겹치는데 값이 다르면 경고를 출력하고 더 나중에 나온 파일 값을 채택)

출력 2 (화물차환산): 교통데이터_1차정리/시간대별_화물차추정통행량/차종별추정교통량_구간별_시간대별/
                    {노선}/{구간}/{연도}/{YYYYMM}_{구간}.csv
                    컬럼: 일자,교통량,화물차비중,화물차환산_통행량
                    화물차비중은 AXIS_TCS_STATIONS에 지정된 "같은 축" TCS 영업소의
                    해당 일자 화물차비중(3+4+5종/총)을 사용 (여러 영업소면 단순 평균).
                    TCS 쪽 그 날짜 데이터가 없으면 화물차환산_통행량은 빈 값(NaN)으로 둠
                    (임의로 0 채우지 않음 - 전체 계획 문서의 결측 처리 원칙과 동일).

주의: 이 스크립트는 매번 원본을 다시 읽어서 1차정리 폴더를 새로 만듭니다(증분 처리 아님).
      VDS 원본 파일 자체가 크지 않아서(수십KB 단위) 매번 전체 재생성해도 빠르고,
      "부분 갱신으로 인한 stale 데이터" 위험이 없어서 더 안전합니다.

배치 위치: 스크립트를 어디에 두고 실행하든(busancargo 루트든 다른 폴더든) 항상 같은
      데이터 폴더를 보도록 PROJECT_DIR 등을 전부 절대경로로 고정했습니다. 나중에 폴더
      구조가 바뀌면 아래 CONFIG 블록만 고치면 됩니다.

실행 전 확인할 것: AXIS_TCS_STATIONS 매핑은 1차 가정입니다(어떤 도로가 어떤 TCS 영업소와
같은 축인지는 전체 계획 문서에 명시적 1:1 대응이 없어서 임의로 잡았습니다). 최종 분석 전에
팀에서 실제 도로 연결관계를 보고 재검토 해주세요.
"""

import re
import csv as csv_module
import unicodedata
from pathlib import Path
from collections import defaultdict
from datetime import date, datetime

# ============================================================
# CONFIG
# ============================================================

# 전부 절대경로로 고정 (스크립트를 어디에 두고 실행하든 항상 같은 폴더를 보게 하기 위함 -
# 예전에 ROOT=스크립트 위치 기준 상대경로를 썼다가, 스크립트가 실제로는
# "비타민 데이터분석 5조" 폴더 밖(busancargo 루트)에 있어서 엉뚱한 새 폴더에
# 저장되는 문제가 있었음)
PROJECT_DIR = Path(r"C:\Users\USER\busancargo\비타민 데이터분석 5조")
VDS_RAW_DIR = PROJECT_DIR / "교통데이터_원본" / "vds_downloads"
BASE_1CHA = PROJECT_DIR / "교통데이터_1차정리" / "시간대별_화물차추정통행량"
VDS_OUT_DIR = BASE_1CHA / "VDS_구간별_시간대별"
CARGO_OUT_DIR = BASE_1CHA / "차종별추정교통량_구간별_시간대별"
TCS_DIR = BASE_1CHA / "TCS_영업소별_시간대별"

# 어떤 노선이 어떤 TCS 영업소(들)와 "같은 축"인지 (1차 가정 - 검토 필요)
AXIS_TCS_STATIONS = {
    "남해2지선": ["가락"],
    "남해선(순천-부산)": ["대동"],
    "중앙선": ["대동"],
    "부산신항선": ["남진례", "대청", "진해", "진해본선", "진례"],
}


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def find_child(parent: Path, name: str):
    if not parent.exists():
        return None
    target = _nfc(name)
    for child in parent.iterdir():
        if _nfc(child.name) == target:
            return child
    return None


def resolve_or_make_child(parent: Path, name: str) -> Path:
    """find_child로 이미 있는 폴더(NFC든 NFD든)를 그대로 쓰고, 없을 때만 새로 만든다.
    (extract_tcs_stations.py에서 실제로 겪은 버그: 정규화 없이 parent/name으로 바로
    쓰면 기존 폴더 옆에 안 보이는 중복 폴더가 생길 수 있어서, 쓰기 경로도 항상 이걸
    통해서 잡는다.)"""
    existing = find_child(parent, name)
    if existing is not None:
        return existing
    new_path = parent / name
    new_path.mkdir(parents=True, exist_ok=True)
    return new_path


# ============================================================
# 1단계: 원본 VDS 파일 읽어서 (노선,구간)별 일자->교통량 딕셔너리로 합치기
# ============================================================

RAW_NAME_PAT = re.compile(r"^VDS_(.+?)_(.+?)_(\d{8})_(\d{8})\.csv$")


def load_raw_vds(raw_dir: Path):
    """returns: {(road, section): {date: volume}}"""
    data = defaultdict(dict)
    conflicts = 0
    if not raw_dir.exists():
        print(f"⚠️ 원본 폴더가 없습니다: {raw_dir}")
        return data

    files = sorted(raw_dir.iterdir(), key=lambda p: p.name)  # 이름순 = 대체로 다운로드 순서와 비슷
    for path in files:
        m = RAW_NAME_PAT.match(_nfc(path.name))
        if not m:
            continue
        road, section, _s, _e = m.groups()
        try:
            with open(path, encoding="cp949", errors="ignore", newline="") as f:
                reader = csv_module.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) < 3:
                        continue
                    _seg, 일자_raw, 교통량_raw = row[0], row[1], row[2]
                    try:
                        d = datetime.strptime(일자_raw.strip(), "%Y.%m.%d").date()
                    except ValueError:
                        continue
                    try:
                        vol = int(교통량_raw.replace(",", "").strip())
                    except ValueError:
                        continue

                    key = (road, section)
                    if d in data[key] and data[key][d] != vol:
                        conflicts += 1
                    data[key][d] = vol  # 나중 파일이 이전 값을 덮어씀
        except Exception as e:
            print(f"  ⚠️ 읽기 실패: {path.name} ({e})")

    if conflicts:
        print(f"⚠️ 같은 날짜인데 값이 다른 경우가 {conflicts}건 있었습니다 (더 나중에 처리된 파일 값 채택).")
    return data


def write_vds_organized(data: dict):
    """(road, section) -> {date: vol} 을 연/월별 csv로 저장"""
    for (road, section), day_map in data.items():
        by_month = defaultdict(list)
        for d, vol in day_map.items():
            by_month[(d.year, d.month)].append((d, vol))

        for (year, month), rows in by_month.items():
            road_dir = resolve_or_make_child(VDS_OUT_DIR, road)
            section_dir = resolve_or_make_child(road_dir, section)
            out_dir = resolve_or_make_child(section_dir, str(year))
            out_path = out_dir / f"{year}{month:02d}_{section}.csv"
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv_module.writer(f)
                w.writerow(["일자", "교통량"])
                for d, vol in sorted(rows):
                    w.writerow([d.isoformat(), vol])

    total_days = sum(len(v) for v in data.values())
    print(f"VDS 1차정리 완료: {len(data)}개 구간, 총 {total_days}일치 저장 -> {VDS_OUT_DIR}")


# ============================================================
# 2단계: TCS 영업소별 일자별 화물차비중 로드
# ============================================================

def load_daily_cargo_ratio(station: str):
    """TCS_영업소별_시간대별/{station}/**/*.csv 를 전부 읽어서
    일자 -> 화물차비중(=  (3+4+5종 합) / 총교통량 합, 하루 전체 시간 합산 기준) 딕셔너리로 반환."""
    station_dir = find_child(TCS_DIR, station)
    result = {}
    if station_dir is None:
        return result

    daily_sum = defaultdict(lambda: [0, 0])  # 일자 -> [3+4+5종합, 총교통량합]
    for year_dir in station_dir.iterdir():
        if not year_dir.is_dir():
            continue
        for f in year_dir.iterdir():
            if not f.name.endswith(".csv"):
                continue
            with open(f, encoding="utf-8-sig", newline="") as fh:
                reader = csv_module.DictReader(fh)
                for row in reader:
                    try:
                        d = row["집계일자"]
                        cargo = float(row["3종교통량"]) + float(row["4종교통량"]) + float(row["5종교통량"])
                        total = float(row["총교통량"])
                    except (KeyError, ValueError):
                        continue
                    daily_sum[d][0] += cargo
                    daily_sum[d][1] += total

    for d, (cargo, total) in daily_sum.items():
        if total > 0:
            result[d] = cargo / total
    return result


def build_axis_ratio_cache():
    """AXIS_TCS_STATIONS에 등장하는 모든 영업소의 일자별 화물차비중을 한 번씩만 로드해서 캐시."""
    all_stations = {s for stations in AXIS_TCS_STATIONS.values() for s in stations}
    cache = {}
    for st in all_stations:
        ratios = load_daily_cargo_ratio(st)
        cache[st] = ratios
        print(f"  [TCS 화물차비중] {st}: {len(ratios)}일치 로드")
    return cache


def axis_ratio_for(road: str, iso_date: str, cache: dict):
    stations = AXIS_TCS_STATIONS.get(road, [])
    vals = [cache[st][iso_date] for st in stations if st in cache and iso_date in cache[st]]
    if not vals:
        return None
    return sum(vals) / len(vals)


# ============================================================
# 3단계: 화물차환산 통행량 계산 + 저장
# ============================================================

def write_cargo_adjusted(data: dict, ratio_cache: dict):
    n_written = 0
    n_missing_ratio = 0
    for (road, section), day_map in data.items():
        by_month = defaultdict(list)
        for d, vol in day_map.items():
            ratio = axis_ratio_for(road, d.isoformat(), ratio_cache)
            if ratio is None:
                n_missing_ratio += 1
                cargo_vol = ""  # TCS 비중 없음 -> 빈 값(NaN) 유지, 0으로 채우지 않음
            else:
                cargo_vol = round(vol * ratio, 2)
            by_month[(d.year, d.month)].append((d, vol, ratio, cargo_vol))

        for (year, month), rows in by_month.items():
            road_dir = resolve_or_make_child(CARGO_OUT_DIR, road)
            section_dir = resolve_or_make_child(road_dir, section)
            out_dir = resolve_or_make_child(section_dir, str(year))
            out_path = out_dir / f"{year}{month:02d}_{section}.csv"
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv_module.writer(f)
                w.writerow(["일자", "교통량", "화물차비중", "화물차환산_통행량"])
                for d, vol, ratio, cargo_vol in sorted(rows):
                    w.writerow([d.isoformat(), vol, "" if ratio is None else round(ratio, 6), cargo_vol])
            n_written += len(rows)

    print(f"화물차환산 통행량 저장 완료: {n_written}행 (TCS 비중 없어서 빈 값 처리된 날: {n_missing_ratio}건) "
          f"-> {CARGO_OUT_DIR}")


def main():
    print("[진단] PROJECT_DIR =", PROJECT_DIR, "| 존재:", PROJECT_DIR.exists())
    print("[진단] VDS_RAW_DIR =", VDS_RAW_DIR, "| 존재:", VDS_RAW_DIR.exists())
    print("[진단] TCS_DIR     =", TCS_DIR, "| 존재:", TCS_DIR.exists())
    if not PROJECT_DIR.exists():
        print("⚠️ PROJECT_DIR가 없습니다. CONFIG의 경로가 맞는지 먼저 확인하세요.")
        return

    print("\n=== 1단계: 원본 VDS 읽기 ===")
    data = load_raw_vds(VDS_RAW_DIR)
    if not data:
        print("읽은 데이터가 없습니다. VDS_RAW_DIR 경로를 확인하세요:", VDS_RAW_DIR)
        return
    for (road, section), day_map in sorted(data.items()):
        print(f"  {road} / {section}: {len(day_map)}일")

    print("\n=== 2단계: 1차정리 구조로 저장 ===")
    write_vds_organized(data)

    print("\n=== 3단계: TCS 화물차비중 로드 ===")
    ratio_cache = build_axis_ratio_cache()

    print("\n=== 4단계: 화물차환산 통행량 계산/저장 ===")
    write_cargo_adjusted(data, ratio_cache)


if __name__ == "__main__":
    main()
