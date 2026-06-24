"""
모멘텀 대장주 · 20MA 눌림목 + 기술적 대시보드 스크리너
======================================================
매일 GitHub Actions에서 실행:
  1) 미국(S&P500) + 한국(KOSPI/KOSDAQ) 유니버스 수집 (섹터 포함)
  2) 상대강도(RS)로 대장주 선별 (추세 필터: 종가 > 200MA)
  3) 기술적 지표 계산: 20MA 눌림목, 이격도(20·60·200), RSI(14),
     52주 고점대비, 거래량비, 엘리어트 파동 '추정'(참고용)
  4) 산출물:
     - 텔레그램 푸시(눌림목 터치/근접)
     - reports/dashboard.html  (대장주를 눈으로 펼쳐 보는 자체완결 대시보드)
     - reports/data.json, reports/latest.md

설정은 CONFIG 한 곳에서 조정. 데이터: FinanceDataReader(유니버스) + yfinance(가격).
"""

import os
import re
import sys
import json
import datetime as dt

import pandas as pd

# ----------------------------------------------------------------------
# 설정 — 여기만 바꾸면 동작이 달라집니다
# ----------------------------------------------------------------------
CONFIG = {
    "rs_weights": {"m3": 0.40, "m6": 0.40, "m12": 0.20},
    "ma_pullback": 20,
    "ma_trend": 200,
    "leaders_per_market": 40,
    "proximity_pct": 1.0,
    "kr_top_n": 250,
    "min_price_usd": 5.0,
    "min_price_krw": 2000,
    "history": "16mo",
    "chart_bars": 130,        # 대시보드 차트에 담을 최근 일봉 수
    "zigzag_pct": 7.0,        # 엘리어트 추정용 스윙 임계치(%)
}

REPORT_DIR = "reports"
TEMPLATE = os.path.join(os.path.dirname(__file__), "dashboard_template.html")


# ----------------------------------------------------------------------
# 1) 유니버스
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
# 2) 가격
# ----------------------------------------------------------------------
def download_prices(rows):
    import yfinance as yf
    symbols = list({r["yahoo"] for r in rows})
    print(f"[price] 다운로드 {len(symbols)} 종목 ({CONFIG['history']})...")
    return yf.download(symbols, period=CONFIG["history"], interval="1d",
                       group_by="ticker", auto_adjust=False, threads=True, progress=False)


def ohlc_for(data, sym):
    try:
        df = data[sym] if isinstance(data.columns, pd.MultiIndex) else data
        df = df.dropna(subset=["Close"])
        return df if len(df) else None
    except Exception:
        return None


# ----------------------------------------------------------------------
# 3) 기술적 지표
# ----------------------------------------------------------------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def pct_return(close, n):
    if len(close) <= n or close.iloc[-(n + 1)] <= 0:
        return None
    return close.iloc[-1] / close.iloc[-(n + 1)] - 1.0


def zigzag(closes, pct):
    """퍼센트 지그재그 피벗: [(i, price, 'H'/'L'), ...]"""
    if len(closes) < 3:
        return []
    piv = []
    thr = pct / 100.0
    last_i, last_p = 0, closes[0]
    ext_i, ext_p = 0, closes[0]
    direction = 0
    for i in range(1, len(closes)):
        p = closes[i]
        if direction >= 0 and p > ext_p:
            ext_i, ext_p = i, p
        elif direction <= 0 and p < ext_p:
            ext_i, ext_p = i, p
        if direction >= 0 and p <= ext_p * (1 - thr):
            piv.append((ext_i, ext_p, 'H'))
            direction, last_i, last_p, ext_i, ext_p = -1, ext_i, ext_p, i, p
        elif direction <= 0 and p >= ext_p * (1 + thr):
            piv.append((ext_i, ext_p, 'L'))
            direction, last_i, last_p, ext_i, ext_p = 1, ext_i, ext_p, i, p
    return piv


def elliott_estimate(closes, pct):
    """지그재그 스윙으로 현재 파동 위치를 '추정'(참고용)."""
    piv = zigzag(closes, pct)
    if len(piv) < 2:
        return "판단 보류", "스윙이 부족해 추정 불가"
    # 마지막 주요 저점 이후의 교차 스윙 수로 임펄스 파동 추정
    last_low_idx = max((k for k, p in enumerate(piv) if p[2] == 'L'), default=None)
    if last_low_idx is None:
        return "판단 보류", "기준 저점 미확인"
    legs = len(piv) - 1 - last_low_idx  # 저점 이후 완성된 다리 수
    cur_up = closes[-1] > piv[-1][1] if piv[-1][2] == 'L' else closes[-1] > piv[-2][1]
    if legs <= 0:
        return "1파 형성 추정", "주요 저점에서 막 반등 시작(추정)"
    if legs >= 5:
        return "조정(A·B·C) 추정", "임펄스 5파 이후 조정 국면일 수 있음(추정)"
    wave = legs + 1
    names = {2: "2파 조정", 3: "3파 상승", 4: "4파 조정", 5: "5파 마무리"}
    label = names.get(wave, f"{wave}파")
    tail = "상승 진행" if cur_up else "되돌림 진행"
    return f"{label} 추정", f"저점 이후 {legs}개 스윙 · {tail}(추정)"


