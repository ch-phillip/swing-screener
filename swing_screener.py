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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pandas as pd
from pykrx import stock

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

try:
    import FinanceDataReader as fdr
    FDR_AVAILABLE = True
except ImportError:
    FDR_AVAILABLE = False

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

# 텔레그램 알림 (토큰·chat_id 는 보안상 환경변수로만 — 코드/깃에 박지 말 것)
#   로컬 실행      : export TELEGRAM_BOT_TOKEN=... ; export TELEGRAM_CHAT_ID=...
#   GitHub Actions : Secret 'TELEGRAM_BOT_TOKEN' / 'TELEGRAM_CHAT_ID' 등록
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TOP_N     = 3      # 알림으로 보낼 상위 후보 수

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
    # ── 매매 가격대 계산 ──
    "stop_buffer":      0.05,              # ATR 없을 때 대체 손절 버퍼 (0.05=−5%)
    "reward_ratio":     2.0,              # 손익비 (익절폭 = 리스크 × 이 값. 2.0=1:2)
    "atr_period":       14,                # ATR 계산 기간
    "atr_mult":         1.0,               # 손절가 = 지지선 − ATR × 이 값 (변동성 반영)
    # ── 추가 지표 ──
    "near_high_pct":    15.0,              # 52주 신고가 −N% 이내면 '신고가 근접'
    "rs_period":        20,                # 상대강도 비교 기간(일)
    "box_period":       20,                # 박스권 돌파 판정용 직전 고점 기간(일)
    "vol_breakout_mult": 1.5,              # 당일 거래량 ≥ 20일평균 × 이 값 → '거래량 돌파'
    # ── 주도섹터 분석 ──
    "sector_min_amount": 5_000_000_000,    # 섹터 집계 포함 최소 거래대금 (50억) — 잡주 노이즈 제거
    "sector_min_stocks": 2,                # 섹터로 인정할 최소 종목 수 (1~2개짜리 노이즈 제외)
    "sector_top_n":      3,                # 기록할 주도섹터 수 (상위 N)
    "leaders_per_sector": 2,               # 섹터별 주도주 수
}

SECTOR_WORKSHEET = "주도섹터"

# ============================================================

def build_sector_map():
    """종목코드 → 섹터(업종) 매핑. FinanceDataReader 'KRX-DESC' 목록 사용.
      - 실제 업종은 'Industry' 컬럼 (예: '특수 목적용 기계 제조업')
      - 'Sector' 컬럼은 시장구분(벤처/중견기업부)이라 사용하지 않음
    실패 시 빈 dict 반환 → 섹터는 '-' 로 채워짐."""
    if not FDR_AVAILABLE:
        print("  ℹ️  FinanceDataReader 미설치 → 섹터 '-' 로 기록 "
              "(pip install finance-datareader)")
        return {}
    sector_map = {}
    try:
        listing = fdr.StockListing("KRX-DESC")
        if "Industry" in listing.columns:
            for _, row in listing.iterrows():
                code = str(row["Code"]).zfill(6)
                sec  = row["Industry"]
                if pd.notna(sec) and str(sec).strip():
                    sector_map[code] = str(sec).strip()
    except Exception as e:
        print(f"  ⚠️  섹터 조회 실패: {e}")
    if sector_map:
        print(f"  🏷️  섹터(업종) 매핑 로드: {len(sector_map)}개 종목")
    return sector_map


def verdict(score, risk_on):
    """종합점수 + 시장국면으로 매매 판단.
    🔴위험회피 시장이면 점수와 무관하게 '관망'(백테스트: 역장 매수는 불리)."""
    if not risk_on:
        return "🔴 관망(역장)"
    if score >= 7:
        return "✅ 진입후보"
    if score >= 5:
        return "⚠️ 관심"
    return "❌ 관망"


def send_telegram(text):
    """텔레그램 메시지 전송. 토큰/chat_id 미설정 시 조용히 패스."""
    token = TELEGRAM_BOT_TOKEN.strip()
    chat  = TELEGRAM_CHAT_ID.strip()
    if not token or not chat:
        print("  ℹ️  텔레그램 미설정 → 알림 생략 "
              "(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수)")
        return False
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as resp:
            ok = resp.status == 200
        print("  📨 텔레그램 전송 완료" if ok else f"  ⚠️  텔레그램 응답 {resp.status}")
        return ok
    except Exception as e:
        print(f"  ⚠️  텔레그램 전송 실패: {e}")
        return False


