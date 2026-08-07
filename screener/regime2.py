"""2축 레짐 엔진 — 장기 구조(S_long)와 단기 국면(S_short)을 분리해 판정.

기존 build_regime()의 문제:
  · 주 판정축이 전부 느린 지표(240일선·52주 신고가)라 급락에 몇 주씩 늦음
  · 추세유지 분기가 below20(빠른 지표)을 아예 보지 않아 도달 불가 코드였음
  · 대표지수 1개(idxs[0])만 봐서 나머지 지수의 급락이 통째로 무시됨
  · index_health가 계산한 ma50이 판정에 전혀 쓰이지 않음

설계 원칙:
  · 라벨은 기존 4개 유지(사용자 혼란 방지). 내부만 2축 연속 점수 → 3x3 → 4라벨로 접음
  · 상태는 스냅샷만 저장하고 라벨/EMA/dwell은 매 실행마다 t=0부터 재생(replay)
    → 같은 bar_date 재실행이 값을 오염시키지 않음(멱등)
  · 빠르게 만든 대가인 휘프소는 히스테리시스·dwell·확인일수·데드밴드로 억제
  · 권장 주식비중은 10%p 격자 + 데드밴드 + 라벨별 상하한으로 묶어 회전율 폭주 방지
"""
import hashlib
import json
import os

LONG_ORDER = ["BEAR", "NEUTRAL", "BULL"]
SHORT_ORDER = ["PULLBACK", "WOBBLE", "CALM"]

# 시장별 파라미터. 튜닝 노브는 히스테리시스 폭 둘뿐, 나머지는 고정.
#  HYST_UP : 상향 전환에 필요한 컷 초과폭 (되돌림에 신중)
#  HYST_DN : 하향 전환에 필요한 컷 미달폭 — 평시 점수가 컷 근처라 이게 0이면 경계에서 매일 진동한다.
#            급락은 crash/emergency가 별도 경로로 즉시 잡으므로 여기에 완충을 둬도 늦지 않는다.
P = {
    "US": {"LCUT": (40.0, 60.0), "SCUT": (35.0, 55.0),
           "LHYST_UP": 6.0, "LHYST_DN": 4.0, "SHYST_UP": 5.0, "SHYST_DN": 4.0, "B20_JUMP": 12.0},
    # KR은 종목 표본이 얕음(≈90개) → 컷 완화 + 히스테리시스 확대로 잡음 흡수
    "KR": {"LCUT": (40.0, 60.0), "SCUT": (30.0, 52.0),
           "LHYST_UP": 8.0, "LHYST_DN": 5.0, "SHYST_UP": 7.0, "SHYST_DN": 5.0, "B20_JUMP": 18.0},
}
DWELL, UP_CONFIRM, DOWN_CONFIRM = 5, 3, 2   # 최소유지 5일 · 상향 3일 확인 · 하향 2일 확인(비대칭)
EQ_STEP, EQ_DEAD, N_GATE, KEEP = 10, 10, 0.85, 800

CELL = {
    ("BULL", "CALM"): "추세유지", ("BULL", "WOBBLE"): "상승 둔화", ("BULL", "PULLBACK"): "관망 후 대응",
    ("NEUTRAL", "CALM"): "관망 후 대응", ("NEUTRAL", "WOBBLE"): "관망 후 대응", ("NEUTRAL", "PULLBACK"): "조정 국면",
    ("BEAR", "CALM"): "조정 국면", ("BEAR", "WOBBLE"): "조정 국면", ("BEAR", "PULLBACK"): "조정 국면",
}
RANK = {"조정 국면": 0, "관망 후 대응": 1, "상승 둔화": 2, "추세유지": 3}
COLOR = {"추세유지": "green", "상승 둔화": "yellow", "관망 후 대응": "yellow", "조정 국면": "red"}
EQBAND = {"추세유지": (70, 100), "상승 둔화": (45, 75), "관망 후 대응": (25, 55), "조정 국면": (0, 30)}
STANCE = {"추세유지": "적극 투자", "상승 둔화": "중립·선별", "관망 후 대응": "방어적 관망", "조정 국면": "현금 우선"}
PREMISE = {
    "추세유지": "상승 추세 견조 — 다수 종목이 240일선 위, 지수도 50일선 위. 눌림목 매수 우호.",
    "상승 둔화": "장기 구조는 살아있으나 단기 흔들림 — 주도주만 홀딩, 신규는 대장주 한정, 레버리지 신규 금지.",
    "관망 후 대응": "방향 불명확 — 신규 진입 보류, 현금 확보, 레버리지 정리.",
    "조정 국면": "다수 종목이 추세 이탈 — 현금 우선, 반등은 비중 축소 기회.",
}
LKO = {"BULL": "강세 구조", "NEUTRAL": "중립 구조", "BEAR": "약세 구조"}
SKO = {"CALM": "순항", "WOBBLE": "흔들림", "PULLBACK": "조정 진행"}


