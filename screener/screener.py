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
import re
import json
import statistics
import datetime as dt

import pandas as pd

# ----------------------------------------------------------------------
CONFIG = {
    "rs_weights": {"m1": 0.30, "m3": 0.40, "m6": 0.30},   # 단기중기(1·3·6개월), 지수 대비 RS
    "ma_pullback": 20,
    "ma_trend": 240,            # 장기 추세선(240일=년선)
    "top_sectors": 3,            # 트랙① 강한 섹터 수
    "leaders_per_sector": 2,     # 트랙① 섹터별 대장주 수
    "individual_top": 10,        # 트랙② 개별 Top N
    "deep_top": 3,               # 트랙② 심층 분석 수
    "hot_top_n": 8,              # 핫한 종목(거래량 급증+단기 모멘텀) 상위 N
    "kr_sector_min": 3,          # KR 섹터 집계 최소 종목 수
    "proximity_pct": 2.0,
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
    "CIBR": {"label": "사이버보안(CIBR)", "members": ["CRWD", "PANW", "FTNT", "ZS", "NET", "OKTA",
            "CYBR", "S", "GEN", "QLYS", "RPD", "TENB"]},
    "ARKK": {"label": "혁신성장(ARKK)", "members": ["COIN", "ROKU", "RBLX", "HOOD", "PLTR", "DKNG",
            "RKLB", "U", "TWLO", "PATH", "TTD", "SHOP"]},
    "PAVE": {"label": "인프라(PAVE)", "members": ["PWR", "ETN", "URI", "VMC", "MLM", "FAST", "PH",
            "AME", "EMR", "NUE"]},
    "IYT": {"label": "운송(IYT)", "members": ["UPS", "FDX", "UBER", "CSX", "NSC", "ODFL", "DAL",
            "UAL", "JBHT", "EXPD"]},
}

# 나스닥100 핵심 구성종목(유니버스 확장용). S&P500과 겹치면 자동 중복 제거.
NASDAQ100 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "AVGO", "TSLA", "COST",
    "NFLX", "AMD", "PEP", "ADBE", "LIN", "CSCO", "TMUS", "INTU", "QCOM", "AMAT",
    "TXN", "ISRG", "AMGN", "HON", "BKNG", "VRTX", "ADP", "REGN", "MU", "PANW",
    "LRCX", "ADI", "KLAC", "SBUX", "MELI", "GILD", "SNPS", "CDNS", "PYPL", "MAR",
    "CRWD", "CSX", "ORLY", "MRVL", "ASML", "ABNB", "FTNT", "DASH", "ADSK", "PCAR",
    "ROP", "MNST", "CPRT", "WDAY", "NXPI", "TTD", "PAYX", "KDP", "ROST", "AEP",
    "FAST", "ODFL", "CHTR", "DDOG", "EA", "VRSK", "CTAS", "EXC", "KHC", "GEHC",
    "CCEP", "LULU", "BKR", "IDXX", "XEL", "CSGP", "ON", "TEAM", "ANSS", "DXCM",
    "ZS", "CDW", "MCHP", "TTWO", "GFS", "BIIB", "ARM", "MDB", "WBD", "ILMN",
]

# 미국 원자재 ETF(주식 아님 → '대장주' 없음, ETF 자체를 RS·눌림목으로 추적)
COMMODITY_ETFS = {
    "GLD": "금", "SLV": "은", "CPER": "구리", "USO": "WTI원유", "UNG": "천연가스",
    "DBA": "농산물", "DBC": "원자재종합", "URA": "우라늄", "GDX": "금광업",
}

# 시클리컬/심리 지표(yfinance) — 달러·변동성·경기민감 원자재
# up_good=True면 상승이 위험자산에 우호적(↑=초록), False면 비우호적(↑=빨강)
MACRO = {
    "DX-Y.NYB":  {"label": "달러지수",   "unit": "",   "up_good": False},
    "^VIX":      {"label": "VIX 변동성", "unit": "",   "up_good": False},
    "HG=F":      {"label": "구리(Dr.)",  "unit": "$",  "up_good": True},
    "CL=F":      {"label": "WTI 유가",   "unit": "$",  "up_good": True},
    "GC=F":      {"label": "금",         "unit": "$",  "up_good": True},
}

# 글로벌 매크로(FRED 무키 CSV) — 금리·물가·고용·유동성. 시장 판단의 경제 펀더멘털.
# (cat, FRED id, label, kind, unit, up_good[위험자산 관점])
#   kind: level=현재값 · yoy=전년동월비% · mom_k=전월대비(천 단위) · lvl_k=값/1000 · lvl_t=값/1e6(조)
FRED_MACRO = [
    ("금리",   "DFF",      "기준금리",      "level", "%",  False),
    ("금리",   "DGS10",    "10년물",        "level", "%",  False),
    ("금리",   "T10Y2Y",   "장단기차",      "level", "%p", True),
    ("물가",   "CPIAUCSL", "CPI",           "yoy",   "%",  False),
    ("물가",   "CPILFESL", "근원CPI",       "yoy",   "%",  False),
    ("물가",   "T10YIE",   "기대인플레",    "level", "%",  False),
    ("고용",   "UNRATE",   "실업률",        "level", "%",  False),
    ("고용",   "PAYEMS",   "비농업고용",    "mom_k", "K",  True),
    ("고용",   "ICSA",     "신규실업수당",  "lvl_k", "K",  False),
    ("유동성", "M2SL",     "M2 통화량",     "yoy",   "%",  True),
    ("유동성", "WALCL",    "연준 자산",     "lvl_t", "조$", True),
]

