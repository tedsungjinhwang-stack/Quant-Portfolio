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
    "KRW=X":     {"label": "원/달러",    "unit": "",   "up_good": False},
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
    kr = None
    try:
        frames = []
        for mkt, suffix in (("KOSPI", ".KS"), ("KOSDAQ", ".KQ")):
            df = fdr.StockListing(mkt).copy()
            df["_suffix"] = suffix
            frames.append(df)
        kr = pd.concat(frames, ignore_index=True)
    except Exception as e:
        print(f"[universe] KR(KOSPI/KOSDAQ) 실패: {e} → KRX 통합목록 재시도")
        try:
            kr = fdr.StockListing("KRX").copy()
            mcol = "Market" if "Market" in kr.columns else None
            kr["_suffix"] = (kr[mcol].map(lambda m: ".KQ" if "KOSDAQ" in str(m).upper() else ".KS")
                             if mcol else ".KS")
        except Exception as e2:
            print(f"[universe] KRX 재시도 실패: {e2}")
            kr = None
    if kr is not None:
        try:
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
            print(f"[universe] KR 목록 처리 실패: {e}")
    # 이름 캐시: 수집 성공 시 저장 · 실패 시 캐시로 복구(이름=코드로 표시되는 사고 방지)
    cache_f = os.path.join(REPORT_DIR, "kr_names.json")
    if len(kr_lookup) >= 500:
        try:
            os.makedirs(REPORT_DIR, exist_ok=True)
            with open(cache_f, "w", encoding="utf-8") as f:
                json.dump(kr_lookup, f, ensure_ascii=False)
        except Exception:
            pass
    else:
        try:
            with open(cache_f, encoding="utf-8") as f:
                kr_lookup = {**json.load(f), **kr_lookup}
            print(f"[universe] KR 이름 캐시 복구 {len(kr_lookup)}종")
        except Exception:
            print("[universe] KR 이름 캐시 없음 — 이름이 코드로 표시될 수 있음")
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
    base_v = float(vol.tail(60).mean()) if (vol is not None and len(vol) >= 60) else 0.0
    prior_lo = float(l.iloc[max(0, n - look):max(1, n - 10)].min())
    prior_hi = float(h.iloc[max(0, n - look):max(1, n - 10)].max())
    if base_v > 0:
        if float(vol.tail(10).mean()) < base_v * 0.8:
            events.append("거래량 마름 → 매집 후반·상승 임박")
        # 최근 구간 최대 거래량일의 위치·방향으로 셀링/바잉 클라이맥스 구분
        vt = vol.tail(look)
        imax = int(vt.values.argmax())
        if float(vt.iloc[imax]) > base_v * 2.5:
            gi = n - len(vt) + imax
            dpos = (float(c.iloc[gi]) - long_lo) / (long_hi - long_lo) if long_hi > long_lo else 0.5
            down = gi > 0 and float(c.iloc[gi]) < float(c.iloc[gi - 1])
            if down and dpos <= 0.45:
                events.append("셀링클라이맥스(SC) → 매집 초입·바닥권(강세 전환)")
            elif (not down) and dpos >= 0.55:
                events.append("바잉클라이맥스(BC) → 분산 초입·천장권(약세 전환)")
            else:
                events.append("거래량 클라이맥스 → 변곡 가능")
        # 거래량 동반 박스 돌파/이탈 → 강세신호(SOS)/약세신호(SOW)
        vol_strong = float(vol.tail(5).mean()) > base_v * 1.2
        if vol_strong and last > prior_hi:
            events.append("강세돌파(SOS) → 매집 종료·상승 시작(강세)")
        if vol_strong and last < prior_lo:
            events.append("약세이탈(SOW) → 분산 종료·하락 시작(약세)")
    if float(l.tail(10).min()) < prior_lo and last > prior_lo:
        events.append("스프링 → 매집 막바지·상승 직전(강세)")
    if float(h.tail(10).max()) > prior_hi and last < prior_hi:
        events.append("업스러스트 → 분산 막바지·하락 직전(약세)")

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
    close = df["Close"].astype(float).dropna()
    if len(close) < 60:
        return None
    last = float(close.iloc[-1])
    asof = pd.to_datetime(close.index[-1]).date().isoformat()
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(min(240, len(close))).mean()
    v50, v200 = ma50.iloc[-1], ma200.iloc[-1]
    slope_up = bool(pd.notna(v200) and len(ma200.dropna()) > 21 and v200 > ma200.iloc[-21])
    wyck, wyck_note = wyckoff_estimate(df)
    return {
        "name": name, "ticker": ticker, "close": round(last, 2), "asof": asof,
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


def macro_read(label, v):
    """지표 현재 '수준' 해석 → {zone: good/ok/warn/bad, note: 한 줄 의미}. (변화가 아닌 레벨 기준)"""
    g = lambda z, n: {"zone": z, "note": n}
    if v is None:
        return g("ok", "")
    if label == "기준금리":
        return g("warn" if v >= 4.5 else "ok" if v >= 2 else "good", "정책금리 — 높을수록 긴축(위험자산 부담), 인하 사이클이면 우호")
    if label == "10년물":
        return g("warn" if v >= 4.5 else "ok" if v >= 3.5 else "good", "장기금리 — 4.5%↑ 밸류에이션 부담, 3.5%↓ 완화적")
    if label == "장단기차":
        return g("bad" if v < 0 else "warn" if v < 0.3 else "good", "10Y−2Y — 음수=금리 역전(침체 선행), 양수=정상")
    if label in ("CPI", "근원CPI"):
        return g("bad" if v >= 3.5 else "warn" if v >= 2.5 else "good", "물가 YoY — 연준 목표 2%. 3%대↑면 긴축 압력")
    if label == "기대인플레":
        return g("warn" if v >= 2.7 else "ok" if v >= 2 else "good", "시장 기대 인플레(10Y) — 2% 부근이 안정")
    if label == "실업률":
        return g("good" if 3.5 <= v <= 4.5 else "warn" if v <= 5.5 else "bad", "3.5~4.5%=완전고용권(양호), 5%대↑ 급등=침체 신호")
    if label == "비농업고용":
        return g("good" if v >= 150 else "warn" if v >= 50 else "bad", "월간 신규고용(천명) — 150K↑ 견조, 0 이하 위축")
    if label == "신규실업수당":
        return g("good" if v < 250 else "ok" if v < 300 else "warn" if v < 350 else "bad", "주간 실업청구(천건) — 낮을수록 고용 견조, 350K↑ 경계")
    if label == "M2 통화량":
        return g("bad" if v < 0 else "warn" if v < 3 else "ok" if v < 7 else "good", "통화량 YoY — 마이너스=긴축(유동성 위축), 높을수록 완화")
    if label == "연준 자산":
        return g("ok", "연준 대차대조표(조$) — 증가=완화(QE), 감소=긴축(QT)")
    if label == "VIX 변동성":
        return g("good" if v < 15 else "ok" if v < 20 else "warn" if v < 30 else "bad", "변동성/공포 — 15↓ 안정, 20~30 불안, 30↑ 공포")
    if label == "달러지수":
        return g("warn" if v >= 105 else "ok" if v >= 100 else "good", "강달러(105↑)=신흥국·위험자산 부담, 약달러=우호")
    if label == "원/달러":
        return g("bad" if v >= 1450 else "warn" if v >= 1400 else "ok" if v >= 1250 else "good", "원화 약세(1400↑)=외국인 이탈 압력, 강세=우호")
    if label == "구리(Dr.)":
        return g("ok", "Dr.코퍼 — 경기 바로미터, 상승=경기 확장 신호")
    if label == "WTI 유가":
        return g("ok", "유가 — 급등=인플레 압력, 급락=수요 둔화 신호")
    if label == "금":
        return g("ok", "안전자산/실질금리 역행 — 급등=불안·완화 기대")
    return g("ok", "")


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
                    "delta": r1, "sub": (f"1M {'+' if (r20 or 0) >= 0 else ''}{r20}%" if r20 is not None else ""),
                    **macro_read(info["label"], round(last, 2))})
    return out