def notify_telegram(df, market_ctx, base_date, top_n=TELEGRAM_TOP_N):
    """내일 후보 상위 N종목을 텔레그램으로 발송."""
    date_str = datetime.strptime(base_date, "%Y%m%d").strftime("%Y-%m-%d")
    entry_n  = sum(verdict(r["종합점수"], bool(r["risk_on"])) == "✅ 진입후보"
                   for _, r in df.iterrows())
    lines = [
        f"📈 <b>스윙 후보 TOP{top_n}</b>  ({date_str} 종가 기준 · 내일 매매용)",
        f"🧭 KOSPI {market_ctx['KOSPI']['label']} · "
        f"KOSDAQ {market_ctx['KOSDAQ']['label']}",
        f"✅ 진입후보 {entry_n}개" + ("" if entry_n else " — 무리 매매 자제, 관망 권장"),
        "",
    ]
    for _, r in df.head(top_n).iterrows():
        v = verdict(r["종합점수"], bool(r["risk_on"]))
        rs_txt = f"{r['RS(%)']:+.1f}" if r["RS(%)"] is not None else "-"
        lines.append(
            f"<b>{r['순위']}. {r['종목명']}</b> ({r['종목코드']})  {v}\n"
            f"  종합 {int(r['종합점수'])}/9 · RS {rs_txt}% · {r['섹터']}\n"
            f"  💰매수 {int(r['매수가']):,} / 🛡️손절 {int(r['손절가']):,} / "
            f"🎯익절 {int(r['익절가']):,}"
        )
    lines.append("\n※ 매수가 지정가 대응 · 갭상승 추격 금지 · 손절 칼같이")
    send_telegram("\n".join(lines))


def calc_trade_levels(current, ma20, atr=None):
    """풀백 스윙 매매 가격대 계산. (FILTER 설정값 사용)
      매수가 = 현재가(진입 기준)
      손절가 = 지지선(현재가·MA20 중 낮은 값) − ATR × atr_mult   ← 변동성 반영
               (ATR 없으면 지지선 × (1 - stop_buffer) 로 대체)
      익절가 = 현재가 + (현재가 - 손절가) × reward_ratio        ← 손익비
    """
    support = min(current, ma20)
    if atr is not None and atr > 0:
        stop = support - atr * FILTER["atr_mult"]
    else:
        stop = support * (1 - FILTER["stop_buffer"])
    stop   = min(stop, current * 0.999)        # 손절가는 항상 현재가보다 낮게
    risk   = current - stop
    target = current + risk * FILTER["reward_ratio"]
    return round(current, 0), round(stop, 0), round(target, 0)


def fetch_universe(markets):
    """FDR 상장목록으로 종목 유니버스 + 거래대금/시총 반환.
    pykrx 1.2.8 의 일괄 API(get_market_ticker_list / get_market_ohlcv_by_ticker)가
    빈 응답을 주는 문제를 우회한다. (개별 OHLCV 는 pykrx 그대로 사용)
    반환: DataFrame(index=종목코드,
                   cols=[Name, Market, Close, Volume, Amount, Marcap, ChagesRatio, _mkt])"""
    if not FDR_AVAILABLE:
        print("  ❌ FinanceDataReader 미설치 → 유니버스 조회 불가 "
              "(pip install finance-datareader)")
        return None
    need = ["Code", "Name", "Market", "Close", "Volume",
            "Amount", "Marcap", "ChagesRatio"]
    frames = []
    for mkt in markets:
        try:
            df = fdr.StockListing(mkt)
            df = df[[c for c in need if c in df.columns]].copy()
            df["_mkt"] = mkt
            frames.append(df)
        except Exception as e:
            print(f"  ⚠️  {mkt} 유니버스 조회 실패: {e}")
    if not frames:
        return None
    uni = pd.concat(frames, ignore_index=True)
    uni["Code"] = uni["Code"].astype(str).str.zfill(6)
    uni = uni.drop_duplicates("Code").set_index("Code")
    return uni


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


