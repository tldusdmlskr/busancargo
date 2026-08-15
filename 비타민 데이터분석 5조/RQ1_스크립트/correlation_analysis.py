# -*- coding: utf-8 -*-
"""
RQ1 물동량-통행량 상관분석 (전체 계획 문서의 1~3단계 그대로 구현)
====================================================================

1단계(파생변수)와 2단계(축별 화물차환산 통행량 산출)는
extract_tcs_stations.py / organize_vds_raw.py 에서 이미 끝났다고 가정하고,
이 스크립트는 그 결과를 모아서

  2단계: 물동량(북컨/남컨/서컨) + 도로구간별 화물차환산_통행량을 날짜 기준으로
         하나의 wide 테이블로 합치고
  3단계: 물동량 3개 x 전체 도로구간 조합에 대해 상관계수 행렬을 계산한다.

전체 계획 문서에 있던 유의사항을 그대로 반영:
  - 물동량 결측(NaN)은 0으로 채우지 않고 그대로 둔다
  - 상관계수는 각 (물동량, 도로구간) 쌍마다 pairwise로 NaN을 제거해서 계산한다
    (전체 테이블을 한 번에 dropna() 하지 않음)
  - 서컨물동량은 2024-03-09 이후만 사용 (물동량_상관분석용.csv에 이미 반영되어 있음)
  - 상관계수 크기만으로 주 이동경로를 단정하지 않는다 -> 결과표에 N, p-value를 같이 남겨서
    팀에서 실제 도로 연결관계/공간적 위치와 같이 검토할 수 있게 한다
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
CARGO_OUT_DIR = (
    PROJECT_DIR / "교통데이터_1차정리" / "시간대별_화물차추정통행량" / "차종별추정교통량_구간별_시간대별"
)

DATE_START = "2022-01-01"
DATE_END = "2025-12-31"

OUT_WIDE_TABLE = PROJECT_DIR / "RQ1_wide_table.csv"
OUT_CORR_TABLE = PROJECT_DIR / "RQ1_상관계수_행렬.csv"


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
# 1. 도로구간별 화물차환산_통행량을 일자별 시리즈로 로드
#    (organize_vds_raw.py가 만든 {road}/{section}/{year}/{yyyymm}_{section}.csv 전부 스캔)
# ============================================================

def load_all_road_segments(cargo_out_dir: Path) -> pd.DataFrame:
    """returns wide DataFrame indexed by 날짜(date), columns = '{road}_{section}'"""
    series = {}
    if not cargo_out_dir.exists():
        print(f"⚠️ {cargo_out_dir} 가 없습니다. organize_vds_raw.py를 먼저 실행하세요.")
        return pd.DataFrame()

    for road_dir in cargo_out_dir.iterdir():
        if not road_dir.is_dir():
            continue
        road = road_dir.name
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
                            v = row.get("화물차환산_통행량")
                            if not d:
                                continue
                            rows[d] = float(v) if v not in (None, "") else None
            series[col_name] = rows

    if not series:
        return pd.DataFrame()

    df = pd.DataFrame(series)
    df.index.name = "날짜"
    df = df.sort_index()
    return df


# ============================================================
# 2. 물동량 로드 + 병합 (wide table)
# ============================================================

def load_cargo_volume(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["날짜"] = pd.to_datetime(df["날짜"]).dt.strftime("%Y-%m-%d")
    df = df.set_index("날짜")
    return df


def build_wide_table():
    cargo_vol = load_cargo_volume(CARGO_VOL_FILE)
    road = load_all_road_segments(CARGO_OUT_DIR)

    wide = cargo_vol.join(road, how="outer")
    wide = wide[(wide.index >= DATE_START) & (wide.index <= DATE_END)]
    wide = wide.sort_index()
    return wide, list(cargo_vol.columns), list(road.columns)


# ============================================================
# 3. 상관계수 행렬 (pairwise, NaN 개별 제거)
# ============================================================

def correlation_matrix(wide: pd.DataFrame, cargo_cols, road_cols):
    results = []
    for c in cargo_cols:
        for r in road_cols:
            sub = wide[[c, r]].dropna()
            n = len(sub)
            if n < 3:
                results.append({
                    "물동량": c, "도로구간": r, "상관계수": None, "p-value": None,
                    "N": n, "비고": "표본 부족(N<3) - 데이터 더 필요",
                })
                continue
            corr, p = pearsonr(sub[c], sub[r])
            results.append({
                "물동량": c, "도로구간": r, "상관계수": round(corr, 4),
                "p-value": round(p, 6), "N": n, "비고": "",
            })

    out = pd.DataFrame(results)
    out["_abs"] = out["상관계수"].abs()
    out = out.sort_values("_abs", ascending=False, na_position="last").drop(columns="_abs")
    return out


def main():
    print("=== wide table 생성 ===")
    wide, cargo_cols, road_cols = build_wide_table()
    if wide.empty:
        print("데이터가 없습니다. TCS/VDS 추출·정리 스크립트를 먼저 실행하세요.")
        return

    print(f"wide table: {wide.shape[0]}행 x {wide.shape[1]}열")
    print(f"물동량 컬럼: {cargo_cols}")
    print(f"도로구간 컬럼({len(road_cols)}개): {road_cols}")

    wide.to_csv(OUT_WIDE_TABLE, encoding="utf-8-sig")
    print(f"저장: {OUT_WIDE_TABLE}")

    if not road_cols:
        print("⚠️ 도로구간 데이터가 하나도 없어서 상관분석을 돌릴 수 없습니다.")
        return

    print("\n=== 상관계수 행렬 계산 ===")
    corr_table = correlation_matrix(wide, cargo_cols, road_cols)
    corr_table.to_csv(OUT_CORR_TABLE, index=False, encoding="utf-8-sig")
    print(corr_table.to_string(index=False))
    print(f"\n저장: {OUT_CORR_TABLE}")
    print("\n※ 상관계수 크기만으로 주 이동경로를 단정하지 말 것 - 실제 도로 연결관계·공간적 위치·")
    print("   p-value·N을 같이 보고, 예상(부두-도로 매핑)과 다른 조합에서 더 높은 상관이 나오면")
    print("   그 결과를 신뢰할 것 (전체 계획 문서 4단계 유의사항).")


if __name__ == "__main__":
    main()