def macro_assessment(macro):
    """매크로 지표로 (1)위험선호 신호 (2)경기순환 국면(인베스트먼트 클락) 판정.
    미국 매크로가 글로벌 위험선호 앵커 → 한·미 공통 적용(한국은 원/달러 추가 참고)."""
    by = {m["label"]: m for m in macro}
    def dr(lab):
        m = by.get(lab)
        return m["delta"] if m and m.get("delta") is not None else 0
    def val(lab):
        m = by.get(lab)
        return m["value"] if m else None

    # (1) 위험선호 신호 — 각 지표의 '좋은 방향' 움직임 집계
    on, off = [], []
    for m in macro:
        d = m.get("delta")
        if d is None or d == 0:
            continue
        (on if (d > 0) == m["up_good"] else off).append(m["label"])
    net = len(on) - len(off)
    risk = {"score": max(0, min(100, round(50 + net * 4))), "net": net,
            "on": on, "off": off,
            "label": "위험선호(완화적)" if net >= 3 else ("위험회피(긴축적)" if net <= -3 else "중립"),
            "tone": "green" if net >= 3 else ("red" if net <= -3 else "yellow")}

    # (2) 경기순환 국면 — 성장축 × 물가축
    growth = 0
    for lab, sign in [("비농업고용", 1), ("실업률", -1), ("신규실업수당", -1), ("구리(Dr.)", 1), ("장단기차", 1)]:
        d = dr(lab)
        growth += sign * (1 if d > 0 else (-1 if d < 0 else 0))
    infl = 0
    for lab in ("CPI", "기대인플레", "WTI 유가"):
        d = dr(lab)
        infl += 1 if d > 0 else (-1 if d < 0 else 0)
    g_up, i_up = growth > 0, infl > 0
    # 국면 → (설명, 자산 포지션 스탠스, 선호/회피)
    PHASE = {
        ("회복"):       ("성장↑·물가↓ — 디스인플레 회복", "주식 비중확대",       "성장주·반도체·경기민감 ↑ / 현금·장기채 ↓"),
        ("확장·과열"):  ("성장↑·물가↑ — 경기 확장",       "주식 유지·경기민감 확대", "에너지·소재·금융·가치 ↑ / 장기채 ↓"),
        ("둔화·스태그"): ("성장↓·물가↑ — 둔화 압력",       "주식 축소·방어 전환",    "헬스케어·필수소비·현금·원자재 ↑ / 고밸류 성장주 ↓"),
        ("침체·디플레"): ("성장↓·물가↓ — 수축",            "주식 최소·방어 우선",    "국채·현금·배당방어주 ↑ / 경기민감·고베타 ↓"),
    }
    if g_up and not i_up:
        phase = "회복"
    elif g_up and i_up:
        phase = "확장·과열"
    elif (not g_up) and i_up:
        phase = "둔화·스태그"
    else:
        phase = "침체·디플레"
    desc, stance, prefer = PHASE[phase]
    flags = []
    curve, vix = val("장단기차"), val("VIX 변동성")
    if curve is not None and curve < 0:
        flags.append("장단기 금리 역전(침체 선행)")
    if vix is not None and vix >= 22:
        flags.append(f"VIX 경계({vix})")
    # 위험선호 신호·침체 플래그로 스탠스 보정
    if risk["net"] <= -3 or flags:
        stance += " · 방어 강화"
    elif risk["net"] >= 3 and phase in ("회복", "확장·과열"):
        stance += " · 위험자산 우호"
    cycle = {"phase": phase, "desc": desc, "stance": stance, "prefer": prefer,
             "growth": growth, "inflation": infl, "flags": flags}
    # 종합 한줄평 — 환경(위험선호)+국면+함의 한 문장
    risk_word = {"green": "완화적", "yellow": "중립적", "red": "긴축적"}[risk["tone"]]
    if phase in ("회복", "확장·과열"):
        lean = "위험자산 우호" if risk["net"] >= 0 else "위험자산 선별"
    else:
        lean = "위험자산 비우호(방어)"
    cau = (" · ⚠ " + ", ".join(f.split("(")[0] for f in flags)) if flags else ""
    summary = f"{risk_word} 환경 · {phase} 국면 → {lean}{cau}"
    return {"risk": risk, "cycle": cycle, "summary": summary}


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
                        "delta": delta, "up_good": up_good, "sub": asof,
                        **macro_read(label, val)})
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------
# 배당 포트폴리오 — 발굴 스크리너 + 매일 자동 모델 포트폴리오
# ----------------------------------------------------------------------
# 배당 유니버스 (ticker→(이름, 섹터)). 미국=ETF+배당주, 한국=고배당주.
DIV_UNIVERSE = {
    "US": {
        "SCHD": ("Schwab 미국배당 ETF", "ETF"), "VYM": ("뱅가드 고배당 ETF", "ETF"),
        "DGRO": ("배당성장 ETF", "ETF"), "VIG": ("배당성장주 ETF", "ETF"),
        "JEPI": ("JPM 프리미엄인컴", "ETF"), "JEPQ": ("JPM 나스닥인컴", "ETF"),
        "SPYD": ("S&P 고배당 ETF", "ETF"), "HDV": ("iShares 고배당 ETF", "ETF"),
        "DGRW": ("WisdomTree 배당성장 ETF", "ETF"), "NOBL": ("S&P 배당귀족 ETF", "ETF"),
        "DVY": ("iShares 셀렉트배당 ETF", "ETF"), "SCHY": ("Schwab 해외배당 ETF", "ETF"),
        "SPHD": ("S&P500 고배당 저변동 ETF", "ETF"), "FDVV": ("Fidelity 고배당 ETF", "ETF"),
        "QYLD": ("Global X 나스닥100 커버드콜", "커버드콜"), "XYLD": ("Global X S&P500 커버드콜", "커버드콜"),
        "RYLD": ("Global X 러셀2000 커버드콜", "커버드콜"), "DIVO": ("Amplify 인핸스드배당", "커버드콜"),
        "SPYI": ("NEOS S&P500 인컴", "커버드콜"), "QQQI": ("NEOS 나스닥100 인컴", "커버드콜"),
        "GPIX": ("Goldman S&P500 프리미엄인컴", "커버드콜"), "GPIQ": ("Goldman 나스닥100 프리미엄인컴", "커버드콜"),
        "FEPI": ("FANG+ 커버드콜", "커버드콜"), "QDTE": ("나스닥100 데일리 커버드콜", "커버드콜"),
        "XDTE": ("S&P500 데일리 커버드콜", "커버드콜"), "ISPY": ("ProShares S&P500 데일리 커버드콜", "커버드콜"),
        "JEPY": ("Defiance S&P500 옵션인컴", "커버드콜"), "BALI": ("iShares 고배당 커버드콜", "커버드콜"),
        "SPYT": ("Defiance S&P500 타겟인컴", "커버드콜"),
        "O": ("리얼티인컴", "리츠"), "MAIN": ("메인스트리트캐피탈", "금융"),
        "KO": ("코카콜라", "필수소비"), "PEP": ("펩시코", "필수소비"), "PG": ("P&G", "필수소비"),
        "JNJ": ("존슨앤존슨", "헬스케어"), "ABBV": ("애브비", "헬스케어"), "MRK": ("머크", "헬스케어"),
        "XOM": ("엑슨모빌", "에너지"), "CVX": ("셰브론", "에너지"),
        "MO": ("알트리아", "필수소비"), "PM": ("필립모리스", "필수소비"),
        "MMM": ("3M", "산업재"), "IBM": ("IBM", "기술"), "CSCO": ("시스코", "기술"),
        "TXN": ("텍사스인스트루먼트", "기술"), "AVGO": ("브로드컴", "기술"),
        "T": ("AT&T", "통신"), "VZ": ("버라이즌", "통신"),
        "MCD": ("맥도날드", "경기소비"), "HD": ("홈디포", "경기소비"), "PFE": ("화이자", "헬스케어"),
    },
    "KR": {
        # 배당 ETF (고배당·리츠·월배당 미국배당다우존스 계열)
        "279530": ("KODEX 고배당", "ETF"), "161510": ("PLUS 고배당주", "ETF"),
        "210780": ("TIGER 코스피고배당", "ETF"), "329200": ("TIGER 리츠부동산인프라", "ETF"),
        "446720": ("SOL 미국배당다우존스", "ETF"), "458730": ("TIGER 미국배당다우존스", "ETF"),
        "402970": ("ACE 미국배당다우존스", "ETF"),
        # 커버드콜(월배당·고분배)
        "458760": ("TIGER 미국배당+7%프리미엄다우존스", "커버드콜"),
        "441680": ("TIGER 미국나스닥100커버드콜", "커버드콜"),
        "483290": ("KODEX 미국배당커버드콜액티브", "커버드콜"),
        # 개별 고배당주
        "005935": ("삼성전자우", "기술"), "033780": ("KT&G", "필수소비"),
        "105560": ("KB금융", "금융"), "055550": ("신한지주", "금융"), "086790": ("하나금융", "금융"),
        "316140": ("우리금융", "금융"), "024110": ("기업은행", "금융"), "138040": ("메리츠금융", "금융"),
        "017670": ("SK텔레콤", "통신"), "030200": ("KT", "통신"),
        "000810": ("삼성화재", "금융"), "032830": ("삼성생명", "금융"), "005830": ("DB손해보험", "금융"),
        "088980": ("맥쿼리인프라", "인프라"), "005387": ("현대차2우B", "경기소비"),
        "000270": ("기아", "경기소비"), "005490": ("POSCO홀딩스", "소재"), "034730": ("SK", "지주"),
    },
}


# 자동 발굴: 배당/커버드콜/리츠/인컴류로 인식할 이름 키워드 / 제외 키워드
DIV_ETF_INC = ("배당", "고배당", "커버드콜", "커버드 콜", "프리미엄", "리츠", "부동산", "인프라",
               "인컴", "분배", "월배당", "타겟위클리", "데일리커버드")
DIV_ETF_CC = ("커버드콜", "커버드 콜", "프리미엄", "+7%", "+10%", "+12%", "+15%", "위클리", "데일리커버드", "타겟")
DIV_ETF_EX = ("레버리지", "인버스", "2X", "3X", "곱버스", "선물", "골드", "금선물", "국고채", "채권", "머니마켓", "CD금리", "SOFR")


def discover_kr_div_etfs(cap=120):
    """네이버 ETF 목록에서 배당·커버드콜·리츠·인컴류 ETF를 자동 발굴 → {code:(name,sector)}.
    레버리지·인버스·채권류는 제외. 실패 시 빈 dict(기존 큐레이션만 사용)."""
    try:
        import requests
        r = requests.get("https://finance.naver.com/api/sise/etfItemList.nhn",
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
                         timeout=20)
        items = r.json().get("result", {}).get("etfItemList", [])
    except Exception as e:
        print(f"[dividend] KR ETF 자동발굴 실패(기존 목록만 사용): {e}")
        return {}
    found = {}
    for it in items:
        code = str(it.get("itemcode", "")).zfill(6)
        nm = (it.get("itemname") or "").strip()
        if not code or not nm:
            continue
        if any(x in nm for x in DIV_ETF_EX):
            continue
        if not any(k in nm for k in DIV_ETF_INC):
            continue
        sec = "커버드콜" if any(k in nm for k in DIV_ETF_CC) else "ETF"
        found[code] = (nm, sec)
    if len(found) > cap:                     # 안전 상한(다운로드 폭주 방지)
        found = dict(list(found.items())[:cap])
    print(f"[dividend] KR ETF 자동발굴 {len(found)}종(배당·커버드콜·리츠·인컴)")
    return found


def _div_metrics(close, divs):
    """가격·배당 시계열로 배당 지표 산출 → dict 또는 None."""
    import pandas as pd
    if close is None or len(close) == 0:
        return None
    close = close.dropna()
    price = float(close.iloc[-1]) if len(close) else None
    if not price:
        return None
    d = divs.dropna() if divs is not None else None
    d = d[d > 0] if d is not None else None
    if d is None or len(d) == 0:
        return None
    idx = pd.to_datetime(d.index)
    last_dt = idx.max()
    ttm = float(d[idx >= (last_dt - pd.Timedelta(days=365))].sum())
    dy = round(ttm / price * 100, 2) if price else None
    # 연도별 배당 합계
    by_year = {}
    for dt_, v in zip(idx, d.values):
        by_year[dt_.year] = by_year.get(dt_.year, 0.0) + float(v)
    years = sorted(by_year)
    full = years[:-1] if years else []           # 마지막 해는 미완성일 수 있어 제외
    cagr = streak = None
    if len(full) >= 2:
        a, b = by_year[full[0]], by_year[full[-1]]
        span = full[-1] - full[0]
        if a > 0 and span > 0:
            cagr = round(((b / a) ** (1 / span) - 1) * 100, 1)
        s = 0
        for i in range(len(full) - 1, 0, -1):
            if by_year[full[i]] >= by_year[full[i - 1]] * 0.999:
                s += 1
            else:
                break
        streak = s
    # 최근 12개월 지급 횟수·지급월
    recent = [(dt_, v) for dt_, v in zip(idx, d.values) if dt_ >= (last_dt - pd.Timedelta(days=365))]
    freq = len(recent)
    months = sorted({dt_.month for dt_, _ in recent})
    # 추세(가격) 수익률
    def _ret(n):
        return round((price / float(close.iloc[-1 - n]) - 1) * 100, 1) if len(close) > n else None
    return {"price": round(price, 2), "yield": dy, "cagr": cagr, "streak": streak,
            "freq": freq, "months": months, "ex_date": last_dt.strftime("%Y-%m-%d"), "ttm_div": round(ttm, 4),
            "ret1d": _ret(1), "ret7d": _ret(5), "ret1m": _ret(21), "ret3m": _ret(63), "ret6m": _ret(126), "ret1y": _ret(252)}


def _zscores(vals):
    xs = [v for v in vals if v is not None]
    if len(xs) < 2:
        return {i: 0.0 for i in range(len(vals))}
    import statistics
    mu = statistics.mean(xs)
    sd = statistics.pstdev(xs) or 1.0
    return {i: ((v - mu) / sd if v is not None else -0.5) for i, v in enumerate(vals)}


# 섹터 성격(레짐 틸트용)
DEF_SECTORS = {"필수소비", "통신", "유틸", "헬스케어", "리츠", "인프라", "ETF"}
CYC_SECTORS = {"경기소비", "소재", "에너지", "기술", "산업재", "금융", "지주"}


def _div_tilt(mac):
    """모멘텀 대시보드의 매크로 레짐 → 배당 포트 성향(성장/중립/방어) + 점수 가중·섹터 보너스."""
    if not mac:
        return {"tilt": "중립", "reason": "레짐 정보 없음", "w": (0.40, 0.35, 0.25), "def_bonus": 0.0, "cyc_bonus": 0.0}
    cyc = mac["cycle"]["phase"]
    net = mac["risk"]["net"]
    flags = mac["cycle"].get("flags") or []
    if cyc in ("회복", "확장·과열") and net >= 0 and not flags:
        return {"tilt": "성장", "reason": f"{cyc}·위험선호 → 배당성장주·경기민감 비중↑",
                "w": (0.30, 0.45, 0.25), "def_bonus": 0.0, "cyc_bonus": 0.25}
    if cyc in ("둔화·스태그", "침체·디플레") or net <= -2 or flags:
        tail = " · 침체신호" if flags else ""
        return {"tilt": "방어", "reason": f"{cyc}{tail} → 고배당·방어주 비중↑",
                "w": (0.45, 0.25, 0.30), "def_bonus": 0.30, "cyc_bonus": -0.20}
    return {"tilt": "중립", "reason": f"{cyc} → 수익률·성장 균형", "w": (0.40, 0.35, 0.25), "def_bonus": 0.0, "cyc_bonus": 0.0}


def _cash_pct(tilt, mac, season_weak):
    """레짐(성향·침체신호) + 계절성으로 현금 보유 비중(%) 산출. 방어·약세계절↑, 성장·강세계절↓."""
    base = {"성장": 4, "중립": 12, "방어": 25}.get(tilt["tilt"], 12)
    why = [tilt["tilt"]]
    if mac and (mac["cycle"].get("flags")):
        base += 6
        why.append("침체신호")
    if season_weak:
        base += 6
        why.append("약세 계절(5~10월)")
    else:
        base -= 3
        why.append("강세 계절")
    cash = max(0, min(40, base))
    return cash, " · ".join(why)