def calc_atr(high, low, close, period=14):
    """평균 진폭(ATR). 변동성 기반 손절폭 산정용. 데이터 부족 시 None."""
    if len(close) <= period:
        return None
    prev_close = close.shift(1)
    tr = pd.concat([(high - low),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else None


def pct_return(series, lag):
    """series 의 lag일 전 대비 수익률(%). 부족 시 None."""
    if len(series) <= lag or pd.isna(series.iloc[-1]) or pd.isna(series.iloc[-1 - lag]):
        return None
    base = series.iloc[-1 - lag]
    if base == 0:
        return None
    return (series.iloc[-1] - base) / base * 100


def fetch_market_context(base_date):
    """시장 국면 + 지수 수익률(상대강도 비교 기준)을 한 번에 산출.
    FDR 지수(KS11=KOSPI, KQ11=KOSDAQ) 사용.
    반환: {시장: {'risk_on': bool, 'label': str, 'ret': float|None}}"""
    ctx = {}
    sym = {"KOSPI": "KS11", "KOSDAQ": "KQ11"}
    start = (datetime.strptime(base_date, "%Y%m%d")
             - timedelta(days=200)).strftime("%Y-%m-%d")
    end = datetime.strptime(base_date, "%Y%m%d").strftime("%Y-%m-%d")
    for mkt, code in sym.items():
        info = {"risk_on": False, "label": "❔ 불명", "ret": None}
        if FDR_AVAILABLE:
            try:
                idx = fdr.DataReader(code, start, end)
                c = idx["Close"].dropna()
                if len(c) >= 60:
                    ma5, ma20, ma60 = (c.rolling(p).mean().iloc[-1] for p in (5, 20, 60))
                    cur = c.iloc[-1]
                    risk_on = (cur > ma20) and (ma5 > ma20 > ma60)
                    info = {
                        "risk_on": bool(risk_on),
                        "label": "🟢 위험선호" if risk_on else "🔴 위험회피",
                        "ret": pct_return(c, FILTER["rs_period"]),
                    }
            except Exception as e:
                print(f"  ⚠️  {mkt} 지수 조회 실패: {e}")
        ctx[mkt] = info
    return ctx


def run_screener():
    base_date  = get_base_date()
    start_date = (datetime.strptime(base_date, "%Y%m%d")
                  - timedelta(days=FILTER["lookback_days"])).strftime("%Y%m%d")

    print(f"\n{'='*58}")
    print(f"  📈 스윙매매 종목 스크리너")
    print(f"  기준일: {base_date}  |  데이터: KRX 개별 OHLCV(pykrx) + 유니버스(FDR)")
    print(f"{'='*58}")

    # 섹터(업종) 매핑 1회 로드
    sector_map = build_sector_map()

    # 1+2. 유니버스 + 거래대금/시총 (FDR로 일괄 — pykrx 일괄 API 빈응답 우회)
    print("  ① 유니버스·거래대금 로드 중 (FDR)...")
    uni = fetch_universe(FILTER["markets"])
    if uni is None or uni.empty:
        print("  ❌ 종목 유니버스를 가져오지 못했습니다 (FDR 확인 필요).")
        return
    liquid  = uni[uni["Amount"] >= FILTER["min_trade_amount"]]
    tickers = [(code, liquid.loc[code, "_mkt"]) for code in liquid.index]
    print(f"     전체 {len(uni)}개 → 거래대금 100억↑ 통과: {len(tickers)}개")

    # 시장 국면 + 지수 수익률(상대강도 기준) 1회 산출
    market_ctx = fetch_market_context(base_date)
    print(f"  🧭 시장국면  KOSPI {market_ctx['KOSPI']['label']}  ·  "
          f"KOSDAQ {market_ctx['KOSDAQ']['label']}")

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
        high    = df["고가"]
        low     = df["저가"]
        volume  = df["거래량"]
        current = close.iloc[-1]

        ma5  = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        ma120 = close.rolling(120).mean().iloc[-1] if len(close) >= 120 else None
        if any(pd.isna(v) for v in (ma5, ma20, ma60)):
            continue

        if ticker in uni.index:
            mktcap       = int(uni.loc[ticker, "Marcap"])
            trade_amount = int(uni.loc[ticker, "Amount"])
        else:
            mktcap       = 0
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

        # ── 추가 지표 ──
        # 52주 신고가 근접도 (최근 250영업일 고가 대비 위치, %; 0에 가까울수록 신고가)
        high_52w  = high.tail(250).max()
        dist_high = (current - high_52w) / high_52w * 100 if high_52w else None
        near_high = dist_high is not None and dist_high >= -FILTER["near_high_pct"]

        # ATR(변동성) 및 변동성 비율
        atr     = calc_atr(high, low, close, FILTER["atr_period"])
        atr_pct = (atr / current * 100) if atr else None

        # 상대강도(RS): 종목 N일 수익률 − 시장(지수) N일 수익률
        stock_ret = pct_return(close, FILTER["rs_period"])
        idx_ret   = market_ctx.get(market, {}).get("ret")
        rs        = (stock_ret - idx_ret) if (stock_ret is not None and idx_ret is not None) else None
        rs_strong = rs is not None and rs > 0

        # 박스권 돌파: 직전 N일 고점을 당일 종가가 돌파
        box_high = high.tail(FILTER["box_period"] + 1).head(FILTER["box_period"]).max()
        breakout = box_high is not None and current > box_high

        # 거래량 돌파: 당일 거래량 ≥ 20일 평균 × 배수
        vol_ma20    = volume.tail(20).mean()
        vol_breakout = vol_ma20 and volume.iloc[-1] >= vol_ma20 * FILTER["vol_breakout_mult"]

        regime    = market_ctx.get(market, {}).get("label", "❔")
        risk_on   = market_ctx.get(market, {}).get("risk_on", False)

        if ticker in uni.index and pd.notna(uni.loc[ticker, "Name"]):
            name = str(uni.loc[ticker, "Name"])
        else:
            name = ticker

        buy_price, stop_price, target_price = calc_trade_levels(current, ma20, atr)
        sector = sector_map.get(ticker, "-")

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
            "매수가": buy_price, "손절가": stop_price, "익절가": target_price,
            "섹터": sector,
            "52주위치(%)": round(dist_high, 1) if dist_high is not None else None,
            "신고가근접": bool(near_high),
            "ATR(%)": round(atr_pct, 2) if atr_pct is not None else None,
            "RS(%)": round(rs, 1) if rs is not None else None,
            "RS강세": bool(rs_strong),
            "박스돌파": bool(breakout),
            "거래량돌파": bool(vol_breakout),
            "시장국면": regime, "risk_on": bool(risk_on),
        })
        time.sleep(0.05)

    print(f"\n  ③ 분석 완료: {len(results)}개 → 상위 {FILTER['top_n']}개 선별")

    # 당일 주도섹터(+주도주)는 스크리닝 결과와 무관하게 항상 산출
    sec_header, sec_rows = analyze_sector_leaders(uni, sector_map, base_date)

    if not results:
        print("  ❌ 조건에 맞는 종목이 없습니다.")
        write_sector_to_gsheet(sec_header, sec_rows)
        return

    # ── 종합점수(0~9) = 기존 5점 + 신고가근접 + RS강세 + 박스돌파 + 거래량돌파 ──
    df_result = pd.DataFrame(results)
    df_result["종합점수"] = (df_result["점수"]
                          + df_result["신고가근접"].astype(int)
                          + df_result["RS강세"].astype(int)
                          + df_result["박스돌파"].astype(int)
                          + df_result["거래량돌파"].astype(int))
    # 순위: 종합점수 ↓ → 상대강도 RS ↓ → 거래대금 ↓
    df_result["_rs"] = df_result["RS(%)"].fillna(-999.0)
    df_result = (df_result
                 .sort_values(["종합점수", "_rs", "거래대금"],
                              ascending=[False, False, False])
                 .drop(columns="_rs")
                 .head(FILTER["top_n"])
                 .reset_index(drop=True))
    df_result.insert(0, "순위", range(1, len(df_result) + 1))

    # 4. 콘솔 출력
    print(f"\n{'─'*84}")
    print(f"  {'순위':^4} {'종목명':^12} {'코드':^8} {'현재가':>9} "
          f"{'20선':>7} {'RS%':>6} {'52주%':>7} {'종합':>5}  {'패턴'}")
    print(f"{'─'*84}")
    for _, r in df_result.iterrows():
        pattern = []
        if not r["risk_on"]: pattern.append("⚠️역장")
        if r["눌림목"]:     pattern.append("눌림목")
        if r["RSI건강"]:   pattern.append("RSI양호")
        if r["거래량수축"]: pattern.append("거래량수축")
        if r["추세강도"]:   pattern.append("추세강")
        if r["신고가근접"]: pattern.append("신고가근접")
        if r["RS강세"]:    pattern.append("RS강세")
        if r["박스돌파"]:   pattern.append("박스돌파")
        if r["거래량돌파"]: pattern.append("거래량돌파")
        rs_txt = f"{r['RS(%)']:>+6.1f}" if r["RS(%)"] is not None else "     -"
        hi_txt = f"{r['52주위치(%)']:>+7.1f}" if r["52주위치(%)"] is not None else "      -"
        print(f"  {r['순위']:^4} {r['종목명'][:10]:^12} {r['종목코드']:^8} "
              f"{r['현재가']:>9,.0f} {r['20일선대비(%)']:>+6.1f}% "
              f"{rs_txt} {hi_txt} {r['종합점수']:>3}/9  {'·'.join(pattern)}")
    print(f"{'─'*84}")
    entry_cnt = sum(verdict(r["종합점수"], bool(r["risk_on"])) == "✅ 진입후보"
                    for _, r in df_result.iterrows())
    top = df_result["종합점수"]
    print(f"  ✅ 진입후보(위험선호+7점↑): {entry_cnt}개  |  "
          f"종합 7+점: {len(top[top>=7])}개  5~6점: {len(top[(top>=5)&(top<7)])}개")

    write_to_gsheet(df_result, base_date)
    write_sector_to_gsheet(sec_header, sec_rows)
    notify_telegram(df_result, market_ctx, base_date)


