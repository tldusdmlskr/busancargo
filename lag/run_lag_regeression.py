"""
RQ1. 시차(lag) 적용 회귀분석 파이프라인
=========================================

상관분석(1단계) -> 시차분석(2단계)에서 확정된 최적 lag를 반영해
회귀분석(3단계)을 수행한다.

- 버전 A: 요일평균을 뺀 편차값으로 회귀 (요일효과 사전 제거)
- 버전 B: 원본단위 + 요일더미 (회귀식 안에서 요일효과 통제)
- 서컨 모델의 신항선 2개 구간(대청IC-진해IC, 남진례IC-대청IC) 다중공선성(VIF) 진단 +
  두 변수를 평균으로 통합한 대안모델까지 같이 산출
- [2026-08-21] 부산신항선 화물차환산(v1)/원시총교통량(v2) 두 버전을 모두 순회해서
  결과에 "부산신항선_버전" 컬럼으로 함께 기록 (상관·시차분석의 버전 구분과 일치)

입력: RQ1_wide_table.csv / RQ1_wide_table_v2_신항선총교통량.csv
      (컬럼: 날짜, 북컨물동량, 남컨물동량, 서컨물동량, <도로구간 9개>)
출력:
    output/RQ1_회귀분석_결과요약_버전A.csv
    output/RQ1_회귀분석_결과요약_버전B.csv
    output/RQ1_VIF진단.csv
    output/RQ1_다중공선성_통합모델_비교.csv
    output/RQ1_잔차진단.csv
    output/plots/<물동량>_<버전>_잔차진단.png
"""

import os
import sys
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.outliers_influence import variance_inflation_factor
from statsmodels.stats.stattools import durbin_watson, jarque_bera
from statsmodels.stats.diagnostic import het_breuschpagan
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# ------------------------------------------------------------------
# 콘솔 인코딩 (Windows) — cmd 기본 코드페이지(cp949)에서 한글 print가
# 깨지거나 UnicodeEncodeError가 나는 걸 막기 위해 stdout/stderr를 UTF-8로 강제
# ------------------------------------------------------------------
if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _pick_korean_font():
    """실제로 렌더링 가능한 한글 폰트만 골라서 쓴다.
    (이전 버전은 fontManager.ttflist 안의 이름을 문자열로만 대조해서,
    실제로 로드되지 않는 폰트 이름이 설정돼도 통과되는 경우가 있었음
    → matplotlib이 그림을 그릴 때마다 "Font family 'NanumGothic' not found"
    경고를 반복 출력하는 원인. findfont(fallback_to_default=False)로
    진짜 사용 가능한 폰트인지 확인한 뒤에만 rcParams에 반영한다.)"""
    candidates = ["Malgun Gothic", "AppleGothic", "NanumGothic", "NanumBarunGothic",
                  "Noto Sans CJK KR", "Noto Sans KR"]
    for name in candidates:
        try:
            path = fm.findfont(fm.FontProperties(family=name), fallback_to_default=False)
            if path:
                return name
        except Exception:
            continue
    return None


_KOREAN_FONT = _pick_korean_font()
if _KOREAN_FONT:
    plt.rcParams["font.family"] = _KOREAN_FONT
else:
    print("[경고] 시스템에서 한글 폰트를 찾지 못했습니다. 그림(plots/*.png)의 한글이 "
          "네모(□)로 보일 수 있습니다 — 결과 수치 자체에는 영향 없습니다. "
          "윈도우라면 보통 '맑은 고딕'이 기본 설치돼 있어야 하는데 못 찾은 상태이니, "
          "필요하면 나눔고딕 등을 설치한 뒤 다시 실행해 주세요.")
plt.rcParams["axes.unicode_minus"] = False

# ------------------------------------------------------------------
# 0. 설정
# ------------------------------------------------------------------

# [2026-08-21 갱신] 상관분석·시차분석이 v1(화물차환산_통행량)/v2(부산신항선만 원시
# 총교통량) 두 버전으로 갈라지면서, 회귀분석 입력도 버전별로 따로 준비되었다.
# 두 버전 모두 돌려서 결과를 나란히 비교할 수 있게 VERSIONS로 순회한다.
VERSIONS = ["v1_화물차환산", "v2_전차종원시"]

WIDE_TABLE_PATHS = {
    "v1_화물차환산": r"C:\Users\USER\busancargo\비타민 데이터분석 5조\RQ1_wide_table.csv",
    "v2_전차종원시": r"C:\Users\USER\busancargo\비타민 데이터분석 5조\RQ1_wide_table_v2_신항선총교통량.csv",
}
OUTPUT_DIR = "output"
PLOT_DIR = os.path.join(OUTPUT_DIR, "plots")

