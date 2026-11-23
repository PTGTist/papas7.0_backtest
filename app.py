import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import date, timedelta
from io import BytesIO
import re

st.set_page_config(page_title="파파스 7.0 백테스트", layout="wide")

sell_ratios = [0.10, 0.25, 0.50, 1.00]
intervals = [x / 200 for x in range(2, 21)]  # 1.0% ~ 10.0%, 0.5% 단위

st.title("파파스 7.0 백테스트")
st.caption(f"{len(sell_ratios)*len(intervals)}개 조합 비교 · 과거 OHLC 데이터 기반 · CSV/XLSX 내보내기")

with st.sidebar:
    st.header("백테스트 조건")
    ticker = st.text_input("종목 티커 / 종목코드", "SOXL",
                           help="미국: SOXL, TQQQ 등 · 국내: 6자리 종목코드(예: 005930) 또는 005930.KS / 035720.KQ").strip().upper()
    start_date = st.date_input("백테스트 시작일", date(2022, 1, 1))
    end_date = st.date_input("백테스트 종료일", date.today() - timedelta(days=1))
    initial_shares = st.number_input("초기 보유 수량", min_value=0.0001, value=120.0, step=1.0)
    avg_cost = st.number_input("초기 평단가(미국 USD · 국내 원)", min_value=0.01, value=30.88, step=0.01)
    st.markdown("---")
    st.subheader("전략 설정")
    st.caption("최초 매도 비율: 10% · 25% · 50% · 100%")
    st.caption("재매수 간격: 1%부터 10%까지 0.5% 단위")
    run = st.button("백테스트 실행", type="primary", use_container_width=True)

st.info(
    "기본 규칙: 최초 5거래일 종가 MOC로 매도 물량을 5등분 매도하고, "
    "5일 평균 매도가를 기준으로 5단계 등비 하락 가격에 재매수합니다. "
    "주봉 몸통이 5주선 위로 완전히 올라오면 1차 메이크업, "
    "30주선이 횡보·상승하면서 주봉 몸통이 300주선 위로 완전히 올라오면 2차 메이크업을 실행합니다."
)

def resolve_symbols(t):
    # 6자리 국내 종목코드면 코스피(.KS) → 코스닥(.KQ) 순서로 시도
    if re.fullmatch(r"[0-9][0-9A-Z]{5}", t):
        return [t + ".KS", t + ".KQ"]
    return [t]

def is_korean(symbol):
    return symbol.endswith(".KS") or symbol.endswith(".KQ")

def download_prices(symbol, start, end):
    # 300주선 계산을 위해 시작일보다 약 7년 앞선 데이터도 확보
    warmup = start - timedelta(days=365 * 8)
    data = yf.download(
        symbol, start=warmup, end=end + timedelta(days=1),
        auto_adjust=True, progress=False, group_by="column"
    )
    if data is None or data.empty:
        raise ValueError("가격 데이터를 가져오지 못했습니다. 티커와 기간을 확인하세요.")
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close"]
    missing = [c for c in required if c not in data.columns]
    if missing:
        raise ValueError(f"가격 데이터에 필요한 열이 없습니다: {missing}")
    data = data[required].dropna().copy()
    data.index = pd.to_datetime(data.index).tz_localize(None)
    return data

def weekly_indicators(daily):
    # 미국 주식 주봉: 금요일 기준. 해당 주 거래가 금요일 전에 끝나도 그 주의 마지막 거래일로 집계.
    w = daily.resample("W-FRI").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()
    w["MA5"] = w["Close"].rolling(5).mean()
    w["MA30"] = w["Close"].rolling(30).mean()
    w["MA300"] = w["Close"].rolling(300).mean()
    w["MA30_prev"] = w["MA30"].shift(1)
    w["BODY_LOW"] = w[["Open", "Close"]].min(axis=1)
    w["BODY_HIGH"] = w[["Open", "Close"]].max(axis=1)
    return w

def build_signals(daily_all):
    weekly = weekly_indicators(daily_all)
    # 주봉 신호일을 해당 주의 마지막 거래일로 연결
    weekly = weekly.copy()
    weekly["signal_date"] = weekly.index.map(
        lambda x: daily_all.loc[:x].index[-1] if len(daily_all.loc[:x]) else pd.NaT
    )
    weekly = weekly.dropna(subset=["signal_date"])
    weekly = weekly[weekly["signal_date"].isin(daily_all.index)]
    return {row["signal_date"]: row for _, row in weekly.iterrows()}

