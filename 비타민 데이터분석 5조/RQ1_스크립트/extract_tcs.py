# -*- coding: utf-8 -*-
"""
TCS 원본(전국 월별 gzip csv) -> 영업소별 시간대별 csv 추출 스크립트
====================================================================

입력: 교통데이터_원본/tcs_downloads/TCS_1시간_YYYYMM.csv
      (사실은 gzip 압축 파일이 .csv 확장자로 저장돼 있음. 인코딩은 CP949(EUC-KR))
      컬럼: 집계일자,집계시,영업소코드,입출구구분코드,TCS하이패스구분코드,
            고속도로운영기관구분코드,영업형태구분코드,
            1종교통량,2종교통량3종교통량,4종교통량,5종교통량6종교통량,총교통량

출력: 교통데이터_1차정리/시간대별_화물차추정통행량/TCS_영업소별_시간대별/{영업소명}/{연도}/{YYYYMM}_{영업소명}.csv
      (기존에 이미 만들어진 가락/대동 폴더와 완전히 같은 포맷으로 맞춤)
      컬럼: 집계일자,집계시,영업소코드,영업소명,
            1종교통량,2종교통량,3종교통량,4종교통량5종교통량,6종교통량,총교통량,
            차종합계,1종비율,2종비율,3종비율,4종비율,5종비율,6종비율

핵심: **영업소 하나에 코드가 여러 개인 경우가 있습니다.**
------------------------------------------------------------------
data.ex.co.kr에서 받은 영업소 코드표(ETC_영업소_..._csv, 이것도 확장자만 .zip이가")
넣고 으로는 gzip 압축 csv라 gzip으로 풀어야 함)를 확인해보니, 나들목 하나가 차로 유형별로
여러 영업소코드로 쪼개져 있는 경우가 있습니다. 예:

    가락  -> 029(개), 246(가락), 446(특), 596(가락2), 977(경)   [5개]
    대동  -> 091(개), 252(대동), 452(특), 634(경)               [4개]
    진례  -> 147(진례), 347(특), 971(경)                        [3개]
    남진례 -> 696                                                [1개]
    대청   -> 697                                                [1개]
    진해   -> 698                                                [1개]
    진해본선 -> 699                                              [1개]

기존에 이미 처리된 가락/대동 1차정리 파일은 대표 코드(246/252) 하나만 쓰고 있어서
하이패스 특차로/경차로 물량이 빠져 있었을 가능성이 높습니다. 이 스크립트는
STATION_CODES에 station -> [코드 리스트]로 넣고 전부 합산하도록 만들었으니,
가락/대동을 다시 뽑으면 기존 파일보다 더 정확한 총교통량이 나올 것입니다
(기존 폴더를 덮어쓰고 싶지 않으면 STATION_CODES에서 가락/대동을 빼고 새 영업소만
먼저 돌려보세요).

코드가 더 필요하면 load_codes_from_table()에 코드표 csv 경로를 넣으면 영업소명으로
자동으로 찾아서 채워줍니다(부분일치 X, 정확히 같은 이름만 - "진례"와 "진례(특)"은
이름이 다르므로 별도로 CODE_TABLE_EXTRA_NAMES에 수동으로 추가해야 함).

이미 처리된 부분(연-월)은 자동으로 스킵합니다(재실행 안전).
"""

import os
import re
import gzip
import unicodedata
import csv as csv_module
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================

# station -> [영업소코드, ...]  (차로 유형별로 코드가 여러 개면 전부 합산해서 집계)
# 아래 값은 ETC_영업소_..._20260814 코드표에서 실제로 확인한 것.
# 나머지(남양산/남장유/동김해/물금/북부산/삼랑진/상동/서김해/서부산/장유)는
# 기존 1차정리 폴더에서 역으로 확인한 대표 코드 1개씩만 들어있음 - 이것도 변형
# 코드(개/특/경 등)가 더 있는지 코드표에서 한 번 더 확인해보는 걸 권장.
STATION_CODES = {
    "가락": ["029", "246", "446", "596", "977"],       # 개/가락/특/가락2/경
    "대동": ["091", "252", "452", "634"],               # 개/대동/특/경
    "남양산": ["250", "450", "636"],                     # 남양산/특/경
    "남장유": ["791"],                                   # 변후코드 없음(코드표엄에서 확인)
    "동김해": ["149", "349", "973"],                    # 동김해/특/경
    "물금": ["251", "451", "635"],                      # 물금/특/경
    "북부산": ["150", "350", "974"],                    # 북부산/특/경
    "삼랑진": ["627"],                                  # 보쁬지 코드 없음
    "상동": ["628"],                                    # 보쁬지 코드 없음
    "서김해": ["148", "348", "972"],                    # 서김해/특/경
    "서부산": ["244", "444", "975"],                    # 서부산/특/경
    "장유": ["245", "445", "976"],                      # 장유/특/경 (791=남장유는 별도 영업소이므로 제외)
    # --- 제2배후도로 핵욫하이 5개 (ETC_영업소 코드표에서 새로 확인) ---
    "남진례": ["696"],
    "대청": ["697"],
    "진해": ["698"],
    "진해본선": ["699"],
    "진례": ["147", "347", "971"],                      # 진례/진례(특)/진례(경)
}