# ----------------------------------------------------------------------
# 영구 보유 장부(book): 배당주는 오래 들고 가므로 매일 새로 뽑지 않고
# 편입/편출만 판단(히스테리시스). reports/dividend_book.json 에 영구 저장.
# ----------------------------------------------------------------------
DIV_BOOK_FILE = os.path.join(REPORT_DIR, "dividend_book.json")
DIV_TARGET = {"US": 12, "KR": 12, "ALL": 14}   # 시장별 목표 보유 종목 수
DIV_EXIT_BUF = 6        # 랭크가 (목표+이 값) 밖으로 밀리면 편출 후보(완충구간)
DIV_MIN_HOLD = 30       # 최소 보유(달력일) — 배당컷 외엔 그 전엔 매도 금지
DIV_MAX_TURN = 1        # 하루 최대 교체(편입) 종목 수 → 점진적 회전
DIV_SEC_CAP = 3         # 섹터 최대 종목 수
DIV_MK_CAP = 9          # (통합) 한 시장 최대 종목 수
DIV_CC_CAP = 1          # 커버드콜 최대 종목 수(NAV 침식·분배 왜곡 쏠림 방지)


def select_book(ranked, current, bar_date, N, mk_cap=None):
    """랭킹된 후보(ranked)와 기존 장부(current)로 오늘의 보유를 결정.
    반환: (picks=보유종목+편입정보, new_book=저장용 장부, changes={buys,sells})."""
    rk = {r["code"]: i for i, r in enumerate(ranked)}
    info = {r["code"]: r for r in ranked}
    today = pd.Timestamp(bar_date)

    def held_days(h):
        try:
            return (today - pd.Timestamp(h["entry_date"])).days
        except Exception:
            return 9999

    def counts(lst):
        sc, mc = {}, {}
        for h in lst:
            r = info.get(h["code"])
            s = r["sector"] if r else h.get("sector")
            m = r["market"] if r else h.get("market")
            sc[s] = sc.get(s, 0) + 1
            mc[m] = mc.get(m, 0) + 1
        return sc, mc

    def cap_ok(sc, mc, r):
        if sc.get(r["sector"], 0) >= DIV_SEC_CAP:
            return False
        if r["sector"] == "커버드콜" and sc.get("커버드콜", 0) >= DIV_CC_CAP:
            return False                                    # 커버드콜 전용 상한(섹터캡보다 엄격)
        if mk_cap and mc.get(r["market"], 0) >= mk_cap:
            return False
        return True

    # 1) 강제 편출: 유니버스 이탈 / 배당 끊김(배당컷)
    kept, sells, buys = [], [], []
    for h in current:
        r = info.get(h["code"])
        if r is None or not r.get("yield"):
            sells.append({"code": h["code"], "name": h.get("name"),
                          "market": h.get("market"), "why": "배당 끊김·유니버스 이탈"})
        else:
            kept.append(dict(h))

    # 2) 재량 스왑(회전 캡): 가장 밀린 보유 ↔ top-N 상위 신규 후보
    turn = 0
    while turn < DIV_MAX_TURN:
        held = {h["code"] for h in kept}
        elig = [h for h in kept
                if rk.get(h["code"], 9999) > (N - 1 + DIV_EXIT_BUF) and held_days(h) >= DIV_MIN_HOLD]
        if not elig:
            break
        weak = max(elig, key=lambda h: rk.get(h["code"], 9999))
        sc, mc = counts([h for h in kept if h["code"] != weak["code"]])
        cand = None
        for r in ranked:
            if rk[r["code"]] >= N:            # 신규 편입은 top-N 안에서만(편입>편출 기준 → 회전 억제)
                break
            if r["code"] in held or not r.get("yield") or not cap_ok(sc, mc, r):
                continue
            cand = r
            break
        if not cand or rk[cand["code"]] >= rk.get(weak["code"], 9999):
            break
        kept = [h for h in kept if h["code"] != weak["code"]]
        sells.append({"code": weak["code"], "name": weak.get("name"),
                      "market": weak.get("market"), "why": f"랭크 {rk.get(weak['code'], 0) + 1}위로 밀림"})
        ne = {"code": cand["code"], "name": cand["name"], "market": cand["market"],
              "sector": cand["sector"], "entry_date": bar_date, "entry_price": cand.get("price")}
        kept.append(ne)
        buys.append(ne)
        turn += 1

    # 3) 빈 슬롯 채우기(강제편출·부족분) — 상위 후보부터, 섹터/시장 캡 준수
    if len(kept) < N:
        held = {h["code"] for h in kept}
        sc, mc = counts(kept)
        for r in ranked:
            if len(kept) >= N:
                break
            if r["code"] in held or not r.get("yield") or not cap_ok(sc, mc, r):
                continue
            ne = {"code": r["code"], "name": r["name"], "market": r["market"],
                  "sector": r["sector"], "entry_date": bar_date, "entry_price": r.get("price")}
            kept.append(ne)
            buys.append(ne)
            sc[r["sector"]] = sc.get(r["sector"], 0) + 1
            mc[r["market"]] = mc.get(r["market"], 0) + 1
            held.add(r["code"])

    # 4) 초과분 정리(만일 N 초과면 랭크 나쁜 것부터 컷)
    kept.sort(key=lambda h: rk.get(h["code"], 9999))
    if len(kept) > N:
        for h in kept[N:]:
            sells.append({"code": h["code"], "name": h.get("name"),
                          "market": h.get("market"), "why": "슬롯 정리"})
        kept = kept[:N]

    # 5) picks(랭킹 정보 + 편입일·보유일·편입후 수익률), 저장용 장부, 변경내역
    picks, new_book = [], []
    for h in kept:
        r = dict(info[h["code"]])
        ep = h.get("entry_price")
        cp = r.get("price")
        if ep is None:
            ep = cp
        r["entry_date"] = h.get("entry_date", bar_date)
        r["held_days"] = held_days({"entry_date": r["entry_date"]})
        r["ret_since"] = round((cp / ep - 1) * 100, 1) if (ep and cp and ep > 0) else None
        picks.append(r)
        new_book.append({"code": r["code"], "name": r.get("name"), "market": r["market"],
                         "sector": r["sector"], "entry_date": r["entry_date"], "entry_price": ep})
    changes = {"buys": [{"code": b["code"], "name": b.get("name"), "market": b.get("market")} for b in buys],
               "sells": sells}
    return picks, new_book, changes


def build_dividend_market(mkt, data, mac=None, season_weak=False, book=None, bar_date=None, universe=None):
    """한 시장의 배당 스크리너(랭킹) + 모델 포트폴리오. mac=매크로 레짐(틸트 반영)."""
    uni = (universe or DIV_UNIVERSE).get(mkt, {})
    rows = []
    for code, (name, sector) in uni.items():
        sym = f"{code}.KS" if mkt == "KR" else code
        df = ohlc_for(data, sym)
        if df is None or len(df["Close"].dropna()) < 120:      # 상장 6개월 미만 등 이력 부족은 제외(불안정)
            continue
        divs = df["Dividends"] if "Dividends" in df.columns else None
        m = _div_metrics(df["Close"], divs)
        if not m or not m.get("yield") or not m.get("freq"):    # 최근 1년 실제 분배(배당) 있는 것만
            continue
        m.update({"code": code, "name": name, "sector": sector, "market": mkt})
        rows.append(m)
    if not rows:
        return None
    # 레짐 틸트: 점수 가중·섹터 보너스를 매크로 국면에 맞춰 조정
    tilt = _div_tilt(mac)
    wy, wg, ws = tilt["w"]
    zy = _zscores([r["yield"] for r in rows])
    zg = _zscores([r["cagr"] for r in rows])
    zs = _zscores([r["streak"] for r in rows])
    for i, r in enumerate(rows):
        trap = -0.8 if (r["yield"] or 0) > 12 else 0.0          # 12%↑ 초고배당 경계
        sec_b = tilt["def_bonus"] if r["sector"] in DEF_SECTORS else (tilt["cyc_bonus"] if r["sector"] in CYC_SECTORS else 0.0)
        r["score"] = round(zy[i] * wy + zg[i] * wg + zs[i] * ws + trap + sec_b, 2)
    rows.sort(key=lambda r: r["score"], reverse=True)
    # 모델 포트폴리오: 영구 장부 기반 편입/편출(오래 보유) — 현금 비중은 레짐·계절로 동적
    book = {} if book is None else book
    picks, new_book, changes = select_book(rows, book.get(mkt, []), bar_date, DIV_TARGET[mkt])
    book[mkt] = new_book
    if not picks:
        return None
    cash, cash_why = _cash_pct(tilt, mac, season_weak)
    port = _assemble_portfolio(picks, tilt, mac, cash, cash_why)
    port["changes"] = changes
    return {"stocks": rows, "portfolio": port}


def _assemble_portfolio(picks, tilt, mac=None, cash=0, cash_why=""):
    """선정 종목 → 가중치(주식분 = 100−현금%)·포트수익률·월별분포·섹터/시장 비중·현금 비중."""
    inv = max(0, 100 - cash)
    lo = min(x["score"] for x in picks)
    base = [max(0.1, p["score"] - lo + 0.5) for p in picks]
    tot = sum(base) or 1.0
    weights = [min(0.14, max(0.04, w / tot)) for w in base]
    wsum = sum(weights) or 1.0
    weights = [round(w / wsum * inv, 1) for w in weights]          # 합계 ≈ 주식분(inv)
    pyld = round(sum(w / 100 * (p["yield"] or 0) for w, p in zip(weights, picks)), 2)   # 현금 드래그 반영
    eq = sum(weights) or 1.0
    pyld_eq = round(sum(w / 100 * (p["yield"] or 0) for w, p in zip(weights, picks)) / (eq / 100), 2)  # 주식분만
    pcagr = round(sum(w / eq * (p["cagr"] or 0) for w, p in zip(weights, picks)), 1)
    def _prw(key):                                                                          # 주식분 가중 기간 가격수익
        return round(sum(w / eq * (p.get(key) or 0) for w, p in zip(weights, picks)), 1)
    pr7d, pr1m, pr3m, pr6m, pr1y = _prw("ret7d"), _prw("ret1m"), _prw("ret3m"), _prw("ret6m"), _prw("ret1y")
    rets = {"7D": pr7d, "1M": pr1m, "3M": pr3m, "6M": pr6m, "1Y": pr1y}
    total1y = round(pr1y * inv / 100 + pyld, 1)                                             # 총수익 ≈ 가격(주식분)+배당
    monthly = [0.0] * 12
    for w, p in zip(weights, picks):
        ann = w / 100 * (p["yield"] or 0)
        ms = p["months"] or list(range(1, 13))
        for mo in ms:
            monthly[mo - 1] += ann / len(ms)
    monthly = [round(x, 3) for x in monthly]
    sec_w, mk_w = {}, {}
    for w, p in zip(weights, picks):
        sec_w[p["sector"]] = round(sec_w.get(p["sector"], 0) + w, 1)
        mk_w[p["market"]] = round(mk_w.get(p["market"], 0) + w, 1)
    holdings = [{**p, "weight": w} for p, w in zip(picks, weights)]
    return {"holdings": holdings, "yield": pyld, "yield_eq": pyld_eq, "cagr": pcagr, "monthly": monthly,
            "ret1y": pr1y, "ret3m": pr3m, "rets": rets, "total1y": total1y,
            "sectors": sec_w, "markets": mk_w, "n": len(holdings),
            "cash": cash, "invested": round(inv, 1), "cash_why": cash_why,
            "tilt": tilt["tilt"], "tilt_reason": tilt["reason"],
            "regime": (mac["cycle"]["phase"] if mac else None),
            "regime_signal": (mac["risk"]["label"] if mac else None)}