def compute(rows, data):
    w = CONFIG["rs_weights"]
    out = []
    for r in rows:
        df = ohlc_for(data, r["yahoo"])
        if df is None or len(df) < 60:
            continue
        close = df["Close"].astype(float)
        last = float(close.iloc[-1])
        floor = CONFIG["min_price_usd"] if r["market"] == "US" else CONFIG["min_price_krw"]
        if last < floor:
            continue

        ma20 = close.rolling(CONFIG["ma_pullback"]).mean()
        ma60 = close.rolling(60).mean()
        ma200 = close.rolling(min(CONFIG["ma_trend"], len(close))).mean()
        m3, m6 = pct_return(close, 63), pct_return(close, 126)
        if m3 is None or m6 is None:
            continue
        m12 = pct_return(close, 252) or m6
        rs = w["m3"] * m3 + w["m6"] * m6 + w["m12"] * m12

        v20 = ma20.iloc[-1]
        hi, lo = float(df["High"].iloc[-1]), float(df["Low"].iloc[-1])
        touched = bool(pd.notna(v20) and lo <= v20 <= hi)
        dist20 = (last - v20) / v20 * 100 if pd.notna(v20) else None

        win52 = close.tail(252)
        high52 = float(win52.max())
        vol = df.get("Volume")
        vol_ratio = None
        if vol is not None and len(vol) >= 20 and vol.tail(20).mean() > 0:
            vol_ratio = float(vol.iloc[-1] / vol.tail(20).mean())

        ell_label, ell_note = elliott_estimate(close.tolist()[-260:], CONFIG["zigzag_pct"])

        # 차트용 최근 봉
        tail = df.tail(CONFIG["chart_bars"])
        bars = [{
            "d": str(idx.date()) if hasattr(idx, "date") else str(idx),
            "o": round(float(row["Open"]), 4), "h": round(float(row["High"]), 4),
            "l": round(float(row["Low"]), 4), "c": round(float(row["Close"]), 4),
        } for idx, row in tail.iterrows()]
        ma20t = [None if pd.isna(x) else round(float(x), 4) for x in ma20.tail(CONFIG["chart_bars"])]
        ma60t = [None if pd.isna(x) else round(float(x), 4) for x in ma60.tail(CONFIG["chart_bars"])]
        ma200t = [None if pd.isna(x) else round(float(x), 4) for x in ma200.tail(CONFIG["chart_bars"])]

        rec = {
            "market": r["market"], "code": r["code"], "name": r["name"], "sector": r["sector"],
            "close": round(last, 2), "rs": round(rs * 100, 1),
            "ma20": round(float(v20), 2) if pd.notna(v20) else None,
            "ma60": round(float(ma60.iloc[-1]), 2) if pd.notna(ma60.iloc[-1]) else None,
            "ma200": round(float(ma200.iloc[-1]), 2) if pd.notna(ma200.iloc[-1]) else None,
            "touched": touched,
            "dist20": round(dist20, 2) if dist20 is not None else None,
            "disp20": round(last / float(v20) * 100, 1) if pd.notna(v20) else None,
            "disp60": round(last / float(ma60.iloc[-1]) * 100, 1) if pd.notna(ma60.iloc[-1]) else None,
            "disp200": round(last / float(ma200.iloc[-1]) * 100, 1) if pd.notna(ma200.iloc[-1]) else None,
            "rsi": round(float(rsi(close).iloc[-1]), 1),
            "high52_pct": round((last / high52 - 1) * 100, 1) if high52 > 0 else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "elliott": ell_label, "elliott_note": ell_note,
            "ret3m": round(m3 * 100, 1), "ret6m": round(m6 * 100, 1),
            "bars": bars, "ma20s": ma20t, "ma60s": ma60t, "ma200s": ma200t,
            "_close_gt_200": bool(pd.notna(ma200.iloc[-1]) and last > ma200.iloc[-1]),
        }
        rec["near"] = bool(rec["dist20"] is not None and abs(rec["dist20"]) <= CONFIG["proximity_pct"] and not touched)
        out.append(rec)

    leaders = []
    for mkt in ("US", "KR"):
        cand = [r for r in out if r["market"] == mkt and r["_close_gt_200"]]
        cand.sort(key=lambda r: r["rs"], reverse=True)
        cand = cand[: CONFIG["leaders_per_market"]]
        for i, r in enumerate(cand, 1):
            r["rs_rank"] = i
            r.pop("_close_gt_200", None)
        leaders.extend(cand)
    return leaders


