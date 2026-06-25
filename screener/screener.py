"""
2트랙 모멘텀 스크리너 + 기술적 대시보드
==========================================
매일 GitHub Actions에서 실행. AI 호출 없음(순수 계산, 토큰 0).

데이터: FinanceDataReader(유니버스) + yfinance(가격).

트랙①  섹터ETF를 RS로 랭킹 → 강한 섹터 3개 → 각 섹터 RS 상위 대장주 2개
        (US: SPDR 섹터 ETF 11종 / KR: 섹터 태그별 RS 중앙값 집계)
트랙②  전체 유니버스 RS 랭킹 → Top 10 → 최상위 3개 '심층'

각 종목 지표: 20MA 눌림목, 이격도(20·60·200), RSI(14), 52주 고점대비,
              거래량비, 엘리어트 파동 '추정'(지그재그 알고리즘·참고용).

산출물: 텔레그램 푸시 + reports/dashboard.html + data.json + latest.md
설정은 CONFIG 한 곳에서.
"""

import os
import sys
import json
import statistics
import datetime as dt

import pandas as pd

# ----------------------------------------------------------------------
CONFIG = {
    "rs_weights": {"m3": 0.40, "m6": 0.40, "m12": 0.20},
    "ma_pullback": 20,
    "ma_trend": 200,
    "top_sectors": 3,            # 트랙① 강한 섹터 수
    "leaders_per_sector": 2,     # 트랙① 섹터별 대장주 수
    "individual_top": 10,        # 트랙② 개별 Top N
    "deep_top": 3,               # 트랙② 심층 분석 수
    "kr_sector_min": 3,          # KR 섹터 집계 최소 종목 수
    "proximity_pct": 1.0,
    "kr_top_n": 250,
    "min_price_usd": 5.0,
    "min_price_krw": 2000,
    "history": "16mo",
    "chart_bars": 130,
    "zigzag_pct": 7.0,
}

# 미국 SPDR 섹터 ETF → GICS 섹터(=S&P500 리스트의 Sector 라벨)
SECTOR_ETFS_US = {
    "XLK": "Information Technology", "XLF": "Financials", "XLV": "Health Care",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLE": "Energy",
    "XLI": "Industrials", "XLB": "Materials", "XLRE": "Real Estate",
    "XLU": "Utilities", "XLC": "Communication Services",
}

# 미국 테마 ETF(SPDR 큰 섹터보다 날카로움) → 구성종목 리스트로 대장주 선별
THEME_ETFS_US = {
    "SMH": {"label": "반도체(SMH)", "members": ["NVDA", "AVGO", "AMD", "QCOM", "TXN", "MU", "AMAT",
            "LRCX", "KLAC", "ADI", "INTC", "MRVL", "MCHP", "NXPI", "ON", "MPWR", "TER", "SWKS"]},
    "IGV": {"label": "소프트웨어(IGV)", "members": ["MSFT", "ORCL", "CRM", "ADBE", "NOW", "INTU", "PANW",
            "SNPS", "CDNS", "FTNT", "CRWD", "PLTR", "DDOG", "SNOW", "TEAM", "WDAY", "ADSK", "ANSS"]},
    "XBI": {"label": "바이오테크(XBI)", "members": ["VRTX", "REGN", "GILD", "AMGN", "BIIB", "MRNA", "INCY",
            "EXEL", "NBIX", "HALO", "ALNY", "BMRN", "SRPT", "UTHR", "NTRA"]},
    "ITA": {"label": "방산·우주(ITA)", "members": ["RTX", "BA", "LMT", "GD", "NOC", "GE", "LHX", "HWM",
            "TDG", "AXON", "HII", "TXT"]},
    "TAN": {"label": "태양광(TAN)", "members": ["FSLR", "ENPH", "SEDG", "RUN", "NXT", "ARRY", "SHLS", "CSIQ"]},
    "JETS": {"label": "항공(JETS)", "members": ["DAL", "UAL", "AAL", "LUV", "ALK", "SKYW", "ALGT"]},
    "KRE": {"label": "지역은행(KRE)", "members": ["TFC", "USB", "PNC", "MTB", "FITB", "KEY", "RF", "HBAN",
            "CFG", "ZION", "CMA"]},
    "XME": {"label": "금속·광산(XME)", "members": ["NEM", "FCX", "NUE", "STLD", "CLF", "X", "AA", "RS",
            "MP", "ATI", "CMC"]},
    "XOP": {"label": "석유 E&P(XOP)", "members": ["COP", "EOG", "DVN", "FANG", "OXY", "HES", "APA",
            "CTRA", "MRO", "MTDR", "OVV"]},
}

# 미국 원자재 ETF(주식 아님 → '대장주' 없음, ETF 자체를 RS·눌림목으로 추적)
COMMODITY_ETFS = {
    "GLD": "금", "SLV": "은", "CPER": "구리", "USO": "WTI원유", "UNG": "천연가스",
    "DBA": "농산물", "DBC": "원자재종합", "URA": "우라늄", "GDX": "금광업",
}

# 시장 건강도(레짐) 판단용 지수
INDEXES = {
    "US": [("^GSPC", "S&P500"), ("^IXIC", "나스닥")],
    "KR": [("^KS11", "코스피"), ("^KQ11", "코스닥")],
}

# 신규상장·성장주 워치리스트 (직접 추가/삭제). RS·200일선이 없어도 완화 지표로 추적.
# US: 야후 티커 그대로 / KR: 6자리 코드(.KS/.KQ 자동). ※ 아래는 예시 — 본인 관심종목으로 교체.
WATCHLIST = {
    "US": ["SPCX", "PLTR", "RDDT", "ARM", "APP", "HOOD", "SMCI", "ASTS", "CRWV"],
    "KR": [],
}