# 시장 건강도(레짐) 판단용 지수
INDEXES = {
    "US": [("^GSPC", "S&P500"), ("^IXIC", "나스닥")],
    "KR": [("^KS11", "코스피"), ("^KQ11", "코스닥")],
}

# 핫한 종목은 수동 리스트 없이 유니버스 전체에서 자동 발굴(거래량 급증 + 단기 모멘텀).
# → hot_score() 참고. 별도 워치리스트 불필요.

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
    "102960": {"label": "기계장비", "members": ["034020", "267260", "298040", "010120", "241560"]},
    "117460": {"label": "에너지화학", "members": ["051910", "011170", "011780", "009830", "010950", "006650"]},
    "140700": {"label": "보험", "members": ["032830", "000810", "005830", "001450", "000370"]},
}

# 신규 테마(KODEX ETF 코드가 영숫자라 yfinance 불안정 → 구성종목 RS 중앙값으로 섹터 강도 산출)
KR_THEME_MEMBERS = {
    "방산": ["012450", "047810", "079550", "064350", "272210", "042660"],
    "조선": ["009540", "329180", "010140", "042660", "010620"],
    "원자력": ["034020", "052690", "051600", "105840", "083650"],
    "로봇·AI": ["277810", "454910", "108490", "056190"],
    "게임": ["259960", "036570", "251270", "263750", "095660"],
    "엔터·미디어": ["352820", "041510", "035900", "122870"],
}

REPORT_DIR = "reports"
TEMPLATE = os.path.join(os.path.dirname(__file__), "dashboard_template.html")
DASHBOARD_URL = "https://quant-portfolio.tedsungjinhwang.workers.dev/"   # 라이브 대시보드(고정 URL)


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


def ytd_return(close):
    """연초 대비(YTD) 수익률. 전년 말 종가를 기준으로."""
    try:
        last = float(close.iloc[-1])
        yr = close.index[-1].year
        before = close[close.index.year < yr]
        base = float(before.iloc[-1]) if len(before) else float(close[close.index.year == yr].iloc[0])
        return (last / base - 1.0) if base > 0 else None
    except Exception:
        return None


def pc(v):
    """수익률(분수) → 퍼센트 반올림 또는 None."""
    return round(v * 100, 1) if v is not None else None


def rs_value(close):
    """단기중기 가중모멘텀(1·3·6개월). 반환: (rs, m3, m6)."""
    w = CONFIG["rs_weights"]
    m1, m3, m6 = pct_return(close, 21), pct_return(close, 63), pct_return(close, 126)
    if m3 is None or m6 is None:
        return None, m3, m6
    m1 = m1 if m1 is not None else m3
    return w["m1"] * m1 + w["m3"] * m3 + w["m6"] * m6, m3, m6


def zigzag(closes, pct):
    """퍼센트 지그재그 피벗: [(i, price, 'H'/'L'), ...]. thr% 이상 반전 시 피벗 확정."""
    n = len(closes)
    if n < 3:
        return []
    thr = pct / 100.0
    piv = []
    hi_i, hi_p = 0, closes[0]
    lo_i, lo_p = 0, closes[0]
    direction = 0   # 0=미정, 1=상승(저점 후 고점 탐색), -1=하락(고점 후 저점 탐색)
    for i in range(1, n):
        p = closes[i]
        if direction >= 0:                      # 고점 추적
            if p > hi_p:
                hi_i, hi_p = i, p
            if p <= hi_p * (1 - thr):            # 고점 대비 thr% 하락 → 고점 확정
                piv.append((hi_i, hi_p, 'H'))
                direction = -1
                lo_i, lo_p = i, p
        if direction <= 0:                      # 저점 추적
            if p < lo_p:
                lo_i, lo_p = i, p
            if p >= lo_p * (1 + thr):            # 저점 대비 thr% 상승 → 저점 확정
                piv.append((lo_i, lo_p, 'L'))
                direction = 1
                hi_i, hi_p = i, p
    return piv


def elliott_estimate(closes, pct):
    piv = zigzag(closes, pct)
    # 강세 추세주는 깊은 되돌림이 드물어 임계치를 자동으로 낮춰 스윙 확보
    for finer in (pct * 0.6, pct * 0.4, pct * 0.25):
        if len(piv) >= 3:
            break
        piv = zigzag(closes, finer)
    if len(piv) < 2:
        return "판단 보류", "스윙 미형성(변동성 낮음)"
    lows = [(k, p[1]) for k, p in enumerate(piv) if p[2] == 'L']
    if not lows:
        return "판단 보류", "기준 저점 미확인"
    # 가장 낮은 저점(주요 바닥)을 임펄스 시작점으로 보고, 그 이후 스윙으로 파동 카운트.
    # 추세가 길면 한 사이클(임펄스 5 + 조정 3 = 8파)을 넘기므로, 현재 사이클 안의
    # 위치(1~5 임펄스 · A·B·C 조정)로 환산한다. 모두 일봉 스윙 기준.
    major_k = min(lows, key=lambda x: x[1])[0]
    after = (len(piv) - 1) - major_k          # 주요 저점 이후 확정된 스윙 수
    if after <= 0:
        return "1파 진행 추정", "주요 저점에서 첫 상승 시작(추정)"
    cycles, pos = divmod(after, 8)            # pos: 0~7 = 현재 사이클 내 위치
    cur = pos + 1                             # 1~8 (1~5 임펄스, 6~8 = ABC)
    tail = f" · {cycles + 1}번째 사이클" if cycles else ""
    if cur <= 5:
        up = (cur % 2 == 1)
        return (f"{cur}파 {'상승' if up else '조정'} 추정",
                f"주요 저점 이후 일봉 {after}개 스윙 · "
                f"{'상승(임펄스)' if up else '되돌림'} 국면{tail}(추정)")
    label = "ABC"[cur - 6]
    return (f"조정 {label}파 추정",
            f"임펄스 5파 이후 {label}파 조정 국면(일봉){tail}(추정)")


