import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import analysis
from .matcher import Team, TeamIndex
from .providers import SPORT_KEYS, Cache, FootballData, OddsAPI, UpstreamError
from .store import Store

log = logging.getLogger("analyzer")

COMPETITIONS = {
    "PL": "Premier League", "PD": "La Liga", "SA": "Serie A",
    "BL1": "Bundesliga", "FL1": "Ligue 1", "CL": "Champions League",
}
INDEX_HTML = Path(__file__).parent / "static" / "index.html"
DISCLAIMER = "ผลเป็นการคาดการณ์เชิงสถิติ ไม่ใช่ผลที่รับประกัน และไม่ใช่คำแนะนำการเดิมพัน"
# ตั้ง ODDS_POLL_MINUTES > 0 เพื่อให้แอปดึงราคาเองเป็นรอบ (เฉพาะแมตช์ที่เคยเปิดดู ภายใน 72 ชม.ก่อนแข่ง)
# ค่าเริ่มต้น 0 = ปิด เพราะกินโควตา The Odds API (ขั้นต่ำจริง 15 นาที ตามอายุแคช)
POLL_MIN = int(os.getenv("ODDS_POLL_MINUTES", "0") or 0)

state: dict = {}


async def _world():
    """โหลดทุกลีกที่แพ็กเกจเข้าถึงได้ -> (seasons, index, comps_by_team, skipped)

    ลีกที่ดึงไม่ได้ (เช่นแพ็กเกจไม่รวม) จะถูกข้ามและรายงานใน skipped
    ถ้าดึงไม่ได้เลยสักลีกจะแจ้ง error
    """
    seasons, skipped = {}, []
    for code in COMPETITIONS:
        try:
            seasons[code] = await state["fd"].season(code)
        except UpstreamError as e:
            skipped.append((code, str(e)))
    if not seasons:
        raise UpstreamError(skipped[0][1] if skipped else "ดึงข้อมูลจาก football-data.org ไม่ได้")
    teams, comps = {}, {}
    for code, (matches, _) in seasons.items():
        for m in matches:
            for side in ("home", "away"):
                tid = m[f"{side}_id"]
                comps.setdefault(tid, set()).add(code)
                if tid not in teams:
                    teams[tid] = Team(tid, m[f"{side}_name"], m.get(f"{side}_short"), m.get(f"{side}_tla"))
    return seasons, TeamIndex(list(teams.values())), comps, skipped


async def poll_once() -> int:
    """บันทึกราคาของแมตช์ที่เคยเปิดดู (ภายใน 72 ชม.ก่อนแข่ง) คืนจำนวนแมตช์ที่บันทึกได้"""
    store, now, by_code = state["store"], time.time(), {}
    for mid, code, utc in store.watched():
        if now <= analysis.ts(utc) <= now + 72 * 3600:
            by_code.setdefault(code, []).append(mid)
    if not by_code:
        return 0
    seasons, idx, _, _ = await _world()
    n = 0
    for code, ids in by_code.items():
        sport = SPORT_KEYS.get(code)
        if not sport or code not in seasons:
            continue
        try:
            events, odds_ts = await state["odds"].events(sport)
        except UpstreamError as e:
            log.warning("poll %s: %s", code, e)
            continue
        by_id = {x["id"]: x for x in seasons[code][0]}
        for mid in ids:
            m = by_id.get(mid)
            if not m or m["status"] not in ("SCHEDULED", "TIMED"):
                continue
            ev = analysis.find_event(events, m, idx)
            if ev:
                analysis.record_odds(ev, store, mid, odds_ts)
                n += 1
    return n


async def _poll_loop():
    while True:
        await asyncio.sleep(max(POLL_MIN, 15) * 60)
        try:
            await poll_once()
        except Exception:
            log.exception("poll failed")