# 이미 lag 적용이 끝난 회귀분석용 파일이 있으면 True로 바꾸고 아래 경로를 채운다.
# (그 경우 build_lagged_features 단계를 건너뛰고 파일을 그대로 사용)
# [2026-08-21] 팀에서 새로 만든 lag 적용 파일이 버전별(v1/v2)로 나뉘어 있어 경로를 갱신.
USE_PRELAGGED_FILES = True
PRELAGGED_FILES = {
    "서컨물동량": {
        "v1_화물차환산": r"C:\Users\USER\busancargo\lag\RQ1_회귀분석용_서컨물동량_v1_화물차환산.csv",
        "v2_전차종원시": r"C:\Users\USER\busancargo\lag\RQ1_회귀분석용_서컨물동량_v2_전차종원시.csv",
    },
    "북컨물동량": {
        "v1_화물차환산": r"C:\Users\USER\busancargo\lag\RQ1_회귀분석용_북컨물동량_v1_화물차환산.csv",
        "v2_전차종원시": r"C:\Users\USER\busancargo\lag\RQ1_회귀분석용_북컨물동량_v2_전차종원시.csv",
    },
    "남컨물동량": {
        "v1_화물차환산": r"C:\Users\USER\busancargo\lag\RQ1_회귀분석용_남컨물동량_v1_화물차환산.csv",
        "v2_전차종원시": r"C:\Users\USER\busancargo\lag\RQ1_회귀분석용_남컨물동량_v2_전차종원시.csv",
    },
}

# 서컨물동량 유효구간 시작일 (그 이전은 결측 처리). 이미 정제된 파일을 쓴다면 None으로.
SEOKEON_VALID_FROM = "2024-03-09"

# [2026-08-21 갱신] 시차분석(2단계) 재실행 결과(오염 제거된 데이터 기준, N=399/648/651/647)로
# 교체. 이전 버전 대비 달라진 점:
#   - 서컨의 '남해선 북부산TG-김해JC'는 완전히 제외했다 — 이 구간은 2023-12-04 이후
#     데이터가 끊긴 죽은 변수라, 오염 제거 후 상관계수 자체가 계산되지 않았다
#     (RQ1_긴급발견_서컨데이터오염.md, RQ1_상관분석_서컨오염_확인.md 참고).
#   - 서컨의 '남해2지선 가락IC-서부산TG' lag 5 -> 1, '남해선 김해JC-동김해IC' lag 5 -> 7.
#   - 신항선 3구간(대청IC-진해IC/남진례IC-대청IC/진해IC-남문대교)은 전부 lag7로 통일
#     (이전에는 진해IC-남문대교만 lag13이었음).
#   - 북컨/남컨은 이번 재실행에서도 그대로 유지됨.
# key: 물동량 컬럼명, value: {도로구간 컬럼명: lag}
LAG_SPEC = {
    "서컨물동량": {
        "남해2지선_가락IC-서부산TG": 1,
        "중앙선_대동IC-초정IC": 5,
        "남해선(순천-부산)_김해JC-동김해IC": 7,
        "부산신항선_대청IC-진해IC": 7,
        "부산신항선_남진례IC-대청IC": 7,
        "부산신항선_진해IC-남문대교": 7,
    },
    "북컨물동량": {
        "남해선(순천-부산)_김해JC-동김해IC": 0,
        "남해선(순천-부산)_북부산TG-김해JC": 0,
        "중앙선_대감JC-대동IC": 0,
        "부산신항선_대청IC-진해IC": 1,
    },
    "남컨물동량": {
        "남해2지선_가락IC-서부산TG": 0,
        "중앙선_대동IC-초정IC": 10,
        "부산신항선_진해IC-남문대교": 0,
    },
}

# 다중공선성이 확인된 서컨 모델의 신항선 두 구간 -> 통합변수로 대체할 때 사용
MULTICOLLINEAR_GROUPS = {
    "서컨물동량": [
        ("부산신항선_대청IC-진해IC", 7),
        ("부산신항선_남진례IC-대청IC", 7),
    ]
}

