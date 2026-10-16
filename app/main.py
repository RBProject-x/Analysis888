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

COMPETITIONS = {
    "PL": "Premier League", "PD": "La Liga", "SA": "Serie A",
    "BL1": "Bundesliga", "FL1": "Ligue 1", "CL": "Champions League",
}
INDEX_HTML = Path(__file__).parent / "static" / "index.html"
DISCLAIMER = "ผลเป็นการคาดการณ์เชิงสถิติ ไม่ใช่ผลที่รับประกัน และไม่ใช่คำแนะนำการเดิมพัน"

state: dict = {}


@asynccontextmanager
async def lifespan(_app):
    client = httpx.AsyncClient()
    cache = Cache()
    db_path = os.getenv("DB_PATH", "data/odds.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    state["fd"] = FootballData(os.getenv("FOOTBALL_DATA_TOKEN", ""), client, cache)
    state["odds"] = OddsAPI(os.getenv("ODDS_API_KEY", ""), client, cache)
    state["store"] = Store(db_path)
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="Football Analyzer", version="1.0.0", lifespan=lifespan)


@app.exception_handler(UpstreamError)
async def upstream_error(_req: Request, exc: UpstreamError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def _comp(code: str) -> str:
    code = code.upper()
    if code not in COMPETITIONS:
        raise HTTPException(400, "ไม่รองรับรายการแข่งขันนี้")
    return code


async def _team_index(code: str):
    teams, fetched = await state["fd"].teams(code)
    return TeamIndex([Team(**t) for t in teams]), fetched


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
async def teams(competition: str, q: str = Query("", max_length=60)):
    idx, _ = await _team_index(_comp(competition))
    return {"teams": [{"id": t.id, "name": t.name} for t in idx.search(q)]}


@app.get("/api/fixtures")
async def fixtures(competition: str, team_id: int):
    code = _comp(competition)
    season, fetched = await state["fd"].season(code)
    now = time.time()
    rows = [m for m in season
            if m["status"] in ("SCHEDULED", "TIMED")
            and team_id in (m["home_id"], m["away_id"])
            and analysis.ts(m["utc"]) >= now - 3 * 3600]
    rows.sort(key=lambda m: m["utc"])
    return {
        "fixtures": [{"id": m["id"], "utc": m["utc"], "home": m["home_name"], "away": m["away_name"]}
                     for m in rows[:8]],
        "fetched_at": fetched,
    }


@app.get("/api/analysis")
async def analysis_route(competition: str, match_id: int):
    code = _comp(competition)
    season, season_ts = await state["fd"].season(code)
    m = next((x for x in season if x["id"] == match_id), None)
    if m is None:
        raise HTTPException(404, "ไม่พบแมตช์นี้ในฤดูกาลปัจจุบัน")
    if m["status"] not in ("SCHEDULED", "TIMED"):
        raise HTTPException(400, "วิเคราะห์ได้เฉพาะแมตช์ที่ยังไม่เริ่ม")

    finished = sorted((x for x in season if x["status"] == "FINISHED" and x["hg"] is not None),
                      key=lambda x: x["utc"])
    pred = analysis.predict(finished, m["home_id"], m["away_id"])

    idx, teams_ts = await _team_index(code)
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

    P = [round(x * 100, 1) for x in pred["probs"]]
    top = pred["top_scores"]
    return {
        "match": {"id": m["id"], "utc": m["utc"], "competition": code,
                  "home": m["home_name"], "away": m["away_name"]},
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
        "confidence": analysis.confidence(pred["n_home"], pred["n_away"], pred["league_based"], market),
        "market": market,
        "market_message": market_msg,
        "lineups": {"status": "unavailable",
                    "message": "ยังไม่มีข้อมูลรายชื่อตัวจริงจากแหล่งข้อมูลที่เชื่อมต่อ"},
        "sources": {"fixtures_fetched_at": season_ts, "teams_fetched_at": teams_ts,
                    "odds_fetched_at": odds_ts},
        "generated_at": time.time(),
        "disclaimer": DISCLAIMER,
    }