def _lin(x, lo, hi, d=50.0):
    """x를 [lo,hi] 구간에서 0~100으로 선형 정규화. None이면 중립 50."""
    if x is None:
        return d
    return max(0.0, min(100.0, (float(x) - lo) / (hi - lo) * 100.0))


def _wavg(pairs):
    ok = [(v, w) for v, w in pairs if v is not None]
    if not ok:
        return None
    return round(sum(v * w for v, w in ok) / sum(w for _, w in ok), 1)


def _avg(a):
    a = [x for x in a if x is not None]
    return sum(a) / len(a) if a else None


def _dist50(ix):
    m = ix.get("ma50")
    return (ix["close"] / m - 1) * 100 if m else None


def snapshot(mkt, pool, idxs):
    """그날의 원시 지표만 계산(라벨 판정 없음) — 이 스냅샷만 저장한다."""
    n = len(pool)
    if n == 0 or not idxs:
        return None
    pct = lambda c: round(sum(1 for r in pool if c(r)) / n * 100, 1)
    below20 = pct(lambda r: r["close"] < r["ma20"])
    below200 = pct(lambda r: r["close"] < r["ma200"])
    nh = pct(lambda r: (r.get("high52_pct") if r.get("high52_pct") is not None else -99) >= -0.5)
    nl = pct(lambda r: (r.get("low52_pct") if r.get("low52_pct") is not None else 99) <= 1.0)
    pct6m = pct(lambda r: (r.get("ret6m") or 0) > 0)
    d50s = [_dist50(ix) for ix in idxs]
    # 장기축: 느린 정보를 버리지 않되 bool을 연속값으로
    s_long = _wavg([
        (_lin(100 - below200, 30, 80), 0.35),
        (_lin(_avg([ix.get("dist200") for ix in idxs]), -12, 12), 0.20),
        (_avg([100.0 if ix.get("slope_up") else 0.0 for ix in idxs]), 0.10),
        (_lin(round(nh - nl, 1), -8, 8), 0.15),
        (_lin(pct6m, 25, 75), 0.20),
    ])
    # 단기축: 빠른 정보에 최대 가중. T5(breadth 변화속도)는 replay에서 주입
    s_short_parts = [
        (_lin(100 - below20, 25, 70), 0.30),
        (_lin(_avg([ix.get("ret1m") for ix in idxs]), -8, 6), 0.20),
        (_lin(_avg([ix.get("ret1w") for ix in idxs]), -3, 3), 0.10),
        (_lin(_avg(d50s), -6, 4), 0.25),
    ]
    crash = (any((ix.get("ret1w") or 0) <= -7 for ix in idxs)
             or any(d is not None and d <= -12 for d in d50s))
    asofs = [ix.get("asof") for ix in idxs if ix.get("asof")]
    return {
        "mkt": mkt, "d": max(asofs) if asofs else None,   # 시장별 거래일 달력
        "n": n, "below20": below20, "below200": below200, "net_nh": round(nh - nl, 1),
        "pct6m": pct6m, "s_long": s_long, "s_short_parts": s_short_parts, "crash": bool(crash),
        "d50": round(_avg(d50s), 2) if _avg(d50s) is not None else None,
        "uhash": hashlib.md5(",".join(sorted(str(r.get("code") or r.get("id"))
                                             for r in pool)).encode()).hexdigest()[:8],
    }