# 잔차진단(Breusch-Pagan)에서 이분산성이 확인된 물동량에는 로버스트 표준오차 적용.
# 서컨: 버전A BP p≈3.4e-23, 버전B BP p≈1.3e-20 → 이분산 뚜렷 → HC3
# 북컨/남컨: BP p 0.07~0.38로 등분산 가정에 문제 없어 일반 OLS 표준오차 유지
ROBUST_COV_TYPE = {
    "서컨물동량": "HC3",
}

DOW_KOR = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(PLOT_DIR, exist_ok=True)


# ------------------------------------------------------------------
# 1. 데이터 로드 & lag 적용
# ------------------------------------------------------------------

def load_wide_table(path):
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df["날짜"] = pd.to_datetime(df["날짜"])
    df = df.sort_values("날짜").reset_index(drop=True)

    if SEOKEON_VALID_FROM is not None and "서컨물동량" in df.columns:
        df.loc[df["날짜"] < SEOKEON_VALID_FROM, "서컨물동량"] = np.nan

    return df


def apply_lag(series, lag):
    """X(t) = road(t + lag).  물동량(t) 발생 후 lag일 뒤 도로에 반영된다고 보고,
    도로 컬럼을 lag만큼 앞으로 당겨(shift(-lag)) 물동량(t)과 같은 행에 정렬한다."""
    return series.shift(-lag)


def build_lagged_dataset(df, y_col, spec):
    """물동량 y_col + spec에 정의된 도로구간(lag 적용)만 모은 데이터프레임 반환"""
    work = pd.DataFrame({"날짜": df["날짜"], y_col: df[y_col]})
    x_cols = []
    for road_col, lag in spec.items():
        new_col = f"{road_col}_lag{lag}"
        work[new_col] = apply_lag(df[road_col], lag)
        x_cols.append(new_col)
    return work, x_cols


# ------------------------------------------------------------------
# 2. 버전 A (요일효과 제거 편차) / 버전 B (원본 + 요일더미) 데이터셋 구성
# ------------------------------------------------------------------

def add_dow(df):
    df = df.copy()
    df["요일"] = df["날짜"].dt.dayofweek.map(DOW_KOR)
    return df


def build_version_A(df, y_col, x_cols):
    """요일별 평균을 빼서 편차값으로 변환한 뒤 회귀에 쓸 데이터셋 반환"""
    work = add_dow(df[["날짜", "요일"] + [y_col] + x_cols].copy()
                    if "요일" in df.columns else add_dow(df))
    work = work.dropna(subset=[y_col] + x_cols).copy()

    for col in [y_col] + x_cols:
        dow_mean = work.groupby("요일")[col].transform("mean")
        work[col + "_편차"] = work[col] - dow_mean

    y = work[y_col + "_편차"].reset_index(drop=True)
    X = work[[c + "_편차" for c in x_cols]].reset_index(drop=True)
    X.columns = x_cols  # 원래 이름 유지
    return X, y, work


def build_version_B(df, y_col, x_cols):
    """원본값 + 요일더미(월요일 기준)로 회귀에 쓸 데이터셋 반환"""
    work = add_dow(df[["날짜", y_col] + x_cols].copy())
    work = work.dropna(subset=[y_col] + x_cols).copy()

    dums = pd.get_dummies(work["요일"], prefix="요일")
    dums = dums.drop(columns=["요일_월"])  # 월요일을 기준범주로
    dums = dums.reindex(columns=[f"요일_{d}" for d in ["화", "수", "목", "금", "토", "일"]],
                         fill_value=0).astype(float)

    y = work[y_col].reset_index(drop=True)
    X = pd.concat([work[x_cols].reset_index(drop=True), dums.reset_index(drop=True)], axis=1)
    return X, y, work


# ------------------------------------------------------------------
# 3. 회귀 적합 + VIF + 잔차진단
# ------------------------------------------------------------------

def check_coverage(X, y_col, version_label, min_nonzero_ratio=0.05):
    """모형에 실제로 들어가는 표본(dropna 이후) 안에서, 각 설명변수가 0/NaN이 아닌
    '진짜 관측치'를 얼마나 갖고 있는지 확인한다. 도로구간 데이터 수집이 특정 시점
    이후 끊겨서 사실상 상수(전부 0)가 돼버린 변수가 섞여 있으면, 회귀계수가 거의 0에
    가까운 값과 비정상적으로 작은 p-value로 나와 "유의한 관계"처럼 보이는 착시가
    생길 수 있다 — 이를 놓치지 않기 위한 자동 점검."""
    rows = []
    for col in X.columns:
        nonzero_ratio = ((X[col] != 0) & X[col].notna()).mean()
        rows.append({
            "물동량": y_col, "버전": version_label, "변수": col,
            "0이_아닌_관측_비율": round(nonzero_ratio, 4),
            "데이터경고": "예" if nonzero_ratio < min_nonzero_ratio else "",
        })
        if nonzero_ratio < min_nonzero_ratio:
            print(f"  [데이터 경고] {y_col} / {col}: 이 모형 표본 안에서 0이 아닌 값이 "
                  f"{nonzero_ratio:.1%}밖에 없음 — 도로구간 데이터 수집이 끊겼을 가능성. "
                  f"이 변수의 계수는 신뢰하지 말 것.")
    return pd.DataFrame(rows)


