# -*- coding: utf-8 -*-
"""
RQ1 (물동량-통행량 상관분석) 착수 전 데이터 현황 점검 스크립트
==============================================================
전체 계획 문서의 "시작 전 체크" 항목을 코드로 확인한다.

필요 데이터
1. 물동량: 북컨(1·2·3부두)·남컨(4·5·6·다목적부두)·서컨(7부두), '1.수출입' 행만,
   2022-01-01~2025-12-31 일별
2. 교통량
   - 제2배후도로: VDS(부산신항선) 4구간 + TCS 5개 영업소(남진례·대청·진해·진해본선·진례)
   - 제1배후도로: 3개 접속점 VDS(남해2지선/남해선/중앙선) + TCS(가락·대동)

주의: 폴더/파일명 중 일부가 유니코드 NFD(자모 분리형)로 저장되어 있어
      단순 문자열 결합으로 경로를 만들면 "존재하지 않음"으로 잘못 판정될 수 있다.
      이 스크립트는 각 단계에서 실제 디렉터리를 나열하고 NFC 기준으로 이름을
      비교해서 찾는 방식(find_child)을 사용해 이 문제를 피한다.

실행:
    python check_data_coverage.py "/path/to/비타민 데이터분석 5조"
    (인자를 생략하면 현재 폴더 기준으로 "비타민 데이터분석 5조" 폴더를 그 상위에서 찾는다)
"""
import sys
import re
import unicodedata
from pathlib import Path
from datetime import date
import pandas as pd

ALL_MONTHS = pd.period_range("2022-01", "2025-12", freq="M").strftime("%Y%m").tolist()


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def find_child(parent: Path, name: str) -> Path | None:
    """parent 아래에서 이름이 NFC 기준으로 name과 같은 자식을 찾아 반환 (없으면 None)."""
    if not parent.exists():
        return None
    target = nfc(name)
    for child in parent.iterdir():
        if nfc(child.name) == target:
            return child
    return None


def resolve_path(root: Path, *parts: str) -> Path | None:
    cur = root
    for part in parts:
        nxt = find_child(cur, part)
        if nxt is None:
            return None
        cur = nxt
    return cur


def month_files(base: Path | None):
    """base 아래 재귀적으로 파일명에 YYYYMM(20xx)이 포함된 것들의 월 집합을 반환."""
    months = set()
    if base is None or not base.exists():
        return months
    for p in base.rglob("*.csv"):
        m = re.search(r"(20\d{4})", p.name)
        if m:
            months.add(m.group(1))
    return months


def report_segment(label, base: Path | None):
    months = month_files(base)
    missing = [m for m in ALL_MONTHS if m not in months]
    exists = base is not None and base.exists()
    print(f"- {label}")
    print(f"    폴더 존재: {exists} | 경로: {base}")
    print(f"    2022-2025 중 파일이 있는 달: {len(months)}/48"
          f" | 결측 달: {len(missing)}/48")
    if missing:
        show = missing if len(missing) <= 12 else missing[:12] + ["..."]
        print(f"    결측 예시: {show}")
    return {
        "구분": label,
        "폴더존재": exists,
        "수집된달수": len(months),
        "결측달수": len(missing),
        "결측비율(%)": round(len(missing) / 48 * 100, 1),
    }