def load_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:                      # 조용히 리셋하지 않는다 — 이력이 사라지면 판정이 흔들림
        raise RuntimeError(f"{path} 손상: {e}")


def save_state(path, st):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, path)                       # 원자적 쓰기


def upsert(rows, snap):
    """같은 날짜는 append가 아니라 교체 — 재실행 멱등성의 핵심."""
    rows = [r for r in rows if r.get("d") != snap["d"]] + [snap]
    rows.sort(key=lambda r: r["d"])
    return rows[-KEEP:]


def axis_state(prev, s, lo, hi, hyst_up, hyst_dn, order):
    """양방향 히스테리시스. 상향은 컷+hyst_up(한 칸씩), 하향은 컷-hyst_dn.
    hyst_dn이 0이면 평시 점수가 컷 근처일 때 경계에서 매일 진동한다(실측 9일마다 라벨 뒤집힘)."""
    if prev is None:
        return order[2] if s >= hi else (order[1] if s >= lo else order[0])
    i = order.index(prev)
    if i == 2:                                    # CALM/BULL에서 내려올 때
        if s < lo - hyst_dn:
            return order[0]
        return order[1] if s < hi - hyst_dn else order[2]
    if i == 1:                                    # 중간 단계
        if s < lo - hyst_dn:
            return order[0]
        if s >= hi + hyst_up:
            return order[2]
        return order[1]
    return order[1] if s >= lo + hyst_up else order[0]   # 최하단은 한 칸만