def compute_vif(X):
    Xc = sm.add_constant(X)
    vif = pd.DataFrame({
        "변수": Xc.columns,
        "VIF": [variance_inflation_factor(Xc.values, i) for i in range(Xc.shape[1])],
    })
    return vif[vif["변수"] != "const"].reset_index(drop=True)


def fit_and_summarize(X, y, y_col, version_label, cov_type=None):
    """cov_type=None -> 일반 OLS 표준오차. cov_type="HC3" 등을 넘기면
    이분산에 강건한(robust) 표준오차로 p-value/유의성을 다시 계산한다.
    계수(점추정치) 자체는 cov_type에 영향받지 않고 동일함 — 달라지는 건
    표준오차·p-value뿐이다."""
    Xc = sm.add_constant(X)
    ols = sm.OLS(y, Xc, missing="drop")
    model = ols.fit(cov_type=cov_type) if cov_type else ols.fit()

    rows = []
    for var in Xc.columns:
        if var == "const":
            continue
        rows.append({
            "물동량": y_col,
            "버전": version_label,
            "변수": var,
            "계수": model.params[var],
            "표준오차유형": cov_type if cov_type else "일반(OLS)",
            "p-value": model.pvalues[var],
            "유의(p<0.05)": model.pvalues[var] < 0.05,
            "R2(모델전체)": model.rsquared,
            "Adj_R2(모델전체)": model.rsquared_adj,
            "N": int(model.nobs),
        })
    result_df = pd.DataFrame(rows)
    return model, result_df


def residual_diagnostics(model, y_col, version_label):
    resid = model.resid
    fitted = model.fittedvalues

    dw = durbin_watson(resid)
    jb_stat, jb_p, skew, kurt = jarque_bera(resid)
    bp_stat, bp_p, _, _ = het_breuschpagan(resid, model.model.exog)

    diag = {
        "물동량": y_col,
        "버전": version_label,
        "Durbin-Watson": dw,
        "Jarque-Bera_p": jb_p,
        "Breusch-Pagan_p": bp_p,
        "AIC": model.aic,
        "BIC": model.bic,
    }

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    axes[0].scatter(fitted, resid, s=10, alpha=0.5)
    axes[0].axhline(0, color="red", linewidth=1)
    axes[0].set_xlabel("적합값")
    axes[0].set_ylabel("잔차")
    axes[0].set_title(f"{y_col} ({version_label}) 잔차 vs 적합값")

    # fit=True: 잔차의 평균·표준편차에 맞춰 이론적 분포(정규분포)를 스케일링한다.
    # fit=True를 안 쓰면 이론적 분위수가 표준정규(평균0, 표준편차1=-3~3)로 고정되는데
    # 잔차는 실제로 수천 단위라서, 사실상 이론축이 0 근처에 눌려 세로 줄무늬처럼
    # 보이는 착시가 생긴다(45도 기준선도 같이 왜곡됨). fit=True로 스케일을 맞춰야
    # "정규분포에서 얼마나 벗어났는지"를 제대로 볼 수 있다.
    sm.qqplot(resid, line="45", fit=True, ax=axes[1])
    axes[1].set_title("Q-Q plot")

    plt.tight_layout()
    fname = os.path.join(PLOT_DIR, f"{y_col}_{version_label}_잔차진단.png")
    plt.savefig(fname, dpi=120)
    plt.close(fig)

    return diag


# ------------------------------------------------------------------
# 4. 다중공선성 통합모델 (서컨 신항선 대청IC-진해IC / 남진례IC-대청IC)
# ------------------------------------------------------------------