def main():
    if len(sys.argv) > 1:
        ROOT = Path(sys.argv[1])
    else:
        ROOT = Path(".")

    START, END = date(2022, 1, 1), date(2025, 12, 31)
    rows = []

    base_1cha = resolve_path(ROOT, "교통데이터_1차정리", "시간대별_화물차추정통행량")

    print("=" * 70)
    print("1. 물동량 데이터")
    print("=" * 70)
    cargo_dir = resolve_path(ROOT, "물동량_downloads")
    cargo_file = find_child(cargo_dir, "물동량_상관분석용.csv") if cargo_dir else None
    if cargo_file and cargo_file.exists():
        df = pd.read_csv(cargo_file, encoding="utf-8-sig")
        df["날짜"] = pd.to_datetime(df["날짜"])
        in_range = df[(df["날짜"] >= pd.Timestamp(START)) & (df["날짜"] <= pd.Timestamp(END))]
        print(f"- 파일: {cargo_file.name} (존재)")
        print(f"  컬럼: {list(df.columns)}")
        print(f"  전체 행수: {len(df)}, 2022~2025 범위 행수: {len(in_range)} (기대값 1461)")
        for col in ["북컨물동량", "남컨물동량", "서컨물동량"]:
            n_na = in_range[col].isna().sum()
            print(f"  {col}: 결측 {n_na}건 / {len(in_range)}건 ({n_na/len(in_range)*100:.1f}%)")
        west_start = pd.Timestamp("2024-03-09")
        west_before = in_range[in_range["날짜"] < west_start]["서컨물동량"]
        nonzero = (west_before.fillna(0) != 0).sum()
        print(f"  서컨(2024-03-09 이전, {len(west_before)}건) 0이 아닌 값: {nonzero}건 "
              f"→ 0이어야 정상(개장 전)")
    else:
        print(f"- 파일 없음 (물동량_downloads/물동량_상관분석용.csv)")
    print()

    print("=" * 70)
    print("2. 제1배후도로 (VDS 3개 접속점 + TCS 가락·대동)")
    print("=" * 70)
    vds_base = resolve_path(base_1cha, "VDS_구간별_시간대별") if base_1cha else None
    for road in ["남해2지선", "남해선", "중앙선"]:
        road_dir = resolve_path(vds_base, road) if vds_base else None
        if road_dir is not None:
            for seg in sorted(road_dir.iterdir(), key=lambda p: p.name):
                if seg.is_dir():
                    rows.append(report_segment(f"VDS-{road}-{seg.name}", seg))
        else:
            print(f"- VDS-{road}: 폴더 없음")
            rows.append({"구분": f"VDS-{road}", "폴더존재": False, "수집된달수": 0,
                         "결측달수": 48, "결측비율(%)": 100.0})

    tcs_base = resolve_path(base_1cha, "TCS_영업소별_시간대별") if base_1cha else None
    for st in ["가락", "대동"]:
        d = resolve_path(tcs_base, st) if tcs_base else None
        rows.append(report_segment(f"TCS-제1배후도로-{st}", d))
    print()

    print("=" * 70)
    print("3. 제2배후도로 (VDS 부산신항선 4구간 + TCS 5개 영업소)")
    print("=" * 70)
    shinhang_dir = resolve_path(vds_base, "부산신항선") if vds_base else None
    if shinhang_dir is not None:
        found_segs = []
        for seg in sorted(shinhang_dir.iterdir(), key=lambda p: p.name):
            if seg.is_dir():
                rows.append(report_segment(f"VDS-부산신항선-{seg.name}", seg))
                found_segs.append(seg.name)
        print(f"  [참고] 실제 존재하는 구간 폴더: {found_segs}")
        print(f"  [참고] 계획서상 4구간(신항교차로-진해IC / 진해IC-대청IC / "
              f"대청IC-남진례IC / 남진례IC-진례JC) 중 '남진례IC-진례JC' 존재?: "
              f"{'남진례IC-진례JC' in found_segs}")
    else:
        print("- VDS-부산신항선: 폴더 없음")
        rows.append({"구분": "VDS-부산신항선", "폴더존재": False, "수집된달수": 0,
                     "결측달수": 48, "결측비율(%)": 100.0})

    for st in ["남진례", "대청", "진해", "진해본선", "진례"]:
        d = resolve_path(tcs_base, st) if tcs_base else None
        rows.append(report_segment(f"TCS-제2배후도로-{st}", d))
    print()

    print("=" * 70)
    print("요약 테이블 저장")
    print("=" * 70)
    out = pd.DataFrame(rows)
    out_file = Path("RQ1_데이터현황점검_결과.csv")
    out.to_csv(out_file, index=False, encoding="utf-8-sig")
    print(out.to_string(index=False))
    print(f"\n저장됨: {out_file.resolve()}")


if __name__ == "__main__":
    main()