def replay(rows, par):
    """저장된 스냅샷 전체를 t=0부터 재생해 오늘의 라벨을 산출.
    증분 갱신을 쓰지 않으므로 같은 날 여러 번 실행해도 결과가 같다."""
    ema = None
    lab = None
    since = 0
    pend, pcnt = None, 0
    ls = ss = None
    eq_disp = None
    crash_off = 99
    flips = []
    out = None
    prev_short = None
    for i, r in enumerate(rows):
        # T5(breadth 변화속도): 5거래일 전 대비. 유니버스가 바뀌었으면 비교 불가 → 중립
        prev5 = rows[i - 5] if i >= 5 else None
        if prev5 and prev5.get("uhash") == r.get("uhash"):
            t5 = _lin(-(r["below20"] - prev5["below20"]), -15, 15)
        else:
            t5 = 50.0
        parts = [tuple(p) for p in r["s_short_parts"]] + [(t5, 0.15)]
        s_short_raw = _wavg(parts)
        ema = s_short_raw if ema is None else 0.5 * s_short_raw + 0.5 * ema      # EMA span=3
        s_long, s_short = r["s_long"], round(ema, 1)

        # 데이터 게이트: 종목 수가 급감한 날은 판정 보류(수집 실패 시 오판 방지)
        ns = sorted(x["n"] for x in rows[max(0, i - 20):i]) or [r["n"]]
        med = ns[len(ns) // 2]
        if r["n"] < med * N_GATE and lab is not None:
            since += 1
            out = dict(out, stale=True, days=since)
            continue

        ls = axis_state(ls, s_long, par["LCUT"][0], par["LCUT"][1],
                        par["LHYST_UP"], par["LHYST_DN"], LONG_ORDER)
        ss = axis_state(ss, s_short, par["SCUT"][0], par["SCUT"][1],
                        par["SHYST_UP"], par["SHYST_DN"], SHORT_ORDER)

        crash_off = 0 if r["crash"] else crash_off + 1
        crash_on = bool(r["crash"] or crash_off < 2)       # 해제는 2거래일 연속 미충족일 때만
        cand = "조정 국면" if crash_on else CELL[(ls, ss)]

        b20_jump = (r["below20"] - rows[i - 1]["below20"]) if i else 0.0
        emergency = (r["crash"] or b20_jump >= par["B20_JUMP"]
                     or (prev_short is not None and s_short <= prev_short - 12))

        if lab is None:
            lab, since, pend, pcnt = cand, 0, None, 0
        elif cand == lab:
            since += 1
            pend, pcnt = None, 0
        else:
            down = RANK[cand] < RANK[lab]
            pcnt = pcnt + 1 if pend == cand else 1
            pend = cand
            need = 1 if (down and emergency) else (DOWN_CONFIRM if down else UP_CONFIRM)
            if (down and emergency) or (since >= DWELL and pcnt >= need):
                flips.append(r["d"])
                lab, since, pend, pcnt = cand, 0, None, 0
            else:
                since += 1

        # 행동 변수: 격자 → 라벨밴드 클립 → 데드밴드(회전율 폭주 방지)
        lo, hi = EQBAND[lab]
        eq = int(round((0.55 * s_long + 0.45 * s_short) / EQ_STEP) * EQ_STEP)
        eq = max(lo, min(hi, eq))
        if eq_disp is not None and abs(eq - eq_disp) < EQ_DEAD:
            eq = max(lo, min(hi, eq_disp))
        eq_disp = eq
        prev_short = s_short

        cut60 = rows[max(0, i - 60)]["d"]
        out = {"date": r["d"], "label": lab, "color": COLOR[lab],
               "s_long": s_long, "s_short": s_short,
               "long_state": ls, "short_state": ss, "crash": crash_on,
               "equity_pct": eq, "days": since, "stale": False,
               "flips60": sum(1 for f in flips if f > cut60)}
    return out


def build_regime2(allrecs, idx_map, indexes, state_path):
    """2축 레짐 판정. 반환 형태는 기존 build_regime과 호환(label/color/premise/exposure/breadth/indexes)."""
    st = load_state(state_path)
    res = {}
    for mkt in ("US", "KR"):
        idxs = [idx_map[t] for t, _ in indexes[mkt] if idx_map.get(t)]
        pool = [r for r in allrecs if r["market"] == mkt and r.get("ma200") and r.get("ma20")]
        snap = snapshot(mkt, pool, idxs)
        if snap is None or not snap.get("d"):
            continue
        st[mkt] = upsert(st.get(mkt, []), snap)
        o = replay(st[mkt], P[mkt])
        if o is None:
            continue
        eq = o["equity_pct"]
        res[mkt] = {
            "label": o["label"], "color": o["color"], "premise": PREMISE[o["label"]],
            "subtitle": f"장기 {LKO[o['long_state']]} · 단기 {SKO[o['short_state']]}",
            "axes": {"long": o["s_long"], "short": o["s_short"],
                     "long_state": o["long_state"], "short_state": o["short_state"],
                     "long_ko": LKO[o["long_state"]], "short_ko": SKO[o["short_state"]]},
            "exposure": {"stance": STANCE[o["label"]],
                         "equity": f"{max(0, eq - 5)}~{min(100, eq + 5)}%",
                         "cash": f"{max(0, 95 - eq)}~{min(100, 105 - eq)}%",
                         "equity_pct": eq},
            "reasons": [
                f"장기 {o['s_long']:.0f}점 — 240일선 위 {100 - snap['below200']:.0f}% · 6개월 상승 {snap['pct6m']:.0f}% · 신고가순증 {snap['net_nh']:+.0f}",
                f"단기 {o['s_short']:.0f}점 — 20일선 위 {100 - snap['below20']:.0f}% · 지수 50일선 {snap['d50']:+.1f}%",
                f"이 라벨 {o['days']}일째 · 최근 60일 변경 {o['flips60']}회"
                + (" · ⚠ 데이터 불완전" if o["stale"] else "")
                + (" · 🚨 급락 감지" if o["crash"] else ""),
            ],
            "breadth": {"below20": snap["below20"], "below200": snap["below200"],
                        "above200": round(100 - snap["below200"], 1),
                        "net_new_high": snap["net_nh"], "n": snap["n"]},
            "indexes": idxs,
        }
    save_state(state_path, st)
    return res