def trend_state(last, v20, v60, v120, ma200_up, rsi_last, piv):
    """종목 추세 상태 (단계 분리):
      추세유지 → 주의 → 하락전환 → 하락추세
      - 하락전환: '20MA에 닿고(=종가 20일선 이탈) + 직전 스윙 저점 이탈'. 전환이 '일어나는' 시점.
      - 하락추세: 120일선(수급선) 종가 이탈. 하락이 '진행' 중(확정).
      - 20MA 위에서 지지 중이면(저점만 살짝 깨도) 정상 눌림목/주의 — 하락전환 아님."""
    if v20 is None and v60 is None:        # 상장 초기 — 이동평균 산출 전
        return "데이터 부족", ["상장 초기 · 이동평균 산출 전"]
    lows = [p[1] for p in piv if p[2] == 'L']
    highs = [p[1] for p in piv if p[2] == 'H']
    broke_low = bool(lows and last < lows[-1])           # 직전 스윙 저점 이탈(저점 낮아짐)
    lower_high = bool(len(highs) >= 2 and highs[-1] < highs[-2])
    below120 = v120 is not None and last < v120
    below60 = v60 is not None and last < v60
    below20 = v20 is not None and last < v20

    # 하락추세(확정): 120선 이탈
    if below120:
        reasons = ["120일선 이탈(하락 진행)"]
        if broke_low: reasons.append("저점 낮아짐")
        if below60: reasons.append("60일선 이탈")
        return "하락추세", reasons

    # 하락전환: 20MA 이탈 + 직전 스윙 저점 이탈 (둘 다 충족해야)
    if below20 and broke_low:
        reasons = ["20일선 이탈 + 직전 스윙 저점 이탈(저점 낮아짐)"]
        if below60: reasons.append("60일선 이탈")
        if lower_high: reasons.append("고점도 낮아짐")
        return "하락전환", reasons

    # 주의: 약화 신호(아직 20MA 위에서 지지 중이거나 일부만 이탈)
    reasons = []
    if below60: reasons.append("60일선 이탈")
    if below20: reasons.append("20일선 종가 이탈(단기 약화)")
    if broke_low: reasons.append("직전 스윙 저점 이탈(단, 20MA 지지 시도)")
    if lower_high: reasons.append("고점 낮아짐")
    if not ma200_up: reasons.append("240일선 하락")
    if rsi_last is not None and rsi_last < 45: reasons.append(f"RSI 약세({rsi_last:.0f})")

    if reasons:
        return "주의", reasons
    return "추세유지", ["20·60·120일선 위 · 스윙 저점 유지"]


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
    width = (hi - lo) / lo if lo > 0 else 1.0
    rangebound = width < 0.18                             # 최근 60일 약 18% 이내 박스권
    # 장기(최대 240일) 레인지 내 위치 — 저점권/고점권 판정(매집 vs 분산 핵심)
    ll = min(240, n)
    long_hi = float(h.tail(ll).max())
    long_lo = float(l.tail(ll).min())
    long_pos = (last - long_lo) / (long_hi - long_lo) if long_hi > long_lo else 0.5
    ma_long = c.rolling(min(120, n)).mean()
    above = last > float(ma_long.iloc[-1])
    slope_up = float(ma_long.iloc[-1]) > float(ma_long.iloc[-min(21, n - 1)])

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

    # 국면: 추세 vs 박스권 → 박스권이면 '장기 위치'로 매집(저점)/분산(고점) 구분
    if rangebound:
        if long_pos >= 0.6:
            phase = "분산 추정 (Distribution)"      # 올라서 고점 횡보
        elif long_pos <= 0.4:
            phase = "매집 추정 (Accumulation)"       # 내려서 저점 횡보
        else:
            phase = "상승 추세 (Markup)" if above else "하락 추세 (Markdown)"
    elif above and slope_up:
        phase = "상승 추세 (Markup)"
    elif (not above) and (not slope_up):
        phase = "하락 추세 (Markdown)"
    else:
        phase = "상승 추세 (Markup)" if above else "하락 추세 (Markdown)"
    return phase, " · ".join(events) if events else "주요 이벤트 미감지"


