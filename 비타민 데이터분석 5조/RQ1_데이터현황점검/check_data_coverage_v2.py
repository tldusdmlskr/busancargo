# -*- coding: utf-8 -*-
"""
RQ1 데이터 현황 점검 v2
========================
check_data_coverage.py(v1)는 VDS를 "1차정리 폴더(organize_vds_raw.py 결과물)" 기준으로
체크했는데, 아직 organize_vds_raw.py를 안 돌렸으면 그 폴더 자체가 없어서 VDS 현황을
전혀 알 수 없었다.

v2는 VDS를 원본(vds_downloads, collect_vds_v2.py가 만드는 파일들) 기준으로
"일(day) 단위" 커버리지를 직접 계산한다. 추가로 collect_vds_v2.py가 남기는
_collection_log.csv를 같이 읽어서, 아직 안 채워진 구간이

  (a) 이번까지 한 번도 OK로 받힌 적 없이 매번 "데이터 없음"으로만 응답이 왔는지
      (=사이트에 그 구간 데이터가 실제로 없다는 뜻, 재시도해도 안 채워질 가능성 높음)
  (b) 아니면 시도 자체가 없었거나 타임아웃/에러로 실패했는지
      (=재시도하면 채워질 수 있음)

를 구분해서 보여준다.

TCS/물동량 체크는 v1과 동일 로직 재사용.

실행:
    python check_data_coverage_v2.py
    (스크립트는 .../비타민 데이터분석 5조/RQ1_데이터현황점검/ 에 있다고 가정.
     다른 위치에서 실행하려면 인자로 "비타민 데이터분석 5조" 경로를 넘기면 됨)
"""
import sys
import re
import csv as csv_module
import unicodedata
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict
import pandas as pd

ALL_MONTHS = pd.period_range("2022-01", "2025-12", freq="M").strftime("%Y%m").tolist()
START_DATE = date(2022, 1, 1)
END_DATE = date(2025, 12, 31)

# collect_vds_v2.py의 TARGET_ROUTES와 동일 (변경 시 같이 맞춰줄 것)
TARGET_ROUTES = {
    "부산신항선": [
        "진해IC-남문대교",
        "남진례IC-대청IC",
        "대청IC-진해IC",
        "진례JC-남진례IC",
    ],
    "남해2지선": [
        "가락IC-서부산TG",
        "서부산IC-가락IC",
    ],
    "남해선(순천-부산)": [
        "김해JC-동김해IC",
        "북부산TG-김해JC",
    ],
    "중앙선": [
        "대동IC-초정IC",
        "대감JC-대동IC",
    ],
}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def find_child(parent: Path, name: str):
    if not parent or not parent.exists():
        return None
    target = nfc(name)
    for child in parent.iterdir():
        if nfc(child.name) == target:
            return child
    return None


def resolve_path(root: Path, *parts: str):
    cur = root
    for part in parts:
        nxt = find_child(cur, part)
        if nxt is None:
            return None
        cur = nxt
    return cur


def month_files(base):
    months = set()
    if base is None or not base.exists():
        return months
    for p in base.rglob("*.csv"):
        m = re.search(r"(20\d{4})", p.name)
        if m:
            months.add(m.group(1))
    return months


def report_month_segment(label, base):
    months = month_files(base)
    missing = [m for m in ALL_MONTHS if m not in months]
    exists = base is not None and base.exists()
    print(f"- {label}")
    print(f"    폴더 존재: {exists} | 경로: {base}")
    print(f"    2022-2025 중 파일이 있는 달: {len(months)}/48 | 결측 달: {len(missing)}/48")
    if missing:
        show = missing if len(missing) <= 12 else missing[:12] + ["..."]
        print(f"    결측 예시: {show}")
    return {
        "구분": label, "폴더존재": exists, "수집단위": "월",
        "수집됨": len(months), "전체": 48,
        "결측비율(%)": round(len(missing) / 48 * 100, 1),
    }


# ============================================================
# VDS 원본(vds_downloads) 일 단위 커버리지
# ============================================================