@asynccontextmanager
async def lifespan(_app):
    client = httpx.AsyncClient()
    cache = Cache()
    db_path = os.getenv("DB_PATH", "data/odds.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    state["fd"] = FootballData(os.getenv("FOOTBALL_DATA_TOKEN", ""), client, cache)
    state["odds"] = OddsAPI(os.getenv("ODDS_API_KEY", ""), client, cache)
    state["store"] = Store(db_path)
    task = asyncio.create_task(_poll_loop()) if POLL_MIN > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await client.aclose()


app = FastAPI(title="Football Analyzer", version="1.1.0", lifespan=lifespan)


@app.exception_handler(UpstreamError)
async def upstream_error(_req: Request, exc: UpstreamError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/competitions")
async def competitions():
    return [{"code": c, "name": n, "has_odds": c in SPORT_KEYS} for c, n in COMPETITIONS.items()]


@app.get("/api/teams")
async def teams(q: str = Query("", max_length=60)):
    """ค้นหาทีมข้ามทุกลีกด้วยชื่ออย่างเดียว"""
    _, idx, comps, skipped = await _world()
    return {
        "teams": [{"id": t.id, "name": t.name, "comps": sorted(comps.get(t.id, []))}
                  for t in idx.search(q)],
        "skipped": [c for c, _ in skipped],
    }


@app.get("/api/fixtures")
async def fixtures(team_id: int):
    seasons, _, _, _ = await _world()
    now, rows = time.time(), {}
    for code, (matches, _) in seasons.items():
        for m in matches:
            if (m["status"] in ("SCHEDULED", "TIMED") and team_id in (m["home_id"], m["away_id"])
                    and analysis.ts(m["utc"]) >= now - 3 * 3600):
                rows[m["id"]] = (m, code)
    ordered = sorted(rows.values(), key=lambda r: r[0]["utc"])[:10]
    return {
        "fixtures": [{"id": m["id"], "utc": m["utc"], "home": m["home_name"], "away": m["away_name"],
                      "competition": c, "competition_name": COMPETITIONS[c]} for m, c in ordered],
        "fetched_at": min(t for _, t in seasons.values()),
    }


@app.get("/api/analysis")
async def analysis_route(match_id: int):
    seasons, idx, _, skipped = await _world()
    found = next(((c, x) for c, (ms, _) in seasons.items() for x in ms if x["id"] == match_id), None)
    if found is None:
        raise HTTPException(404, "ไม่พบแมตช์นี้ในฤดูกาลปัจจุบัน")
    code, m = found
    if m["status"] not in ("SCHEDULED", "TIMED"):
        raise HTTPException(400, "วิเคราะห์ได้เฉพาะแมตช์ที่ยังไม่เริ่ม")

    # ฟอร์มของทีมนับทุกรายการที่ทีมลงเล่น ส่วนค่าเฉลี่ยประตูใช้ลีกของแมตช์นี้
    # (ถ้าเป็น Champions League ใช้ค่าเฉลี่ยรวมของลีกในประเทศ)
    pool = {}
    for c, (ms, _) in seasons.items():
        for x in ms:
            if x["status"] == "FINISHED" and x["hg"] is not None:
                pool[x["id"]] = (c, x)
    finished = sorted((x for _, x in pool.values()), key=lambda x: x["utc"])
    league_fin = [x for c, x in pool.values() if (c == code if code != "CL" else c != "CL")]
    pred = analysis.predict(finished, m["home_id"], m["away_id"], league_fin)
    state["store"].watch(m["id"], code, m["utc"])

    market, market_msg, odds_ts = None, None, None
    sport = SPORT_KEYS.get(code)
    if sport is None:
        market_msg = "รายการนี้ยังไม่มีข้อมูลราคาในแอป"
    else:
        try:
            events, odds_ts = await state["odds"].events(sport)
            ev = analysis.find_event(events, m, idx)
            if ev is None:
                market_msg = "ยังไม่พบราคาของแมตช์นี้ (อาจยังไม่เปิดตลาด หรือชื่อทีมไม่ตรงกับระบบ)"
            else:
                market = analysis.market_block(ev, state["store"], m["id"], odds_ts, pred["probs"])
        except UpstreamError as e:
            market_msg = str(e)

    note = ("รายการนี้มีทีมต่างลีก โมเดลไม่ได้ปรับตามระดับความแข็งของแต่ละลีก"
            if code == "CL" else None)
    P = [round(x * 100, 1) for x in pred["probs"]]
    top = pred["top_scores"]
    return {
        "match": {"id": m["id"], "utc": m["utc"], "competition": code,
                  "competition_name": COMPETITIONS[code], "home": m["home_name"], "away": m["away_name"]},
        "prediction": {
            "expected_goals": {"home": round(pred["lam_h"], 2), "away": round(pred["lam_a"], 2)},
            "most_likely_score": {"score": top[0]["score"], "p": round(top[0]["p"] * 100, 1)},
            "top_scores": [{"score": t["score"], "p": round(t["p"] * 100, 1)} for t in top],
            "probs": {"home": P[0], "draw": P[1], "away": P[2]},
            "over": {str(k): round(v * 100, 1) for k, v in pred["over"].items()},
            "btts": round(pred["btts"] * 100, 1),
            "total_mode": pred["total_mode"],
        },
        "form": {"home": analysis.form(pred["home_rows"]), "away": analysis.form(pred["away_rows"])},
        "confidence": analysis.confidence(pred["n_home"], pred["n_away"], pred["league_based"], market, note),
        "market": market,
        "market_message": market_msg,
        "lineups": {"status": "unavailable",
                    "message": "ยังไม่มีข้อมูลรายชื่อตัวจริงจากแหล่งข้อมูลที่เชื่อมต่อ"},
        "sources": {"fixtures_fetched_at": min(t for _, t in seasons.values()),
                    "odds_fetched_at": odds_ts},
        "skipped": [c for c, _ in skipped],
        "generated_at": time.time(),
        "disclaimer": DISCLAIMER,
    }