def _open_spreadsheet():
    """구글시트 인증 후 (Spreadsheet, sid) 반환. 실패 시 (None, None).
    인증 우선순위:
      1) 환경변수 GOOGLE_CREDENTIALS_JSON  ← GitHub Actions Secret
      2) 로컬 파일 GOOGLE_CREDENTIALS_FILE ← 로컬 실행
    """
    if not GSPREAD_AVAILABLE:
        print("\n  ℹ️  gspread 미설치 → pip install gspread google-auth")
        return None, None

    sid = SPREADSHEET_ID.strip()
    if sid == "여기에_시트_ID_입력" or not sid:
        print("\n  ⚠️  SPREADSHEET_ID 가 설정되지 않았습니다.")
        print("     - GitHub Actions: Secret 'SPREADSHEET_ID' 에 URL ID 등록")
        print("     - 로컬 실행     : swing_screener.py 의 SPREADSHEET_ID 변수 수정")
        print("     - ID 위치       : https://docs.google.com/spreadsheets/d/[ID]/edit")
        return None, None

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
            return None, None

        gc = gspread.authorize(creds)
        return gc.open_by_key(sid), sid
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"\n  ❌ 시트 ID '{sid}' 를 찾을 수 없음")
        print(f"     서비스 계정 이메일에 편집자 권한이 공유되어 있는지 확인")
        return None, None
    except Exception as e:
        print(f"\n  ❌ 구글시트 인증 실패: {e}")
        return None, None