# 한국 KODEX 섹터 ETF(코드는 yfinance .KS) + 대표 구성종목(대장주 후보).
# KRX/pykrx 로그인 없이 동작하도록, ETF는 RS 랭킹용·구성종목은 직접 정의.
KR_SECTOR_ETFS = {
    "091160": {"label": "반도체", "members": ["005930", "000660", "042700", "240810", "357780", "058470", "095340"]},
    "305720": {"label": "2차전지", "members": ["373220", "006400", "003670", "247540", "086520", "066970", "020150"]},
    "244580": {"label": "바이오", "members": ["207940", "068270", "196170", "328130", "145020", "302440"]},
    "091170": {"label": "은행", "members": ["105560", "055550", "086790", "316140", "138040", "024110"]},
    "102970": {"label": "증권", "members": ["005940", "016360", "006800", "039490", "071050"]},
    "117680": {"label": "철강", "members": ["005490", "004020", "103140", "014820"]},
    "091180": {"label": "자동차", "members": ["005380", "000270", "012330", "011210", "204320"]},
    "117700": {"label": "건설", "members": ["000720", "028050", "047040", "006360", "375500"]},
    "140710": {"label": "운송", "members": ["086280", "011200", "003490", "000120"]},
}

REPORT_DIR = "reports"
TEMPLATE = os.path.join(os.path.dirname(__file__), "dashboard_template.html")


# ----------------------------------------------------------------------
# 유니버스
# ----------------------------------------------------------------------
def get_universe():
    import FinanceDataReader as fdr
    rows = []
    try:
        sp = fdr.StockListing("S&P500")
        sym_col = "Symbol" if "Symbol" in sp.columns else sp.columns[0]
        sec_col = "Sector" if "Sector" in sp.columns else None
        name_col = "Name" if "Name" in sp.columns else sym_col
        for _, r in sp.iterrows():
            sym = str(r[sym_col]).strip()
            if not sym or sym.lower() == "nan":
                continue
            rows.append({"market": "US", "code": sym, "yahoo": sym.replace(".", "-"),
                         "name": str(r.get(name_col, sym)),
                         "sector": str(r[sec_col]) if sec_col else "N/A"})
        print(f"[universe] US S&P500: {sum(1 for x in rows if x['market']=='US')}")
    except Exception as e:
        print(f"[universe] US 실패: {e}")

    kr_lookup = {}   # 전체 상장 KR: code -> {suffix, name} (구성종목 suffix 해석용)
    try:
        frames = []
        for mkt, suffix in (("KOSPI", ".KS"), ("KOSDAQ", ".KQ")):
            df = fdr.StockListing(mkt).copy()
            df["_suffix"] = suffix
            frames.append(df)
        kr = pd.concat(frames, ignore_index=True)
        code_col = next((c for c in ("Code", "Symbol") if c in kr.columns), kr.columns[0])
        name_col = "Name" if "Name" in kr.columns else code_col
        cap_col = next((c for c in ("Marcap", "MarketCap", "Amount") if c in kr.columns), None)
        for _, r in kr.iterrows():
            code = str(r[code_col]).strip().zfill(6)
            if code.isdigit():
                kr_lookup[code] = {"suffix": r["_suffix"], "name": str(r.get(name_col, code))}
        if cap_col:
            kr = kr.sort_values(cap_col, ascending=False)
        kr = kr.head(CONFIG["kr_top_n"])
        for _, r in kr.iterrows():
            code = str(r[code_col]).strip().zfill(6)
            if not code.isdigit():
                continue
            rows.append({"market": "KR", "code": code, "yahoo": f"{code}{r['_suffix']}",
                         "name": str(r.get(name_col, code)), "sector": "N/A"})
        print(f"[universe] KR top{CONFIG['kr_top_n']}: {sum(1 for x in rows if x['market']=='KR')}")
    except Exception as e:
        print(f"[universe] KR 실패: {e}")
    return rows, kr_lookup


# ----------------------------------------------------------------------
# 가격 / 지표
# ----------------------------------------------------------------------
def download_prices(symbols):
    import yfinance as yf
    print(f"[price] 다운로드 {len(symbols)} 심볼 ({CONFIG['history']})...")
    return yf.download(list(symbols), period=CONFIG["history"], interval="1d",
                       group_by="ticker", auto_adjust=False, threads=True, progress=False)


def ohlc_for(data, sym):
    try:
        df = data[sym] if isinstance(data.columns, pd.MultiIndex) else data
        df = df.dropna(subset=["Close"])
        return df if len(df) else None
    except Exception:
        return None


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, 1e-9))


def pct_return(close, n):
    if len(close) <= n or close.iloc[-(n + 1)] <= 0:
        return None
    return close.iloc[-1] / close.iloc[-(n + 1)] - 1.0


def rs_value(close):
    w = CONFIG["rs_weights"]
    m3, m6 = pct_return(close, 63), pct_return(close, 126)
    if m3 is None or m6 is None:
        return None, None, None, None
    m12 = pct_return(close, 252) or m6
    return w["m3"] * m3 + w["m6"] * m6 + w["m12"] * m12, m3, m6, m12


def zigzag(closes, pct):
    if len(closes) < 3:
        return []
    piv, thr = [], pct / 100.0
    ext_i, ext_p, direction = 0, closes[0], 0
    for i in range(1, len(closes)):
        p = closes[i]
        if direction >= 0 and p > ext_p:
            ext_i, ext_p = i, p
        elif direction <= 0 and p < ext_p:
            ext_i, ext_p = i, p
        if direction >= 0 and p <= ext_p * (1 - thr):
            piv.append((ext_i, ext_p, 'H')); direction, ext_i, ext_p = -1, i, p
        elif direction <= 0 and p >= ext_p * (1 + thr):
            piv.append((ext_i, ext_p, 'L')); direction, ext_i, ext_p = 1, i, p
    return piv