def run_one(daily_all, sig_by_date, start, end, shares0, avg0, sell_ratio, gap):
    daily = daily_all.loc[(daily_all.index.date >= start) & (daily_all.index.date <= end)].copy()
    if daily.empty:
        raise ValueError("선택한 기간에 거래 데이터가 없습니다.")

    shares = float(shares0)
    cash = 0.0
    initial_equity = shares0 * avg0
    sold_qty = shares0 * sell_ratio
    # 정확한 5등분을 위해 소수점 수량 허용
    tranche_sell = sold_qty / 5.0
    sale_proceeds = 0.0
    sell_days = list(daily.index[:5])
    trade_log = []
    daily_rows = []
    buyback_levels = []
    base_price = None
    buyback_qty_each = sold_qty / 5.0
    bought_back_spend = 0.0
    buyback_done = set()
    stage1_done = False
    stage2_done = False
    stage_queue = []  # (stage, remaining_days, budget_per_day)
    stage1_trigger_date = None
    stage2_trigger_date = None

    for i, (dt, bar) in enumerate(daily.iterrows()):
        # 최초 5거래일 종가 MOC 매도
        if dt in sell_days:
            q = min(tranche_sell, shares)
            if q > 0:
                px = float(bar["Close"])
                shares -= q
                cash += q * px
                sale_proceeds += q * px
                trade_log.append([dt, "최초 매도(MOC)", px, q, q * px, f"5일 분할 {sell_days.index(dt)+1}/5"])
            if dt == sell_days[-1]:
                base_price = sale_proceeds / sold_qty if sold_qty else None
                if base_price and base_price > 0:
                    buyback_levels = [base_price * ((1-gap) ** n) for n in range(1, 6)]

        # 기존 등비 재매수 주문: 당일 저가가 지정가 이하이면 체결
        if base_price is not None and dt not in sell_days:
            for n, level in enumerate(buyback_levels, start=1):
                if n in buyback_done:
                    continue
                if float(bar["Low"]) <= level and cash > 0:
                    px = min(float(bar["Open"]), level) if float(bar["Open"]) < level else level
                    q = min(buyback_qty_each, cash / px)
                    if q > 0:
                        spend = q * px
                        shares += q
                        cash -= spend
                        bought_back_spend += spend
                        buyback_done.add(n)
                        trade_log.append([dt, f"등비 재매수 {n}차", px, q, spend, f"기준가 {base_price:.4f}, 간격 {gap*100:.1f}%"])

        # 주봉 신호는 해당 주 마지막 거래일 종가 이후 확인, 다음 거래일부터 5일간 LOC 매수
        sig = sig_by_date.get(dt)
        if sig is not None and base_price is not None:
            body_low = sig["BODY_LOW"]
            body_high = sig["BODY_HIGH"]
            ma5, ma30, ma300 = sig["MA5"], sig["MA30"], sig["MA300"]
            if (not stage1_done and pd.notna(ma5) and body_low > ma5):
                # 1차 총 예산은 최초 매도대금의 50%, 기존 등비 재매수 금액을 차감
                budget = max(0.0, sale_proceeds * 0.50 - bought_back_spend)
                stage1_done = True
                stage1_trigger_date = dt
                if budget > 0:
                    stage_queue.append({"name": "1차 메이크업(5주선)", "budget": min(budget, cash), "days": 5})
            if (not stage2_done and stage1_done and pd.notna(ma30) and pd.notna(ma300)
                and pd.notna(sig["MA30_prev"]) and ma30 >= sig["MA30_prev"] and body_low > ma300):
                budget = max(0.0, sale_proceeds - bought_back_spend -
                             sum(x.get("remaining_budget", 0) for x in stage_queue))
                stage2_done = True
                stage2_trigger_date = dt
                if budget > 0:
                    stage_queue.append({"name": "2차 메이크업(30·300주선)", "budget": min(budget, cash), "days": 5})

        # 메이크업 큐: 신호 다음 거래일부터 5일간 매일 종가 LOC 매수
        # 당일 신호일에는 주문하지 않도록 queue_start를 다음 인덱스로 설정
        if stage_queue:
            for item in list(stage_queue):
                if item.get("start_after") is None:
                    # 큐 생성일을 기록하고 다음 거래일부터 시작
                    item["start_after"] = dt
                    continue
                if dt <= item["start_after"]:
                    continue
                if item["days"] > 0 and cash > 0:
                    days_left = item["days"]
                    daily_budget = min(item["budget"] / days_left, cash)
                    px = float(bar["Close"])  # LOC 체결가 근사
                    q = daily_budget / px
                    if q > 0:
                        spend = q * px
                        shares += q
                        cash -= spend
                        bought_back_spend += spend
                        item["budget"] -= spend
                        item["days"] -= 1
                        trade_log.append([dt, item["name"], px, q, spend, "다음 거래일부터 5일 종가 LOC 근사"])
                else:
                    item["days"] = 0
                if item["days"] <= 0 or item["budget"] <= 0.01:
                    stage_queue.remove(item)

        equity = shares * float(bar["Close"]) + cash
        daily_rows.append([dt, float(bar["Close"]), shares, cash, equity])

    daily_result = pd.DataFrame(daily_rows, columns=["Date", "Close", "Shares", "Cash", "Equity"]).set_index("Date")
    trades = pd.DataFrame(trade_log, columns=["Date", "Action", "Price", "Quantity", "Amount", "Note"])
    last_close = float(daily["Close"].iloc[-1])
    final_equity = shares * last_close + cash
    total_return = (final_equity / initial_equity - 1) * 100 if initial_equity else 0
    peak = daily_result["Equity"].cummax()
    mdd = ((daily_result["Equity"] / peak) - 1).min() * 100 if len(daily_result) else 0
    sell_count = int((trades["Action"] == "최초 매도(MOC)").sum()) if not trades.empty else 0
    return {
        "매도비율": f"{sell_ratio*100:.0f}%", "재매수간격": f"{gap*100:.1f}%",
        "최종자산($)": final_equity, "최종주식수": shares, "최종현금($)": cash,
        "실현 매도대금($)": sale_proceeds, "누적 매수금액($)": bought_back_spend,
        "평가손익+실현손익($)": final_equity-initial_equity,
        "수익률(%)": total_return, "MDD(%)": mdd,
        "등비재매수체결": len(buyback_done), "1차메이크업": "실행" if stage1_done else "미실행",
        "2차메이크업": "실행" if stage2_done else "미실행",
        "기준매도가($)": base_price if base_price else np.nan,
        "마지막종가($)": last_close,
        "거래내역": trades, "일별자산": daily_result,
        "기준일": daily.index[0], "종료일": daily.index[-1],
    }

