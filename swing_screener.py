"""
============================================================
 스윙매매 종목 스크리너
------------------------------------------------------------
 데이터  : pykrx (KRX 공식 데이터, 무료)
 출력    : 콘솔 출력 + 구글시트 스크리닝 탭 자동 기록
 실행    : python swing_screener.py
 권장    : 매일 장 마감 후 (17시 이후) 실행
============================================================
"""

import os
import json
import time
from datetime import datetime, timedelta

import pandas as pd
from pykrx import stock

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# ============================================================
#  설정 영역
# ============================================================

# 서비스 계정 키 파일 (로컬 실행 시. GitHub Actions는 환경변수 자동 사용)
GOOGLE_CREDENTIALS_FILE = "service_account.json"

# 구글시트 ID (URL의 /d/ 뒤 문자열)
#   https://docs.google.com/spreadsheets/d/ [여기] /edit
#
#   우선순위: 환경변수 SPREADSHEET_ID > 아래 직접 입력값
#   GitHub Actions : Secret 'SPREADSHEET_ID' 에 등록
#   로컬 실행      : 아래 문자열을 직접 수정
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1SKIaqF9C8xjGLS18chIx6KZ8iPxgxJQtEMGt4zxIrZI")
WORKSHEET_NAME = "스크리닝"

FILTER = {
    "min_trade_amount":  10_000_000_000,   # 거래대금 최소 100억 (유동성)
    "min_market_cap":   100_000_000_000,   # 시가총액 최소 1000억 (규모)
    "max_ext_ma20":     12.0,              # 20일선 대비 최대 이격 (%) — 초과 시 과열로 제외(고점 차단)
    "pullback_zone":    (-3.0, 7.0),       # 이상적 눌림목·지지 구간 (20일선 대비 %)
    "rsi_zone":         (45.0, 68.0),      # 건강한 상승 RSI 구간 (과열 아님)
    "rsi_period":       14,
    "ma_period":        [5, 20, 60, 120],
    "lookback_days":    250,               # 120일선·추세 계산을 위해 충분히 확보
    "top_n":            30,
    "markets":          ["KOSPI", "KOSDAQ"],
}

# ============================================================

def get_base_date():
    today = datetime.today()
    offset = {5: 1, 6: 2}.get(today.weekday(), 0)
    base = today - timedelta(days=offset)
    return base.strftime("%Y%m%d")


def fetch_ohlcv(ticker, start, end, retries=3):
    for i in range(retries):
        try:
            df = stock.get_market_ohlcv_by_date(start, end, ticker)
            if df is not None and not df.empty:
                return df
        except Exception:
            time.sleep(0.5)
    return None


def calc_rsi(close, period=14):
    """Wilder RSI. 데이터 부족 시 None."""
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def ma_slope(close, period, lag):
    """이동평균의 lag일 전 대비 변화율(%). 상승 추세 판정용."""
    ma = close.rolling(period).mean()
    if len(ma) <= lag or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-1 - lag]):
        return None
    base = ma.iloc[-1 - lag]
    if base == 0:
        return None
    return (ma.iloc[-1] - base) / base * 100


