"""
============================================================
 스윙 스크리너 백테스트
------------------------------------------------------------
 목적 : "매일 종합점수 상위 N종목 매수 → 손절/익절 규칙" 의
        과거 성과(승률·손익비·기대값)를 실측한다.
 데이터: pykrx 개별 OHLCV(고가/저가 포함) + FDR 지수(상대강도)
 주의 : 유니버스를 '현재 거래대금 상위'로 고정하므로 생존편향이
        존재한다. 절대 수치보다 '규칙의 상대적 유효성' 참고용.
 실행 : python backtest.py
============================================================
"""

import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pykrx import stock

import swing_screener as s
from swing_screener import FILTER, calc_trade_levels, fetch_universe, fetch_ohlcv

try:
    import FinanceDataReader as fdr
    FDR_AVAILABLE = True
except ImportError:
    FDR_AVAILABLE = False

# ============================================================
#  백테스트 설정
# ============================================================
BT = {
    "universe_size": 40,    # 백테스트 대상 종목 수 (현재 거래대금 상위)
    "test_days":     90,    # 검증 구간(영업일). 최근 N일에 대해 신호 생성
    "max_hold":      10,    # 최대 보유일 (손절·익절 미도달 시 청산)
    "top_n":         3,     # 매일 매수할 상위 종목 수 (종합점수 순)
    "min_score":     0,     # 종합점수 최소 컷 (0=컷 없음, 예: 7 이면 7점↑만)
    "history_days":  500,   # 종목별 OHLCV 확보 기간(지표 워밍업 + 검증 + 보유)
}


# ============================================================
#  지표 시리즈 (벡터화 — 룩어헤드 없이 날짜별 신호 산출)
# ============================================================
def rsi_series(close, period):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / period, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out[al == 0] = 100.0
    return out


def atr_series(high, low, close, period):
    pc = close.shift(1)
    tr = pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def build_signals(df, idx_ret):
    """종목 OHLCV → 날짜별 신호 DataFrame.
    idx_ret: 해당 시장 지수의 rs_period 수익률 시리즈(날짜 정렬)."""
    close, high, low, volume = df["종가"], df["고가"], df["저가"], df["거래량"]
    out = pd.DataFrame(index=df.index)

    ma5  = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()

    dist_ma20  = (close - ma20) / ma20 * 100
    ma60_slope = (ma60 - ma60.shift(20)) / ma60.shift(20) * 100
    ma20_slope = (ma20 - ma20.shift(5)) / ma20.shift(5) * 100
    rsi = rsi_series(close, FILTER["rsi_period"])
    atr = atr_series(high, low, close, FILTER["atr_period"])

    jeong = (ma5 > ma20) & (ma20 > ma60)
    uptrend = (close > ma60) & (ma60_slope > 0)
    not_ext = dist_ma20 <= FILTER["max_ext_ma20"]
    liquidity = (close * volume) >= FILTER["min_trade_amount"]

    vol_recent = volume.rolling(5).mean()
    vol_base   = volume.shift(5).rolling(20).mean()
    vol_ratio  = vol_recent / vol_base

    pb_lo, pb_hi   = FILTER["pullback_zone"]
    rsi_lo, rsi_hi = FILTER["rsi_zone"]
    in_pullback = (dist_ma20 >= pb_lo) & (dist_ma20 <= pb_hi)
    rsi_healthy = (rsi >= rsi_lo) & (rsi <= rsi_hi)
    vol_dry     = vol_ratio < 1.0
    trend_strong = (ma20_slope > 0) & (ma120.isna() | (close > ma120))

    score = (liquidity.astype(int) + in_pullback.astype(int)
             + rsi_healthy.astype(int) + vol_dry.astype(int)
             + trend_strong.astype(int))

    high_52w  = high.rolling(250).max()
    dist_high = (close - high_52w) / high_52w * 100
    near_high = dist_high >= -FILTER["near_high_pct"]

    stock_ret = (close / close.shift(FILTER["rs_period"]) - 1) * 100
    rs = stock_ret - idx_ret.reindex(df.index)
    rs_strong = rs > 0

    box_high = high.shift(1).rolling(FILTER["box_period"]).max()
    breakout = close > box_high
    vol_ma20 = volume.rolling(20).mean()
    vol_breakout = volume >= vol_ma20 * FILTER["vol_breakout_mult"]

    out["pass"]   = jeong & uptrend & not_ext & liquidity
    out["종합점수"] = (score + near_high.astype(int) + rs_strong.astype(int)
                    + breakout.astype(int) + vol_breakout.astype(int))
    out["rs"]     = rs
    out["close"]  = close
    out["ma20"]   = ma20
    out["atr"]    = atr
    out["amount"] = close * volume
    return out