if run:
    if start_date > end_date:
        st.error("시작일은 종료일보다 빠르거나 같아야 합니다.")
        st.stop()
    try:
        with st.spinner("과거 가격 데이터 수집 및 전체 조합을 계산하고 있습니다..."):
            prices, last_err = None, None
            for sym in resolve_symbols(ticker):
                try:
                    prices = download_prices(sym, start_date, end_date)
                    ticker = sym
                    break
                except ValueError as err:
                    last_err = err
            if prices is None:
                raise last_err
            signals = build_signals(prices)  # 주봉 신호는 한 번만 계산
            results = []
            details = {}
            for sr in sell_ratios:
                for gap in intervals:
                    r = run_one(prices, signals, start_date, end_date, initial_shares, avg_cost, sr, gap)
                    details[(sr, gap)] = r
                    results.append({k:v for k,v in r.items() if k not in ["거래내역", "일별자산"]})
            summary = pd.DataFrame(results)
            summary = summary.sort_values(["수익률(%)"], ascending=False).reset_index(drop=True)
        # 화면 조작(선택 상자 변경)으로 다시 그려져도 결과가 사라지지 않도록 저장
        st.session_state["res"] = dict(ticker=ticker, start=start_date, end=end_date,
                                       summary=summary, details=details, prices=prices,
                                       krw=is_korean(ticker))
    except Exception as e:
        st.session_state.pop("res", None)
        st.error(f"실행 오류: {e}")