# 코드표 CSV를 구했연으릴 여ꠀ로로 벭��만 없이. load_codes_from_table()이 "정확히 같은
# 이름"인 컬럼만 자동으로 채워주므로, 아래처럼 별칭을 추가하면 그 이름으로 코드표에서 찾아
# STATION_CODES[key]에 append 합니다. (이미 위에서 직접 채웠으므로 지금은 비워둬도 됩니다.)
CODE_TABLE_CSV = None  # 예: "./영업소코드.csv"
CODE_TABLE_EXTRA_NAMES = {
    # "station_key": ["코드표상의 영업소명1", "영업소명2", ...]
}

# 전부 절대경로로 고정 (스크립트를 어디에 두고 실행하든 항상 같은 폴더를 보게 하기 위함 -
# organize_vds_raw.py / correlation_analysis.py와 동일한 이유)
PROJECT_DIR = Path(r"C:\Users\USER\busancargo\비타민 데이터분석 5조")
TCS_RAW_DIR = PROJECT_DIR / "교통데이터_원본" / "tcs_downloads"
OUT_BASE = PROJECT_DIR / "교통데이터_1차정리" / "시간대별_화물차추정통행량" / "TCS_영업소별_시간대별"

TCS_COLUMNS = [
    "집계일자", "집계시", "영업소코드", "입출구구분코드", "TCS하이패스구분코드",
    "고속도로운영기관구분코드", "영업형태구분코드",
    "1종교통량", "2종교통량", "3종교통량", "4종교통량", "5종교통량", "6종교통량", "총교통량",
]

OUT_COLUMNS = [
    "집계일자", "집계시", "영업소코드", "영업소명",
    "1종교통량", "2종교통량", "3종교통량", "4종교통량", "5종교통량", "6종교통량", "총교통량",
    "차종합계", "1종비율", "2종비율", "3종비율", "4종비율", "5종비율", "6종비율",
]


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def find_child(parent: Path, name: str):
    """NFC/NFD 정규화가 섞여 있어도 안전하게 자식 폴더/파일을 찾는다."""
    if not parent.exists():
        return None
    target = _nfc(name)
    for child in parent.iterdir():
        if _nfc(child.name) == target:
            return child
    return None


def resolve_or_make_child(parent: Path, name: str) -> Path:
    """find_child로 이미 존재하는 폴더(NFC든 NFD든)를 찾아서 그대로 쓰고, 없을 때만
    새로 만든다. 이게 없으면 이미 있는 폴더가 이름의 유니코드 정규화 형태(NFC/NFD)가
    달라서, 쓰기 경로(parent/name)가 기존 폴더 옆에 '보이지 않는' 중복 폴더를 새로
    만들어버리는 문제가 생긴다 (실제로 가락/대동 등 12개 영업소에서 이 버그로
    재추출한 데이터가 전부 안 보이는 새 폴더에 쌓이고 있었음 - already_done()은
    find_child로 기존 폴더를 보는데, 쓰기는 정규화 없이 새 경로를 만들어서 발생)."""
    existing = find_child(parent, name)
    if existing is not None:
        return existing
    new_path = parent / name
    new_path.mkdir(parents=True, exist_ok=True)
    return new_path


def load_codes_from_table(csv_path: str, extra_names: dict):
    """영업소 코드표 CSV(참고: 실제로는 .zip 확장자라도 gzip일 수 있으니 미리 풀어둘 것)에서
    영업소명 -> 코드 매핑을 읽어와서, extra_names에 지정된 station -> [코드표상의 이름들]에
    해당하는 코드를 찾아 리스트로 돌려준다. 컬럼명이 정확히 뭔지 몰라도 '영업소'+'코드' /
    '영업소'+'명'이 들어간 컬럼을 자동으로 찾는다."""
    result = {}
    if not csv_path or not os.path.isfile(csv_path) or not extra_names:
        return result

    name_to_code = {}
    opener = gzip.open if _is_gzip(Path(csv_path)) else open  # data.ex.co.kr은 .zip이라고 써놓고 실제론 gzip인 경우가 많음
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            with opener(csv_path, "rt", encoding=enc, newline="") as f:
                reader = csv_module.DictReader(f)
                fieldnames = reader.fieldnames or []
                code_col = next((c for c in fieldnames if "영업소" in c and "코드" in c), None)
                name_col = next((c for c in fieldnames if "영업소" in c and ("명" in c or "이름" in c)), None)
                if not code_col or not name_col:
                    continue
                for row in reader:
                    name = (row.get(name_col) or "").strip()
                    code = (row.get(code_col) or "").strip()
                    if name and code:
                        name_to_code[name] = code
                break
        except UnicodeDecodeError:
            continue

    if not name_to_code:
        print(f"⚠️ 코드표 컬럼을 못 찾았습니다: {csv_path} (직접 STATION_CODES에 채워주세요)")
        return result

    for station, alias_names in extra_names.items():
        codes = [name_to_code[n] for n in alias_names if n in name_to_code]
        missing = [n for n in alias_names if n not in name_to_code]
        if missing:
            print(f"⚠️ 코드표에서 못 찾은 이름: {missing} (station={station})")
        if codes:
            result[station] = codes
    return result