def run_screener():
    base_date  = get_base_date()
    start_date = (datetime.strptime(base_date, "%Y%m%d")
                  - timedelta(days=FILTER["lookback_days"])).strftime("%Y%m%d")

    print(f"\n{'='*58}")
    print(f"  📈 스윙매매 종목 스크리너")
    print(f"  기준일: {base_date}  |  데이터: KRX (pykrx)")
    print(f"{'='*58}")

    # 1. 전 종목 리스트
    tickers = []
    for mkt in FILTER["markets"]:
        try:
            t = stock.get_market_ticker_list(base_date, market=mkt)
            tickers += [(tk, mkt) for tk in t]
        except Exception as e:
            print(f"  ⚠️  {mkt} 종목 리스트 오류: {e}")

    print(f"  총 {len(tickers)}개 종목 대상")

    # 2. 거래대금/시가총액 일괄조회 + 1차 필터
    print("  ① 거래대금 필터 적용 중...")
    market_df = None
    try:
        market_df = stock.get_market_ohlcv_by_ticker(base_date, market="ALL")
        valid_tickers = set(market_df[market_df["거래대금"] >= FILTER["min_trade_amount"]].index.tolist())
        tickers = [(tk, mkt) for tk, mkt in tickers if tk in valid_tickers]
        print(f"     거래대금 100억↑ 통과: {len(tickers)}개")
    except Exception as e:
        print(f"  ⚠️  거래대금 일괄조회 실패 ({e}), 전체 종목 진행")

    # 3. 종목별 상세 분석 — 추세 속 눌림목(풀백) 스윙 전략
    print("  ② 추세·눌림목·과열 분석 중...")
    results = []
    total = len(tickers)
    pb_lo, pb_hi   = FILTER["pullback_zone"]
    rsi_lo, rsi_hi = FILTER["rsi_zone"]

    for i, (ticker, market) in enumerate(tickers):
        if i % 50 == 0:
            print(f"     {i}/{total} 처리 중...", end="\r")

        df = fetch_ohlcv(ticker, start_date, base_date)
        if df is None or len(df) < 61:
            continue

        close   = df["종가"]
        volume  = df["거래량"]
        current = close.iloc[-1]

        ma5  = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        ma120 = close.rolling(120).mean().iloc[-1] if len(close) >= 120 else None
        if any(pd.isna(v) for v in (ma5, ma20, ma60)):
            continue

        if market_df is not None and ticker in market_df.index:
            mktcap = int(market_df.loc[ticker, "시가총액"])
        else:
            mktcap = 0

        trade_amount = current * volume.iloc[-1]
        dist_ma20    = (current - ma20) / ma20 * 100
        ma60_slope   = ma_slope(close, 60, 20)   # 장기 추세 방향
        ma20_slope   = ma_slope(close, 20, 5)    # 단기 추세 방향
        rsi          = calc_rsi(close, FILTER["rsi_period"])

        jeong_baeyeol = ma5 > ma20 > ma60

        # ── 하드 필터: 추세 + 과열 차단 (하나라도 실패 시 후보 제외) ──
        uptrend      = (current > ma60) and (ma60_slope is not None and ma60_slope > 0)
        not_extended = dist_ma20 <= FILTER["max_ext_ma20"]     # 고점 추격 차단
        liquidity    = (trade_amount >= FILTER["min_trade_amount"]
                        and mktcap >= FILTER["min_market_cap"])
        if not (jeong_baeyeol and uptrend and not_extended and liquidity):
            continue

        # ── 거래량 수축: 최근 5일 평균 vs 직전 20일 평균 (건강한 눌림 = <1) ──
        vol_recent = volume.tail(5).mean()
        vol_base   = volume.tail(25).head(20).mean()
        vol_ratio  = (vol_recent / vol_base) if vol_base and vol_base > 0 else 1.0

        # ── 5점 채점 (변별력 있는 5개 기준) ──
        in_pullback   = pb_lo <= dist_ma20 <= pb_hi
        rsi_healthy   = rsi is not None and rsi_lo <= rsi <= rsi_hi
        vol_dry       = vol_ratio < 1.0
        trend_strong  = (ma20_slope is not None and ma20_slope > 0
                         and (ma120 is None or current > ma120))

        score = sum([liquidity, in_pullback, rsi_healthy, vol_dry, trend_strong])

        try:
            name = stock.get_market_ticker_name(ticker)
        except Exception:
            name = ticker

        results.append({
            "날짜": datetime.strptime(base_date, "%Y%m%d").strftime("%Y-%m-%d"),
            "종목명": name, "종목코드": ticker, "시장": market,
            "현재가": current, "거래대금": trade_amount, "시가총액": mktcap,
            "ma5": round(ma5, 0), "ma20": round(ma20, 0), "ma60": round(ma60, 0),
            "20일선대비(%)": round(dist_ma20, 2),
            "RSI": round(rsi, 1) if rsi is not None else None,
            "거래량비": round(vol_ratio, 2),
            "정배열": jeong_baeyeol, "눌림목": in_pullback,
            "RSI건강": rsi_healthy, "거래량수축": vol_dry, "추세강도": trend_strong,
            "점수": score,
        })
        time.sleep(0.05)

    print(f"\n  ③ 분석 완료: {len(results)}개 → 상위 {FILTER['top_n']}개 선별")

    if not results:
        print("  ❌ 조건에 맞는 종목이 없습니다.")
        return

    # 동점 시: 눌림목 깊은 순(20선에 가까운 순) → 거래대금 순
    df_result = pd.DataFrame(results)
    df_result["_pb_dist"] = df_result["20일선대비(%)"].abs()
    df_result = (df_result
                 .sort_values(["점수", "_pb_dist", "거래대금"],
                              ascending=[False, True, False])
                 .drop(columns="_pb_dist")
                 .head(FILTER["top_n"])
                 .reset_index(drop=True))

    # 4. 콘솔 출력
    print(f"\n{'─'*72}")
    print(f"  {'#':^3} {'종목명':^12} {'코드':^8} {'시장':^6} "
          f"{'현재가':>9} {'20선대비':>8} {'RSI':>5} {'거래량':>5} {'점수':>4} {'패턴'}")
    print(f"{'─'*72}")
    for idx, r in df_result.iterrows():
        pattern = []
        if r["눌림목"]:     pattern.append("눌림목")
        if r["RSI건강"]:   pattern.append("RSI양호")
        if r["거래량수축"]: pattern.append("거래량수축")
        if r["추세강도"]:   pattern.append("추세강")
        rsi_txt = f"{r['RSI']:>5.0f}" if r["RSI"] is not None else "    -"
        print(f"  {idx+1:^3} {r['종목명'][:10]:^12} {r['종목코드']:^8} "
              f"{r['시장']:^6} {r['현재가']:>9,.0f} "
              f"{r['20일선대비(%)']:>+7.1f}% {rsi_txt} {r['거래량비']:>5.2f} "
              f"{r['점수']:>3}/5  {'·'.join(pattern)}")
    print(f"{'─'*72}")
    print(f"  ✅ 5점: {len(df_result[df_result['점수']==5])}개  "
          f"4점: {len(df_result[df_result['점수']==4])}개  "
          f"3점: {len(df_result[df_result['점수']==3])}개")

    write_to_gsheet(df_result, base_date)