def elliott_estimate(closes, pct):
    piv = zigzag(closes, pct)
    if len(piv) < 2:
        return "판단 보류", "스윙이 부족해 추정 불가"
    last_low = max((k for k, p in enumerate(piv) if p[2] == 'L'), default=None)
    if last_low is None:
        return "판단 보류", "기준 저점 미확인"
    legs = len(piv) - 1 - last_low
    cur_up = closes[-1] > piv[-1][1]
    if legs <= 0:
        return "1파 형성 추정", "주요 저점에서 막 반등 시작(추정)"
    if legs >= 5:
        return "조정(A·B·C) 추정", "임펄스 5파 이후 조정 국면일 수 있음(추정)"
    names = {2: "2파 조정", 3: "3파 상승", 4: "4파 조정", 5: "5파 마무리"}
    label = names.get(legs + 1, f"{legs+1}파")
    return f"{label} 추정", f"저점 이후 {legs}개 스윙 · {'상승' if cur_up else '되돌림'} 진행(추정)"


def trend_state(last, v20, v60, v120, ma200_up, rsi_last, piv):
    """종목 추세 상태 (단계 분리):
      추세유지 → 주의 → 하락전환 → 하락추세
      - 하락전환: 직전 스윙 저점 이탈(저점 낮아짐). 아직 120선 위 — 전환이 '일어나는' 시점.
      - 하락추세: 120일선(수급선) 종가 이탈. 전환은 이미 끝, 하락이 '진행' 중(확정).
      - '20MA 닿고 반등'=눌림목(추세유지) / '닿고 직전 저점까지 깸'=하락전환."""
    if v20 is None and v60 is None:        # 상장 초기 — 이동평균 산출 전
        return "데이터 부족", ["상장 초기 · 이동평균 산출 전"]
    lows = [p[1] for p in piv if p[2] == 'L']
    highs = [p[1] for p in piv if p[2] == 'H']
    broke_low = bool(lows and last < lows[-1])           # 직전 스윙 저점 이탈(저점 낮아짐)
    lower_high = bool(len(highs) >= 2 and highs[-1] < highs[-2])
    below120 = v120 is not None and last < v120
    below60 = v60 is not None and last < v60
    below20 = v20 is not None and last < v20

    # 하락추세(확정): 120선 이탈 — 전환은 그 전에 이미 끝남
    if below120:
        reasons = ["120일선 이탈(하락 진행)"]
        if broke_low: reasons.append("저점 낮아짐")
        if below60: reasons.append("60일선 이탈")
        return "하락추세", reasons

    # 하락전환(신호): 직전 스윙 저점 이탈, 아직 120선 위
    if broke_low:
        reasons = ["직전 스윙 저점 이탈(저점 낮아짐)"]
        if below60: reasons.append("60일선 이탈")
        if lower_high: reasons.append("고점도 낮아짐")
        return "하락전환", reasons

    # 주의: 저점은 지켰지만 약화 신호
    reasons = []
    if below60: reasons.append("60일선 이탈")
    if lower_high: reasons.append("고점 낮아짐")
    if not ma200_up: reasons.append("200일선 하락")
    if rsi_last is not None and rsi_last < 45: reasons.append(f"RSI 약세({rsi_last:.0f})")
    if below20 and not below60: reasons.append("20일선 종가 이탈(단기 약화)")

    if reasons:
        return "주의", reasons
    return "추세유지", ["120·60·20일선 위 · 스윙 저점 유지"]


def wyckoff_estimate(df, piv=None):
    """가격+거래량 기반 와이코프 국면 '추정'(참고용):
       매집(Accumulation)/상승(Markup)/분산(Distribution)/하락(Markdown) + 이벤트."""
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    if len(c) < 40:
        return "판단 보류", "데이터 부족"
    n = len(c)
    last = float(c.iloc[-1])
    look = min(60, n)
    hi = float(h.tail(look).max())
    lo = float(l.tail(look).min())
    pos = (last - lo) / (hi - lo) if hi > lo else 0.5     # 레인지 내 위치 0~1
    width = (hi - lo) / lo if lo > 0 else 1.0
    rangebound = width < 0.18                             # 약 18% 이내 박스권
    ma_long = c.rolling(min(120, n)).mean()
    above = last > float(ma_long.iloc[-1])
    slope_up = float(ma_long.iloc[-1]) > float(ma_long.iloc[-min(21, n - 1)])
    ref = float(c.iloc[max(0, n - 1 - look * 2)])         # 약 6개월 전(레인지 이전)
    came_down = ref > hi * 0.98
    came_up = ref < lo * 1.02

    events = []
    vol = df.get("Volume")
    if vol is not None and len(vol) >= 60 and float(vol.tail(60).mean()) > 0:
        base_v = float(vol.tail(60).mean())
        if float(vol.tail(10).mean()) < base_v * 0.8:
            events.append("거래량 마름(매집 성숙)")
        if float(vol.tail(5).max()) > base_v * 2.5:
            events.append("거래량 클라이맥스")
    prior_lo = float(l.iloc[max(0, n - look):max(1, n - 10)].min())
    prior_hi = float(h.iloc[max(0, n - look):max(1, n - 10)].max())
    if float(l.tail(10).min()) < prior_lo and last > prior_lo:
        events.append("스프링(하단 가짜 이탈 후 복귀)")
    if float(h.tail(10).max()) > prior_hi and last < prior_hi:
        events.append("업스러스트(상단 가짜 돌파 후 실패)")

    if rangebound and (came_down or pos < 0.5):
        phase = "매집 추정 (Accumulation)"
    elif rangebound and (came_up or pos >= 0.5):
        phase = "분산 추정 (Distribution)"
    elif above and slope_up and pos > 0.5:
        phase = "상승 추세 (Markup)"
    elif (not above) and (not slope_up):
        phase = "하락 추세 (Markdown)"
    else:
        phase = "상승 추세 (Markup)" if above else "하락 추세 (Markdown)"
    return phase, " · ".join(events) if events else "주요 이벤트 미감지"