def month_range(raw_dir: Path):
    """tcs_downloads 안의 TCS_1시간_YYYYMM.csv 파일들에서 (year, month) 목록을 뽑는다."""
    if not raw_dir.exists():
        return []
    out = []
    for p in raw_dir.iterdir():
        m = re.match(r"^TCS_1시간_(\d{4})(\d{2})\.csv$", _nfc(p.name))
        if m:
            out.append((int(m.group(1)), int(m.group(2)), p))
    return sorted(out)


def already_done(station: str, year: int, month: int) -> bool:
    out_dir = find_child(OUT_BASE, station)
    if out_dir is None:
        return False
    yr_dir = find_child(out_dir, str(year))
    if yr_dir is None:
        return False
    fname = f"{year}{month:02d}_{station}.csv"
    return find_child(yr_dir, fname) is not None


def _normalize_date(raw: str) -> str:
    """'20200101' 또는 '2020-01-01' 둘 다 'YYYY-MM-DD'로 통일."""
    s = raw.strip().strip('"')
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


def read_month_rows(path: Path, target_codes: set):
    """gzip + CP949 TCS 월별 원본 파일을 읽으면서 target_codes에 해당하는 행만 돌려준다.

    원본 아카이브가 연도별로 포맷이 다르다는 걸 실제로 확인했음:
      - 2016~2019년대: '|' 구분자, 헤더 없음, 날짜 YYYYMMDD, 따옴표 없음
      - 2020~2023년대: ',' 구분자, 헤더 없음, 날짜 YYYYMMDD, 따옴표 없음
      - 2020년 이후   : ',' 구분자, 헤더 있음(따옴표로 감싼 컬럼명), 날짜 YYYY-MM-DD
    그래서 첫 줄을 보고 구분자/헤더 유무를 자동 판별하고, 날짜는 항상 YYYY-MM-DD로
    정규화해서 돌려준다(안 그러면 organize_vds_raw.py에서 VDS 날짜와 매칭이 안 됨).
    코드 컬럼은 원본에 공백 패딩이 섞여 있어서(예: '618 ') strip 후 비교한다.
    """
    opener = gzip.open if _is_gzip(path) else open
    with opener(path, "rt", encoding="cp949", errors="ignore", newline="") as f:
        first_line = f.readline()
        if not first_line:
            return
        delimiter = "|" if first_line.count("|") > first_line.count(",") else ","
        first_field = first_line.strip().split(delimiter)[0].strip().strip('"')
        has_header = first_field == "집계일자"

        f.seek(0)
        reader = csv_module.reader(f, delimiter=delimiter)
        if has_header:
            next(reader, None)  # 헤더 skip

        for row in reader:
            # 원본 끝에 trailing delimiter가 있어서 마지막에 빈 문자열 필드가 하나 더 붙는 경우가 있음
            if row and row[-1] == "":
                row = row[:-1]
            if len(row) < len(TCS_COLUMNS):
                continue
            rec = dict(zip(TCS_COLUMNS, row))
            code = rec["영업소코드"].strip().strip('"')
            if code in target_codes:
                rec["영업소코드"] = code  # 패딩 공백 제거된 값으로 덮어쓰기 (아래 rows_by_code[...] 키와 맞춰야 함)
                rec["집계일자"] = _normalize_date(rec["집계일자"])
                rec["집계시"] = rec["집계시"].strip().strip('"')
                yield rec