def build_multicollinearity_fixed(lagged, y_col, x_cols, group):
    """이미 lag가 적용된 데이터셋(lagged, x_cols)을 그대로 재사용해서
    group에 속한 컬럼들만 평균으로 통합한다.
    (주의: 반드시 run_for_target에서 쓴 것과 '같은' lagged 데이터프레임을 넣어야
    N/표본 구성이 원래 모델과 동일하게 유지되고 비교가 유효하다.
    raw wide table을 다시 불러 lag를 새로 적용하면, USE_PRELAGGED_FILES=True로
    이미 정제된 파일을 쓴 경우와 표본이 달라져 비교가 무효해진다 — 이전 버전의 버그.)"""
    group_cols = [f"{road_col}_lag{lag}" for road_col, lag in group]
    missing = [c for c in group_cols if c not in x_cols]
    if missing:
        raise ValueError(f"통합 대상 컬럼을 lagged 데이터셋에서 찾을 수 없습니다: {missing}")

    work = lagged.copy()
    fixed_x_cols = [c for c in x_cols if c not in group_cols]

    combined_name = "신항선_대청진해_통합(평균)"
    work[combined_name] = work[group_cols].mean(axis=1)
    fixed_x_cols.append(combined_name)

    return work, fixed_x_cols


# ------------------------------------------------------------------
# 5. 메인 파이프라인
# ------------------------------------------------------------------

def run_for_target(df, y_col, spec, sinhang_version, all_results_A, all_results_B, all_vif, all_diag, all_coverage):
    # 서컨물동량은 팀이 공유한 "정제 완료" 파일조차 개장 전(2024-03-09 이전, 7부두
    # 미운영) 793일치가 걸러지지 않고 남아있던 이력이 있다(RQ1_상관분석_서컨오염_확인.md).
    # 이 구간의 "물동량"은 실측치가 아니라 운영 시작 전이라 0으로 잡힌 값이며, 이
    # 값들이 요일별 평균 계산에 섞여 들어가면 개장 이후 정상 구간의 편차값까지 함께
    # 왜곡된다. 그래서 서컨물동량은 파일 출처(USE_PRELAGGED_FILES 설정)와 무관하게
    # 항상 해당 버전(v1/v2) wide table의 원본 날짜 필터(SEOKEON_VALID_FROM)를 거쳐
    # 새로 만든다 — "정제됐다"고 알려진 파일을 신뢰하지 않고 매번 원본에서 재검증한다.
    if y_col == "서컨물동량" and SEOKEON_VALID_FROM is not None:
        lagged, x_cols = build_lagged_dataset(df, y_col, spec)
        print(f"[{y_col}/{sinhang_version}] 개장일({SEOKEON_VALID_FROM}) 필터를 원본 wide table에서부터 "
              f"다시 적용 (제공된 회귀분석용 파일은 이 구간이 안 걸러져 있을 수 있어 사용하지 않음)")
    elif USE_PRELAGGED_FILES:
        lagged = pd.read_csv(PRELAGGED_FILES[y_col][sinhang_version], parse_dates=["날짜"])
        x_cols = [c for c in lagged.columns if c not in ("날짜", y_col)]
    else:
        lagged, x_cols = build_lagged_dataset(df, y_col, spec)

    cov_type = ROBUST_COV_TYPE.get(y_col)  # 이분산 확인된 물동량만 HC3, 나머지는 None(일반 OLS)

    # ---- 데이터 커버리지 점검 (dropna 이후, 버전 B 기준 표본으로 확인) ----
    X_b_check, _, _ = build_version_B(lagged, y_col, x_cols)
    coverage = check_coverage(X_b_check[x_cols], y_col, "B")
    coverage.insert(1, "부산신항선_버전", sinhang_version)
    all_coverage.append(coverage)

    # ---- 버전 A ----
    X_a, y_a, _ = build_version_A(lagged, y_col, x_cols)
    model_a, res_a = fit_and_summarize(X_a, y_a, y_col, "A_요일효과제거_편차", cov_type=cov_type)
    res_a.insert(1, "부산신항선_버전", sinhang_version)
    all_results_A.append(res_a)
    diag_a = residual_diagnostics(model_a, y_col, f"A_{sinhang_version}")
    diag_a["부산신항선_버전"] = sinhang_version
    all_diag.append(diag_a)

    # ---- 버전 B ----
    X_b, y_b, _ = build_version_B(lagged, y_col, x_cols)
    model_b, res_b = fit_and_summarize(X_b, y_b, y_col, "B_원본단위_요일더미", cov_type=cov_type)
    res_b.insert(1, "부산신항선_버전", sinhang_version)
    all_results_B.append(res_b)
    diag_b = residual_diagnostics(model_b, y_col, f"B_{sinhang_version}")
    diag_b["부산신항선_버전"] = sinhang_version
    all_diag.append(diag_b)

    # ---- VIF (버전 B의 도로 변수 기준으로 진단 — 원단위 해석이 목적이라 여기서 확인) ----
    vif_b = compute_vif(X_b[x_cols])
    vif_b.insert(0, "버전", "B")
    vif_b.insert(0, "부산신항선_버전", sinhang_version)
    vif_b.insert(0, "물동량", y_col)
    all_vif.append(vif_b)

    print(f"[{y_col}/{sinhang_version}] 버전A N={int(model_a.nobs)}, R2={model_a.rsquared:.4f} | "
          f"버전B N={int(model_b.nobs)}, R2={model_b.rsquared:.4f}")

    return model_b, x_cols, vif_b, lagged