def metrics(meta, df, relaxed=False):
    """종목 1개의 지표 dict(또는 None).
    relaxed=True: 신규상장·짧은 데이터용 — 최소 봉수 완화 + RS는 가용 구간 수익률로 대용."""
    min_bars = 6 if relaxed else 60
    if df is None or len(df) < min_bars:
        return None
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])
    floor = CONFIG["min_price_krw"] if meta["market"] == "KR" else CONFIG["min_price_usd"]
    if last < floor:
        return None
    rs, m3, m6, m12 = rs_value(close)
    if rs is None:
        if not relaxed:
            return None
        n = len(close) - 1
        base = float(close.iloc[0])
        since = (last / base - 1) if base > 0 else 0.0   # 상장 후(가용 구간) 수익률
        m3 = pct_return(close, min(63, n)) or since
        m6 = pct_return(close, min(126, n)) or since
        m12 = pct_return(close, min(252, n)) or m6
        rs = m3  # RS 대용

    ma20 = close.rolling(CONFIG["ma_pullback"]).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(min(120, len(close))).mean()
    ma200 = close.rolling(min(CONFIG["ma_trend"], len(close))).mean()
    v20, v60, v120, v200 = ma20.iloc[-1], ma60.iloc[-1], ma120.iloc[-1], ma200.iloc[-1]
    hi, lo = float(df["High"].iloc[-1]), float(df["Low"].iloc[-1])
    touched = bool(pd.notna(v20) and lo <= v20 <= hi)
    dist20 = (last - v20) / v20 * 100 if pd.notna(v20) else None
    high52 = float(close.tail(252).max())
    low52 = float(close.tail(252).min())
    vol = df.get("Volume")
    vr = None
    if vol is not None and len(vol) >= 20 and vol.tail(20).mean() > 0:
        vr = float(vol.iloc[-1] / vol.tail(20).mean())
    closes_tail = close.tolist()[-260:]
    piv = zigzag(closes_tail, CONFIG["zigzag_pct"])
    ell, note = elliott_estimate(closes_tail, CONFIG["zigzag_pct"])
    wyck, wyck_note = wyckoff_estimate(df, piv)
    ma200_up = bool(pd.notna(v200) and len(ma200.dropna()) > 21 and v200 > ma200.iloc[-21])
    rsi_last = float(rsi(close).iloc[-1])
    tstate, treasons = trend_state(
        last, float(v20) if pd.notna(v20) else None, float(v60) if pd.notna(v60) else None,
        float(v120) if pd.notna(v120) else None, ma200_up, rsi_last, piv)

    cb = CONFIG["chart_bars"]
    tail = df.tail(cb)
    bars = [{"d": str(idx.date()) if hasattr(idx, "date") else str(idx),
             "o": round(float(r["Open"]), 4), "h": round(float(r["High"]), 4),
             "l": round(float(r["Low"]), 4), "c": round(float(r["Close"]), 4)}
            for idx, r in tail.iterrows()]
    arr = lambda s: [None if pd.isna(x) else round(float(x), 4) for x in s.tail(cb)]

    return {
        "id": f"{meta['market']}:{meta['code']}",
        "market": meta["market"], "code": meta["code"], "name": meta["name"], "sector": meta["sector"],
        "close": round(last, 2), "rs": round(rs * 100, 1), "_rs_raw": rs,
        "ma20": round(float(v20), 2) if pd.notna(v20) else None,
        "ma60": round(float(v60), 2) if pd.notna(v60) else None,
        "ma120": round(float(v120), 2) if pd.notna(v120) else None,
        "ma200": round(float(v200), 2) if pd.notna(v200) else None,
        "touched": touched, "dist20": round(dist20, 2) if dist20 is not None else None,
        "near": bool(dist20 is not None and abs(dist20) <= CONFIG["proximity_pct"] and not touched),
        "disp20": round(last / float(v20) * 100, 1) if pd.notna(v20) else None,
        "disp60": round(last / float(v60) * 100, 1) if pd.notna(v60) else None,
        "disp200": round(last / float(v200) * 100, 1) if pd.notna(v200) else None,
        "rsi": round(rsi_last, 1),
        "trend_state": tstate, "trend_reasons": treasons, "ma200_up": ma200_up,
        "high52_pct": round((last / high52 - 1) * 100, 1) if high52 > 0 else None,
        "low52_pct": round((last / low52 - 1) * 100, 1) if low52 > 0 else None,
        "days": len(df), "is_new": bool(len(df) < 252),
        "vol_ratio": round(vr, 2) if vr else None,
        "elliott": ell, "elliott_note": note,
        "wyckoff": wyck, "wyckoff_note": wyck_note,
        "ret3m": round(m3 * 100, 1), "ret6m": round(m6 * 100, 1),
        "trend_ok": bool(pd.notna(v200) and last > float(v200)),
        "bars": bars, "ma20s": arr(ma20), "ma60s": arr(ma60),
        "ma120s": arr(ma120), "ma200s": arr(ma200),
    }


# ----------------------------------------------------------------------
# 2트랙 구성
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 시장 건강도(레짐)
# ----------------------------------------------------------------------
def index_health(name, ticker, df):
    if df is None or len(df) < 60:
        return None
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(min(200, len(close))).mean()
    v50, v200 = ma50.iloc[-1], ma200.iloc[-1]
    slope_up = bool(pd.notna(v200) and len(ma200.dropna()) > 21 and v200 > ma200.iloc[-21])
    wyck, wyck_note = wyckoff_estimate(df)
    return {
        "name": name, "ticker": ticker, "close": round(last, 2),
        "wyckoff": wyck, "wyckoff_note": wyck_note,
        "ma50": round(float(v50), 2) if pd.notna(v50) else None,
        "ma200": round(float(v200), 2) if pd.notna(v200) else None,
        "above200": bool(pd.notna(v200) and last > v200),
        "golden": bool(pd.notna(v50) and pd.notna(v200) and v50 > v200),
        "dist200": round((last / float(v200) - 1) * 100, 1) if pd.notna(v200) else None,
        "slope_up": slope_up,
        "rsi": round(float(rsi(close).iloc[-1]), 1),
    }