def build_dividend_integrated(per_market, mac=None, season_weak=False, book=None, bar_date=None):
    """미국+한국 통합 모델 포트폴리오 — 양 시장 후보를 한 풀로 합쳐 공정 비교·구성."""
    pool = []
    for mk in ("US", "KR"):
        if mk in per_market:
            pool += per_market[mk]["stocks"]
    if not pool:
        return None
    tilt = _div_tilt(mac)
    wy, wg, ws = tilt["w"]
    zy = _zscores([r["yield"] for r in pool])
    zg = _zscores([r["cagr"] for r in pool])
    zs = _zscores([r["streak"] for r in pool])
    ranked = []
    for i, r in enumerate(pool):
        trap = -0.8 if (r["yield"] or 0) > 12 else 0.0
        sec_b = tilt["def_bonus"] if r["sector"] in DEF_SECTORS else (tilt["cyc_bonus"] if r["sector"] in CYC_SECTORS else 0.0)
        rr = dict(r)
        rr["score"] = round(zy[i] * wy + zg[i] * wg + zs[i] * ws + trap + sec_b, 2)
        ranked.append(rr)
    ranked.sort(key=lambda r: r["score"], reverse=True)
    book = {} if book is None else book
    picks, new_book, changes = select_book(ranked, book.get("ALL", []), bar_date,
                                           DIV_TARGET["ALL"], mk_cap=DIV_MK_CAP)
    book["ALL"] = new_book
    if not picks:
        return None
    cash, cash_why = _cash_pct(tilt, mac, season_weak)
    port = _assemble_portfolio(picks, tilt, mac, cash, cash_why)
    port["changes"] = changes
    return {"stocks": ranked, "portfolio": port}