def write_to_gsheet(df, base_date):
    """스크리닝 결과를 WORKSHEET_NAME 탭에 누적 기록."""
    sh, sid = _open_spreadsheet()
    if sh is None:
        return
    try:
        ws = sh.worksheet(WORKSHEET_NAME)

        existing = ws.get_all_values()
        next_row = len(existing) + 1

        def cell(v):
            """None/NaN → 빈칸으로 안전 변환."""
            return "" if v is None or (isinstance(v, float) and pd.isna(v)) else v

        rows = []
        for _, r in df.iterrows():
            judgment = verdict(r["종합점수"], bool(r["risk_on"]))
            rsi_txt = f"{r['RSI']:.0f}" if r["RSI"] is not None else "-"
            memo = (f"이격{r['20일선대비(%)']:+.1f}% · RSI{rsi_txt} · "
                    f"거래량x{r['거래량비']:.2f}")
            # A~P(기존 템플릿 유지): 날짜/종목명/코드/시장/거래대금100억↑/정배열·추세/
            #   RSI건강/눌림목/거래량수축/점수/판단/메모/매수가/손절가/익절가/섹터
            # Q~Z(신규 추가): 순위/종합점수/시장국면/RS(%)/52주위치(%)/ATR(%)/
            #   신고가근접/RS강세/박스돌파/거래량돌파
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
                int(r["매수가"]), int(r["손절가"]), int(r["익절가"]),
                r["섹터"],
                int(r["순위"]), int(r["종합점수"]), r["시장국면"],
                cell(r["RS(%)"]), cell(r["52주위치(%)"]), cell(r["ATR(%)"]),
                bool(r["신고가근접"]), bool(r["RS강세"]),
                bool(r["박스돌파"]), bool(r["거래량돌파"]),
            ])

        ws.update(f"A{next_row}", rows)
        print(f"  ✅ 구글시트 기록 완료: {len(rows)}행 추가 (행 {next_row}~)")
        print(f"  🔗 https://docs.google.com/spreadsheets/d/{sid}")
    except Exception as e:
        print(f"\n  ❌ 구글시트 기록 실패: {e}")