def _is_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def aggregate_and_write(station: str, codes: list, year: int, month: int, rows):
    """station에 속한 코드 여러 개(예: 가락 5개)의 행을 전부 합쳐서 (일자,시) 단위로
    집계한 뒤 차종비율까지 계산해서 out 포맷으로 저장. 원본은 입출구구분코드 등으로
    나뉘어 여러 행이 같은 (집계일자,집계시,코드)에 존재할 수 있어서 전부 합산해야 함."""
    agg = {}  # (일자, 시) -> [1..6종, 총교통량] 합계
    for r in rows:
        key = (r["집계일자"], r["집계시"])
        vals = agg.setdefault(key, [0, 0, 0, 0, 0, 0, 0])
        try:
            for i, col in enumerate([
                "1종교통량", "2종교통량", "3종교통량", "4종교통량", "5종교통량", "6종교통량", "총교통량"
            ]):
                vals[i] += int(float(r[col] or 0))
        except ValueError:
            continue

    rep_code = codes[0]  # 출력 파일에 남길 대표 코드 (여러 개 합산했다는 건 STATION_CODES를 보면 알 수 있음)

    if not agg:
        # 이 달에 데이터가 0건이어도 "이미 확인했음" 표시로 헤더만 있는 빈 파일을 남긴다.
        # 이걸 안 하면 already_done()이 매번 False를 반환해서, 원본에 영구적으로 없는
        # 영업소(남진례/대청/진해/진해본선 등) 때문에 재실행할 때마다 전체 기간을 처음부터
        # 다시 다 스캔하게 된다 (실제로 이 버그 때문에 가락/대동 재추출이 여러 번 안 끝났었음).
        station_dir = resolve_or_make_child(OUT_BASE, station)
        out_dir = resolve_or_make_child(station_dir, str(year))
        out_path = out_dir / f"{year}{month:02d}_{station}.csv"
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv_module.writer(f)
            w.writerow(OUT_COLUMNS)
        return 0

    out_rows = []
    for (일자, 시), vals in sorted(agg.items()):
        v1, v2, v3, v4, v5, v6, vt = vals
        차종합계 = v1 + v2 + v3 + v4 + v5 + v6
        if 차종합계 > 0:
            ratios = [round(v / 차종합계, 10) for v in (v1, v2, v3, v4, v5, v6)]
        else:
            ratios = [0, 0, 0, 0, 0, 0]
        out_rows.append([일자, 시, rep_code, station, v1, v2, v3, v4, v5, v6, vt, 차종합계, *ratios])

    station_dir = resolve_or_make_child(OUT_BASE, station)
    out_dir = resolve_or_make_child(station_dir, str(year))
    out_path = out_dir / f"{year}{month:02d}_{station}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv_module.writer(f)
        w.writerow(OUT_COLUMNS)
        w.writerows(out_rows)

    return len(out_rows)


def main():
    print("[진단] PROJECT_DIR =", PROJECT_DIR, "| 존재:", PROJECT_DIR.exists())
    print("[진단] TCS_RAW_DIR =", TCS_RAW_DIR, "| 존재:", TCS_RAW_DIR.exists())
    if not PROJECT_DIR.exists():
        print("⚠️ PROJECT_DIR가 없습니다. CONFIG의 경로가 맞는지 먼저 확인하세요.")
        return

    codes_from_table = load_codes_from_table(CODE_TABLE_CSV, CODE_TABLE_EXTRA_NAMES)
    for name, codes in codes_from_table.items():
        if not STATION_CODES.get(name):
            STATION_CODES[name] = codes

    missing = [k for k, v in STATION_CODES.items() if not v]
    if missing:
        print(f"⚠️ 아직 코드를 모르는 영업소: {missing}")
        print("   -> STATION_CODES에 직접 채우거나 CODE_TABLE_CSV/CODE_TABLE_EXTRA_NAMES를 지정해주세요.")

    active = {name: codes for name, codes in STATION_CODES.items() if codes}
    if not active:
        print("채울 영업소 코드가 하나도 없어서 종료합니다.")
        return

    target_codes = {c for codes in active.values() for c in codes}

    months = month_range(TCS_RAW_DIR)
    print(f"원본 월 파일 {len(months)}개 발견 (TCS_RAW_DIR={TCS_RAW_DIR})")
    for name, codes in active.items():
        tag = f" (코드 {len(codes)}개 합산)" if len(codes) > 1 else ""
        print(f"  대상: {name} = {codes}{tag}")

    for year, month, path in months:
        todo_stations = [name for name in active if not already_done(name, year, month)]
        if not todo_stations:
            continue  # 이 달은 대상 영업소가 다 이미 처리됨 -> 스킵

        print(f"[{year}{month:02d}] 처리 중 (대상 영업소: {todo_stations})")
        rows_by_code = {c: [] for c in target_codes}
        for rec in read_month_rows(path, target_codes):
            rows_by_code[rec["영업소코드"]].append(rec)

        for station in todo_stations:
            codes = active[station]
            rows = [r for c in codes for r in rows_by_code.get(c, [])]
            n = aggregate_and_write(station, codes, year, month, rows)
            if n:
                print(f"  ✅ {station}({','.join(codes)}): {n}행 저장")
            else:
                print(f"  ⚠️ {station}({','.join(codes)}): 이 달 데이터 없음(코드 자체가 없거나 실제로 데이터 없음)")


if __name__ == "__main__":
    main()