def index_ret_series(code, start, end):
    if not FDR_AVAILABLE:
        return pd.Series(dtype=float)
    idx = fdr.DataReader(code, start, end)
    c = idx["Close"].dropna()
    c.index = pd.to_datetime(c.index)
    return (c / c.shift(FILTER["rs_period"]) - 1) * 100


def index_regime_series(code, start, end):
    """지수 일별 위험선호(risk_on) 여부 시리즈. (close>ma20 & ma5>ma20>ma60)"""
    if not FDR_AVAILABLE:
        return pd.Series(dtype=bool)
    idx = fdr.DataReader(code, start, end)
    c = idx["Close"].dropna()
    c.index = pd.to_datetime(c.index)
    ma5, ma20, ma60 = (c.rolling(p).mean() for p in (5, 20, 60))
    return (c > ma20) & (ma5 > ma20) & (ma20 > ma60)


# ============================================================
#  포워드 시뮬레이션 (한 종목 한 진입)
# ============================================================
def simulate_trade(high, low, close, i, stop, target, max_hold):
    """i일 종가 진입 후, 다음날부터 손절/익절 도달 검사.
    반환: (청산수익률%, 보유일, 결과['익절'|'손절'|'시간초과'])"""
    entry = close.iloc[i]
    n = len(close)
    for h in range(1, max_hold + 1):
        j = i + h
        if j >= n:
            break
        # 보수적: 같은 날 손절·익절 동시 도달 시 손절 우선
        if low.iloc[j] <= stop:
            return (stop / entry - 1) * 100, h, "손절"
        if high.iloc[j] >= target:
            return (target / entry - 1) * 100, h, "익절"
    # 미도달 → 보유 마지막 날 종가 청산
    j = min(i + max_hold, n - 1)
    held = j - i
    if held <= 0:
        return None
    return (close.iloc[j] / entry - 1) * 100, held, "시간초과"


def load_data():
    """OHLCV·지표·지수 1회 로드. (sig_map, ohlc_map, regime_map, test_dates) 반환."""
    base_date = s.get_base_date()
    end_dt    = datetime.strptime(base_date, "%Y%m%d")
    start_dt  = end_dt - timedelta(days=BT["history_days"])
    start, end = start_dt.strftime("%Y%m%d"), base_date
    sidx, eidx = start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")

    uni = fetch_universe(FILTER["markets"])
    if uni is None or uni.empty:
        print("  ❌ 유니버스 로드 실패"); return None
    uni = uni.sort_values("Amount", ascending=False).head(BT["universe_size"])
    print(f"  대상 종목: {len(uni)}개")

    idx_rets = {"KOSPI":  index_ret_series("KS11", sidx, eidx),
                "KOSDAQ": index_ret_series("KQ11", sidx, eidx)}
    regime_map = {"KOSPI":  index_regime_series("KS11", sidx, eidx),
                  "KOSDAQ": index_regime_series("KQ11", sidx, eidx)}

    print("  종목별 OHLCV·지표 계산 중...")
    sig_map, ohlc_map, mkt_map = {}, {}, {}
    codes = list(uni.index)
    for k, code in enumerate(codes):
        print(f"     {k+1}/{len(codes)}", end="\r")
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 260:
            continue
        df.index = pd.to_datetime(df.index)
        mkt = uni.loc[code, "_mkt"]
        sig_map[code]  = build_signals(df, idx_rets.get(mkt, pd.Series(dtype=float)))
        ohlc_map[code] = df
        mkt_map[code]  = mkt
        time.sleep(0.03)
    print(f"\n  유효 종목: {len(sig_map)}개")
    if not sig_map:
        return None

    all_dates = sorted(set().union(*[s_.index for s_ in sig_map.values()]))
    test_dates = all_dates[-(BT["test_days"] + BT["max_hold"]):-BT["max_hold"]] \
        if len(all_dates) > BT["test_days"] + BT["max_hold"] else all_dates[:-BT["max_hold"]]
    return sig_map, ohlc_map, mkt_map, regime_map, test_dates


