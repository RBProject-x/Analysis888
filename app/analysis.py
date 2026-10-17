"""ประกอบผลวิเคราะห์ (ไม่มีการเรียกเครือข่ายที่นี่)"""

from datetime import datetime
from statistics import median

from . import model
from .matcher import normalize

OUTCOMES = ["เหย้าชนะ", "เสมอ", "เยือนชนะ"]  # ลำดับเดียวกับ probs = [home, draw, away]


def ts(utc: str) -> float:
    return datetime.fromisoformat(utc.replace("Z", "+00:00")).timestamp()


def predict(finished, home_id, away_id, league_finished=None):
    lh, la, league_based = model.league_averages(
        league_finished if league_finished is not None else finished)
    hr = model.team_rows(finished, home_id)
    ar = model.team_rows(finished, away_id)
    lam_h, lam_a = model.expected_goals(hr, ar, lh, la)
    s = model.summarize(model.score_matrix(lam_h, lam_a))
    return {
        "lam_h": lam_h, "lam_a": lam_a, "league_based": league_based,
        "probs": [s["home"], s["draw"], s["away"]],
        "btts": s["btts"], "over": s["over"], "top_scores": s["top_scores"],
        "total_mode": s["total_mode"],
        "n_home": len(hr), "n_away": len(ar),
        "home_rows": hr, "away_rows": ar,
    }


def form(rows):
    """ฟอร์มล่าสุด 5 นัด (ใหม่สุดก่อน)"""
    out = []
    for r in rows[:5]:
        res = "W" if r["gf"] > r["ga"] else "L" if r["gf"] < r["ga"] else "D"
        out.append({"result": res, "score": f'{r["gf"]}-{r["ga"]}', "opp": r["opp"], "home": r["home"]})
    return out


def implied(prices):
    """ราคาทศนิยม -> ความน่าจะเป็นที่หักส่วนต่างเจ้ามือแล้ว และค่า overround"""
    q = [1 / p for p in prices]
    s = sum(q)
    return [x / s for x in q], s - 1


def book_prices(ev):
    """คืน {bookmaker_key: (ชื่อเจ้า, ราคาเหย้า, เสมอ, เยือน)} เฉพาะเจ้าที่มีครบ 3 ผล"""
    out = {}
    home_n, away_n = normalize(ev["home_team"]), normalize(ev["away_team"])
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk.get("key") != "h2h":
                continue
            d = {}
            for o in mk.get("outcomes", []):
                n = normalize(o["name"])
                if n == home_n:
                    d["h"] = o["price"]
                elif n == away_n:
                    d["a"] = o["price"]
                elif n == "draw":
                    d["d"] = o["price"]
            if len(d) == 3:
                out[bk["key"]] = (bk.get("title", bk["key"]), d["h"], d["d"], d["a"])
    return out


def find_event(events, match, index):
    """หาแมตช์ในรายการราคาที่ตรงกับแมตช์ของเรา (ชื่อทีมถูกจับคู่แล้ว และเวลาใกล้กัน)"""
    kickoff = ts(match["utc"])
    for ev in events:
        if abs(ts(ev["commence_time"]) - kickoff) > 12 * 3600:
            continue
        h, a = index.match(ev["home_team"]), index.match(ev["away_team"])
        if h and a and h.team.id == match["home_id"] and a.team.id == match["away_id"]:
            return ev
    return None


