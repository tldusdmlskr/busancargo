# -*- coding: utf-8 -*-
"""
RQ1 물동량-통행량 상관분석 (전체 계획 문서의 1~3단계 그대로 구현)
====================================================================

1단계(파생변수)와 2단계(축별 화물차환산 통행량 산출)는
extract_tcs_stations.py / organize_vds_raw.py 에서 이미 끝났다고 가정하고,
이 스크립트는 그 결과를 모아서

  2단계: 물동량(북컨/남컨/서컨) + 도로구간별 교통량을 날짜 기준으로 하나의
         wide 테이블로 합치고
  3단계: 물동량 3개 x 전체 도로구간 조합에 대해 상관계수 행렬을 계산한다.

전체 계획 문서에 있던 유의사항을 그대로 반영:
  - 물동량 결측(NaN)은 0으로 채우지 않고 그대로 둔다
  - 상관계수는 각 (물동량, 도로구간) 쌍마다 pairwise로 NaN을 제거해서 계산한다
    (전체 테이블을 한 번에 dropna() 하지 않음)
  - 서컨물동량은 2024-03-09 이후만 사용 (물동량_상관분석용.csv에 이미 반영되어 있음)
  - 상관계수 크기만으로 주 이동경로를 단정하지 않는다 -> 결과표에 N, p-value를 같이 남겨서
    팀에서 실제 도로 연결관계/공간적 위치와 같이 검토할 수 있게 한다

2026-08-16 팀 논의 반영: 아래 2개 버전을 각각 산출한다.

  버전1 (화물차환산_통행량 버전, 기존과 동일한 방식)
    - 남해선(순천-부산)/남해2지선/중앙선: 화물차환산_통행량
      (가락·대동 TCS 화물차비중을 같은 축 VDS 교통량에 곱한 값)
    - 부산신항선: 화물차환산_통행량
      (진례 TCS 화물차비중을 적용 - 팀에서 "진례 채택"으로 확정. 남진례·대청·진해·
       진해본선은 원본 데이터가 없어서 실질적으로 진례 단독 비중과 동일함)
    -> RQ1_wide_table.csv / RQ1_상관계수_행렬.csv

  버전2 (부산신항선만 총교통량 버전)
    - 남해선(순천-부산)/남해2지선/중앙선: 화물차환산_통행량 (버전1과 동일)
    - 부산신항선: 화물차비중을 곱하지 않은 VDS 원시 총교통량(전 차종 포함)
      -> 부산신항선은 화물차비중 표본이 진례 TCS 영업소 하나뿐이라 너무 적으니,
         화물차 추정치 대신 구간 전체 교통량을 그대로 물동량과 비교해보기로
         팀에서 결정함 (2026-08-16)
    -> RQ1_wide_table_v2_신항선총교통량.csv / RQ1_상관계수_행렬_v2_신항선총교통량.csv
"""

import csv as csv_module
import unicodedata
from pathlib import Path
from collections import defaultdict

import pandas as pd
from scipy.stats import pearsonr

# ============================================================
# CONFIG
# ============================================================

# 전부 절대경로로 고정 (스크립트를 어디에 두고 실행하든 항상 같은 폴더를 보게 하기 위함)
PROJECT_DIR = Path(r"C:\Users\USER\busancargo\비타민 데이터분석 5조")
CARGO_VOL_FILE = PROJECT_DIR / "물동량_downloads" / "물동량_상관분석용.csv"
BASE_1CHA = PROJECT_DIR / "교통데이터_1차정리" / "시간대별_화물차추정통행량"
CARGO_OUT_DIR = BASE_1CHA / "차종별추정교통량_구간별_시간대별"   # 화물차환산_통행량 (일자,교통량,화물차비중,화물차환산_통행량)
VDS_OUT_DIR = BASE_1CHA / "VDS_구간별_시간대별"                  # 원시 총교통량, 전 차종 포함 (일자,교통량)

SINHANG_ROAD = "부산신항선"   # 버전2에서 원시 총교통량으로 대체할 도로

DATE_START = "2022-01-01"
DATE_END = "2026-07-31"      # 2026년 확보된 데이터까지 (2026-08-16 팀 논의로 확장)

OUT_WIDE_TABLE = PROJECT_DIR / "RQ1_wide_table.csv"
OUT_CORR_TABLE = PROJECT_DIR / "RQ1_상관계수_행렬.csv"
OUT_WIDE_TABLE_V2 = PROJECT_DIR / "RQ1_wide_table_v2_신항선총교통량.csv"
OUT_CORR_TABLE_V2 = PROJECT_DIR / "RQ1_상관계수_행렬_v2_신항선총교통량.csv"


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