def main():
    all_results_A, all_results_B, all_vif, all_diag, all_coverage = [], [], [], [], []
    collinearity_compare = []

    for sinhang_version in VERSIONS:
        df = load_wide_table(WIDE_TABLE_PATHS[sinhang_version])

        for y_col, spec in LAG_SPEC.items():
            model_b, x_cols, vif_b, lagged = run_for_target(
                df, y_col, spec, sinhang_version,
                all_results_A, all_results_B, all_vif, all_diag, all_coverage)

            # 다중공선성 해결 대안모델 (서컨만 해당) — 반드시 원래 모델과 같은 lagged 데이터셋을 재사용
            if y_col in MULTICOLLINEAR_GROUPS:
                group = MULTICOLLINEAR_GROUPS[y_col]
                cov_type = ROBUST_COV_TYPE.get(y_col)
                fixed_lagged, fixed_x_cols = build_multicollinearity_fixed(lagged, y_col, x_cols, group)
                X_fb, y_fb, _ = build_version_B(fixed_lagged, y_col, fixed_x_cols)
                model_fixed, res_fixed = fit_and_summarize(X_fb, y_fb, y_col, "B_다중공선성해결(통합변수)",
                                                             cov_type=cov_type)
                res_fixed.insert(1, "부산신항선_버전", sinhang_version)

                vif_fixed = compute_vif(X_fb[fixed_x_cols])

                collinearity_compare.append({
                    "물동량": y_col,
                    "부산신항선_버전": sinhang_version,
                    "모델": "원래(개별 변수 2개)",
                    "R2": model_b.rsquared,
                    "Adj_R2": model_b.rsquared_adj,
                    "AIC": model_b.aic,
                    "BIC": model_b.bic,
                    "최대VIF": vif_b["VIF"].max(),
                })
                collinearity_compare.append({
                    "물동량": y_col,
                    "부산신항선_버전": sinhang_version,
                    "모델": "통합변수(평균) 대체",
                    "R2": model_fixed.rsquared,
                    "Adj_R2": model_fixed.rsquared_adj,
                    "AIC": model_fixed.aic,
                    "BIC": model_fixed.bic,
                    "최대VIF": vif_fixed["VIF"].max(),
                })
                all_results_B.append(res_fixed)

    # ---- 저장 ----
    pd.concat(all_results_A, ignore_index=True).to_csv(
        os.path.join(OUTPUT_DIR, "RQ1_회귀분석_결과요약_버전A.csv"), index=False, encoding="utf-8-sig")
    pd.concat(all_results_B, ignore_index=True).to_csv(
        os.path.join(OUTPUT_DIR, "RQ1_회귀분석_결과요약_버전B.csv"), index=False, encoding="utf-8-sig")
    pd.concat(all_vif, ignore_index=True).to_csv(
        os.path.join(OUTPUT_DIR, "RQ1_VIF진단.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(all_diag).to_csv(
        os.path.join(OUTPUT_DIR, "RQ1_잔차진단.csv"), index=False, encoding="utf-8-sig")
    pd.concat(all_coverage, ignore_index=True).to_csv(
        os.path.join(OUTPUT_DIR, "RQ1_변수별_데이터커버리지.csv"), index=False, encoding="utf-8-sig")
    if collinearity_compare:
        pd.DataFrame(collinearity_compare).to_csv(
            os.path.join(OUTPUT_DIR, "RQ1_다중공선성_통합모델_비교.csv"), index=False, encoding="utf-8-sig")

    print("\n완료. output/ 폴더에 결과 CSV와 output/plots/ 에 잔차진단 그림이 저장되었습니다.")


if __name__ == "__main__":
    main()