def build_regime(allrecs, idx_map):
    """StockEasy 방법론 재현: 이평선 이탈비율 + 52주 신고가/신저가 순증 + 지수추세 → 국면."""
    out = {}
    for mkt in ("US", "KR"):
        idxs = [idx_map[t] for t, _ in INDEXES[mkt] if idx_map.get(t)]
        pool = [r for r in allrecs if r["market"] == mkt and r.get("ma200") and r.get("ma20")]
        n = len(pool)
        pct = lambda c: round(sum(1 for r in pool if c(r)) / n * 100, 1) if n else None

        below20 = pct(lambda r: r["close"] < r["ma20"])      # 20일선 이탈비율(↑=약세)
        below200 = pct(lambda r: r["close"] < r["ma200"])    # 200일선 이탈비율(↑=약세)
        above200 = round(100 - below200, 1) if below200 is not None else None
        new_high = pct(lambda r: r.get("high52_pct") is not None and r["high52_pct"] >= -0.5)  # 52주 신고가
        near_high = pct(lambda r: r.get("high52_pct") is not None and r["high52_pct"] >= -3)   # 신고가 근접
        new_low = pct(lambda r: r.get("low52_pct") is not None and r["low52_pct"] <= 1.0)      # 52주 신저가
        net_nh = round((new_high or 0) - (new_low or 0), 1) if n else None  # 신고가-신저가 순증

        primary = idxs[0] if idxs else None
        idx_up = bool(primary and primary["above200"])
        reasons = []
        if primary:
            reasons.append(f"{primary['name']} {'200일선 위' if primary['above200'] else '200일선 아래'}"
                           f"({primary['dist200']:+.1f}%, {'정배열' if primary['golden'] else '역배열'})")
        if below200 is not None:
            reasons.append(f"200일선 이탈 {below200:.0f}% · 20일선 이탈 {below20:.0f}% (낮을수록 강세)")
            reasons.append(f"52주 신고가 {new_high:.0f}% · 신저가 {new_low:.0f}% (순증 {net_nh:+.0f})")

        # 국면 판정 (StockEasy 라벨 체계)
        weak200 = below200 if below200 is not None else 50
        weak20 = below20 if below20 is not None else 50
        nn = net_nh if net_nh is not None else 0
        if idx_up and weak200 < 40 and nn >= 0:
            label, color = "추세유지", "green"
            premise = "상승 추세 견조 — 다수 종목이 200일선 위, 신고가 우위. 눌림목 매수 우호."
        elif (not idx_up) or weak200 > 60:
            label, color = "조정 국면", "red"
            premise = "다수 종목이 200일선 이탈 — 눌림목 매수 저확률, 현금·방어 비중 우선."
        elif idx_up and (weak20 > 60 or nn < 0):
            label, color = "상승 둔화", "yellow"
            premise = "지수는 위지만 신고가 줄고 이탈 증가 — 주도 섹터·대장주만 선별 대응."
        else:
            label, color = "관망 후 대응", "yellow"
            premise = "방향 불명확 — 신호 확인 후 대응, 신규 진입 신중."

        # 레짐 → 권장 스탠스(현금/투자 비중) — 참고용 휴리스틱
        EXPOSURE = {
            "추세유지": {"stance": "적극 투자", "equity": "80~100%", "cash": "0~20%"},
            "상승 둔화": {"stance": "중립·선별", "equity": "50~70%", "cash": "30~50%"},
            "관망 후 대응": {"stance": "방어적 관망", "equity": "30~50%", "cash": "50~70%"},
            "조정 국면": {"stance": "현금 우선", "equity": "0~30%", "cash": "70~100%"},
        }
        out[mkt] = {"label": label, "color": color, "premise": premise, "reasons": reasons,
                    "exposure": EXPOSURE.get(label),
                    "breadth": {"below20": below20, "below200": below200, "above200": above200,
                                "new_high": new_high, "near_high": near_high, "new_low": new_low,
                                "net_new_high": net_nh, "n": n},
                    "indexes": idxs}
    return out


# ----------------------------------------------------------------------
# 계절성(시클리컬)
# ----------------------------------------------------------------------
def build_seasonality(monthly, month):
    """과거 월간 데이터로 '이번 달' 섹터별 평균 수익률·승률(계절성) 계산."""
    pools = {
        "US": list(SECTOR_ETFS_US.items()),
        "KR": [(f"{c}.KS", info["label"]) for c, info in KR_SECTOR_ETFS.items()],
    }
    idx = {"US": ("^GSPC", "S&P500"), "KR": ("^KS11", "코스피")}
    out = {}
    for mkt in ("US", "KR"):
        secs = []
        for etf, label in pools[mkt]:
            df = ohlc_for(monthly, etf)
            if df is None:
                continue
            r = df["Close"].astype(float).pct_change().dropna()
            mr = r[r.index.month == month]
            if len(mr) < 3:
                continue
            secs.append({"etf": etf.replace(".KS", ""), "label": label,
                         "avg": round(float(mr.mean()) * 100, 2),
                         "hit": int(round(float((mr > 0).mean()) * 100)), "n": int(len(mr))})
        secs.sort(key=lambda x: x["avg"], reverse=True)
        it, inm = idx[mkt]
        idf = ohlc_for(monthly, it)
        iavg = ihit = None
        if idf is not None:
            ir = idf["Close"].astype(float).pct_change().dropna()
            im = ir[ir.index.month == month]
            if len(im) >= 3:
                iavg = round(float(im.mean()) * 100, 2)
                ihit = int(round(float((im > 0).mean()) * 100))
        window = "강세 구간 (11~4월)" if month in (11, 12, 1, 2, 3, 4) else "약세 구간 (5~10월 · Sell in May)"
        out[mkt] = {"month": month, "sectors": secs, "strong": secs[:3], "weak": secs[-3:][::-1],
                    "index": {"name": inm, "avg": iavg, "hit": ihit}, "window": window}
    return out