# ============================================================
# 1. 도로구간별 교통량을 일자별 시리즈로 로드 (범용화)
#    (organize_vds_raw.py가 만든 {road}/{section}/{year}/{yyyymm}_{section}.csv 전부 스캔)
#
#    value_col로 어떤 컬럼을 읽을지 지정한다.
#      - CARGO_OUT_DIR + "화물차환산_통행량" -> 화물차 추정 통행량
#      - VDS_OUT_DIR   + "교통량"           -> 원시 총교통량(전 차종)
#    only_roads / exclude_roads로 특정 도로만 포함하거나 제외할 수 있다.
# ============================================================

def load_segment_series(base_dir: Path, value_col: str, only_roads=None, exclude_roads=None) -> dict:
    """returns {'{road}_{section}': {날짜(str): 값}}"""
    only_roads_nfc = {_nfc(r) for r in only_roads} if only_roads else None
    exclude_roads_nfc = {_nfc(r) for r in exclude_roads} if exclude_roads else set()

    series = {}
    if not base_dir.exists():
        print(f"⚠️ {base_dir} 가 없습니다. organize_vds_raw.py를 먼저 실행하세요.")
        return series

    for road_dir in base_dir.iterdir():
        if not road_dir.is_dir():
            continue
        road = road_dir.name
        road_key = _nfc(road)
        if only_roads_nfc is not None and road_key not in only_roads_nfc:
            continue
        if road_key in exclude_roads_nfc:
            continue

        for section_dir in road_dir.iterdir():
            if not section_dir.is_dir():
                continue
            section = section_dir.name
            col_name = f"{road}_{section}"
            rows = {}
            for year_dir in section_dir.iterdir():
                if not year_dir.is_dir():
                    continue
                for f in year_dir.iterdir():
                    if not f.name.endswith(".csv"):
                        continue
                    with open(f, encoding="utf-8-sig", newline="") as fh:
                        reader = csv_module.DictReader(fh)
                        for row in reader:
                            d = row.get("일자")
                            v = row.get(value_col)
                            if not d:
                                continue
                            rows[d] = float(v) if v not in (None, "") else None
            series[col_name] = rows

    return series


# ============================================================
# 2. 물동량 로드 + 병합 (wide table) - 버전별
# ============================================================