# ----------------------------------------------------------------------
# 4) 산출물
# ----------------------------------------------------------------------
def latest_bar_date(data):
    try:
        return pd.to_datetime(data.index[-1]).date().isoformat()
    except Exception:
        return dt.date.today().isoformat()


def build_message(leaders, bar_date):
    touched = sorted([r for r in leaders if r["touched"]], key=lambda r: (r["market"], r["rs_rank"]))
    near = sorted([r for r in leaders if r["near"]], key=lambda r: (r["market"], r["rs_rank"]))

    def line(r):
        flag = "🟢" if r["market"] == "US" else "🔵"
        return (f"{flag} <b>{r['name']}</b> ({r['code']}) · {r['sector']}\n"
                f"     RS#{r['rs_rank']} · 종가 {r['close']:,} · 20MA {r['dist20']:+.1f}% · "
                f"RSI {r['rsi']} · {r['elliott']}")

    head = f"📉 <b>20MA 눌림목 알림</b> · 기준일 {bar_date}"
    if touched:
        msg = f"{head}\n\n<b>● 눌림목 터치 {len(touched)}건</b>\n" + "\n".join(line(r) for r in touched)
    else:
        msg = f"{head}\n\n오늘 20MA 터치한 대장주는 없습니다."
    if near:
        msg += f"\n\n<b>○ 근접(±{CONFIG['proximity_pct']:.0f}%) {len(near)}건</b>\n" + "\n".join(line(r) for r in near)
    msg += "\n\n<i>대시보드(차트·이격도·엘리어트 추정): reports/dashboard.html · 무료 지연 종가 기준</i>"
    return msg, touched, near


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


def write_outputs(leaders, touched, near, bar_date):
    os.makedirs(REPORT_DIR, exist_ok=True)
    payload = {"bar_date": bar_date,
               "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
               "config": {k: CONFIG[k] for k in ("ma_pullback", "ma_trend", "leaders_per_market",
                                                 "proximity_pct", "rs_weights", "zigzag_pct")},
               "counts": {"leaders": len(leaders), "touched": len(touched), "near": len(near)},
               "leaders": leaders}
    with open(os.path.join(REPORT_DIR, "data.json"), "w") as f:
        json.dump(payload, f, ensure_ascii=False)

    # 대시보드(자체완결 HTML): 템플릿에 데이터 주입
    try:
        with open(TEMPLATE, encoding="utf-8") as f:
            tpl = f.read()
        html = tpl.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
        with open(os.path.join(REPORT_DIR, "dashboard.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print("[report] reports/dashboard.html 저장")
    except Exception as e:
        print(f"[report] 대시보드 생성 실패: {e}")

    # 요약 마크다운
    def tbl(items):
        h = ["| 시장 | 종목 | 코드 | 섹터 | RS# | 종가 | 20이격% | RSI | 엘리어트(추정) |",
             "|---|---|---|---|---|---|---|---|---|"]
        for r in items:
            h.append(f"| {r['market']} | {r['name']} | {r['code']} | {r['sector']} | {r['rs_rank']} | "
                     f"{r['close']:,} | {r['dist20']:+.1f} | {r['rsi']} | {r['elliott']} |")
        return "\n".join(h)
    md = (f"# 20MA 눌림목 · 기술적 리포트 — {bar_date}\n\n"
          f"- 대장주 {len(leaders)} · 터치 **{len(touched)}** · 근접 {len(near)}\n\n"
          f"## 눌림목 터치\n\n{tbl(touched)}\n\n## 근접\n\n{tbl(near)}\n")
    with open(os.path.join(REPORT_DIR, f"momentum-pullback-{bar_date}.md"), "w") as f:
        f.write(md)
    with open(os.path.join(REPORT_DIR, "latest.md"), "w") as f:
        f.write(md)
    print("[report] markdown 저장")


def main():
    rows = get_universe()
    if not rows:
        send_telegram("⚠️ 스크리너: 유니버스 수집 실패.")
        sys.exit(0)
    data = download_prices(rows)
    bar_date = latest_bar_date(data)
    leaders = compute(rows, data)
    print(f"[result] 대장주 {len(leaders)}건")
    msg, touched, near = build_message(leaders, bar_date)
    write_outputs(leaders, touched, near, bar_date)
    send_telegram(msg)
    print("[done]")


if __name__ == "__main__":
    main()