def analyze_sector_leaders(uni, sector_map, base_date):
    """당일 주도섹터(상위 N)와 섹터별 주도주를 산출.
    데이터: FDR 유니버스(ChagesRatio=일별등락률, Amount=거래대금) + 업종 매핑.
    반환: (헤더 list, 시트행 list of list). 데이터 부족 시 (None, None)."""
    if uni is None or "ChagesRatio" not in uni.columns:
        print("  ⚠️  등락률 데이터 없음 → 주도섹터 분석 건너뜀")
        return None, None

    df = uni.copy()
    df["업종"] = [sector_map.get(code) for code in df.index]
    df = df[df["업종"].notna() & df["ChagesRatio"].notna()
            & (df["Amount"] >= FILTER["sector_min_amount"])]
    if df.empty:
        print("  ⚠️  주도섹터 집계 대상 종목이 없음")
        return None, None

    g = df.groupby("업종")["ChagesRatio"].agg(평균등락="mean", 종목수="count")
    g = g[g["종목수"] >= FILTER["sector_min_stocks"]]
    if g.empty:
        print("  ⚠️  최소 종목 수를 만족하는 섹터가 없음")
        return None, None
    g = g.sort_values("평균등락", ascending=False).head(FILTER["sector_top_n"])

    date_str = datetime.strptime(base_date, "%Y%m%d").strftime("%Y-%m-%d")
    n_lead   = FILTER["leaders_per_sector"]

    header = ["날짜", "순위", "섹터", "섹터평균등락(%)", "종목수"]
    for k in range(1, n_lead + 1):
        header += [f"주도주{k}", f"등락{k}(%)"]

    rows = []
    print(f"\n  ── 📊 당일 주도섹터 TOP{len(g)} ──")
    for rank, (sector, srow) in enumerate(g.iterrows(), start=1):
        sec_df = df[df["업종"] == sector]
        # 주도주 = 상승 종목 중 거래대금 상위(섹터를 끌어올린 실제 주도주).
        #          상승 종목이 부족하면 등락률 상위로 보충.
        up = sec_df[sec_df["ChagesRatio"] > 0].sort_values("Amount", ascending=False)
        if len(up) >= n_lead:
            leaders = up.head(n_lead)
        else:
            leaders = sec_df.sort_values("ChagesRatio", ascending=False).head(n_lead)
        row = [date_str, rank, sector,
               round(float(srow["평균등락"]), 2), int(srow["종목수"])]
        lead_txt = []
        for code, lr in leaders.iterrows():
            row += [str(lr["Name"]), round(float(lr["ChagesRatio"]), 2)]
            lead_txt.append(f"{lr['Name']}({lr['ChagesRatio']:+.1f}%)")
        # 주도주가 부족하면 빈칸 채움
        while len(row) < len(header):
            row += ["", ""]
        rows.append(row)
        print(f"   {rank}. {sector}  평균{srow['평균등락']:+.2f}% "
              f"({int(srow['종목수'])}종목)  주도주: {' · '.join(lead_txt)}")

    return header, rows


def write_sector_to_gsheet(header, rows):
    """주도섹터 결과를 SECTOR_WORKSHEET 탭에 누적 기록 (탭 없으면 생성)."""
    if not rows:
        return
    sh, sid = _open_spreadsheet()
    if sh is None:
        return
    try:
        try:
            ws = sh.worksheet(SECTOR_WORKSHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=SECTOR_WORKSHEET,
                                  rows=2000, cols=max(len(header), 12))
            ws.update("A1", [header])
            print(f"  🆕 '{SECTOR_WORKSHEET}' 탭 생성 + 헤더 작성")

        existing = ws.get_all_values()
        if len(existing) == 0:          # 빈 탭이면 헤더부터
            ws.update("A1", [header])
            next_row = 2
        else:
            next_row = len(existing) + 1

        ws.update(f"A{next_row}", rows)
        print(f"  ✅ '{SECTOR_WORKSHEET}' 기록 완료: {len(rows)}행 추가 (행 {next_row}~)")
    except Exception as e:
        print(f"\n  ❌ 주도섹터 기록 실패: {e}")


if __name__ == "__main__":
    run_screener()