# ----------------------------------------------------------------------
# 신규상장 · 성장주
# ----------------------------------------------------------------------
def fundamentals(sym):
    """yfinance에서 매출·이익 성장률(분수). 실패 시 (None, None)."""
    try:
        import yfinance as yf
        info = yf.Ticker(sym).get_info()
        rg = info.get("revenueGrowth")
        eg = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
        return (float(rg) if rg is not None else None,
                float(eg) if eg is not None else None)
    except Exception:
        return (None, None)


def growth_score(rec, rev_g, earn_g):
    """가격 모멘텀 + 신고가 + 거래량 + (선택)펀더멘털 → 0~100 성장 점수."""
    s = 50.0
    if rec.get("ret3m") is not None:
        s += max(-20, min(30, rec["ret3m"] * 0.4))          # 3개월 모멘텀
    if rec.get("high52_pct") is not None and rec["high52_pct"] >= -3:
        s += 8                                              # 신고가 근접/갱신
    if rec.get("vol_ratio") and rec["vol_ratio"] >= 1.5:
        s += 6                                              # 거래량 급증
    if rev_g is not None:
        s += max(-10, min(20, rev_g * 100 * 0.4))           # 매출 성장률
    if earn_g is not None:
        s += max(-6, min(12, earn_g * 100 * 0.15))          # 이익 성장률
    if rec.get("trend_state") in ("하락전환", "하락추세"):
        s -= 12
    return round(max(0, min(100, s)), 1)


def build_tracks(allrecs, units_by_market):
    """units_by_market = {'US':[unit...], 'KR':[unit...]}; unit={etf,label,kind,rs,members?}.
       kind 'sector' = GICS 섹터태그로 대장주 매칭(미국 SPDR), 'theme' = 구성종목 리스트."""
    markets = {}
    for mkt in ("US", "KR"):
        recs = [r for r in allrecs if r["market"] == mkt and r["trend_ok"]]
        recs.sort(key=lambda r: r["_rs_raw"], reverse=True)

        top = recs[: CONFIG["individual_top"]]
        for i, r in enumerate(top, 1):
            r["rs_rank"] = i
        deep_ids = [r["id"] for r in top[: CONFIG["deep_top"]]]

        sectors = []
        ranked = sorted([u for u in units_by_market.get(mkt, []) if u.get("rs") is not None],
                        key=lambda u: u["rs"], reverse=True)
        for u in ranked[: CONFIG["top_sectors"]]:
            if u["kind"] == "sector":
                leaders = [r for r in recs if r["sector"] == u["label"]]
            else:  # 구성종목 리스트(미국 테마 / 한국 KODEX 섹터)
                ms = set(u.get("members", []))
                leaders = [r for r in recs if r["code"] in ms]
            leaders = leaders[: CONFIG["leaders_per_sector"]]
            sectors.append({"sector": u["label"], "etf": u["etf"], "kind": u["kind"],
                            "etf_rs": round(u["rs"] * 100, 1),
                            "leader_ids": [r["id"] for r in leaders]})

        markets[mkt] = {"sectors": sectors, "top_ids": [r["id"] for r in top], "deep_ids": deep_ids}
    return markets


def collect_selected(markets, allrecs):
    ids = set()
    for mkt in markets.values():
        ids.update(mkt["top_ids"])
        for s in mkt["sectors"]:
            ids.update(s["leader_ids"])
    index = {r["id"]: r for r in allrecs}
    stocks = {}
    for i in ids:
        r = dict(index[i])
        r.pop("_rs_raw", None)
        r.setdefault("rs_rank", 0)
        stocks[i] = r
    return stocks


# ----------------------------------------------------------------------
# 산출물
# ----------------------------------------------------------------------
def latest_bar_date(data):
    try:
        return pd.to_datetime(data.index[-1]).date().isoformat()
    except Exception:
        return dt.date.today().isoformat()