def movement_signals(hist):
    moves = []
    for key, rows in hist.items():
        if len(rows) < 2 or rows[0][0] == rows[-1][0]:
            continue
        o = implied(rows[0][1:])[0]
        c = implied(rows[-1][1:])[0]
        moves.append([(c[i] - o[i]) * 100 for i in range(3)])

    if len(moves) < 2:
        return [{"level": "info", "title": "ยังไม่มีประวัติราคาพอสำหรับดูการเคลื่อนไหว",
                 "detail": "แอปบันทึกราคาทุกครั้งที่ดึงข้อมูล ต้องมีราคาจาก 2 เจ้าขึ้นไปในช่วงเวลาที่ต่างกัน"}]

    sig = []
    for i, name in enumerate(OUTCOMES):
        vals = [m[i] for m in moves]
        mean = sum(vals) / len(vals)
        if abs(mean) < 1:
            continue
        ups = sum(1 for v in vals if v > 0.5)
        downs = sum(1 for v in vals if v < -0.5)
        if ups and downs:
            sig.append({"level": "warn", "title": "สัญญาณผิดปกติ ควรตรวจสอบเพิ่มเติม",
                        "detail": f"ราคาของ{name} เคลื่อนสวนทางกันระหว่างเจ้ามือ ({ups} เจ้าขึ้น {downs} เจ้าลง)"})
        else:
            direction = "เพิ่มขึ้น" if mean > 0 else "ลดลง"
            big = abs(mean) >= 5
            sig.append({"level": "warn" if big else "ok", "big": big,
                        "title": "ราคาขยับมาก" if big else "ตลาดเคลื่อนไหวไปทางเดียวกัน",
                        "detail": f"ความน่าจะเป็นของ{name} {direction} ประมาณ {abs(mean):.1f} จุด "
                                  f"เทียบกับราคาแรกที่แอปบันทึก"})
    if not sig:
        sig.append({"level": "ok", "title": "ราคานิ่ง",
                    "detail": "ความน่าจะเป็นของแต่ละผลเปลี่ยนไม่ถึง 1 จุด เทียบกับราคาแรกที่บันทึก"})
    return sig


def dispersion_signals(books, imp):
    if len(books) < 3:
        return []
    out = []
    for k in imp:
        for i in range(3):
            others = [imp[o][i] for o in imp if o != k]
            d = (imp[k][i] - median(others)) * 100
            if abs(d) >= 3:
                out.append({"level": "warn", "title": "ราคาเจ้าหนึ่งต่างจากตลาดส่วนใหญ่",
                            "detail": f"{books[k][0]}: {OUTCOMES[i]} ต่างจากค่ากลางของเจ้าอื่น {d:+.1f} จุด"})
    return out[:3]


def book_spreads(ev):
    """แฮนดิแคปของทีมเหย้า: {key: (ชื่อเจ้า, เส้นแฮนดิแคปเหย้า, ราคาเหย้า, ราคาเยือน)}

    เส้นเป็นลบ = เหย้าต่อ (เช่น -0.25) เป็นบวก = เหย้ารอง (เช่น +0.75)
    """
    out = {}
    home_n, away_n = normalize(ev["home_team"]), normalize(ev["away_team"])
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk.get("key") != "spreads":
                continue
            d = {}
            for o in mk.get("outcomes", []):
                if "point" not in o:
                    continue
                n = normalize(o["name"])
                if n == home_n:
                    d["h"] = (o["point"], o["price"])
                elif n == away_n:
                    d["a"] = (o["point"], o["price"])
            if len(d) == 2:
                out[bk["key"]] = (bk.get("title", bk["key"]), d["h"][0], d["h"][1], d["a"][1])
    return out


def fmt_line(x):
    if abs(x) < 1e-9:
        return "0"
    return f"{x:+.2f}".rstrip("0").rstrip(".")


def line_text(x):
    if abs(x) < 1e-9:
        return "เสมอ (0)"
    return f"{fmt_line(x)} (เหย้า{'ต่อ' if x < 0 else 'รอง'} {abs(x):g})"


def spread_signals(hist):
    """เทียบแฮนดิแคปแรกที่แอปบันทึกกับล่าสุด (ค่ากลางข้ามเจ้า)"""
    first, last, t0 = [], [], None
    for rows in hist.values():
        if len(rows) < 2 or rows[0][0] == rows[-1][0]:
            continue
        first.append(rows[0][1])
        last.append(rows[-1][1])
        t0 = rows[0][0] if t0 is None else min(t0, rows[0][0])
    if not first:
        return []
    o, c = median(first), median(last)
    d = c - o
    if abs(d) < 0.25 - 1e-9:
        return [{"level": "ok", "title": "แฮนดิแคปนิ่ง", "since": t0,
                 "detail": f"แฮนดิแคปเหย้าอยู่ที่ {line_text(c)} ไม่ขยับจากครั้งแรกที่บันทึก"}]
    flip = o * c < 0
    big = abs(d) >= 0.5 or flip
    view = "ตลาดมองทีมเหย้าแย่ลง" if d > 0 else "ตลาดมองทีมเหย้าดีขึ้น"
    detail = f"แรกที่บันทึก {line_text(o)} → ล่าสุด {line_text(c)} (เปลี่ยน {abs(d):g} ลูก) {view}"
    if flip:
        detail += " และสลับฝั่งต่อ-รอง"
    return [{"level": "warn" if big else "info", "big": big, "since": t0,
             "title": "แฮนดิแคปขยับมาก" if big else "แฮนดิแคปขยับเล็กน้อย", "detail": detail}]