def build_dividends(data, mac=None, season_weak=False, bar_date=None, universe=None):
    """미국·한국 + 통합 배당 스크리너/모델 포트폴리오. data엔 배당(actions) 포함.
    영구 장부(dividend_book.json)로 매일 편입/편출만 판단(오래 보유)."""
    try:
        with open(DIV_BOOK_FILE, encoding="utf-8") as f:
            book = json.load(f)
    except Exception:
        book = {}
    out = {}
    for mkt in ("US", "KR"):
        try:
            d = build_dividend_market(mkt, data, mac, season_weak, book, bar_date, universe)
            if d:
                out[mkt] = d
        except Exception as e:
            print(f"[dividend] {mkt} 실패: {e}")
    try:
        allp = build_dividend_integrated(out, mac, season_weak, book, bar_date)
        if allp:
            out["ALL"] = allp
    except Exception as e:
        print(f"[dividend] 통합 실패: {e}")
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(DIV_BOOK_FILE, "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False)
        chg = {mk: (len(out[mk]["portfolio"].get("changes", {}).get("buys", [])),
                    len(out[mk]["portfolio"].get("changes", {}).get("sells", [])))
               for mk in out}
        print(f"[dividend] 장부 저장 → {DIV_BOOK_FILE} · 오늘 편입/편출 {chg}")
    except Exception as e:
        print(f"[dividend] 장부 저장 실패: {e}")
    return out


NAV_FILE = os.path.join(REPORT_DIR, "dividend_nav.json")


def _hold_sym(h):
    return f'{h["code"]}.KS' if h.get("market") == "KR" else h["code"]


def update_dividend_nav(dividends, data, bar_date):
    """모델 포트폴리오의 '가상 운용' 누적성과를 매일 스냅샷으로 적립.
    - 전일 스냅샷의 보유종목·비중으로 당일까지의 가격수익을 계산해 NAV(가격지수)를 복리 갱신.
    - tr_nav(총수익지수)는 가격수익 + 배당 적립(전일 포트 수익률 × 경과일/365)으로 갱신.
    - 같은 날 재실행은 멱등(중복 적립 안 함). reports/dividend_nav.json 에 영구 저장(워크플로 커밋).
    결과 곡선·누적수익률을 각 포트폴리오 dict에 부착해 대시보드에서 차트로 표시."""
    try:
        with open(NAV_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {}
    hist.setdefault("inception", bar_date)
    mkts = hist.setdefault("markets", {})
    today_ts = pd.Timestamp(bar_date)
    for mk in ("ALL", "US", "KR"):
        port = (dividends.get(mk) or {}).get("portfolio")
        if not port or not port.get("holdings"):
            continue
        rec = mkts.get(mk)
        if rec is None:                                          # 최초 → 기준 100 으로 출발
            rec = {"nav": 100.0, "tr_nav": 100.0,
                   "history": [{"date": bar_date, "nav": 100.0, "tr_nav": 100.0}]}
        else:
            last_date = rec["history"][-1]["date"] if rec.get("history") else None
            if last_date != bar_date and rec.get("holdings"):    # 신규 거래일만 적립
                prev_ts = pd.Timestamp(last_date)
                days = max(1, (today_ts - prev_ts).days)
                pr = 0.0
                for h in rec["holdings"]:
                    df = ohlc_for(data, _hold_sym(h))
                    if df is None:
                        continue
                    c = df["Close"]
                    cp = c.asof(prev_ts)
                    cn = float(c.iloc[-1])
                    if cp is not None and not pd.isna(cp) and float(cp) > 0:
                        pr += (h["weight"] / 100.0) * (cn / float(cp) - 1.0)
                div_acc = (rec.get("yield", 0) / 100.0) * (days / 365.0)
                rec["nav"] = round(rec["nav"] * (1 + pr), 4)
                rec["tr_nav"] = round(rec["tr_nav"] * (1 + pr + div_acc), 4)
                rec["history"].append({"date": bar_date, "nav": rec["nav"], "tr_nav": rec["tr_nav"]})
        # 다음 적립을 위해 당일 목표 보유·수익률 저장
        rec["holdings"] = [{"code": h["code"], "market": h.get("market", mk), "weight": h["weight"]}
                           for h in port["holdings"]]
        rec["yield"] = port.get("yield", 0)
        rec["history"] = rec["history"][-400:]
        mkts[mk] = rec
        first = rec["history"][0]
        port["nav_curve"] = rec["history"]
        port["since"] = first["date"]
        port["days_tracked"] = len(rec["history"])
        port["cum_price"] = round((rec["nav"] / first["nav"] - 1) * 100, 1)
        port["cum_total"] = round((rec["tr_nav"] / first["tr_nav"] - 1) * 100, 1)
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(NAV_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
        print(f"[dividend] NAV 적립 → {NAV_FILE} (inception {hist['inception']})")
    except Exception as e:
        print(f"[dividend] NAV 저장 실패: {e}")


# ----------------------------------------------------------------------
# 레버리지 ETF(불/베어 쌍 + 개별주) — 트레이딩용 모멘텀·기술 지표
# ----------------------------------------------------------------------
# (테마, 시장, 배율, 불코드, 불이름, 베어코드, 베어이름)
LEV_PAIRS = [
    ("반도체", "US", 3, "SOXL", "Direxion 반도체 불3x", "SOXS", "반도체 베어3x"),
    ("나스닥100", "US", 3, "TQQQ", "ProShares 나스닥100 3x", "SQQQ", "나스닥100 베어3x"),
    ("금융", "US", 3, "FAS", "금융 3x", "FAZ", "금융 베어3x"),
    ("바이오", "US", 3, "LABU", "바이오 3x", "LABD", "바이오 베어3x"),
    ("에너지", "US", 2, "ERX", "에너지 2x", "ERY", "에너지 베어2x"),
    ("금광", "US", 2, "NUGT", "금광 2x", "DUST", "금광 베어2x"),
    ("중국", "US", 3, "YINN", "중국 3x", "YANG", "중국 베어3x"),
    ("미국채 20y+", "US", 3, "TMF", "장기국채 3x", "TMV", "장기국채 베어3x"),
    ("부동산", "US", 3, "DRN", "부동산 3x", "DRV", "부동산 베어3x"),
    ("지역은행", "US", 3, "DPST", "지역은행 3x", None, None),
    ("유틸리티", "US", 3, "UTSL", "유틸 3x", None, None),
    ("방산·우주", "US", 3, "DFEN", "방산 3x", None, None),
    ("주택건설", "US", 3, "NAIL", "주택건설 3x", None, None),
    ("소매", "US", 3, "RETL", "소매 3x", None, None),
    ("코스피200", "KR", 2, "122630", "KODEX 레버리지", "252670", "KODEX 200선물인버스2X"),
    ("코스닥150", "KR", 2, "233740", "KODEX 코스닥150레버리지", "251340", "KODEX 코스닥150선물인버스2X"),
    ("비트코인", "CRYPTO", 2, "BITU", "ProShares 비트코인 2x", "SBIT", "비트코인 -2x"),
    ("이더리움", "CRYPTO", 2, "ETHU", "ProShares 이더 2x", "ETHD", "이더 -2x"),
]
# 개별주 레버리지(2x 롱) — (이름, 시장, 배율, 코드)
LEV_SINGLES = [
    ("테슬라 2x", "US", 2, "TSLL"), ("엔비디아 2x", "US", 2, "NVDL"), ("코인베이스 2x", "US", 2, "CONL"),
    ("MSTR 2x", "US", 2, "MSTX"), ("애플 2x", "US", 2, "AAPU"), ("AMD 2x", "US", 2, "AMDL"),
    ("삼성전자 2x(KODEX)", "KR", 2, "0193W0"), ("SK하이닉스 2x(KODEX)", "KR", 2, "0193T0"),
]


def _lev_metrics(df):
    """레버리지 ETF 1종의 추세·모멘텀 지표(가격·기간수익·RSI·20MA이격·추세·신고가이격)."""
    close = df["Close"].dropna()
    if len(close) < 60:
        return None
    price = float(close.iloc[-1])
    if not price:
        return None
    ma = lambda n: float(close.rolling(n).mean().iloc[-1]) if len(close) >= n else None
    ma20, ma60 = ma(20), ma(60)
    r = lambda n: round((price / float(close.iloc[-1 - n]) - 1) * 100, 1) if len(close) > n else None
    hi = float(close.iloc[-252:].max())
    rsi_v = round(float(rsi(close).iloc[-1]), 0)
    dist20 = round((price / ma20 - 1) * 100, 1) if ma20 else None
    if ma20 and ma60:
        trend = "상승추세" if price > ma20 > ma60 else ("하락추세" if price < ma20 < ma60 else "횡보")
    else:
        trend = "—"
    return {"price": round(price, 2), "ret1d": r(1), "ret1w": r(5), "ret1m": r(21), "ret3m": r(63),
            "rsi": rsi_v, "dist20": dist20, "trend": trend, "high52": round((price / hi - 1) * 100, 1)}


def build_leverage(data):
    """레버리지 ETF 불/베어 쌍 + 개별주 → 지표 부착. data는 가격 다운로드 결과."""
    def rec(code, market):
        sym = f"{code}.KS" if market == "KR" else code
        df = ohlc_for(data, sym)
        return _lev_metrics(df) if df is not None else None

    pairs = []
    for theme, mk, x, bc, bn, rc_, rn in LEV_PAIRS:
        b = rec(bc, mk)
        s = rec(rc_, mk) if rc_ else None       # 베어 쌍 없는 3x(불 단독)는 None
        if not b and not s:
            continue
        pairs.append({"theme": theme, "market": mk, "x": x,
                      "bull": ({**b, "code": bc, "name": bn} if b else None),
                      "bear": ({**s, "code": rc_, "name": rn} if s else None)})
    singles = []
    for nm, mk, x, code in LEV_SINGLES:
        m = rec(code, mk)
        if m:
            singles.append({**m, "code": code, "name": nm, "market": mk, "x": x})
    print(f"[leverage] 쌍 {len(pairs)}테마 · 개별주 {len(singles)}종")
    return {"pairs": pairs, "singles": singles}


# ---- 4시간봉 스윙 시그널(상승시작·풀백·하락전환/CHoCH) ------------------------
LEV_SIG_FILE = os.path.join(REPORT_DIR, "lev_signals.json")


def _resample_4h(df1h):
    """1시간봉 OHLC → 4시간봉으로 합성. 480MA 계산 위해 충분한 봉이 있어야 함."""
    if df1h is None or len(df1h) == 0:
        return None
    d = df1h.dropna(subset=["Close"])
    try:
        out = pd.DataFrame({
            "Open": d["Open"].resample("4h").first(),
            "High": d["High"].resample("4h").max(),
            "Low": d["Low"].resample("4h").min(),
            "Close": d["Close"].resample("4h").last(),
        }).dropna()
    except Exception:
        return None
    return out if len(out) >= 60 else None


def lev_signals(df4):
    """4h OHLC로 시그널 검출 — '당일(가장 최근 거래일) 4h봉 중 하나라도' 조건 충족 시.
    (하루 1배치라 봉 사이 크로스가 깜빡일 수 있어 당일 발생 여부로 판정)
    · 상승시작: 역배열에서 240선을 당일 상향돌파
    · 풀백:     60선이 당일 어느 봉의 범위 안(저가≤60≤고가)에 들어옴 = '터치'
    · 하락전환: 직전 스윙 전저점을 당일 하향돌파(CHoCH)  · RSI 상승 다이버전스면 '상승다이버전스'"""
    c, low, high = df4["Close"], df4["Low"], df4["High"]
    n = len(c)
    ma = lambda k: c.rolling(k).mean()
    m60, m120, m240, m480 = ma(60), ma(120), ma(240), ma(480)
    rv = rsi(c)
    idx = [str(t) for t in df4.index]
    dates = [t.split(" ")[0].split("T")[0] for t in idx]   # 봉의 날짜 부분
    today = dates[-1]

    def align(i):
        v = [m60.iloc[i], m120.iloc[i], m240.iloc[i], m480.iloc[i]]
        if any(pd.isna(x) for x in v):
            return "?"
        if v[0] < v[1] < v[2] < v[3]:
            return "역배열"
        if v[0] > v[1] > v[2] > v[3]:
            return "정배열"
        return "혼조"

    kk = 3
    lv = low.values
    piv = [(j, lv[j]) for j in range(kk, n - kk) if lv[j] == lv[j - kk:j + kk + 1].min()]

    def prior_sl(i):
        best = None
        for j, v in piv:
            if j < i - kk:
                best = (j, v)
            else:
                break
        return best

    tset = set()
    for i in range(1, n):
        if dates[i] != today:                  # 당일 봉만
            continue
        if not pd.isna(m240.iloc[i]) and align(i - 1) == "역배열" \
                and c.iloc[i] > m240.iloc[i] and c.iloc[i - 1] <= m240.iloc[i - 1]:
            tset.add("상승시작")
        if not pd.isna(m60.iloc[i]) and low.iloc[i] <= m60.iloc[i] <= high.iloc[i]:
            tset.add("풀백")                    # 60선이 봉 범위 안 = 당일 터치
        sl = prior_sl(i)
        if sl is not None and c.iloc[i] < sl[1] and c.iloc[i - 1] >= sl[1]:
            j = sl[0]
            if not pd.isna(rv.iloc[i]) and not pd.isna(rv.iloc[j]) and rv.iloc[i] > rv.iloc[j]:
                tset.add("상승다이버전스")
            else:
                tset.add("하락전환")
    order = ["상승시작", "풀백", "하락전환", "상승다이버전스"]
    types_today = [t for t in order if t in tset]

    last = n - 1
    vs = lambda m: ("위" if c.iloc[-1] > m.iloc[last] else "아래") if not pd.isna(m.iloc[last]) else "?"
    return {"today": today, "types_today": types_today,
            "state": {"align": align(last), "vs240": vs(m240), "vs60": vs(m60)}}


def _lev_chart(d4, cap=150):
    """4h 캔들 차트용 시계열(최근 cap봉) — 캔들 + 60·120·240·480 MA + RSI(14)."""
    c = d4["Close"]
    ma = lambda k: c.rolling(k).mean()
    m60, m120, m240, m480, m960, rv = ma(60), ma(120), ma(240), ma(480), ma(960), rsi(c)
    sl = slice(-cap, None)

    def arr(s):
        return [None if pd.isna(x) else round(float(x), 3) for x in s.iloc[sl]]

    bars = [{"o": round(float(o), 3), "h": round(float(h), 3), "l": round(float(l), 3), "c": round(float(cl), 3)}
            for o, h, l, cl in zip(d4["Open"].iloc[sl], d4["High"].iloc[sl], d4["Low"].iloc[sl], c.iloc[sl])]
    return {"bars": bars, "ma60s": arr(m60), "ma120s": arr(m120), "ma240s": arr(m240),
            "ma480s": arr(m480), "ma960s": arr(m960), "rsis": arr(rv)}


def build_lev_signals(data1h):
    """레버리지 전 종목 4h 시그널(롱만) + 클릭용 4h 차트(전 종목). 상태파일로 중복 알림 방지."""
    try:
        with open(LEV_SIG_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    allt = []
    for _t, mk, _x, bc, bn, rc_, rn in LEV_PAIRS:
        allt.append((bc, bn, mk, True))          # 불=롱(시그널 O)
        if rc_:
            allt.append((rc_, rn, mk, False))    # 베어=시그널 X, 차트만
    allt += [(code, nm, mk, True) for nm, mk, _x, code in LEV_SINGLES]
    out = {"US": [], "KR": [], "CRYPTO": []}
    fresh_all, charts = [], {}
    for code, name, mk, is_long in allt:
        sym = f"{code}.KS" if mk == "KR" else code
        d4 = _resample_4h(ohlc_for(data1h, sym))
        if d4 is None:
            continue
        charts[code] = _lev_chart(d4)
        if not is_long:
            continue
        r = lev_signals(d4)
        prev = st.get(code) if isinstance(st.get(code), dict) else {}
        already = prev.get("types", []) if prev.get("date") == r["today"] else []
        new_types = [t for t in r["types_today"] if t not in already]   # 당일 이미 알린 건 제외(재실행 중복 방지)
        st[code] = {"date": r["today"], "types": r["types_today"]}
        out.setdefault(mk, []).append({"code": code, "name": name, "market": mk,
                                       "state": r["state"], "sig": r["types_today"]})   # 표: 당일 신호 전부
        for typ in new_types:                                          # 패널·텔레그램: 새로 뜬 것만
            fresh_all.append({"code": code, "name": name, "market": mk, "type": typ})
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(LEV_SIG_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        print(f"[lev-sig] state 저장 실패: {e}")
    tot = sum(len(v) for v in out.values())
    print(f"[lev-sig] fresh 시그널 {len(fresh_all)}건 · 상태 {tot}종 · 차트 {len(charts)}종")
    return {**out, "fresh": fresh_all, "charts": charts}


def lev_signal_text(fresh):
    """텔레그램용 4h 시그널 요약."""
    ico = {"상승시작": "🟢 상승 시작(240선 돌파)", "풀백": "🔵 풀백 타점(60선 터치)",
           "하락전환": "🔴 하락 전환(전저점 붕괴)", "상승다이버전스": "🟣 상승 다이버전스(전저점 붕괴+RSI 반등)"}
    lines = ["", "⚡ <b>4시간봉 레버리지 시그널</b>"]
    for f in fresh:
        flag = "🟢" if f["market"] == "US" else "🔵"
        lines.append(f"{ico.get(f['type'], f['type'])} — {flag}{f['name']}({f['code']})")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# ETF 모멘텀 TOP10 (테마포착) — 광역 ETF 유니버스 리스크조정 모멘텀 스캔
#  · 스코어: 15영업일 수익률 ÷ 15일 변동성 → 크로스섹션 Z → 비대칭 매핑(1+Z / 1/(1-Z))
#  · 운용: 상위 10종목 동일비중(10%) · 14영업일 리밸런싱 · -10% 로스컷 즉시 교체
#          · 손실 종목 1회 보유 연장 · 로스컷 매수분은 첫 리밸런싱까지 무조건 보유
# ----------------------------------------------------------------------
ETFMOM_BOOK_FILE = os.path.join(REPORT_DIR, "etfmom_book.json")
ETFMOM_NAV_FILE = os.path.join(REPORT_DIR, "etfmom_nav.json")
ETFMOM_N = 10           # 보유 종목 수(동일비중)
ETFMOM_REB_BD = 14      # 리밸런싱 주기(영업일)
ETFMOM_OBS = 15         # 모멘텀 관측 기간(영업일)
ETFMOM_LOSSCUT = -10.0  # 로스컷(%)

# 미국 큐레이션 유니버스 — ticker: (한글명, 테마)
US_ETF_UNIVERSE = {
    "SPY": ("S&P500", "미국 대형"), "QQQ": ("나스닥100", "미국 테크"), "DIA": ("다우30", "미국 대형"),
    "IWM": ("러셀2000", "미국 중소형"), "RSP": ("S&P 동일가중", "미국 대형"), "MDY": ("미드캡400", "미국 중소형"),
    "MTUM": ("모멘텀 팩터", "팩터·모멘텀"), "SPMO": ("S&P 모멘텀", "팩터·모멘텀"), "QUAL": ("퀄리티", "팩터·모멘텀"),
    "USMV": ("저변동", "팩터·모멘텀"), "VLUE": ("밸류 팩터", "가치·성장"), "VUG": ("성장주", "가치·성장"),
    "VTV": ("가치주", "가치·성장"), "COWZ": ("캐시카우100", "팩터·모멘텀"), "CALF": ("소형 캐시카우", "미국 중소형"),
    "SCHD": ("슈왑 배당", "배당·인컴"), "VYM": ("뱅가드 고배당", "배당·인컴"), "VIG": ("배당성장", "배당·인컴"),
    "DVY": ("셀렉트 배당", "배당·인컴"), "NOBL": ("배당귀족", "배당·인컴"), "SDY": ("배당 SPDR", "배당·인컴"),
    "HDV": ("고배당 iShares", "배당·인컴"), "DGRW": ("배당성장 WT", "배당·인컴"), "SPYD": ("S&P 고배당", "배당·인컴"),
    "XLK": ("기술 섹터", "미국 테크"), "VGT": ("뱅가드 기술", "미국 테크"),
    "SMH": ("반도체 VanEck", "반도체"), "SOXX": ("반도체 iShares", "반도체"),
    "IGV": ("소프트웨어", "AI·소프트웨어"), "AIQ": ("AI 테크", "AI·소프트웨어"), "IRBO": ("AI·로봇", "AI·소프트웨어"),
    "FDN": ("인터넷", "클라우드·인터넷"), "SKYY": ("클라우드", "클라우드·인터넷"), "WCLD": ("클라우드 WT", "클라우드·인터넷"),
    "CIBR": ("사이버보안", "사이버보안"), "HACK": ("사이버보안 ETFMG", "사이버보안"),
    "BOTZ": ("로봇·AI", "로봇·자동화"), "ROBO": ("로보틱스", "로봇·자동화"),
    "ESPO": ("비디오게임", "게임·엔터"), "HERO": ("게임·e스포츠", "게임·엔터"),
    "BLOK": ("블록체인", "크립토 관련주"), "DAPP": ("디지털자산", "크립토 관련주"),
    "WGMI": ("비트 마이너", "크립토 관련주"), "BITQ": ("크립토 이코노미", "크립토 관련주"),
    "XLV": ("헬스케어 섹터", "바이오·헬스케어"), "XBI": ("바이오테크", "바이오·헬스케어"), "IBB": ("나스닥 바이오", "바이오·헬스케어"),
    "IHI": ("의료기기", "의료기기"), "XPH": ("제약", "제약"), "PPH": ("제약 VanEck", "제약"),
    "XLF": ("금융 섹터", "은행·금융"), "KBE": ("은행", "은행·금융"), "KRE": ("지역은행", "은행·금융"),
    "IAI": ("증권·거래소", "증권"), "IAK": ("보험", "보험"),
    "XLI": ("산업재 섹터", "인프라·전력"), "ITA": ("방산·우주 iShares", "방산·우주"), "PPA": ("방산 Invesco", "방산·우주"),
    "XAR": ("방산 SPDR", "방산·우주"), "UFO": ("우주", "방산·우주"),
    "JETS": ("항공", "항공·운송"), "IYT": ("운송", "항공·운송"), "XTN": ("운송 SPDR", "항공·운송"),
    "PAVE": ("미국 인프라", "인프라·전력"), "IFRA": ("인프라 iShares", "인프라·전력"), "GRID": ("스마트그리드", "인프라·전력"),
    "XLU": ("유틸리티 섹터", "인프라·전력"),
    "XHB": ("주택건설", "주택·건설"), "ITB": ("주택건설 iShares", "주택·건설"),
    "XLY": ("경기소비 섹터", "소매·소비"), "XLP": ("필수소비 섹터", "소매·소비"), "XRT": ("소매", "소매·소비"),
    "PEJ": ("레저·엔터", "레저·여행"), "BETZ": ("스포츠베팅", "레저·여행"),
    "DRIV": ("자율주행·EV", "전기차·자율주행"),
    "XLE": ("에너지 섹터", "석유·가스"), "XOP": ("석유 E&P", "석유·가스"), "OIH": ("오일서비스", "석유·가스"),
    "AMLP": ("MLP 인프라", "석유·가스"), "FCG": ("천연가스주", "석유·가스"),
    "TAN": ("태양광", "태양광"), "FAN": ("풍력", "클린에너지"), "ICLN": ("클린에너지", "클린에너지"),
    "QCLN": ("클린에너지 FT", "클린에너지"), "PBW": ("클린에너지 Invesco", "클린에너지"),
    "URA": ("우라늄", "우라늄·원자력"), "URNM": ("우라늄 광산", "우라늄·원자력"), "NLR": ("원자력", "우라늄·원자력"),
    "XLB": ("소재 섹터", "철강·소재"), "GDX": ("금광", "금·귀금속"), "GDXJ": ("주니어 금광", "금·귀금속"),
    "SIL": ("은광", "금·귀금속"), "SILJ": ("주니어 은광", "금·귀금속"),
    "COPX": ("구리 광산", "구리·산업금속"), "REMX": ("희토류", "희토류·광물"), "LIT": ("리튬·배터리", "리튬·2차전지"),
    "XME": ("금속·광산", "구리·산업금속"), "SLX": ("철강", "철강·소재"),
    "MOO": ("농업", "농업"), "PHO": ("물 산업", "물"), "FIW": ("물 FT", "물"),
    "KWEB": ("중국 인터넷", "중국"), "CQQQ": ("중국 테크", "중국"), "MCHI": ("MSCI 중국", "중국"),
    "FXI": ("중국 대형", "중국"), "ASHR": ("중국 A주", "중국"),
    "INDA": ("MSCI 인도", "인도"), "EPI": ("인도 어닝스", "인도"), "SMIN": ("인도 소형", "인도"),
    "EWJ": ("MSCI 일본", "일본"), "DXJ": ("일본 헤지", "일본"), "EWY": ("MSCI 한국", "한국"), "EWT": ("MSCI 대만", "대만"),
    "EWZ": ("브라질", "브라질"), "EWW": ("멕시코", "멕시코"), "EPU": ("페루", "페루"), "ECH": ("칠레", "중남미"),
    "ARGT": ("아르헨티나", "중남미"), "ILF": ("라틴아메리카", "중남미"),
    "EIDO": ("인도네시아", "동남아"), "VNM": ("베트남", "동남아"), "THD": ("태국", "동남아"),
    "EWM": ("말레이시아", "동남아"), "EWS": ("싱가포르", "동남아"),
    "KSA": ("사우디", "중동"), "QAT": ("카타르", "중동"), "UAE": ("UAE", "중동"), "TUR": ("터키", "중동"),
    "EZA": ("남아공", "신흥국"), "EEM": ("신흥국", "신흥국"), "VWO": ("뱅가드 신흥국", "신흥국"), "FM": ("프론티어", "신흥국"),
    "EWU": ("영국", "유럽"), "EWG": ("독일", "유럽"), "EWQ": ("프랑스", "유럽"), "EWI": ("이탈리아", "유럽"),
    "EWP": ("스페인", "유럽"), "GREK": ("그리스", "유럽"), "EPOL": ("폴란드", "유럽"),
    "VGK": ("유럽", "유럽"), "FEZ": ("유로스톡스50", "유럽"), "EWA": ("호주", "기타"), "EWC": ("캐나다", "기타"),
}

# 한국 ETF 자동수집 제외 키워드(파생·채권·금리·환·혼합·리츠 등)
KR_ETFMOM_EX = ("레버리지", "인버스", "2X", "곱버스", "선물", "채권", "국고채", "회사채", "은행채", "전단채",
                "금리", "CD", "SOFR", "머니마켓", "단기자금", "통안", "파킹", "액티브", "TDF", "TRF",
                "혼합", "멀티에셋", "EMP", "리츠", "부동산", "달러", "엔화", "위안", "커버드본드", "본드",
                "KOFR", "초단기", "국공채", "물가", "만기", "크레딧", "하이일드")

# 이름 키워드 → 테마 (순서 중요: 먼저 매칭되는 것 우선)
KR_THEME_MAP = [
    ("반도체", "반도체"), ("AI", "AI·소프트웨어"), ("인공지능", "AI·소프트웨어"), ("소프트웨어", "AI·소프트웨어"),
    ("인터넷", "클라우드·인터넷"), ("클라우드", "클라우드·인터넷"), ("2차전지", "리튬·2차전지"), ("배터리", "리튬·2차전지"),
    ("조선", "조선"), ("방산", "방산·우주"), ("우주", "방산·우주"), ("바이오", "바이오·헬스케어"),
    ("헬스케어", "바이오·헬스케어"), ("의료", "의료기기"), ("제약", "제약"),
    ("은행", "은행·금융"), ("금융", "은행·금융"), ("증권", "증권"), ("보험", "보험"),
    ("전력", "인프라·전력"), ("전선", "인프라·전력"), ("변압기", "인프라·전력"), ("유틸", "인프라·전력"),
    ("원자력", "우라늄·원자력"), ("원전", "우라늄·원자력"), ("SMR", "우라늄·원자력"),
    ("태양광", "태양광"), ("수소", "클린에너지"), ("신재생", "클린에너지"), ("친환경", "클린에너지"),
    ("철강", "철강·소재"), ("화학", "철강·소재"), ("정유", "석유·가스"), ("에너지", "석유·가스"),
    ("금현물", "금·귀금속"), ("골드", "금·귀금속"), ("은현물", "금·귀금속"), ("구리", "구리·산업금속"),
    ("희토류", "희토류·광물"), ("자동차", "전기차·자율주행"), ("전기차", "전기차·자율주행"),
    ("고배당", "배당·인컴"), ("배당", "배당·인컴"), ("커버드콜", "커버드콜"), ("프리미엄", "커버드콜"), ("타겟", "커버드콜"),
    ("게임", "게임·엔터"), ("엔터", "게임·엔터"), ("미디어", "게임·엔터"), ("K-POP", "게임·엔터"), ("콘텐츠", "게임·엔터"),
    ("로봇", "로봇·자동화"), ("건설", "주택·건설"), ("여행", "레저·여행"), ("레저", "레저·여행"), ("카지노", "레저·여행"),
    ("음식료", "소매·소비"), ("필수소비", "소매·소비"), ("화장품", "소매·소비"), ("뷰티", "소매·소비"),
    ("통신", "통신"), ("해운", "항공·운송"), ("운송", "항공·운송"), ("항공", "항공·운송"),
    ("중국", "중국"), ("차이나", "중국"), ("인도", "인도"), ("일본", "일본"), ("닛케이", "일본"),
    ("미국나스닥", "미국 테크"), ("미국테크", "미국 테크"), ("미국빅테크", "미국 테크"), ("미국S&P", "미국 대형"),
    ("미국500", "미국 대형"), ("미국", "미국 대형"), ("밸류업", "가치·성장"), ("밸류", "가치·성장"), ("저PBR", "가치·성장"),
    ("삼성그룹", "한국"), ("그룹", "한국"), ("코스닥", "한국"), ("코스피", "한국"), ("KRX", "한국"),
    ("코리아", "한국"), ("KTOP", "한국"), ("K-", "한국"), ("디스플레이", "IT하드웨어"), ("IT", "IT하드웨어"), ("전자", "IT하드웨어"),
]


def kr_etf_theme(name):
    for kw, th in KR_THEME_MAP:
        if kw in name:
            return th
    return "한국 광의"


def discover_kr_etfs(cap=700):
    """네이버 ETF 전체 목록 → 주식형(+현물 원자재) ETF만 {code:(name, theme)}. 실패 시 빈 dict."""
    try:
        import requests
        r = requests.get("https://finance.naver.com/api/sise/etfItemList.nhn",
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
                         timeout=20)
        items = r.json().get("result", {}).get("etfItemList", [])
    except Exception as e:
        print(f"[etfmom] KR ETF 목록 수집 실패: {e}")
        return {}
    found = {}
    for it in items:
        code = str(it.get("itemcode", "")).zfill(6)
        nm = (it.get("itemname") or "").strip()
        if not code or not nm or any(x in nm for x in KR_ETFMOM_EX):
            continue
        try:
            if float(it.get("marketSum") or 0) < 50:      # 시총 50억 미만 초소형 제외(유동성)
                continue
        except Exception:
            pass
        found[code] = (nm, kr_etf_theme(nm))
        if len(found) >= cap:
            break
    print(f"[etfmom] KR ETF 자동수집 {len(found)}종(주식형·현물)")
    return found


def build_etfmom_universe():
    """{code: (name, theme, market)} — 미국 큐레이션 + 한국 자동수집."""
    uni = {c: (n, t, "US") for c, (n, t) in US_ETF_UNIVERSE.items()}
    for c, (n, t) in discover_kr_etfs().items():
        uni[c] = (n, t, "KR")
    return uni


def etfmom_scores(data, uni):
    """리스크조정 모멘텀 스코어. 반환: score 내림차순 rank 부여 리스트."""
    rows = []
    for code, (name, theme, mk) in uni.items():
        sym = f"{code}.KS" if mk == "KR" else code
        df = ohlc_for(data, sym)
        if df is None:
            continue
        c = df["Close"].dropna()
        if len(c) < ETFMOM_OBS + 20:                     # 이력 부족(신규상장) 제외
            continue
        price = float(c.iloc[-1])
        base = float(c.iloc[-1 - ETFMOM_OBS])
        if not price or not base or base <= 0:
            continue
        m = price / base - 1.0                            # 15영업일 수익률
        sig = float(c.pct_change().iloc[-ETFMOM_OBS:].std())
        if not sig or pd.isna(sig) or sig < 0.0015:       # 초저변동(현금성·채권성) 제외 — 리스크조정 점수 왜곡 방지
            continue
        rr = lambda n: round((price / float(c.iloc[-1 - n]) - 1) * 100, 1) if len(c) > n else None
        rows.append({"code": code, "name": name, "theme": theme, "market": mk,
                     "price": round(price, 2), "ret15": round(m * 100, 1),
                     "ret1w": rr(5), "ret1m": rr(21),
                     "madj": m / sig, "ytd": pc(ytd_return(c))})
    if len(rows) < 20:
        return []
    vals = [r["madj"] for r in rows]
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals) or 1.0
    for r in rows:
        z = (r["madj"] - mu) / sd
        r["score"] = round(1 + z if z >= 0 else 1 / (1 - z), 3)   # 비대칭 매핑
        r.pop("madj")
    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def etfmom_themes(ranked, topn=30, k=5):
    """모멘텀 상위 30개에서 중복 테마 가중 → 주도테마 TOP5(+테마별 대장 ETF)."""
    top = ranked[:topn]
    agg = {}
    for r in top:
        a = agg.setdefault(r["theme"], {"theme": r["theme"], "pts": 0, "leaders": []})
        a["pts"] += topn + 1 - r["rank"]                 # 랭크 가중(1위=30점)
        if len(a["leaders"]) < 3:
            a["leaders"].append({"code": r["code"], "name": r["name"], "market": r["market"],
                                 "ytd": r["ytd"], "ret15": r["ret15"], "ret1w": r["ret1w"],
                                 "ret1m": r["ret1m"], "rank": r["rank"]})
    out = sorted(agg.values(), key=lambda a: a["pts"], reverse=True)[:k]
    mx = out[0]["pts"] if out else 1
    for a in out:
        a["w"] = round(a["pts"] / mx * 100)
    return out


def update_etfmom_book(ranked, bar_date):
    """TOP10 가상 포트 장부 운용 — 14영업일 리밸런싱 + 일일 로스컷 + 손실 1회 연장."""
    try:
        with open(ETFMOM_BOOK_FILE, encoding="utf-8") as f:
            book = json.load(f)
    except Exception:
        book = {}
    info = {r["code"]: r for r in ranked}
    holdings = book.get("holdings", [])
    last_reb = book.get("last_reb")
    buys, sells, losscuts = [], [], []

    def top_fill(held, n, prev_entry=None):
        """상위 랭크에서 미보유 n개 편입(직전 보유였으면 편입가 승계)."""
        got = []
        for r in ranked:
            if len(got) >= n:
                break
            if r["code"] in held:
                continue
            pe = (prev_entry or {}).get(r["code"])
            got.append({"code": r["code"], "name": r["name"], "market": r["market"], "theme": r["theme"],
                        "entry_date": pe["entry_date"] if pe else bar_date,
                        "entry_price": pe["entry_price"] if pe else r["price"],
                        "extended": False, "losscut": False})
            held.add(r["code"])
        return got

    if not holdings:                                    # 최초 구성
        holdings = top_fill(set(), ETFMOM_N)
        buys = [dict(h) for h in holdings]
        last_reb = bar_date
    else:
        # 1) 일일 점검: 유니버스 제외(필터 강화·상폐 등) 퇴출 + 로스컷(-10%) 즉시 교체
        kept = []
        held = {h["code"] for h in holdings}
        for h in holdings:
            r = info.get(h["code"])
            if r is None:                                # 유니버스에서 빠진 종목 → 즉시 퇴출
                sells.append({"code": h["code"], "name": h["name"], "market": h["market"],
                              "why": "유니버스 제외", "pl": None})
                held.discard(h["code"])
                continue
            cur = r["price"]
            pl = (cur / h["entry_price"] - 1) * 100 if (cur and h.get("entry_price")) else 0
            if pl <= ETFMOM_LOSSCUT:
                losscuts.append({"code": h["code"], "name": h["name"], "market": h["market"], "pl": round(pl, 1)})
                held.discard(h["code"])
            else:
                kept.append(h)
        if len(kept) < ETFMOM_N:
            add = top_fill(held, ETFMOM_N - len(kept))
            for a in add:
                a["losscut"] = True                      # 대체 매수 → 첫 리밸런싱까지 보유(일일 회전 방지)
            buys += add
            kept += add
        holdings = kept
        # 2) 14영업일 경과 시 리밸런싱
        bd = len(pd.bdate_range(last_reb, bar_date)) - 1 if last_reb else 0
        if bd >= ETFMOM_REB_BD:
            prev_entry = {h["code"]: h for h in holdings}
            keep = []
            for h in holdings:
                r = info.get(h["code"])
                cur = r["price"] if r else None
                pl = (cur / h["entry_price"] - 1) * 100 if (cur and h.get("entry_price")) else 0
                if h.get("losscut"):                    # 로스컷 매수분: 첫 리밸런싱 무조건 보유 → 다음부턴 일반 룰
                    h["losscut"] = False
                    keep.append(h)
                elif pl < 0 and not h.get("extended"):  # 손실 종목 1회 연장
                    h["extended"] = True
                    keep.append(h)
                else:                                   # 이익(또는 연장 소진) → 모멘텀 룰로 재선정
                    sells.append({"code": h["code"], "name": h["name"], "market": h["market"],
                                  "why": ("이익 실현" if pl >= 0 else "연장 소진"), "pl": round(pl, 1)})
            held = {h["code"] for h in keep}
            add = top_fill(held, ETFMOM_N - len(keep), prev_entry)
            # 매도했지만 상위라 다시 편입된 종목은 '계속 보유'로 정정
            readd = {a["code"] for a in add}
            sells = [s for s in sells if s["code"] not in readd]
            buys += [a for a in add if a["code"] not in {b["code"] for b in buys}]
            holdings = keep + add
            last_reb = bar_date

    # 보유 지표 부착
    today = pd.Timestamp(bar_date)
    for h in holdings:
        r = info.get(h["code"], {})
        h["price"] = r.get("price")
        h["score"] = r.get("score")
        h["rank"] = r.get("rank")
        h["ret15"] = r.get("ret15")
        h["ret1w"] = r.get("ret1w")
        h["ret1m"] = r.get("ret1m")
        h["ytd"] = r.get("ytd")
        h["ret_since"] = round((h["price"] / h["entry_price"] - 1) * 100, 1) if (h.get("price") and h.get("entry_price")) else None
        try:
            h["held_days"] = (today - pd.Timestamp(h["entry_date"])).days
        except Exception:
            h["held_days"] = None
    book = {"holdings": [{k: h[k] for k in ("code", "name", "market", "theme", "entry_date", "entry_price", "extended", "losscut")}
                         for h in holdings],
            "last_reb": last_reb}
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(ETFMOM_BOOK_FILE, "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False)
    except Exception as e:
        print(f"[etfmom] 장부 저장 실패: {e}")
    next_reb = pd.Timestamp(last_reb) + pd.tseries.offsets.BDay(ETFMOM_REB_BD)
    dleft = max(0, len(pd.bdate_range(bar_date, next_reb.date().isoformat())) - 1)
    print(f"[etfmom] 보유 {len(holdings)} · 편입 {len(buys)} · 편출 {len(sells)} · 로스컷 {len(losscuts)} · 다음 리밸런싱 D-{dleft}")
    return {"holdings": holdings, "last_reb": last_reb, "next_reb": next_reb.date().isoformat(), "dleft": dleft,
            "changes": {"buys": [{"code": b["code"], "name": b["name"], "market": b["market"]} for b in buys],
                        "sells": sells, "losscut": losscuts}}


def update_etfmom_nav(data, bar_date, port):
    """가상 포트 NAV(가격, 동일비중 10%) 매일 적립 — 배당 NAV와 동일 패턴."""
    try:
        with open(ETFMOM_NAV_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {}
    hist.setdefault("inception", bar_date)
    today_ts = pd.Timestamp(bar_date)
    rec = hist.get("rec")
    if rec is None:
        rec = {"nav": 100.0, "history": [{"date": bar_date, "nav": 100.0}]}
    else:
        last_date = rec["history"][-1]["date"] if rec.get("history") else None
        if last_date != bar_date and rec.get("holdings"):
            prev_ts = pd.Timestamp(last_date)
            pr = 0.0
            for h in rec["holdings"]:
                sym = f'{h["code"]}.KS' if h.get("market") == "KR" else h["code"]
                df = ohlc_for(data, sym)
                if df is None:
                    continue
                c = df["Close"].dropna()
                cp = c.asof(prev_ts)
                cn = float(c.iloc[-1])
                if cp is not None and not pd.isna(cp) and float(cp) > 0:
                    pr += (1.0 / ETFMOM_N) * (cn / float(cp) - 1.0)
            rec["nav"] = round(rec["nav"] * (1 + pr), 4)
            rec["history"].append({"date": bar_date, "nav": rec["nav"]})
    rec["holdings"] = [{"code": h["code"], "market": h["market"]} for h in port["holdings"]]
    rec["history"] = rec["history"][-400:]
    # 이벤트 마커: 리밸런싱(reb) · 로스컷(lc) — NAV 곡선 위 점선 표시용
    ch = port.get("changes", {})
    ev = rec.get("events", [])
    def _add(t):
        if not any(e["date"] == bar_date and e["t"] == t for e in ev):
            ev.append({"date": bar_date, "t": t})
    if port.get("last_reb") == bar_date and (ch.get("buys") or ch.get("sells")):
        _add("reb")
    if ch.get("losscut"):
        _add("lc")
    rec["events"] = ev[-120:]
    hist["rec"] = rec
    try:
        with open(ETFMOM_NAV_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
    except Exception as e:
        print(f"[etfmom] NAV 저장 실패: {e}")
    first = rec["history"][0]
    port["nav_curve"] = rec["history"]
    port["events"] = rec["events"]
    port["since"] = first["date"]
    port["days_tracked"] = len(rec["history"])
    port["cum"] = round((rec["nav"] / first["nav"] - 1) * 100, 1)


# ----------------------------------------------------------------------
# 무한매수법(분할매수) 대상 ETF 후보 — 현재가·변동성·이격·낙폭 지표
#  (실제 매수/매도 주문 계산은 대시보드에서 사용자 포지션 기준으로 수행)
# ----------------------------------------------------------------------
# (티커, 표시명, 배율, 기초지수, 형태, 등급, 비고)
#  등급 — 주력: 원 전략의 기본 대상 / 대안: 지수 3배로 대체 가능 / 주의: ETN 신용·조기청산 위험
#         비권장: 2배는 40거래일 내 +10% 도달 확률이 낮아 사이클이 잘 안 돌아감
INF_ETFS = [
    ("TQQQ", "나스닥100 3배", 3, "나스닥100", "ETF", "주력", "기본 대상 · 익절 +10%(v2.2)/+15%(v3.0)"),
    ("SOXL", "반도체 3배", 3, "필라델피아 반도체", "ETF", "주력", "고변동 · 익절 +20%(v3.0) · 시드 소진도 빠름"),
]
# 무한매수법 유의종목(거래량 부족 → LOC 종가 왜곡) — 커뮤니티 지정, 추천 아님
INF_AVOID = ["BNKU", "CURE", "DRN", "DUSL", "HIBL", "MIDU", "NAIL",
             "PILL", "RETL", "TPOR", "UTSL", "WANT", "WEBL"]


def build_infinite(data):
    """무한매수 후보 ETF 지표 — 현재가·일변동성·200MA 이격·52주 고점대비·1년 MDD."""
    out = []
    for tk, name, x, base, kind, tier, memo in INF_ETFS:
        df = ohlc_for(data, tk)
        if df is None:
            continue
        c = df["Close"].dropna()
        if len(c) < 120:
            continue
        price = float(c.iloc[-1])
        if not price:
            continue
        r = c.pct_change().dropna()
        vol_d = float(r.iloc[-60:].std()) * 100 if len(r) >= 60 else None          # 최근 60일 일변동성(%)
        ma200 = float(c.rolling(min(200, len(c))).mean().iloc[-1])
        y = c.iloc[-252:] if len(c) >= 252 else c
        hi = float(y.max())
        mdd = float(((y / y.cummax()) - 1).min()) * 100                             # 1년 최대낙폭(%)
        out.append({
            "code": tk, "name": name, "x": x, "base": base, "kind": kind,
            "tier": tier, "memo": memo,
            "price": round(price, 2),
            "vol": round(vol_d, 2) if vol_d else None,
            "dist200": round((price / ma200 - 1) * 100, 1) if ma200 else None,
            "high52": round((price / hi - 1) * 100, 1) if hi else None,
            "mdd1y": round(mdd, 1),
            "ret1m": round((price / float(c.iloc[-22]) - 1) * 100, 1) if len(c) > 22 else None,
            "ret1y": round((price / float(c.iloc[-252]) - 1) * 100, 1) if len(c) > 252 else None,
        })
    print(f"[infinite] 후보 ETF {len(out)}종")
    return out


INF_BT_SEED = 40000.0     # 백테스트 가정 원금($) — 1회분 절반으로 최소 2주 매수 가능한 규모
INF_BT_SPLIT = 40         # 분할수
INF_BT_BASE = 10          # 익절 기준%(v2.2: 종목 무관 10)


def inf_backtest(df, base=INF_BT_BASE, a=INF_BT_SPLIT, seed=INF_BT_SEED, start=None):
    """무한매수법(v2.2 정액법) 일별 시뮬레이션 → 월별 수익률·일별 NAV·사이클 통계.
    체결 판정: 지정가매도=장중 고가 도달 / LOC=종가가 기준보다 유리할 때 종가 체결 / MOC=종가 무조건."""
    if df is None:
        return None
    d = df.dropna(subset=["Close", "High"])
    if start:
        d = d[d.index >= pd.Timestamp(start)]
    if len(d) < 20:
        return None
    one = seed / a
    cash, qty, avg, T = seed, 0, 0.0, 0.0
    cycles, wins, buys, sells, qcuts = 0, 0, 0, 0, 0
    realized = 0.0
    nav_hist, peak, mdd = [], None, 0.0
    for ts, row in d.iterrows():
        c, hi = float(row["Close"]), float(row["High"])
        # ── 매도 (보유분이 있을 때: 지정가(¾) → LOC/MOC(¼) 순서)
        if qty > 0 and avg > 0:
            quarter = T > a - 1
            star = avg * (1 + base * (1 - 2 * T / a) / 100)
            lmt = avg * (1 + base / 100)
            q4 = int(qty // 4)
            rest = qty - q4
            if rest > 0 and hi >= lmt:                       # ¾ 지정가 매도(장중)
                prev = qty
                cash += rest * lmt
                realized += rest * (lmt - avg)
                qty -= rest
                T = T * (qty / prev) if prev > 0 else 0.0
                sells += 1
                wins += 1
            if qty > 0:
                q4 = int(qty // 4)
                if q4 > 0 and (quarter or c >= star):        # ¼ MOC(쿼터모드) 또는 별지점 LOC
                    prev = qty
                    cash += q4 * c
                    realized += q4 * (c - avg)
                    qty -= q4
                    T = T * (qty / prev) if prev > 0 else 0.0
                    sells += 1
                    if quarter:
                        qcuts += 1
            if qty <= 0:                                     # 사이클 종료
                avg, T = 0.0, 0.0
                cycles += 1
        # ── 매수 (쿼터모드에는 매수 안 함)
        if not (T > a - 1):
            if qty <= 0 or avg <= 0:                         # 첫 매수(평단 없음 → 큰수 LOC는 사실상 체결)
                n = int(min(cash, one) // c)
                if n > 0:
                    cash -= n * c
                    avg, qty = c, n
                    T += (n * c) / one
                    buys += 1
            else:
                star = avg * (1 + base * (1 - 2 * T / a) / 100)
                legs = ([(avg, one / 2), (star - 0.01, one / 2)] if T < a / 2 else [(star - 0.01, one)])
                for lim, amt in legs:
                    if c <= lim:                             # LOC 체결 → 종가로 매수
                        n = int(min(cash, amt) // c)
                        if n > 0:
                            avg = (qty * avg + n * c) / (qty + n)
                            qty += n
                            cash -= n * c
                            T += (n * c) / one
                            buys += 1
        nav = cash + qty * c
        peak = nav if peak is None else max(peak, nav)
        mdd = min(mdd, nav / peak - 1)
        nav_hist.append((ts, nav))
    # ── 월별 수익률
    monthly, prev = [], None
    ser = pd.Series([v for _, v in nav_hist], index=[t for t, _ in nav_hist])
    for mkey, grp in ser.groupby(ser.index.to_period("M")):
        endv = float(grp.iloc[-1])
        basev = prev if prev is not None else seed
        monthly.append({"m": str(mkey), "ret": round((endv / basev - 1) * 100, 2), "nav": round(endv, 2)})
        prev = endv
    last = float(ser.iloc[-1])
    return {"seed": seed, "split": a, "base": base,
            "start": nav_hist[0][0].date().isoformat(), "end": nav_hist[-1][0].date().isoformat(),
            "monthly": monthly, "total": round((last / seed - 1) * 100, 2),
            "nav": round(last, 2), "mdd": round(mdd * 100, 1),
            "cycles": cycles, "wins": wins, "buys": buys, "sells": sells, "qcuts": qcuts,
            "realized": round(realized, 0), "open_qty": qty, "open_avg": round(avg, 2) if avg else 0,
            "T": round(T, 2), "cash": round(cash, 0),
            "curve": [{"date": t.date().isoformat(), "nav": round(v, 2)} for t, v in nav_hist]}


def build_inf_backtest(data, bar_date):
    """무한매수 후보 종목별 올해(YTD) 백테스트."""
    y0 = f"{bar_date[:4]}-01-01"
    out = {}
    for tk, name, *_ in INF_ETFS:
        try:
            r = inf_backtest(ohlc_for(data, tk), start=y0)
            if r:
                r["name"] = name
                out[tk] = r
                print(f"[inf-bt] {tk} {bar_date[:4]}년: {r['total']:+.1f}% · 사이클 {r['cycles']} · MDD {r['mdd']}%")
        except Exception as e:
            print(f"[inf-bt] {tk} 실패: {e}")
    return out


def etfmom_text(em):
    """텔레그램용 테마포착 요약(리밸런싱·로스컷 발생 시만 호출)."""
    ch = em.get("changes", {})
    lines = ["", "🚀 <b>ETF 모멘텀 TOP10 (테마포착)</b>"]
    for l in ch.get("losscut", []):
        lines.append(f"🔴 로스컷 {('🔵' if l['market']=='KR' else '🟢')}{l['name']} ({l['pl']}%)")
    for b in ch.get("buys", []):
        lines.append(f"🟢 편입 {('🔵' if b['market']=='KR' else '🟢')}{b['name']}")
    for s in ch.get("sells", []):
        lines.append(f"⚪ 편출 {('🔵' if s['market']=='KR' else '🟢')}{s['name']} ({s['why']})")
    th = em.get("themes", [])
    if th:
        lines.append("📌 주도테마: " + " · ".join(f"{t['theme']}" for t in th))
    return "\n".join(lines)


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
            df_i = ohlc_for(data, tk)
            # 지수(^GSPC·^KS11 등)는 야후 피드가 개별종목보다 하루 늦게 갱신되는 일이 잦음.
            # bar_date보다 뒤처지면 개별 재조회로 최신 봉을 보강(집합 다운로드가 놓친 마지막 봉 확보).
            try:
                if df_i is None or pd.to_datetime(df_i["Close"].dropna().index[-1]).date().isoformat() < bar_date:
                    import yfinance as yf
                    fresh = yf.download(tk, period="6mo", interval="1d",
                                        auto_adjust=False, threads=False, progress=False)
                    if fresh is not None and len(fresh):
                        if isinstance(fresh.columns, pd.MultiIndex):
                            fresh.columns = fresh.columns.get_level_values(0)
                        fresh = fresh.dropna(subset=["Close"])
                        if len(fresh) and (df_i is None or pd.to_datetime(fresh.index[-1]) > pd.to_datetime(df_i["Close"].dropna().index[-1])):
                            df_i = fresh
                            print(f"[regime] {tk} 지수 최신봉 보강 → {pd.to_datetime(fresh.index[-1]).date()}")
            except Exception as e:
                print(f"[regime] {tk} 재조회 실패: {e}")
            h = index_health(nm, tk, df_i)
            if h:
                idx_map[tk] = h
    regime = build_regime(allrecs, idx_map)
    stale = [f"{h['name']}({h['asof']})" for mk in regime for h in regime[mk].get("indexes", []) if h.get("asof") and h["asof"] < bar_date]
    if stale:
        print(f"[regime] ⚠️ 기준일({bar_date})보다 뒤처진 지수: {', '.join(stale)}")
    print(f"[regime] US={regime['US']['label']} KR={regime['KR']['label']}")

    macro = build_global_macro() + build_macro(data)
    print(f"[macro] {len(macro)}개 지표(FRED 금리·물가·고용·유동성 + 시클리컬): "
          + ", ".join(f"{m['label']} {m['value']}{m['unit']}" for m in macro))
    mac = macro_assessment(macro)   # 미국 매크로 = 글로벌 앵커 → 한·미 레짐 공통 적용
    for mk in regime:
        regime[mk]["macro"] = mac
    print(f"[macro] 경기국면={mac['cycle']['phase']} · 위험선호 신호={mac['risk']['label']}({mac['risk']['score']})")

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

    # 배당: 유니버스 배당이력(actions) 별도 다운로드 → 스크리너+모델 포트폴리오
    try:
        import yfinance as yf
        kr_uni = {**discover_kr_div_etfs(), **DIV_UNIVERSE["KR"]}   # 자동발굴 + 큐레이션(큐레이션 우선)
        div_universe = {"US": DIV_UNIVERSE["US"], "KR": kr_uni}
        div_syms = ([f"{c}.KS" for c in kr_uni] + list(DIV_UNIVERSE["US"]))
        div_data = yf.download(div_syms, period="6y", interval="1d",
                               group_by="ticker", auto_adjust=False, actions=True, threads=True, progress=False)
        season_weak = int(bar_date[5:7]) in (5, 6, 7, 8, 9, 10)   # 5~10월 약세(Sell in May)
        dividends = build_dividends(div_data, mac, season_weak, bar_date, div_universe)  # 영구 장부 편입/편출 + 레짐 현금
        update_dividend_nav(dividends, div_data, bar_date)         # 가상 운용 누적성과(매일 스냅샷 적립)
        for mk, dd in dividends.items():
            p = dd.get("portfolio", {})
            print(f"[dividend] {mk}: {len(dd.get('stocks', []))}종목 · 모델 {p.get('n')} · 수익률 {p.get('yield')}% · 성장 {p.get('cagr')}% · 틸트 {p.get('tilt')}")
    except Exception as e:
        print(f"[dividend] 실패: {e}")
        dividends = {}

    # 레버리지 ETF: 별도 다운로드 → 불/베어 쌍·개별주 모멘텀 지표
    try:
        import yfinance as yf
        lev_syms = []
        for _t, mk, _x, bc, _bn, rc_, _rn in LEV_PAIRS:
            lev_syms.append(f"{bc}.KS" if mk == "KR" else bc)
            if rc_:
                lev_syms.append(f"{rc_}.KS" if mk == "KR" else rc_)
        lev_syms += [f"{c}.KS" if mk == "KR" else c for _n, mk, _x, c in LEV_SINGLES]
        inf_only = [t for t, *_ in INF_ETFS if t not in lev_syms]      # 무한매수 후보 중 미포함분
        lev_data = yf.download(lev_syms + inf_only, period="2y", interval="1d",
                               group_by="ticker", auto_adjust=False, threads=True, progress=False)
        leverage = build_leverage(lev_data)
        try:
            infinite = build_infinite(lev_data)
            inf_bt = build_inf_backtest(lev_data, bar_date)
        except Exception as e:
            print(f"[infinite] 실패: {e}")
            infinite, inf_bt = [], {}
        try:  # 4시간봉 시그널: 1시간봉 다운로드 → 4h 합성 → 상승시작/풀백/하락전환
            lev1h = yf.download(lev_syms, period="720d", interval="1h",
                                group_by="ticker", auto_adjust=False, threads=True, progress=False)
            leverage["signals"] = build_lev_signals(lev1h)
        except Exception as e:
            print(f"[lev-sig] 실패: {e}")
            leverage["signals"] = {"US": [], "KR": [], "fresh": []}
    except Exception as e:
        print(f"[leverage] 실패: {e}")
        leverage = {"pairs": [], "singles": [], "signals": {"US": [], "KR": [], "fresh": []}}
        infinite, inf_bt = [], {}

    # ETF 모멘텀 TOP10(테마포착): 광역 ETF 유니버스 스캔 → 가상 포트 운용 + 주도테마 TOP5
    try:
        import yfinance as yf
        emu = build_etfmom_universe()
        em_syms = [f"{c}.KS" if v[2] == "KR" else c for c, v in emu.items()]
        em_data = yf.download(em_syms, period="1y", interval="1d",
                              group_by="ticker", auto_adjust=False, threads=True, progress=False)
        ranked = etfmom_scores(em_data, emu)
        print(f"[etfmom] 유니버스 {len(emu)} · 스코어링 {len(ranked)}종")
        if ranked:
            etfmom = update_etfmom_book(ranked, bar_date)
            update_etfmom_nav(em_data, bar_date, etfmom)
            etfmom["themes"] = etfmom_themes(ranked)
            etfmom["top30"] = [{k: r[k] for k in ("rank", "code", "name", "theme", "market", "score",
                                                  "ret15", "ret1w", "ret1m", "ytd")}
                               for r in ranked[:30]]
            etfmom["universe"] = len(ranked)
        else:
            etfmom = {}
    except Exception as e:
        print(f"[etfmom] 실패: {e}")
        etfmom = {}

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
        "generated_at": (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST"),
        "config": {k: CONFIG[k] for k in ("ma_pullback", "ma_trend", "top_sectors",
                                          "leaders_per_sector", "individual_top", "deep_top",
                                          "proximity_pct", "rs_weights", "zigzag_pct")},
        "regime": regime, "seasonality": seasonality, "macro": macro, "dividends": dividends,
        "leverage": leverage, "etfmom": etfmom, "infinite": infinite, "inf_bt": inf_bt,
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
    msg = build_message(markets, stocks, bar_date, payload["commodities"], regime,
                        payload["hot"], seasonality, payload["consensus"])
    lev_fresh = leverage.get("signals", {}).get("fresh", [])
    if lev_fresh:
        msg += "\n" + lev_signal_text(lev_fresh)
    em_ch = etfmom.get("changes", {}) if etfmom else {}
    if em_ch.get("buys") or em_ch.get("sells") or em_ch.get("losscut"):
        msg += "\n" + etfmom_text(etfmom)
    send_telegram(msg)
    print("[done]")


if __name__ == "__main__":
    main()