def metrics(meta, df, relaxed=False, bench=0.0):
    """종목 1개의 지표 dict(또는 None).
    relaxed=True: 신규상장·짧은 데이터용 — 최소 봉수 완화 + RS는 가용 구간 수익률로 대용.
    bench: 벤치마크(지수) 가중모멘텀. RS = 종목 가중모멘텀 − 지수 가중모멘텀(지수 대비 상대강도)."""
    min_bars = 6 if relaxed else 60
    if df is None or len(df) < min_bars:
        return None
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])
    floor = CONFIG["min_price_krw"] if meta["market"] == "KR" else CONFIG["min_price_usd"]
    if last < floor:
        return None
    rs, m3, m6 = rs_value(close)
    if rs is None:
        if not relaxed:
            return None
        n = len(close) - 1
        base = float(close.iloc[0])
        since = (last / base - 1) if base > 0 else 0.0   # 상장 후(가용 구간) 수익률
        m3 = pct_return(close, min(63, n)) or since
        m6 = pct_return(close, min(126, n)) or since
        rs = m3  # RS 대용
    rs -= bench   # 지수 대비 상대강도(초과 모멘텀)

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
    r1d, r1w, r1m = pct_return(close, 1), pct_return(close, 5), pct_return(close, 21)
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
        "yahoo": meta.get("yahoo", meta["code"]),
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
        "ret1d": round(r1d * 100, 2) if r1d is not None else None,
        "ret1w": round(r1w * 100, 1) if r1w is not None else None,
        "ret1m": round(r1m * 100, 1) if r1m is not None else None,
        "ret3m": round(m3 * 100, 1), "ret6m": round(m6 * 100, 1),
        "ret1y": pc(pct_return(close, 252)), "ret_ytd": pc(ytd_return(close)),
        "trend_ok": bool(pd.notna(v200) and last > float(v200)),
        "bars": bars, "ma20s": arr(ma20), "ma60s": arr(ma60),
        "ma120s": arr(ma120), "ma200s": arr(ma200),
        "rsis": [None if pd.isna(x) else round(float(x), 1) for x in rsi(close).tail(cb)],
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
    ma200 = close.rolling(min(240, len(close))).mean()
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
        "ret1d": (lambda v: round(v * 100, 2) if v is not None else None)(pct_return(close, 1)),
        "ret1w": pc(pct_return(close, 5)), "ret1m": pc(pct_return(close, 21)),
        "ret3m": pc(pct_return(close, 63)), "ret6m": pc(pct_return(close, 126)),
        "ret1y": pc(pct_return(close, 252)), "ret_ytd": pc(ytd_return(close)),
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
        below200 = pct(lambda r: r["close"] < r["ma200"])    # 240일선 이탈비율(↑=약세)
        above200 = round(100 - below200, 1) if below200 is not None else None
        new_high = pct(lambda r: r.get("high52_pct") is not None and r["high52_pct"] >= -0.5)  # 52주 신고가
        near_high = pct(lambda r: r.get("high52_pct") is not None and r["high52_pct"] >= -3)   # 신고가 근접
        new_low = pct(lambda r: r.get("low52_pct") is not None and r["low52_pct"] <= 1.0)      # 52주 신저가
        net_nh = round((new_high or 0) - (new_low or 0), 1) if n else None  # 신고가-신저가 순증

        primary = idxs[0] if idxs else None
        idx_up = bool(primary and primary["above200"])
        reasons = []
        if primary:
            reasons.append(f"{primary['name']} {'240일선 위' if primary['above200'] else '240일선 아래'}"
                           f"({primary['dist200']:+.1f}%, {'정배열' if primary['golden'] else '역배열'})")
        if below200 is not None:
            reasons.append(f"240일선 이탈 {below200:.0f}% · 20일선 이탈 {below20:.0f}% (낮을수록 강세)")
            reasons.append(f"52주 신고가 {new_high:.0f}% · 신저가 {new_low:.0f}% (순증 {net_nh:+.0f})")

        # 국면 판정 (StockEasy 라벨 체계)
        weak200 = below200 if below200 is not None else 50
        weak20 = below20 if below20 is not None else 50
        nn = net_nh if net_nh is not None else 0
        if idx_up and weak200 < 40 and nn >= 0:
            label, color = "추세유지", "green"
            premise = "상승 추세 견조 — 다수 종목이 240일선 위, 신고가 우위. 눌림목 매수 우호."
        elif (not idx_up) or weak200 > 60:
            label, color = "조정 국면", "red"
            premise = "다수 종목이 240일선 이탈 — 눌림목 매수 저확률, 현금·방어 비중 우선."
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


def build_macro(data):
    """시클리컬/심리 지표(yfinance) — 현재값 + 전일 변화율. cat='시클리컬'."""
    out = []
    for tk, info in MACRO.items():
        df = ohlc_for(data, tk)
        if df is None or len(df) < 2:
            continue
        c = df["Close"]
        last = float(c.iloc[-1])
        r1 = round((last / float(c.iloc[-2]) - 1) * 100, 2) if len(c) >= 2 else None
        r20 = round((last / float(c.iloc[-21]) - 1) * 100, 1) if len(c) >= 21 else None
        out.append({"cat": "시클리컬", "label": info["label"], "unit": info["unit"],
                    "up_good": info["up_good"], "value": round(last, 2),
                    "delta": r1, "sub": (f"1M {'+' if (r20 or 0) >= 0 else ''}{r20}%" if r20 is not None else "")})
    return out


def _fred_series(series_id):
    """FRED 무키 CSV → [(date, value)...] (시간순). 결측('.') 제외."""
    import requests
    r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout=15)
    rows = []
    for line in r.text.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 2 and p[1] not in ("", "."):
            try:
                rows.append((p[0], float(p[1])))
            except ValueError:
                pass
    return rows