def load_cargo_volume(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["날짜"] = pd.to_datetime(df["날짜"]).dt.strftime("%Y-%m-%d")
    df = df.set_index("날짜")
    return df


def build_wide_table(version: str):
    """version: 'v1' (전체 화물차환산_통행량) / 'v2' (부산신항선만 원시 총교통량)
    returns: wide(DataFrame), cargo_cols(list), road_cols(list), col_measure(dict: 컬럼명 -> 측정방식 라벨)
    """
    cargo_vol = load_cargo_volume(CARGO_VOL_FILE)

    if version == "v1":
        road_series = load_segment_series(CARGO_OUT_DIR, "화물차환산_통행량")
        measure_of_road = defaultdict(lambda: "화물차환산_통행량")
    elif version == "v2":
        road_series = {}
        road_series.update(
            load_segment_series(CARGO_OUT_DIR, "화물차환산_통행량", exclude_roads={SINHANG_ROAD})
        )
        road_series.update(
            load_segment_series(VDS_OUT_DIR, "교통량", only_roads={SINHANG_ROAD})
        )
        measure_of_road = defaultdict(lambda: "화물차환산_통행량")
        measure_of_road[SINHANG_ROAD] = "총교통량(전차종, 화물차비중 미적용)"
    else:
        raise ValueError(f"알 수 없는 version: {version}")

    if not road_series:
        return pd.DataFrame(), list(cargo_vol.columns), [], {}

    road_df = pd.DataFrame(road_series)
    road_df.index.name = "날짜"
    road_df = road_df.sort_index()

    wide = cargo_vol.join(road_df, how="outer")
    wide = wide[(wide.index >= DATE_START) & (wide.index <= DATE_END)]
    wide = wide.sort_index()

    # 결측 진단(최장연속결측일수/결측_집중연도)을 정확히 계산하려면 "데이터가 아예
    # 없어서 애초에 행 자체가 없는 날짜"도 명시적인 NaN 행으로 나타나야 한다.
    # (outer join만으로는 물동량·도로구간 둘 다 결측인 날짜는 행 자체가 안 생김)
    full_idx = pd.date_range(DATE_START, DATE_END).strftime("%Y-%m-%d")
    wide = wide.reindex(full_idx)
    wide.index.name = "날짜"

    # 컬럼명("{road}_{section}")에서 도로명을 뽑아 측정방식 라벨을 매핑
    # (도로명 자체에는 "_"가 없고 구간명 쪽에만 "-"가 쓰이므로 첫 "_" 기준 분리가 안전함)
    col_measure = {col: measure_of_road[_nfc(col.split("_", 1)[0])] for col in road_df.columns}

    return wide, list(cargo_vol.columns), list(road_df.columns), col_measure


# ============================================================
# 3. 상관계수 행렬 (pairwise, NaN 개별 제거) + 결측 진단
# ============================================================

def longest_missing_streak(is_na: pd.Series) -> int:
    """연속 결측(True) 구간 중 가장 긴 길이(일)를 반환한다."""
    max_streak = 0
    streak = 0
    for v in is_na:
        if v:
            streak += 1
            if streak > max_streak:
                max_streak = streak
        else:
            streak = 0
    return max_streak


def missing_concentration_years(is_na: pd.Series, threshold: float = 0.9) -> str:
    """연도별 결측 비율이 threshold(기본 90%) 이상인 연도를 콤마로 이어서 반환한다.
    (특정 연도에 결측이 몰려 있는지 - 예: 그 해에는 수집을 아예 안 한 경우 - 를 보기 위함)"""
    years = pd.Series(is_na.index).str.slice(0, 4)
    df = pd.DataFrame({"연도": years.values, "결측": is_na.values})
    ratio_by_year = df.groupby("연도")["결측"].mean()
    bad_years = ratio_by_year[ratio_by_year >= threshold].index.tolist()
    return ",".join(bad_years) if bad_years else ""


def correlation_matrix(wide: pd.DataFrame, cargo_cols, road_cols, col_measure: dict):
    results = []
    for c in cargo_cols:
        for r in road_cols:
            pair = wide[[c, r]]
            sub = pair.dropna()
            n = len(sub)
            measure = col_measure.get(r, "")

            # 결측 진단은 두 컬럼 중 "도로구간" 쪽(r) 기준으로 계산한다.
            # (물동량은 서컨물동량의 2024-03-09 이전 제외처럼 이미 알려진 사유로 결측이
            #  생기는 경우가 대부분이라, 여기서는 도로 데이터 수집 공백을 보는 게 목적)
            is_na_r = pair[r].isna()
            longest_gap = longest_missing_streak(is_na_r)
            missing_years = missing_concentration_years(is_na_r)
            missing_rate = round(is_na_r.mean(), 3)

            if n < 3:
                results.append({
                    "물동량": c, "도로구간": r, "교통량_측정방식": measure,
                    "상관계수": None, "p-value": None,
                    "N": n, "비고": "표본 부족(N<3) - 데이터 더 필요",
                    "도로구간_결측률": missing_rate,
                    "최장연속결측일수": longest_gap,
                    "결측_집중연도(90%+)": missing_years,
                })
                continue
            corr, p = pearsonr(sub[c], sub[r])
            results.append({
                "물동량": c, "도로구간": r, "교통량_측정방식": measure,
                "상관계수": round(corr, 4),
                "p-value": round(p, 6), "N": n, "비고": "",
                "도로구간_결측률": missing_rate,
                "최장연속결측일수": longest_gap,
                "결측_집중연도(90%+)": missing_years,
            })

    out = pd.DataFrame(results)
    out["_abs"] = out["상관계수"].abs()
    out = out.sort_values("_abs", ascending=False, na_position="last").drop(columns="_abs")
    return out


# ============================================================
# 4. 버전별 실행 + main
# ============================================================

def run_version(version: str, out_wide: Path, out_corr: Path, label: str):
    print("=" * 70)
    print(f"[{label}]")
    print("=" * 70)
    print("=== wide table 생성 ===")
    wide, cargo_cols, road_cols, col_measure = build_wide_table(version)
    if wide.empty:
        print("데이터가 없습니다. TCS/VDS 추출·정리 스크립트를 먼저 실행하세요.")
        return

    print(f"wide table: {wide.shape[0]}행 x {wide.shape[1]}열")
    print(f"물동량 컬럼: {cargo_cols}")
    print(f"도로구간 컬럼({len(road_cols)}개): {road_cols}")

    wide.to_csv(out_wide, encoding="utf-8-sig")
    print(f"저장: {out_wide}")

    if not road_cols:
        print("⚠️ 도로구간 데이터가 하나도 없어서 상관분석을 돌릴 수 없습니다.")
        return

    print("\n=== 상관계수 행렬 계산 ===")
    corr_table = correlation_matrix(wide, cargo_cols, road_cols, col_measure)
    corr_table.to_csv(out_corr, index=False, encoding="utf-8-sig")
    print(corr_table.to_string(index=False))
    print(f"\n저장: {out_corr}")
    print()


def main():
    run_version(
        "v1", OUT_WIDE_TABLE, OUT_CORR_TABLE,
        "버전1: 화물차환산_통행량 (부산신항선=진례 화물차비중 적용, 전체 계획대로)",
    )
    run_version(
        "v2", OUT_WIDE_TABLE_V2, OUT_CORR_TABLE_V2,
        "버전2: 부산신항선만 원시 총교통량(전차종) 사용, 나머지는 화물차환산_통행량과 동일",
    )
    print("※ 상관계수 크기만으로 주 이동경로를 단정하지 말 것 - 실제 도로 연결관계·공간적 위치·")
    print("   p-value·N을 같이 보고, 예상(부두-도로 매핑)과 다른 조합에서 더 높은 상관이 나오면")
    print("   그 결과를 신뢰할 것 (전체 계획 문서 4단계 유의사항).")


if __name__ == "__main__":
    main()