def build_message(markets, stocks, bar_date, commodity_ids=(), regime=None, growth_ids=(), seasonality=None):
    regime = regime or {}
    seasonality = seasonality or {}
    flag = lambda m: "🟢" if m == "US" else "🔵"
    light = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    lines = [f"📈 <b>2트랙 모멘텀 스크리너</b> · 기준일 {bar_date}"]
    for mkt in ("US", "KR"):
        mk = markets.get(mkt, {})
        if not mk.get("sectors") and not mk.get("top_ids"):
            continue
        rg = regime.get(mkt)
        rg_txt = f" — {light.get(rg['color'],'')} <b>{rg['label']}</b>" if rg else ""
        lines.append(f"\n{flag(mkt)} <b>{'미국' if mkt=='US' else '한국'}</b>{rg_txt}")
        if rg:
            lines.append(f"  대전제: {rg['premise']}")
            ex = rg.get("exposure")
            if ex:
                lines.append(f"  💰 권장: {ex['stance']} (주식 {ex['equity']} / 현금 {ex['cash']})")
            idxs = rg.get("indexes") or []
            if idxs and idxs[0].get("wyckoff"):
                lines.append(f"  🔍 와이코프(지수): {idxs[0]['name']} {idxs[0]['wyckoff']}")
        se = seasonality.get(mkt)
        if se and se.get("strong"):
            lines.append(f"  📅 {se['month']}월 계절 강세: " + ", ".join(s["label"] for s in se["strong"]))
        secs = mk.get("sectors", [])
        if secs:
            lines.append("· 💪 강한 섹터(RS): " + ", ".join(f"{s['sector']} {s['etf_rs']:.0f}" for s in secs))
            for s in secs:
                names = []
                for i in s["leader_ids"]:
                    r = stocks[i]
                    st = r.get("trend_state")
                    warn = "🔴" if st == "하락추세" else ("🟠" if st == "하락전환" else "")
                    names.append(f"{r['name']}{'🟢눌림' if r['touched'] else ''}{warn}")
                if names:
                    lines.append(f"   - {s['sector']}: " + ", ".join(names))
        touched = [stocks[i] for i in mk.get("top_ids", []) if stocks[i]["touched"]]
        if touched:
            lines.append("· 트랙② Top10 중 눌림목: " + ", ".join(f"{r['name']}(RS#{r['rs_rank']})" for r in touched))
        deep = [stocks[i] for i in mk.get("deep_ids", [])]
        if deep:
            lines.append("· 심층 Top3: " + ", ".join(
                f"{r['name']}({r.get('wyckoff','').split('(')[0].strip()}·{r['elliott']})" for r in deep))
        watch_ids = set(mk.get("top_ids", [])) | {i for s in mk.get("sectors", []) for i in s["leader_ids"]}
        flip = [stocks[i]["name"] for i in watch_ids if stocks[i].get("trend_state") == "하락전환"]
        down = [stocks[i]["name"] for i in watch_ids if stocks[i].get("trend_state") == "하락추세"]
        if flip:
            lines.append("🟠 하락전환(저점 이탈): " + ", ".join(flip))
        if down:
            lines.append("🔴 하락추세(120선 이탈): " + ", ".join(down))
    if commodity_ids:
        lines.append("\n🟡 <b>원자재(RS 상위)</b>: " +
                     ", ".join(f"{stocks[i]['name'].split('(')[0]}{'🟢터치' if stocks[i]['touched'] else ''}"
                               for i in commodity_ids[:5]))
    if growth_ids:
        lines.append("\n🚀 <b>성장주/신규 워치</b>: " +
                     ", ".join(f"{stocks[i]['name']}(점수{stocks[i].get('growth_score','-')}"
                               f"{'·신규' if stocks[i].get('is_new') else ''})" for i in growth_ids[:6]))
    lines.append("\n<i>대시보드(차트·이격도·엘리어트): reports/dashboard.html · 무료 지연 종가</i>")
    return "\n".join(lines)


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram] 토큰/챗ID 없음 — 전송 건너뜀")
        return
    import requests
    for i in range(0, len(text), 3800):
        rsp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            data={"chat_id": chat, "text": text[i:i+3800], "parse_mode": "HTML",
                                  "disable_web_page_preview": True}, timeout=30)
        print(f"[telegram] {rsp.status_code} {rsp.text[:100]}")