if "res" in st.session_state:
    res = st.session_state["res"]
    ticker, start_date, end_date = res["ticker"], res["start"], res["end"]
    summary, details, prices = res["summary"], res["details"], res["prices"]
    krw = res.get("krw", False)
    def money(v):
        return f"{v:,.0f}원" if krw else f"${v:,.2f}"
    def relabel(df):
        return df.rename(columns=lambda c: c.replace("($)", "(원)")) if krw else df
    st.success(f"{ticker} · {start_date} ~ {end_date} · {len(summary)}개 시나리오 계산 완료")
    c1,c2,c3 = st.columns(3)
    best = summary.iloc[0]
    c1.metric("수익률 1위", f"{best['수익률(%)']:.2f}%", f"매도 {best['매도비율']} / 간격 {best['재매수간격']}")
    c2.metric("최고 최종자산", money(best['최종자산($)']))
    c3.metric("해당 전략 MDD", f"{best['MDD(%)']:.2f}%")
    st.subheader(f"{len(summary)}개 전략 성과 비교")
    st.dataframe(relabel(summary.drop(columns=["기준매도가($)", "마지막종가($)"])), use_container_width=True, hide_index=True)
    pivot = summary.pivot(index="매도비율", columns="재매수간격", values="수익률(%)")
    st.subheader("수익률 비교표(%)")
    st.dataframe(pivot, use_container_width=True)
    st.subheader("개별 전략 상세")
    selected_sell = st.selectbox("매도 비율", sell_ratios, format_func=lambda x:f"{x*100:.0f}%")
    selected_gap = st.selectbox("재매수 간격", intervals, format_func=lambda x:f"{x*100:.1f}%")
    detail = details[(selected_sell, selected_gap)]
    d1,d2,d3,d4 = st.columns(4)
    d1.metric("최종 자산", money(detail['최종자산($)']))
    d2.metric("최종 보유 수량", f"{detail['최종주식수']:,.4f}")
    d3.metric("현금", money(detail['최종현금($)']))
    d4.metric("MDD", f"{detail['MDD(%)']:.2f}%")
    st.dataframe(detail["거래내역"], use_container_width=True, hide_index=True)
    st.line_chart(detail["일별자산"]["Equity"])
    # 다운로드용 통합 엑셀
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        relabel(summary).to_excel(writer, sheet_name="전략비교", index=False)
        detail["거래내역"].to_excel(writer, sheet_name="선택전략_매매내역", index=False)
        detail["일별자산"].to_excel(writer, sheet_name="선택전략_일별자산")
        prices.loc[(prices.index.date >= start_date) & (prices.index.date <= end_date)].to_excel(writer, sheet_name="가격데이터")
    st.download_button(
        "결과 엑셀 다운로드",
        data=output.getvalue(),
        file_name=f"{ticker}_papas7_backtest_{start_date}_{end_date}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    st.download_button(
        "전략 비교 CSV 다운로드",
        data=relabel(summary).to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{ticker}_papas7_summary.csv",
        mime="text/csv"
    )
    with st.expander("백테스트 계산 가정 및 유의사항"):
        st.markdown("""
        - 초기 매도는 시작일부터 첫 5거래일에 매도 물량을 5등분해 각 거래일 종가로 매도합니다.
        - 5일 평균 매도가를 기준으로 1~5차 재매수 지정가를 `기준가 × (1-간격)^n`으로 계산합니다.
        - 재매수는 일중 저가가 지정가 이하이면 체결된 것으로 가정합니다. 시가가 지정가보다 낮으면 시가 체결로 근사합니다.
        - 5주선 메이크업은 주봉 몸통 하단(시가·종가 중 낮은 값)이 5주선보다 높을 때 신호가 발생합니다.
        - 2차 메이크업은 30주선이 전주보다 낮지 않고 주봉 몸통 하단이 300주선보다 높을 때 발생합니다.
        - 메이크업은 신호 다음 거래일부터 5거래일 동안 종가 LOC로 매수하는 것으로 근사합니다.
        - 1차 메이크업 예산은 최초 매도대금의 50%에서 기존 등비 재매수 금액을 뺀 금액입니다. 2차는 최초 매도대금에서 누적 매수금액을 뺀 잔액입니다.
        - 주봉 신호는 금요일 기준 주봉이 확정된 뒤 확인합니다. 미완성 주봉은 신호에 사용하지 않습니다.
        - 배당, 세금, 환전, 수수료, 슬리피지, 주문 최소단위 및 실제 LOC 체결 우선순위는 반영하지 않습니다.
        - Yahoo Finance 조정주가를 사용합니다. 데이터 제공처의 수정·누락 가능성이 있습니다.
        - 300주선 계산을 위해 시작일보다 약 8년 전부터 데이터를 가져옵니다.
        """)
else:
    st.subheader("사용 순서")
    st.markdown("""
    1. 티커와 백테스트 날짜를 입력합니다.
    2. 초기 보유 수량과 평단가를 입력합니다.
    3. **백테스트 실행**을 누릅니다.
    4. 전체 조합의 성과를 비교하고, 원하는 조합의 거래내역을 확인합니다.
    5. 결과 엑셀을 내려받아 보관합니다.

    처음 실행할 때는 인터넷 연결과 Yahoo Finance 데이터 접근이 필요합니다.
    """)