def build_global_macro():
    """글로벌 매크로(FRED) — 금리·물가·고용·유동성. 각 지표 현재값 + 직전 대비 변화·기준일."""
    out = []
    for cat, sid, label, kind, unit, up_good in FRED_MACRO:
        try:
            rows = _fred_series(sid)
            if len(rows) < 2:
                continue
            asof, last = rows[-1]
            prev = rows[-2][1]
            if kind == "level":
                val, delta = round(last, 2), round(last - prev, 2)
            elif kind == "lvl_k":
                val, delta = round(last / 1000, 1), round((last - prev) / 1000, 1)
            elif kind == "lvl_t":
                val, delta = round(last / 1e6, 2), round((last - prev) / 1e6, 2)
            elif kind == "mom_k":
                val = delta = round(last - prev)          # PAYEMS는 천 단위 → 전월대비 증감(천명)
            elif kind == "yoy":
                if len(rows) < 13:
                    continue
                val = round((last / rows[-13][1] - 1) * 100, 1)
                prev_yoy = round((rows[-2][1] / rows[-14][1] - 1) * 100, 1) if len(rows) >= 14 else val
                delta = round(val - prev_yoy, 2)
            else:
                continue
            out.append({"cat": cat, "label": label, "value": val, "unit": unit,
                        "delta": delta, "up_good": up_good, "sub": asof})
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------
# 핫한 종목(거래량 급증 + 단기 모멘텀)
# ----------------------------------------------------------------------
def hot_score(rec):
    """지금 '뜨거운' 정도 → 0~100. 거래량 급증 + 단기 모멘텀(1주·1개월) + 신고가 근접 결합.
    유니버스 전체 종목에 대해 계산하고 상위 N개를 핫한 종목으로 노출(수동 리스트 없음)."""
    s = 0.0
    vr = rec.get("vol_ratio")
    if vr:
        s += max(0.0, min(35.0, (vr - 1.0) * 30.0))      # 거래량 20일평균比(2배=+30, 2.17배=+35)
    if rec.get("ret1w") is not None:
        s += max(-10.0, min(30.0, rec["ret1w"] * 2.0))   # 1주 모멘텀(+15%=+30)
    if rec.get("ret1m") is not None:
        s += max(-8.0, min(20.0, rec["ret1m"] * 0.6))    # 1개월 모멘텀(+33%=+20)
    h = rec.get("high52_pct")
    if h is not None:
        s += max(0.0, min(15.0, (h + 10.0) * 1.5))       # 신고가 근접(0%=+15, -10%↓=0)
    if rec.get("trend_state") in ("하락전환", "하락추세"):
        s -= 15.0                                        # 추세 꺾인 급등은 감점
    return round(max(0.0, min(100.0, s)), 1)


# ----------------------------------------------------------------------
# 컨센서스 목표가(애널리스트) — 선택 종목만 조회
# ----------------------------------------------------------------------
# 미국 투자의견 영문 → 한글
REC_KO = {"strong_buy": "적극매수", "buy": "매수", "hold": "중립",
          "underperform": "비중축소", "sell": "매도", "none": "-"}


def rec_from_naver(rm):
    """네이버 recommMean(1~5, 높을수록 매수) → 한글 투자의견."""
    if rm is None:
        return "-"
    if rm >= 4.5: return "적극매수"
    if rm >= 3.5: return "매수"
    if rm >= 2.5: return "중립"
    if rm >= 1.5: return "비중축소"
    return "매도"


def consensus_us(sym):
    """미국: yfinance 집계 컨센서스 → {mean,high,low,n,rec,date} 또는 None."""
    try:
        import yfinance as yf
        i = yf.Ticker(sym).get_info()
        mean = i.get("targetMeanPrice")
        if mean is None:
            return None
        return {"mean": float(mean),
                "high": float(i["targetHighPrice"]) if i.get("targetHighPrice") else None,
                "low": float(i["targetLowPrice"]) if i.get("targetLowPrice") else None,
                "n": i.get("numberOfAnalystOpinions"),
                "rec": REC_KO.get(i.get("recommendationKey"), i.get("recommendationKey") or "-"),
                "date": None}
    except Exception:
        return None


def consensus_kr(code):
    """한국: 네이버 증권 통합 API의 consensusInfo(목표주가 평균·투자의견 평균) → dict 또는 None.
    코스피·코스닥 모두 커버(증권사 컨센서스 집계). 고가/저가/애널수는 미제공."""
    try:
        import requests
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                                                "AppleWebKit/605.1.15", "Referer": "https://m.stock.naver.com/"})
        c = (r.json() or {}).get("consensusInfo") or {}
        ptm = c.get("priceTargetMean")
        if not ptm:
            return None
        rm = c.get("recommMean")
        return {"mean": float(str(ptm).replace(",", "")), "high": None, "low": None, "n": None,
                "rec": rec_from_naver(float(rm) if rm else None), "date": c.get("createDate")}
    except Exception:
        return None


def attach_consensus(stocks):
    """선택된 종목(원자재 제외)에 컨센서스 목표가·상승여력 부착. 미국=yfinance·한국=네이버.
    조회된 id 리스트(상승여력 내림차순) 반환."""
    got = []
    for sid, s in stocks.items():
        mkt = s.get("market")
        if mkt == "CMD":                      # 원자재 ETF는 애널 컨센서스 없음
            continue
        c = consensus_kr(s["code"]) if mkt == "KR" else consensus_us(s.get("yahoo") or s["code"])
        if not c:
            continue
        close = s.get("close")
        s["t_mean"] = round(c["mean"], 2)
        s["t_high"] = round(c["high"], 2) if c["high"] else None
        s["t_low"] = round(c["low"], 2) if c["low"] else None
        s["t_n"] = c["n"]
        s["t_rec"] = c["rec"]
        s["t_date"] = c.get("date")
        s["t_upside"] = round((c["mean"] / close - 1) * 100, 1) if close else None
        got.append(sid)
    got.sort(key=lambda i: (stocks[i].get("t_upside") if stocks[i].get("t_upside") is not None else -999),
             reverse=True)
    return got