def load_vds_day_coverage(raw_dir: Path):
    coverage = defaultdict(set)
    if raw_dir is None or not raw_dir.exists():
        return coverage
    files = [f.name for f in raw_dir.iterdir() if f.name.lower().endswith(".csv")]
    existing_nfc = {nfc(f): f for f in files}
    date_pat = re.compile(r"^(\d{8})_(\d{8})\.csv$")

    for route, sections in TARGET_ROUTES.items():
        for section in sections:
            prefix = nfc(f"VDS_{route}_{section}_")
            for fname_nfc in existing_nfc:
                if not fname_nfc.startswith(prefix):
                    continue
                rest = fname_nfc[len(prefix):]
                m = date_pat.match(rest)
                if not m:
                    continue
                s, e = m.groups()
                try:
                    sd = datetime.strptime(s, "%Y%m%d").date()
                    ed = datetime.strptime(e, "%Y%m%d").date()
                except ValueError:
                    continue
                d = sd
                while d <= ed:
                    if START_DATE <= d <= END_DATE:
                        coverage[(route, section)].add(d)
                    d += timedelta(days=1)
    return coverage


def missing_ranges(covered_days: set, start: date, end: date):
    """빠진 날짜들을 연속 구간으로 묶어서 반환."""
    ranges = []
    d = start
    run_start = None
    while d <= end + timedelta(days=1):
        is_missing = d <= end and d not in covered_days
        if is_missing and run_start is None:
            run_start = d
        elif not is_missing and run_start is not None:
            ranges.append((run_start, d - timedelta(days=1)))
            run_start = None
        d += timedelta(days=1)
    return ranges


