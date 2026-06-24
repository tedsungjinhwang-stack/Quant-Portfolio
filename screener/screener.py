"""
2트랙 모멘텀 스크리너 + 기술적 대시보드
==========================================
매일 GitHub Actions에서 실행. AI 호출 없음(순수 계산, 토큰 0).

데이터: FinanceDataReader(유니버스) + yfinance(가격).

트랙①  섹터ETF를 RS로 랭킹 → 강한 섹터 3개 → 각 섹터 RS 상위 대장주 3개
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
    "leaders_per_sector": 3,     # 트랙① 섹터별 대장주 수
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

# 한국: KODEX 섹터/테마 ETF를 FDR 목록에서 자동 수집할 때 쓰는 키워드
KR_SECTOR_KEYWORDS = (
    "반도체", "2차전지", "바이오", "헬스케어", "제약", "은행", "증권", "보험", "자동차",
    "철강", "화학", "에너지", "건설", "운송", "조선", "기계", "미디어", "게임", "엔터",
    "인터넷", "소프트웨어", "방산", "우주항공", "로봇", "원자력", "화장품", "음식료",
    "유통", "항공", "K-", "콘텐츠", "전력",
)
# 섹터가 아닌 ETF(레버리지·채권·해외·팩터 등)를 걸러내는 블랙리스트
KR_ETF_BLACK = (
    "레버리지", "인버스", "채권", "국고", "단기", "금리", "통안", "달러", "엔", "미국", "차이나",
    "중국", "일본", "유럽", "글로벌", "선진", "신흥", "인디아", "베트남", "리츠", "고배당", "배당",
    "ESG", "TR", "선물", "원유", "골드", "은선물", "구리", "2X", "합성", "MSCI", "코스피200",
    "코스닥150", "TOP", "밸류", "모멘텀", "퀄리티", "로우볼", "액티브", "혼합", "단기채",
)

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
        sec_col = next((c for c in ("Sector", "Industry", "SectorName") if c in kr.columns), None)
        if cap_col:
            kr = kr.sort_values(cap_col, ascending=False)
        kr = kr.head(CONFIG["kr_top_n"])
        for _, r in kr.iterrows():
            code = str(r[code_col]).strip().zfill(6)
            if not code.isdigit():
                continue
            rows.append({"market": "KR", "code": code, "yahoo": f"{code}{r['_suffix']}",
                         "name": str(r.get(name_col, code)),
                         "sector": str(r[sec_col]) if sec_col else "N/A"})
        print(f"[universe] KR top{CONFIG['kr_top_n']}: {sum(1 for x in rows if x['market']=='KR')}")
    except Exception as e:
        print(f"[universe] KR 실패: {e}")
    return rows


# ----------------------------------------------------------------------
# 한국 KODEX 섹터 ETF (자동 수집) + 구성종목
# ----------------------------------------------------------------------
def get_kr_sector_etfs():
    """FDR ETF 목록에서 KODEX 섹터/테마 ETF만 골라 반환."""
    import FinanceDataReader as fdr
    try:
        etf = fdr.StockListing("ETF/KR")
    except Exception as e:
        print(f"[kr-etf] 목록 실패: {e}")
        return []
    code_col = next((c for c in ("Symbol", "Code") if c in etf.columns), etf.columns[0])
    name_col = "Name" if "Name" in etf.columns else etf.columns[1]
    out, seen = [], set()
    for _, r in etf.iterrows():
        name = str(r[name_col]).strip()
        code = str(r[code_col]).strip().zfill(6)
        if not code.isdigit() or code in seen:
            continue
        if not name.upper().startswith("KODEX"):
            continue
        if not any(k in name for k in KR_SECTOR_KEYWORDS):
            continue
        if any(b in name for b in KR_ETF_BLACK):
            continue
        seen.add(code)
        label = name.replace("KODEX", "").strip()
        out.append({"code": code, "yahoo": f"{code}.KS", "name": name, "label": label})
    print(f"[kr-etf] KODEX 섹터 ETF {len(out)}개 수집")
    return out


def kr_etf_holdings(code, top=12):
    """pykrx로 ETF 구성종목 코드 목록(비중순). 실패 시 빈 리스트."""
    try:
        from pykrx import stock
    except Exception as e:
        print(f"[holdings] pykrx 없음: {e}")
        return []
    try:
        d = stock.get_nearest_business_day_in_a_week()
    except Exception:
        d = dt.date.today().strftime("%Y%m%d")
    pdf = None
    for call in (lambda: stock.get_etf_portfolio_deposit_file(code),
                 lambda: stock.get_etf_portfolio_deposit_file(d, code)):
        try:
            pdf = call()
            if pdf is not None and len(pdf):
                break
        except Exception:
            pdf = None
    if pdf is None or not len(pdf):
        return []
    try:
        wcol = next((c for c in ("비중", "weight", "Weight") if c in pdf.columns), None)
        if wcol:
            pdf = pdf.sort_values(wcol, ascending=False)
        return [str(x).zfill(6) for x in pdf.index.tolist()[:top]]
    except Exception:
        return [str(x).zfill(6) for x in pdf.index.tolist()[:top]]


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


def metrics(meta, df):
    """종목 1개의 지표 dict(또는 None)."""
    if df is None or len(df) < 60:
        return None
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])
    floor = CONFIG["min_price_usd"] if meta["market"] == "US" else CONFIG["min_price_krw"]
    if last < floor:
        return None
    rs, m3, m6, m12 = rs_value(close)
    if rs is None:
        return None

    ma20 = close.rolling(CONFIG["ma_pullback"]).mean()
    ma60 = close.rolling(60).mean()
    ma200 = close.rolling(min(CONFIG["ma_trend"], len(close))).mean()
    v20, v60, v200 = ma20.iloc[-1], ma60.iloc[-1], ma200.iloc[-1]
    hi, lo = float(df["High"].iloc[-1]), float(df["Low"].iloc[-1])
    touched = bool(pd.notna(v20) and lo <= v20 <= hi)
    dist20 = (last - v20) / v20 * 100 if pd.notna(v20) else None
    high52 = float(close.tail(252).max())
    vol = df.get("Volume")
    vr = None
    if vol is not None and len(vol) >= 20 and vol.tail(20).mean() > 0:
        vr = float(vol.iloc[-1] / vol.tail(20).mean())
    ell, note = elliott_estimate(close.tolist()[-260:], CONFIG["zigzag_pct"])

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
        "ma200": round(float(v200), 2) if pd.notna(v200) else None,
        "touched": touched, "dist20": round(dist20, 2) if dist20 is not None else None,
        "near": bool(dist20 is not None and abs(dist20) <= CONFIG["proximity_pct"] and not touched),
        "disp20": round(last / float(v20) * 100, 1) if pd.notna(v20) else None,
        "disp60": round(last / float(v60) * 100, 1) if pd.notna(v60) else None,
        "disp200": round(last / float(v200) * 100, 1) if pd.notna(v200) else None,
        "rsi": round(float(rsi(close).iloc[-1]), 1),
        "high52_pct": round((last / high52 - 1) * 100, 1) if high52 > 0 else None,
        "vol_ratio": round(vr, 2) if vr else None,
        "elliott": ell, "elliott_note": note,
        "ret3m": round(m3 * 100, 1), "ret6m": round(m6 * 100, 1),
        "trend_ok": bool(pd.notna(v200) and last > float(v200)),
        "bars": bars, "ma20s": arr(ma20), "ma60s": arr(ma60), "ma200s": arr(ma200),
    }


# ----------------------------------------------------------------------
# 2트랙 구성
# ----------------------------------------------------------------------
def build_tracks(allrecs, us_etf_rs, kr_etfs, kr_etf_rs, holdings_fn):
    markets = {}
    for mkt in ("US", "KR"):
        recs = [r for r in allrecs if r["market"] == mkt and r["trend_ok"]]
        recs.sort(key=lambda r: r["_rs_raw"], reverse=True)
        by_code = {r["code"]: r for r in recs}

        # 트랙② 개별 Top N + 심층
        top = recs[: CONFIG["individual_top"]]
        for i, r in enumerate(top, 1):
            r["rs_rank"] = i
        deep_ids = [r["id"] for r in top[: CONFIG["deep_top"]]]

        # 트랙① 강한 섹터 → 대장주
        sectors = []
        if mkt == "US":
            ranked = sorted(
                [(etf, sec, us_etf_rs.get(etf)) for etf, sec in SECTOR_ETFS_US.items() if us_etf_rs.get(etf) is not None],
                key=lambda x: x[2], reverse=True)
            for etf, sec, ersv in ranked[: CONFIG["top_sectors"]]:
                leaders = [r for r in recs if r["sector"] == sec][: CONFIG["leaders_per_sector"]]
                sectors.append({"sector": sec, "etf": etf, "etf_rs": round(ersv * 100, 1),
                                "leader_ids": [r["id"] for r in leaders]})

        elif kr_etfs:
            # KODEX 섹터 ETF를 RS로 랭킹 → 강한 섹터 → 구성종목 중 RS 상위 대장주
            ranked = sorted([e for e in kr_etfs if kr_etf_rs.get(e["yahoo"]) is not None],
                            key=lambda e: kr_etf_rs[e["yahoo"]], reverse=True)
            for e in ranked[: CONFIG["top_sectors"]]:
                holds = holdings_fn(e["code"])
                leaders = [by_code[c] for c in holds if c in by_code]
                if not leaders:  # 폴백: ETF 이름 키워드로 섹터태그 매칭
                    kw = next((k for k in KR_SECTOR_KEYWORDS if k in e["name"]), None)
                    if kw:
                        leaders = [r for r in recs if kw in (r["sector"] or "") or kw in r["name"]]
                leaders = leaders[: CONFIG["leaders_per_sector"]]
                sectors.append({"sector": e["label"], "etf": e["code"],
                                "etf_rs": round(kr_etf_rs[e["yahoo"]] * 100, 1),
                                "leader_ids": [r["id"] for r in leaders]})
        else:
            # 최종 폴백: 섹터태그 RS 중앙값 집계
            groups = {}
            for r in recs:
                if r["sector"] and r["sector"] != "N/A":
                    groups.setdefault(r["sector"], []).append(r)
            scored = [(sec, statistics.median([x["_rs_raw"] for x in g]))
                      for sec, g in groups.items() if len(g) >= CONFIG["kr_sector_min"]]
            scored.sort(key=lambda x: x[1], reverse=True)
            for sec, score in scored[: CONFIG["top_sectors"]]:
                leaders = groups[sec][: CONFIG["leaders_per_sector"]]
                sectors.append({"sector": sec, "etf": "(섹터태그 집계)", "etf_rs": round(score * 100, 1),
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


def build_message(markets, stocks, bar_date):
    flag = lambda m: "🟢" if m == "US" else "🔵"
    lines = [f"📈 <b>2트랙 모멘텀 스크리너</b> · 기준일 {bar_date}"]
    for mkt in ("US", "KR"):
        mk = markets.get(mkt, {})
        if not mk.get("sectors") and not mk.get("top_ids"):
            continue
        lines.append(f"\n{flag(mkt)} <b>{'미국' if mkt=='US' else '한국'}</b>")
        secs = mk.get("sectors", [])
        if secs:
            lines.append("· 트랙① 강한 섹터: " + ", ".join(s["sector"] for s in secs))
            for s in secs:
                names = []
                for i in s["leader_ids"]:
                    r = stocks[i]
                    names.append(f"{r['name']}{'🟢터치' if r['touched'] else ''}")
                if names:
                    lines.append(f"   - {s['sector']}: " + ", ".join(names))
        touched = [stocks[i] for i in mk.get("top_ids", []) if stocks[i]["touched"]]
        if touched:
            lines.append("· 트랙② Top10 중 눌림목: " + ", ".join(f"{r['name']}(RS#{r['rs_rank']})" for r in touched))
        deep = [stocks[i] for i in mk.get("deep_ids", [])]
        if deep:
            lines.append("· 심층 Top3: " + ", ".join(f"{r['name']}·{r['elliott']}" for r in deep))
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
    rows = get_universe()
    if not rows:
        send_telegram("⚠️ 스크리너: 유니버스 수집 실패.")
        sys.exit(0)
    kr_etfs = get_kr_sector_etfs()
    symbols = ({r["yahoo"] for r in rows} | set(SECTOR_ETFS_US.keys())
               | {e["yahoo"] for e in kr_etfs})
    data = download_prices(symbols)
    bar_date = latest_bar_date(data)

    allrecs = []
    for r in rows:
        rec = metrics(r, ohlc_for(data, r["yahoo"]))
        if rec:
            allrecs.append(rec)

    us_etf_rs = {}
    for etf in SECTOR_ETFS_US:
        df = ohlc_for(data, etf)
        if df is not None:
            v, *_ = rs_value(df["Close"].astype(float))
            us_etf_rs[etf] = v
    kr_etf_rs = {}
    for e in kr_etfs:
        df = ohlc_for(data, e["yahoo"])
        if df is not None:
            v, *_ = rs_value(df["Close"].astype(float))
            kr_etf_rs[e["yahoo"]] = v
    print(f"[result] 종목 {len(allrecs)} · US섹터ETF {sum(1 for v in us_etf_rs.values() if v is not None)} "
          f"· KR섹터ETF {sum(1 for v in kr_etf_rs.values() if v is not None)}")

    markets = build_tracks(allrecs, us_etf_rs, kr_etfs, kr_etf_rs, kr_etf_holdings)
    stocks = collect_selected(markets, allrecs)
    payload = {
        "bar_date": bar_date,
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "config": {k: CONFIG[k] for k in ("ma_pullback", "ma_trend", "top_sectors",
                                          "leaders_per_sector", "individual_top", "deep_top",
                                          "proximity_pct", "rs_weights", "zigzag_pct")},
        "markets": markets, "stocks": stocks,
        "counts": {"stocks": len(stocks),
                   "touched": sum(1 for r in stocks.values() if r["touched"]),
                   "near": sum(1 for r in stocks.values() if r["near"])},
    }
    write_outputs(payload)
    send_telegram(build_message(markets, stocks, bar_date))
    print("[done]")


if __name__ == "__main__":
    main()