def evaluate(data, top_n, max_hold, min_score, regime_only=False):
    """주어진 설정으로 매매 시뮬 → trades 리스트 반환."""
    sig_map, ohlc_map, mkt_map, regime_map, test_dates = data
    trades = []
    for d in test_dates:
        cands = []
        for code, sg in sig_map.items():
            if d not in sg.index:
                continue
            row = sg.loc[d]
            if not bool(row["pass"]) or pd.isna(row["종합점수"]):
                continue
            if row["종합점수"] < min_score:
                continue
            if regime_only:
                reg = regime_map.get(mkt_map[code])
                if reg is None or d not in reg.index or not bool(reg.loc[d]):
                    continue
            cands.append((code, float(row["종합점수"]),
                          row["rs"] if pd.notna(row["rs"]) else -999.0,
                          row["amount"]))
        if not cands:
            continue
        cands.sort(key=lambda x: (-x[1], -x[2], -x[3]))
        for code, sc, _, _ in cands[:top_n]:
            df = ohlc_map[code]
            i = df.index.get_loc(d)
            sg = sig_map[code].loc[d]
            _, stop, target = calc_trade_levels(
                sg["close"], sg["ma20"],
                sg["atr"] if pd.notna(sg["atr"]) else None)
            res = simulate_trade(df["고가"], df["저가"], df["종가"],
                                 i, stop, target, max_hold)
            if res is None:
                continue
            ret, held, outcome = res
            trades.append({"날짜": d, "종목": code, "종합점수": int(sc),
                           "수익률": ret, "보유일": held, "결과": outcome})
    return trades