def load_collection_log(raw_dir: Path):
    """(route, section) -> {날짜: {'OK':있었는지, 'NO_DATA':있었는지, ...}} 형태로
    그 구간의 시도 이력을 요약."""
    log_path = raw_dir / "_collection_log.csv" if raw_dir else None
    summary = defaultdict(lambda: defaultdict(set))  # (route,section) -> date -> {statuses}
    if not log_path or not log_path.exists():
        return summary
    with open(log_path, encoding="utf-8-sig", newline="") as f:
        reader = csv_module.DictReader(f)
        for row in reader:
            try:
                route, section = row["노선"], row["구간"]
                sd = datetime.strptime(row["시작일"], "%Y-%m-%d").date()
                ed = datetime.strptime(row["종료일"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            status = row["상태"]
            summary[(route, section)][(sd, ed)].add(status)
    return summary


def classify_missing_range(route, section, r_start, r_end, log_summary):
    """이 결측 구간이 로그상 실제로 어떤 상태였는지 분류."""
    windows = log_summary.get((route, section), {})
    statuses = set()
    for (ws, we), stat_set in windows.items():
        # 겹치는 윈도우만
        if we < r_start or ws > r_end:
            continue
        statuses |= stat_set

    if not statuses:
        return "시도이력없음(아직 수집 시도 안 함)"
    if "OK" in statuses:
        # OK가 있었는데 아직도 결측이면 -> 파일명 매칭/부분날짜 문제일 수 있음
        return "이상함: 로그엔 OK인데 파일 커버리지엔 없음(재확인 필요)"
    if statuses == {"NO_DATA_OR_BLOCKED"}:
        return "매번 '데이터 없음' 응답(진짜 결측 가능성 높음)"
    return f"기타/실패({', '.join(sorted(statuses))}) - 재시도하면 채워질 수 있음"


def report_vds_raw():
    print("=" * 70)
    print("VDS 원본(vds_downloads) 일 단위 커버리지")
    print("=" * 70)

    global PROJECT_DIR
    raw_dir = resolve_path(PROJECT_DIR, "교통데이터_원본", "vds_downloads")
    if raw_dir is None:
        print("⚠️ vds_downloads 폴더를 못 찾았습니다.")
        return []

    coverage = load_vds_day_coverage(raw_dir)
    log_summary = load_collection_log(raw_dir)
    total_days = (END_DATE - START_DATE).days + 1

    rows = []
    for route, sections in TARGET_ROUTES.items():
        for section in sections:
            days = coverage.get((route, section), set())
            n_covered = len(days)
            pct = round(n_covered / total_days * 100, 1)
            print(f"\n- VDS-{route}-{section}")
            print(f"    2022-01-01~2025-12-31 중 확보일: {n_covered}/{total_days}일 ({pct}%)")

            miss = missing_ranges(days, START_DATE, END_DATE)
            genuine_gap_days = 0
            retry_gap_days = 0
            for r_start, r_end in miss:
                r_days = (r_end - r_start).days + 1
                verdict = classify_missing_range(route, section, r_start, r_end, log_summary)
                if "진짜 결측" in verdict:
                    genuine_gap_days += r_days
                elif "시도이력없음" in verdict or "재시도" in verdict:
                    retry_gap_days += r_days
                if len(miss) <= 15:
                    print(f"      결측: {r_start} ~ {r_end} ({r_days}일) -> {verdict}")

            if len(miss) > 15:
                print(f"      (결측 구간 {len(miss)}개, 상위 일부만 표시하려면 스크립트 조정 필요 - 총 {sum((e-s).days+1 for s,e in miss)}일 결측)")

            print(f"    -> 결측 중 '진짜 데이터없음'으로 보이는 일수: {genuine_gap_days}일, "
                  f"'재시도하면 채워질 수 있는' 일수: {retry_gap_days}일")

            rows.append({
                "구분": f"VDS원본-{route}-{section}", "폴더존재": True, "수집단위": "일",
                "수집됨": n_covered, "전체": total_days,
                "결측비율(%)": round((total_days - n_covered) / total_days * 100, 1),
                "결측중_진짜없음_추정일수": genuine_gap_days,
                "결측중_재시도필요_추정일수": retry_gap_days,
            })
    return rows


# ============================================================
# main
# ============================================================

def main():
    global PROJECT_DIR
    if len(sys.argv) > 1:
        PROJECT_DIR = Path(sys.argv[1])
    else:
        PROJECT_DIR = Path(__file__).resolve().parent.parent

    rows = []

    print("=" * 70)
    print("1. 물동량 데이터")
    print("=" * 70)
    cargo_dir = resolve_path(PROJECT_DIR, "물동량_downloads")
    cargo_file = find_child(cargo_dir, "물동량_상관분석용.csv") if cargo_dir else None
    if cargo_file and cargo_file.exists():
        df = pd.read_csv(cargo_file, encoding="utf-8-sig")
        df["날짜"] = pd.to_datetime(df["날짜"])
        in_range = df[(df["날짜"] >= pd.Timestamp(START_DATE)) & (df["날짜"] <= pd.Timestamp(END_DATE))]
        print(f"- 파일: {cargo_file.name} (존재)")
        print(f"  전체 행수: {len(df)}, 2022~2025 범위 행수: {len(in_range)} (기대값 1461)")
        for col in ["북컨물동량", "남컨물동량", "서컨물동량"]:
            if col in in_range.columns:
                n_na = in_range[col].isna().sum()
                print(f"  {col}: 결측 {n_na}건 / {len(in_range)}건 ({n_na/len(in_range)*100:.1f}%)")
    else:
        print("- 파일 없음 (물동량_downloads/물동량_상관분석용.csv)")
    print()

    rows.extend(report_vds_raw())
    print()

    print("=" * 70)
    print("2. TCS (1차정리 결과, TCS_영업소별_시간대별)")
    print("=" * 70)
    base_1cha = resolve_path(PROJECT_DIR, "교통데이터_1차정리", "시간대별_화물차추정통행량")
    tcs_base = resolve_path(base_1cha, "TCS_영업소별_시간대별") if base_1cha else None
    for st in ["가락", "대동", "남진례", "대청", "진해", "진해본선", "진례"]:
        d = resolve_path(tcs_base, st) if tcs_base else None
        rows.append(report_month_segment(f"TCS-{st}", d))
    print()

    print("=" * 70)
    print("요약 테이블 저장")
    print("=" * 70)
    out = pd.DataFrame(rows)
    out_file = Path(__file__).resolve().parent / "RQ1_데이터현황점검_결과_v2.csv"
    out.to_csv(out_file, index=False, encoding="utf-8-sig")
    print(out.to_string(index=False))
    print(f"\n저장됨: {out_file}")


if __name__ == "__main__":
    main()
