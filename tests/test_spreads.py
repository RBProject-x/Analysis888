import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app import analysis, main
from app.providers import Cache, OddsAPI
from tests.test_api import TEAMS, FakeFD, _season

KICK = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(line):
    return {"home_team": "Arsenal", "away_team": "Manchester United", "bookmakers": [
        {"key": "b1", "title": "Book One", "markets": [{"key": "spreads", "outcomes": [
            {"name": "Arsenal", "price": 1.95, "point": line},
            {"name": "Manchester United", "price": 1.95, "point": -line}]}]}]}


def test_book_spreads_parses_home_line():
    out = analysis.book_spreads(_ev(-0.25))
    assert out["b1"][1] == -0.25


def test_line_formatting():
    assert analysis.fmt_line(-0.25) == "-0.25"
    assert analysis.fmt_line(0.75) == "+0.75"
    assert analysis.fmt_line(-0.5) == "-0.5"
    assert analysis.fmt_line(0) == "0"
    assert "เหย้าต่อ 0.25" in analysis.line_text(-0.25)
    assert "เหย้ารอง 0.75" in analysis.line_text(0.75)


def test_big_handicap_move_flips_side():
    hist = {k: [(1000, -0.25, 1.9, 1.9), (2000, 0.75, 1.9, 1.9)] for k in "abc"}
    (s,) = analysis.spread_signals(hist)
    assert s["big"] is True and s["level"] == "warn"
    assert "-0.25" in s["detail"] and "+0.75" in s["detail"] and "สลับฝั่ง" in s["detail"]


def test_small_and_no_handicap_move():
    small = {k: [(1000, -0.5, 1.9, 1.9), (2000, -0.75, 1.9, 1.9)] for k in "ab"}
    (s,) = analysis.spread_signals(small)
    assert not s.get("big") and s["level"] == "info"

    flat = {"a": [(1000, -0.5, 1.9, 1.9), (2000, -0.5, 1.9, 1.9)]}
    assert analysis.spread_signals(flat)[0]["level"] == "ok"
    assert analysis.spread_signals({"a": [(1000, -0.5, 1.9, 1.9)]}) == []


def test_big_1x2_move_is_flagged():
    hist = {"a": [(1000, 2.0, 3.4, 3.8), (2000, 1.6, 4.0, 5.5)],
            "b": [(1000, 2.05, 3.4, 3.7), (2000, 1.65, 4.0, 5.2)]}
    sig = analysis.movement_signals(hist)
    assert any(s.get("big") for s in sig)


def test_spreads_fallback_to_h2h_on_422():
    calls = []

    def handler(req):
        calls.append(req.url.params["markets"])
        if "spreads" in req.url.params["markets"]:
            return httpx.Response(422, json={"message": "INVALID_MARKET"})
        return httpx.Response(200, json=[])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await OddsAPI("k", c, Cache()).events("soccer_epl")

    events, _ = asyncio.run(go())
    assert events == [] and calls == ["h2h,spreads", "h2h"]


class FD(FakeFD):
    async def season(self, code):
        s = _season()
        s[-1]["utc"] = KICK
        return s, 1_700_000_000.0


class Odds:
    def __init__(self):
        self.n = 0

    async def events(self, sport):
        n, self.n = self.n, self.n + 1
        line = -0.25 if n == 0 else 0.75
        bks = [{"key": k, "title": t, "markets": [
            {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": h}, {"name": "Draw", "price": d},
                                        {"name": "Manchester United", "price": a}]},
            {"key": "spreads", "outcomes": [{"name": "Arsenal", "price": 1.95, "point": line},
                                            {"name": "Manchester United", "price": 1.95, "point": -line}]}]}
            for k, t, h, d, a in [("b1", "Book One", 2.1, 3.4, 3.5), ("b2", "Book Two", 2.05, 3.45, 3.6),
                                  ("b3", "Book Three", 2.15, 3.35, 3.45)]]
        return [{"commence_time": KICK, "home_team": "Arsenal", "away_team": "Manchester United",
                 "bookmakers": bks}], 1_700_000_000.0 + n * 1000


def test_flow_detects_handicap_move_and_polls(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    with TestClient(main.app) as c:
        main.state["fd"], main.state["odds"] = FD(), Odds()

        first = c.get("/api/analysis", params={"match_id": 999}).json()
        assert first["market"]["alerts"] == []
        assert first["market"]["handicap"]["line"] == -0.25

        second = c.get("/api/analysis", params={"match_id": 999}).json()
        alerts = second["market"]["alerts"]
        assert alerts and any("+0.75" in a["detail"] for a in alerts)
        assert second["market"]["handicap"]["line"] == 0.75

        assert asyncio.run(main.poll_once()) == 1
        assert all(len(v) == 3 for v in main.state["store"].spread_history(999).values())


def test_search_without_competition(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    with TestClient(main.app) as c:
        main.state["fd"] = FD()
        body = c.get("/api/teams", params={"q": "Man Utd"}).json()
        assert body["teams"][0]["id"] == 66 and body["teams"][0]["comps"]
        fx = c.get("/api/fixtures", params={"team_id": 66}).json()["fixtures"]
        assert fx and fx[0]["competition_name"]