# 네이버 리포트 투자의견(한/영 혼재) → 한글 6단계
OPINION_NORM = {
    "적극매수": "적극매수", "강력매수": "적극매수", "strongbuy": "적극매수",
    "매수": "매수", "buy": "매수",
    "비중확대": "비중확대", "outperform": "비중확대", "overweight": "비중확대",
    "중립": "중립", "hold": "중립", "neutral": "중립", "marketperform": "중립",
    "비중축소": "비중축소", "underperform": "비중축소", "underweight": "비중축소", "reduce": "비중축소",
    "매도": "매도", "sell": "매도",
}


def _opinion_norm(op):
    if not op:
        return None
    k = op.replace(" ", "").lower()
    return OPINION_NORM.get(k) or (None if op in ("없음", "-", "N/A") else op)


def consensus_detail(code, cap=25):
    """한국 종목의 증권사별 컨센서스(목표가·투자의견 시계열). 네이버 리서치 + 리포트 상세 파싱.
    반환: {reports:[{broker,date,target,opinion,title}], count, with_target,
           mean, median, high, low, high_broker, low_broker, dist:{의견:건수}} 또는 None."""
    import requests, statistics
    mh = {"User-Agent": "Mozilla/5.0 (iPhone)", "Referer": "https://m.stock.naver.com/"}
    ph = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
          "Referer": "https://finance.naver.com/research/"}
    try:
        lst = requests.get(f"https://m.stock.naver.com/api/research/stock/{code}?pageSize={cap}&page=1",
                           headers=mh, timeout=10).json()
    except Exception:
        return None
    reports = []
    for it in lst:
        rep = {"broker": it.get("brokerName"), "date": it.get("writeDate"), "title": it.get("title")}
        try:
            html = requests.get(f"https://finance.naver.com/research/company_read.naver?nid={it['researchId']}",
                                headers=ph, timeout=10).content.decode("euc-kr", "replace")
            mt = re.search(r'class="money"><strong>([\d,]+)', html)
            mo = re.search(r'class="coment">\s*([^<]+?)\s*<', html)
            if mt:
                rep["target"] = int(mt.group(1).replace(",", ""))
            op = _opinion_norm(mo.group(1).strip() if mo else None)
            if op:
                rep["opinion"] = op
        except Exception:
            pass
        reports.append(rep)
    # 증권사별 '최신 1건'만 컨센서스 통계에 사용(같은 증권사의 과거 목표가 중복 제외).
    # 급등주는 옛 목표가가 평균을 왜곡하므로 표준 방식(각 사 현재 의견)으로 집계.
    latest = {}
    for r in reports:
        if not r.get("target"):
            continue
        b = r.get("broker") or "?"
        if b not in latest or (r.get("date") or "") > (latest[b].get("date") or ""):
            latest[b] = r
    lat = list(latest.values())
    tg = [r["target"] for r in lat]
    if not tg:
        return None
    hi = max(lat, key=lambda r: r["target"])
    lo = min(lat, key=lambda r: r["target"])
    dist = {}
    for r in lat:
        if r.get("opinion"):
            dist[r["opinion"]] = dist.get(r["opinion"], 0) + 1
    return {"reports": reports, "count": len(reports), "brokers": len(lat), "with_target": len(tg),
            "mean": round(statistics.mean(tg)), "median": round(statistics.median(tg)),
            "high": max(tg), "low": min(tg),
            "high_broker": hi.get("broker"), "low_broker": lo.get("broker"), "dist": dist}


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