def write_to_gsheet(df, base_date):
    """
    시트 열기: open_by_key(SPREADSHEET_ID) 사용
    인증 우선순위:
      1) 환경변수 GOOGLE_CREDENTIALS_JSON  ← GitHub Actions Secret
      2) 로컬 파일 GOOGLE_CREDENTIALS_FILE ← 로컬 실행
    """
    if not GSPREAD_AVAILABLE:
        print("\n  ℹ️  gspread 미설치 → pip install gspread google-auth")
        return

    # 시트 ID 검증
    sid = SPREADSHEET_ID.strip()
    if sid == "여기에_시트_ID_입력" or not sid:
        print("\n  ⚠️  SPREADSHEET_ID 가 설정되지 않았습니다.")
        print("     - GitHub Actions: Secret 'SPREADSHEET_ID' 에 URL ID 등록")
        print("     - 로컬 실행     : swing_screener.py 의 SPREADSHEET_ID 변수 수정")
        print("     - ID 위치       : https://docs.google.com/spreadsheets/d/[ID]/edit")
        return

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            info  = json.loads(creds_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            print("\n  🔑 인증: GitHub Secret (환경변수)")
        elif os.path.exists(GOOGLE_CREDENTIALS_FILE):
            creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
            print("\n  🔑 인증: 로컬 서비스 계정 파일")
        else:
            print(f"\n  ⚠️  인증 정보 없음")
            print(f"     - GitHub Actions: Secret 'GOOGLE_CREDENTIALS_JSON' 등록")
            print(f"     - 로컬: {GOOGLE_CREDENTIALS_FILE} 파일 배치")
            return

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sid)          # ← 이름 대신 ID로 열기
        ws = sh.worksheet(WORKSHEET_NAME)

        existing = ws.get_all_values()
        next_row = len(existing) + 1

        rows = []
        for _, r in df.iterrows():
            judgment = ("✅ 진입후보" if r["점수"] >= 4
                        else "⚠️ 관심"  if r["점수"] == 3
                        else "❌ 관망")
            rsi_txt = f"{r['RSI']:.0f}" if r["RSI"] is not None else "-"
            memo = (f"이격{r['20일선대비(%)']:+.1f}% · RSI{rsi_txt} · "
                    f"거래량x{r['거래량비']:.2f}")
            # 시트 컬럼: 거래대금100억↑ / 정배열·추세 / 모멘텀(RSI) / 차트위치(눌림목) / 거래량수축
            rows.append([
                r["날짜"], r["종목명"], r["종목코드"], r["시장"],
                bool(r["거래대금"] >= FILTER["min_trade_amount"]),
                bool(r["정배열"] and r["추세강도"]),
                bool(r["RSI건강"]),
                bool(r["눌림목"]),
                bool(r["거래량수축"]),
                int(r["점수"]),
                judgment,
                memo,
            ])

        ws.update(f"A{next_row}", rows)
        print(f"  ✅ 구글시트 기록 완료: {len(rows)}행 추가 (행 {next_row}~)")
        print(f"  🔗 https://docs.google.com/spreadsheets/d/{sid}")

    except gspread.exceptions.SpreadsheetNotFound:
        print(f"\n  ❌ 시트 ID '{sid}' 를 찾을 수 없음")
        print(f"     서비스 계정 이메일에 편집자 권한이 공유되어 있는지 확인")
    except Exception as e:
        print(f"\n  ❌ 구글시트 기록 실패: {e}")


if __name__ == "__main__":
    run_screener()