def stats(trades):
    """trades → 핵심 지표 dict."""
    if not trades:
        return None
    t = pd.DataFrame(trades)
    n = len(t)
    wins, losses = t[t["수익률"] > 0], t[t["수익률"] <= 0]
    avg_win  = wins["수익률"].mean() if len(wins) else 0.0
    avg_loss = losses["수익률"].mean() if len(losses) else 0.0
    return {
        "n": n, "win_rate": len(wins) / n * 100,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "rr": (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf"),
        "expectancy": t["수익률"].mean(), "hold": t["보유일"].mean(),
    }


def run_backtest():
    print(f"\n{'='*60}")
    print(f"  🔬 스윙 스크리너 백테스트")
    print(f"  검증구간 최근 {BT['test_days']}영업일 · 보유 최대 {BT['max_hold']}일 · "
          f"매일 상위 {BT['top_n']}종목")
    print(f"{'='*60}")
    data = load_data()
    if data is None:
        print("  ❌ 데이터 로드 실패"); return
    trades = evaluate(data, BT["top_n"], BT["max_hold"], BT["min_score"])
    report(trades)


def run_compare():
    """데이터 1회 로드 후 여러 설정 비교."""
    print(f"\n{'='*78}")
    print(f"  🔬 백테스트 설정 비교 (검증 {BT['test_days']}영업일)")
    print(f"{'='*78}")
    data = load_data()
    if data is None:
        print("  ❌ 데이터 로드 실패"); return

    scenarios = [
        ("기준(top3·보유10·컷0)",       dict(top_n=3, max_hold=10, min_score=0)),
        ("시장국면 필터(위험선호만)",     dict(top_n=3, max_hold=10, min_score=0, regime_only=True)),
        ("고득점만(7점↑)",              dict(top_n=3, max_hold=10, min_score=7)),
        ("7점↑ + 시장국면",             dict(top_n=3, max_hold=10, min_score=7, regime_only=True)),
        ("집중(top1)",                 dict(top_n=1, max_hold=10, min_score=0)),
        ("분산(top5)",                 dict(top_n=5, max_hold=10, min_score=0)),
        ("단기보유(5일)",               dict(top_n=3, max_hold=5,  min_score=0)),
        ("장기보유(20일)",              dict(top_n=3, max_hold=20, min_score=0)),
    ]

    hdr = f"  {'시나리오':<24}{'매매':>5}{'승률':>7}{'평균승':>8}{'평균패':>8}{'손익비':>7}{'기대값':>8}{'보유':>6}"
    print(f"\n{hdr}")
    print(f"  {'-'*72}")
    for name, cfg in scenarios:
        st = stats(evaluate(data, **cfg))
        if st is None:
            print(f"  {name:<24}{'— 매매 없음':>20}")
            continue
        print(f"  {name:<24}{st['n']:>5}{st['win_rate']:>6.1f}%"
              f"{st['avg_win']:>+8.2f}{st['avg_loss']:>+8.2f}"
              f"{st['rr']:>6.2f}{st['expectancy']:>+8.2f}{st['hold']:>5.1f}일")
    print(f"  {'-'*72}")
    print("  ⚠️ 유니버스=현재 거래대금 상위 고정 → 생존편향·강세장 구간 보정 필요. 참고용.")


def report(trades):
    print(f"\n{'─'*60}")
    if not trades:
        print("  ❌ 체결된 매매가 없습니다 (조건을 만족한 신호 없음).")
        return
    t = pd.DataFrame(trades)
    n = len(t)
    wins = t[t["수익률"] > 0]
    losses = t[t["수익률"] <= 0]
    win_rate = len(wins) / n * 100
    avg_win  = wins["수익률"].mean() if len(wins) else 0.0
    avg_loss = losses["수익률"].mean() if len(losses) else 0.0
    expectancy = t["수익률"].mean()
    rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf")

    print(f"  📊 백테스트 결과")
    print(f"{'─'*60}")
    print(f"  총 매매      : {n}건")
    print(f"  승률         : {win_rate:5.1f}%  (승 {len(wins)} / 패 {len(losses)})")
    print(f"  평균수익(승) : {avg_win:+5.2f}%")
    print(f"  평균손실(패) : {avg_loss:+5.2f}%")
    print(f"  실현 손익비  : {rr:4.2f} : 1")
    print(f"  기대값/매매  : {expectancy:+5.2f}%   ← 양수면 통계적 우위")
    print(f"  평균보유일   : {t['보유일'].mean():4.1f}일")
    # 결과 분포
    dist = t["결과"].value_counts()
    print(f"  청산유형     : " +
          "  ".join(f"{k} {v}건" for k, v in dist.items()))
    # 종합점수대별 성과
    print(f"{'─'*60}")
    print(f"  [종합점수대별 성과]")
    for sc in sorted(t["종합점수"].unique(), reverse=True):
        sub = t[t["종합점수"] == sc]
        wr = (sub["수익률"] > 0).mean() * 100
        print(f"   {sc}점: {len(sub):3}건  승률{wr:5.1f}%  기대값{sub['수익률'].mean():+5.2f}%")
    print(f"{'─'*60}")
    print("  ⚠️ 유니버스=현재 거래대금 상위 고정 → 생존편향 존재. 참고용 지표.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        run_compare()
    else:
        run_backtest()