def build_message(markets, stocks, bar_date, commodity_ids=(), regime=None, hot_ids=(),
                  seasonality=None, consensus_ids=()):
    regime = regime or {}
    seasonality = seasonality or {}
    flag = lambda m: "🟢" if m == "US" else "🔵"
    light = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    lines = [f"📈 <b>증시 추세추종 스크리너</b> · 기준일 {bar_date}",
             f"📊 대시보드: {DASHBOARD_URL}"]
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
            lines.append("· 트랙② Top10 중 풀백 타점: " + ", ".join(f"{r['name']}(RS#{r['rs_rank']})" for r in touched))
        deep = [stocks[i] for i in mk.get("deep_ids", [])]
        if deep:
            lines.append("· 심층 Top3: " + ", ".join(
                f"{r['name']}(1M{r.get('ret1m',0):+.0f}%·{r.get('wyckoff','').split('(')[0].strip()})" for r in deep))
        watch_ids = set(mk.get("top_ids", [])) | {i for s in mk.get("sectors", []) for i in s["leader_ids"]}
        flip = [stocks[i]["name"] for i in watch_ids if stocks[i].get("trend_state") == "하락전환"]
        down = [stocks[i]["name"] for i in watch_ids if stocks[i].get("trend_state") == "하락추세"]
        if flip:
            lines.append("🟠 하락전환(저점 이탈): " + ", ".join(flip))
        if down:
            lines.append("🔴 하락추세(120선 이탈): " + ", ".join(down))
    if commodity_ids:
        lines.append("\n🟡 <b>원자재(RS 상위)</b>: " +
                     ", ".join(f"{stocks[i]['name'].split('(')[0]}{'🟢풀백' if stocks[i]['touched'] else ''}"
                               for i in commodity_ids[:5]))
    if hot_ids:
        lines.append("\n🔥 <b>핫한 종목(거래량·단기급등)</b>: " +
                     ", ".join(f"{stocks[i]['name']}(점수{stocks[i].get('hot_score','-')}"
                               f"{'·신규상장' if stocks[i].get('is_new') else ''})" for i in hot_ids[:6]))
    if consensus_ids:
        lines.append("\n🎯 <b>컨센서스 상승여력</b>(목표가 대비): " +
                     ", ".join(f"{stocks[i]['name']}({stocks[i].get('t_upside',0):+.0f}%·{stocks[i].get('t_rec','-')})"
                               for i in consensus_ids[:6] if stocks[i].get('t_upside') is not None))
    lines.append(f"\n<i>📊 전체 차트·기술적 분석 → {DASHBOARD_URL}\n무료 지연 종가 · RS=지수 대비 상대강도</i>")
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
        # Cloudflare Pages 배포용(고정 URL 루트). build output dir = public
        os.makedirs("public", exist_ok=True)
        with open(os.path.join("public", "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print("[report] reports/dashboard.html + public/index.html 저장")
    except Exception as e:
        print(f"[report] 대시보드 생성 실패: {e}")

    md = [f"# 2트랙 모멘텀 리포트 — {payload['bar_date']}\n"]
    for mkt in ("US", "KR"):
        mk = payload["markets"].get(mkt, {})
        md.append(f"## {'🟢 미국' if mkt=='US' else '🔵 한국'}\n")
        md.append("### 트랙① 강한 섹터 → 대장주")
        for s in mk.get("sectors", []):
            names = ", ".join(f"{payload['stocks'][i]['name']}({payload['stocks'][i]['code']})"
                              + ("🟢풀백" if payload['stocks'][i]['touched'] else "") for i in s["leader_ids"])
            md.append(f"- **{s['sector']}** [{s['etf']} RS {s['etf_rs']}]: {names or '해당 없음'}")
        fr = lambda v: "–" if v is None else f"{v:+.1f}"
        fr2 = lambda v: "–" if v is None else f"{v:+.2f}"
        md.append("\n### 트랙② 개별 RS Top10 (RS=지수 대비)")
        md.append("| RS# | 종목 | 종가 | 1D% | 7D% | 1M% | 3M% | RS | 20MA% | RSI | 풀백 | 추세 | 엘리어트 |")
        md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i in mk.get("top_ids", []):
            r = payload["stocks"][i]
            pb = "풀백 타점" if r["touched"] else ("근접" if r["near"] else "–")
            star = " ★" if i in mk.get("deep_ids", []) else ""
            md.append(f"| {r['rs_rank']} | {r['name']}({r['code']}) | {r['close']:,} | "
                      f"{fr2(r.get('ret1d'))} | {fr(r.get('ret1w'))} | {fr(r.get('ret1m'))} | {fr(r.get('ret3m'))} | "
                      f"{r['rs']} | {r['dist20']:+.1f} | {r['rsi']} | {pb} | {r.get('trend_state','')} | {r['elliott']}{star} |")
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
    for sym in NASDAQ100:                       # 나스닥100 편입(S&P500과 겹치면 스킵)
        if sym not in us_codes:
            us_codes.add(sym)
            rows.append({"market": "US", "code": sym, "yahoo": sym.replace(".", "-"),
                         "name": sym, "sector": "Nasdaq100"})
    for info in THEME_ETFS_US.values():
        for sym in info["members"]:
            if sym not in us_codes:
                us_codes.add(sym)
                rows.append({"market": "US", "code": sym, "yahoo": sym.replace(".", "-"),
                             "name": sym, "sector": info["label"]})
    # 한국 KODEX 섹터 ETF 구성종목 편입(suffix는 전체 상장목록에서 해석)
    kr_codes = {r["code"] for r in rows if r["market"] == "KR"}
    kr_member_groups = ([(info["label"], info["members"]) for info in KR_SECTOR_ETFS.values()]
                        + list(KR_THEME_MEMBERS.items()))
    for label, members in kr_member_groups:
        for code in members:
            if code not in kr_codes:
                kr_codes.add(code)
                lk = kr_lookup.get(code, {"suffix": ".KS", "name": code})
                rows.append({"market": "KR", "code": code, "yahoo": f"{code}{lk['suffix']}",
                             "name": lk["name"], "sector": label})

    index_tickers = {t for lst in INDEXES.values() for t, _ in lst}
    kr_etf_yahoos = {f"{c}.KS" for c in KR_SECTOR_ETFS}
    symbols = ({r["yahoo"] for r in rows} | set(SECTOR_ETFS_US.keys())
               | set(THEME_ETFS_US.keys()) | set(COMMODITY_ETFS.keys())
               | set(MACRO.keys()) | index_tickers | kr_etf_yahoos)
    data = download_prices(symbols)
    bar_date = latest_bar_date(data)

    # 벤치마크(지수) 가중모멘텀 — RS를 지수 대비 상대강도로 계산하기 위함
    bench_rs = {}
    for mk, tk in (("US", "^GSPC"), ("KR", "^KS11")):
        d = ohlc_for(data, tk)
        v = (rs_value(d["Close"].astype(float))[0] if d is not None else None)
        bench_rs[mk] = v or 0.0
    bench_rs["CMD"] = bench_rs["US"]
    print(f"[bench] US={bench_rs['US']*100:.1f} KR={bench_rs['KR']*100:.1f} (지수 가중모멘텀)")

    allrecs = []
    for r in rows:
        rec = metrics(r, ohlc_for(data, r["yahoo"]), bench=bench_rs.get(r["market"], 0.0))
        if rec:
            allrecs.append(rec)

    def etf_rs(sym, mkt="US"):
        df = ohlc_for(data, sym)
        if df is None:
            return None
        v, *_ = rs_value(df["Close"].astype(float))
        return None if v is None else v - bench_rs.get(mkt, 0.0)

    # 섹터/테마 유닛(시장별 한 풀에서 RS 랭킹)
    us_units = [{"etf": etf, "label": sec, "kind": "sector", "rs": etf_rs(etf, "US")}
                for etf, sec in SECTOR_ETFS_US.items()]
    us_units += [{"etf": etf, "label": info["label"], "kind": "theme",
                  "members": info["members"], "rs": etf_rs(etf, "US")}
                 for etf, info in THEME_ETFS_US.items()]
    rs_by_code = {r["code"]: r["_rs_raw"] for r in allrecs}
    def member_median_rs(members):
        vals = [rs_by_code[c] for c in members if c in rs_by_code]
        return statistics.median(vals) if vals else None
    # KODEX 섹터 ETF: ETF RS, 실패 시 구성종목 RS중앙값으로 폴백
    kr_units = [{"etf": code, "label": info["label"], "kind": "theme", "members": info["members"],
                 "rs": etf_rs(f"{code}.KS", "KR") if etf_rs(f"{code}.KS", "KR") is not None else member_median_rs(info["members"])}
                for code, info in KR_SECTOR_ETFS.items()]
    # 신규 테마: 구성종목 RS중앙값
    kr_units += [{"etf": "(구성종목)", "label": label, "kind": "theme", "members": members,
                  "rs": member_median_rs(members)}
                 for label, members in KR_THEME_MEMBERS.items()]
    units_by_market = {"US": us_units, "KR": kr_units}

    # 원자재 ETF(주식 아님 → ETF 자체를 rec로)
    commodities = []
    for tk, label in COMMODITY_ETFS.items():
        rec = metrics({"market": "CMD", "code": tk, "name": f"{label}({tk})", "sector": "원자재"},
                      ohlc_for(data, tk), bench=bench_rs["CMD"])
        if rec:
            commodities.append(rec)
    commodities.sort(key=lambda r: r["_rs_raw"], reverse=True)

    # 핫한 종목: 유니버스 전체에서 거래량 급증 + 단기 모멘텀 점수화 → 상위 N(시장별)
    for rec in allrecs:
        rec["hot_score"] = hot_score(rec)
    hot = []
    for mkt in ("US", "KR"):
        cand = sorted((r for r in allrecs if r["market"] == mkt),
                      key=lambda r: r["hot_score"], reverse=True)
        hot += cand[: CONFIG["hot_top_n"]]
    print(f"[result] 종목 {len(allrecs)} · US유닛 {sum(1 for u in us_units if u['rs'] is not None)} "
          f"· KR유닛 {sum(1 for u in kr_units if u['rs'] is not None)} · 원자재 {len(commodities)} · 핫 {len(hot)}")

    idx_map = {}
    for mkt, lst in INDEXES.items():
        for tk, nm in lst:
            h = index_health(nm, tk, ohlc_for(data, tk))
            if h:
                idx_map[tk] = h
    regime = build_regime(allrecs, idx_map)
    print(f"[regime] US={regime['US']['label']} KR={regime['KR']['label']}")

    macro = build_global_macro() + build_macro(data)
    print(f"[macro] {len(macro)}개 지표(FRED 금리·물가·고용·유동성 + 시클리컬): "
          + ", ".join(f"{m['label']} {m['value']}{m['unit']}" for m in macro))

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
    for rec in hot:          # 핫한 종목을 stocks에 추가(이미 선택됐으면 hot_score만 병합)
        if rec["id"] in stocks:
            stocks[rec["id"]].update({k: rec[k] for k in ("hot_score", "is_new", "days")})
        else:
            c = dict(rec); c.pop("_rs_raw", None); c.setdefault("rs_rank", 0)
            stocks[rec["id"]] = c

    # 컨센서스 목표가: 선택된 모멘텀 종목만 조회(미국=yfinance·한국=네이버)
    consensus_ids = attach_consensus(stocks)
    print(f"[consensus] {len(consensus_ids)}종목 목표가 부착(상승여력 내림차순)")

    # 한국 딥 Top3 + 섹터 대장주만 증권사별 풀 컨센서스(시계열·의견분포) 수집(중복 제거)
    kr = markets.get("KR", {})
    detail_ids = list(kr.get("deep_ids", []))
    for sec in kr.get("sectors", []):
        for lid in sec.get("leader_ids", []):
            if lid not in detail_ids:
                detail_ids.append(lid)
    cons_detail = {}
    for sid in detail_ids:
        code = stocks.get(sid, {}).get("code")
        if not code:
            continue
        d = consensus_detail(code)
        if d:
            cons_detail[sid] = d
    print(f"[consensus] 한국 딥+대장주 {len(cons_detail)}종목 증권사별 시계열 수집")

    payload = {
        "bar_date": bar_date,
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "config": {k: CONFIG[k] for k in ("ma_pullback", "ma_trend", "top_sectors",
                                          "leaders_per_sector", "individual_top", "deep_top",
                                          "proximity_pct", "rs_weights", "zigzag_pct")},
        "regime": regime, "seasonality": seasonality, "macro": macro,
        "markets": markets, "stocks": stocks,
        "commodities": [c["id"] for c in commodities],
        "hot": [h["id"] for h in hot],
        "consensus": consensus_ids,
        "consensus_detail": cons_detail,
        "counts": {"stocks": len(stocks),
                   "touched": sum(1 for r in stocks.values() if r["touched"]),
                   "near": sum(1 for r in stocks.values() if r["near"])},
    }
    write_outputs(payload)
    send_telegram(build_message(markets, stocks, bar_date, payload["commodities"], regime,
                                payload["hot"], seasonality, payload["consensus"]))
    print("[done]")


if __name__ == "__main__":
    main()