def write_outputs(payload):
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "data.json"), "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    try:
        with open(TEMPLATE, encoding="utf-8") as f:
            tpl = f.read()
        html = tpl.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
        with open(os.path.join(REPORT_DIR, "dashboard.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print("[report] reports/dashboard.html 저장")
    except Exception as e:
        print(f"[report] 대시보드 생성 실패: {e}")

    md = [f"# 2트랙 모멘텀 리포트 — {payload['bar_date']}\n"]
    for mkt in ("US", "KR"):
        mk = payload["markets"].get(mkt, {})
        md.append(f"## {'🟢 미국' if mkt=='US' else '🔵 한국'}\n")
        md.append("### 트랙① 강한 섹터 → 대장주")
        for s in mk.get("sectors", []):
            names = ", ".join(f"{payload['stocks'][i]['name']}({payload['stocks'][i]['code']})"
                              + ("🟢터치" if payload['stocks'][i]['touched'] else "") for i in s["leader_ids"])
            md.append(f"- **{s['sector']}** [{s['etf']} RS {s['etf_rs']}]: {names or '해당 없음'}")
        md.append("\n### 트랙② 개별 RS Top10")
        md.append("| RS# | 종목 | 코드 | 섹터 | 종가 | 20이격% | RSI | 눌림목 | 엘리어트 | 심층 |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for i in mk.get("top_ids", []):
            r = payload["stocks"][i]
            md.append(f"| {r['rs_rank']} | {r['name']} | {r['code']} | {r['sector']} | {r['close']:,} | "
                      f"{r['dist20']:+.1f} | {r['rsi']} | {'터치' if r['touched'] else ('근접' if r['near'] else '–')} | "
                      f"{r['elliott']} | {'★' if i in mk.get('deep_ids',[]) else ''} |")
        md.append("")
    text = "\n".join(md)
    with open(os.path.join(REPORT_DIR, f"momentum-2track-{payload['bar_date']}.md"), "w") as f:
        f.write(text)
    with open(os.path.join(REPORT_DIR, "latest.md"), "w") as f:
        f.write(text)
    print("[report] markdown 저장")


def main():
    rows, kr_lookup = get_universe()
    if not rows:
        send_telegram("⚠️ 스크리너: 유니버스 수집 실패.")
        sys.exit(0)
    # 미국 테마 ETF 구성종목을 유니버스에 편입(S&P500에 없으면 추가)
    us_codes = {r["code"] for r in rows if r["market"] == "US"}
    for info in THEME_ETFS_US.values():
        for sym in info["members"]:
            if sym not in us_codes:
                us_codes.add(sym)
                rows.append({"market": "US", "code": sym, "yahoo": sym.replace(".", "-"),
                             "name": sym, "sector": info["label"]})
    # 한국 KODEX 섹터 ETF 구성종목 편입(suffix는 전체 상장목록에서 해석)
    kr_codes = {r["code"] for r in rows if r["market"] == "KR"}
    for info in KR_SECTOR_ETFS.values():
        for code in info["members"]:
            if code not in kr_codes:
                kr_codes.add(code)
                lk = kr_lookup.get(code, {"suffix": ".KS", "name": code})
                rows.append({"market": "KR", "code": code, "yahoo": f"{code}{lk['suffix']}",
                             "name": lk["name"], "sector": info["label"]})

    # 신규상장·성장주 워치리스트 (market, code, yahoo, name)
    watch = []
    for code in WATCHLIST.get("US", []):
        watch.append(("US", code, code.replace(".", "-"), code))
    for code in WATCHLIST.get("KR", []):
        lk = kr_lookup.get(code, {"suffix": ".KS", "name": code})
        watch.append(("KR", code, f"{code}{lk['suffix']}", lk["name"]))

    index_tickers = {t for lst in INDEXES.values() for t, _ in lst}
    kr_etf_yahoos = {f"{c}.KS" for c in KR_SECTOR_ETFS}
    symbols = ({r["yahoo"] for r in rows} | set(SECTOR_ETFS_US.keys())
               | set(THEME_ETFS_US.keys()) | set(COMMODITY_ETFS.keys())
               | index_tickers | kr_etf_yahoos | {y for _, _, y, _ in watch})
    data = download_prices(symbols)
    bar_date = latest_bar_date(data)

    allrecs = []
    for r in rows:
        rec = metrics(r, ohlc_for(data, r["yahoo"]))
        if rec:
            allrecs.append(rec)

    def etf_rs(sym):
        df = ohlc_for(data, sym)
        if df is None:
            return None
        v, *_ = rs_value(df["Close"].astype(float))
        return v

    # 섹터/테마 유닛(시장별 한 풀에서 RS 랭킹)
    us_units = [{"etf": etf, "label": sec, "kind": "sector", "rs": etf_rs(etf)}
                for etf, sec in SECTOR_ETFS_US.items()]
    us_units += [{"etf": etf, "label": info["label"], "kind": "theme",
                  "members": info["members"], "rs": etf_rs(etf)}
                 for etf, info in THEME_ETFS_US.items()]
    kr_units = [{"etf": code, "label": info["label"], "kind": "theme",
                 "members": info["members"], "rs": etf_rs(f"{code}.KS")}
                for code, info in KR_SECTOR_ETFS.items()]
    units_by_market = {"US": us_units, "KR": kr_units}

    # 원자재 ETF(주식 아님 → ETF 자체를 rec로)
    commodities = []
    for tk, label in COMMODITY_ETFS.items():
        rec = metrics({"market": "CMD", "code": tk, "name": f"{label}({tk})", "sector": "원자재"},
                      ohlc_for(data, tk))
        if rec:
            commodities.append(rec)
    commodities.sort(key=lambda r: r["_rs_raw"], reverse=True)

    # 성장주/신규상장 워치(완화 지표 + 펀더멘털)
    growth = []
    for mkt, code, yahoo, name in watch:
        rec = metrics({"market": mkt, "code": code, "name": name, "sector": "성장주/신규"},
                      ohlc_for(data, yahoo), relaxed=True)
        if not rec:
            continue
        rev_g, earn_g = fundamentals(yahoo)
        rec["rev_growth"] = round(rev_g * 100, 1) if rev_g is not None else None
        rec["earn_growth"] = round(earn_g * 100, 1) if earn_g is not None else None
        rec["growth_score"] = growth_score(rec, rev_g, earn_g)
        rec.pop("_rs_raw", None)
        rec["rs_rank"] = 0
        growth.append(rec)
    growth.sort(key=lambda r: r["growth_score"], reverse=True)
    print(f"[result] 종목 {len(allrecs)} · US유닛 {sum(1 for u in us_units if u['rs'] is not None)} "
          f"· KR유닛 {sum(1 for u in kr_units if u['rs'] is not None)} · 원자재 {len(commodities)} · 성장주 {len(growth)}")

    idx_map = {}
    for mkt, lst in INDEXES.items():
        for tk, nm in lst:
            h = index_health(nm, tk, ohlc_for(data, tk))
            if h:
                idx_map[tk] = h
    regime = build_regime(allrecs, idx_map)
    print(f"[regime] US={regime['US']['label']} KR={regime['KR']['label']}")

    # 계절성: 별도 월간(장기) 데이터로 '이번 달' 섹터 평균수익률
    try:
        import yfinance as yf
        season_syms = (set(SECTOR_ETFS_US) | {f"{c}.KS" for c in KR_SECTOR_ETFS} | {"^GSPC", "^KS11"})
        monthly = yf.download(list(season_syms), period="15y", interval="1mo",
                              group_by="ticker", auto_adjust=False, threads=True, progress=False)
        seasonality = build_seasonality(monthly, int(bar_date[5:7]))
        print(f"[season] {bar_date[5:7]}월 · US섹터 {len(seasonality['US']['sectors'])} KR섹터 {len(seasonality['KR']['sectors'])}")
    except Exception as e:
        print(f"[season] 실패: {e}")
        seasonality = {}

    markets = build_tracks(allrecs, units_by_market)
    stocks = collect_selected(markets, allrecs)
    for rec in commodities:  # 원자재를 stocks에 추가(상세 차트용)
        c = dict(rec); c.pop("_rs_raw", None); c.setdefault("rs_rank", 0)
        stocks[c["id"]] = c
    for rec in growth:       # 성장주/신규를 stocks에 추가
        stocks[rec["id"]] = rec
    payload = {
        "bar_date": bar_date,
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "config": {k: CONFIG[k] for k in ("ma_pullback", "ma_trend", "top_sectors",
                                          "leaders_per_sector", "individual_top", "deep_top",
                                          "proximity_pct", "rs_weights", "zigzag_pct")},
        "regime": regime, "seasonality": seasonality,
        "markets": markets, "stocks": stocks,
        "commodities": [c["id"] for c in commodities],
        "growth": [g["id"] for g in growth],
        "counts": {"stocks": len(stocks),
                   "touched": sum(1 for r in stocks.values() if r["touched"]),
                   "near": sum(1 for r in stocks.values() if r["near"])},
    }
    write_outputs(payload)
    send_telegram(build_message(markets, stocks, bar_date, payload["commodities"], regime,
                                payload["growth"], seasonality))
    print("[done]")


if __name__ == "__main__":
    main()