def record_odds(ev, store, match_id, odds_ts):
    b = book_prices(ev)
    if b:
        store.record(match_id, odds_ts, [(k, v[1], v[2], v[3]) for k, v in b.items()])
    s = book_spreads(ev)
    if s:
        store.record_spreads(match_id, odds_ts, [(k, v[1], v[2], v[3]) for k, v in s.items()])


def market_block(ev, store, match_id, odds_ts, probs):
    record_odds(ev, store, match_id, odds_ts)
    books = book_prices(ev)
    if not books:
        return {"available": False, "message": "ยังไม่มีราคา 1X2 จากแหล่งข้อมูลนี้"}

    imp = {k: implied(v[1:])[0] for k, v in books.items()}
    n = len(books)
    mean = [sum(imp[k][i] for k in imp) / n for i in range(3)]
    best = []
    for i in range(3):
        _, v = max(books.items(), key=lambda kv: kv[1][1 + i])
        best.append({"price": v[1 + i], "book": v[0]})
    ev_pct = [round((probs[i] * best[i]["price"] - 1) * 100, 1) for i in range(3)]
    edge = [round((probs[i] - mean[i]) * 100, 1) for i in range(3)]

    signals = (spread_signals(store.spread_history(match_id))
               + movement_signals(store.history(match_id)) + dispersion_signals(books, imp))
    cur = [v[1] for v in book_spreads(ev).values()]
    handicap = {"books": len(cur), "line": median(cur), "text": line_text(median(cur))} if cur else None
    return {
        "available": True,
        "alerts": [s for s in signals if s.get("big")],
        "handicap": handicap,
        "books": n,
        "fetched_at": odds_ts,
        "market_probs": [round(x * 100, 1) for x in mean],
        "overround_pct": round(sum(implied(v[1:])[1] for v in books.values()) / n * 100, 1),
        "best": best,
        "ev_pct": ev_pct,
        "edge_points": edge,
        "signals": signals,
    }


def confidence(n_home, n_away, league_based, market, note=None):
    pts, reasons = 0, []
    m = min(n_home, n_away)
    if m >= 8:
        pts += 2
        reasons.append("ฟอร์มย้อนหลังอย่างน้อย 8 นัดทั้งสองทีม")
    elif m >= 5:
        pts += 1
        reasons.append("ฟอร์มย้อนหลัง 5–7 นัดบางทีม")
    else:
        reasons.append("ฟอร์มย้อนหลังน้อยกว่า 5 นัดบางทีม")

    if league_based:
        pts += 1
        reasons.append("ค่าเฉลี่ยลีกคำนวณจากผลการแข่งขันจริงของฤดูกาลนี้")
    else:
        reasons.append("ข้อมูลลีกไม่พอ ใช้ค่ากลางทั่วไปแทน")

    if market and market.get("available"):
        if market["books"] >= 2:
            pts += 1
        reasons.append(f"เทียบกับราคาตลาด {market['books']} เจ้า")
        if max(abs(e) for e in market["edge_points"]) > 8:
            pts -= 1
            reasons.append("โมเดลต่างจากตลาดมาก ควรตรวจสอบข้อมูลก่อนใช้ผล")
    else:
        reasons.append("ไม่มีราคาตลาดให้เทียบ")

    if note:
        reasons.append(note)
    reasons.append("ยังไม่มีข้อมูลรายชื่อตัวจริงที่ยืนยันแล้ว")
    level = "สูง" if pts >= 4 else "ปานกลาง" if pts >= 2 else "ต่ำ"
    return {"level": level, "reasons": reasons}
