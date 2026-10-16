from fastapi.testclient import TestClient

from app import main

FUTURE = "2030-01-01T15:00:00Z"
TEAMS = [
    {"id": 57, "name": "Arsenal FC", "short": "Arsenal", "tla": "ARS"},
    {"id": 66, "name": "Manchester United FC", "short": "Man United", "tla": "MUN"},
    {"id": 73, "name": "Tottenham Hotspur FC", "short": "Tottenham Hotspur", "tla": "TOT"},
    {"id": 61, "name": "Chelsea FC", "short": "Chelsea", "tla": "CHE"},
]
NAMES = {t["id"]: t["name"] for t in TEAMS}


def _season():
    pairs = [(57, 66), (73, 61), (57, 73), (66, 61), (57, 61), (66, 73)]
    out, mid = [], 1
    for rnd in range(8):
        for h, a in pairs:
            out.append({"id": mid, "utc": f"2025-01-{rnd + 1:02d}T15:00:00Z", "status": "FINISHED",
                        "home_id": h, "away_id": a, "home_name": NAMES[h], "away_name": NAMES[a],
                        "hg": (rnd + h) % 3, "ag": (rnd + a) % 2})
            mid += 1
    out.append({"id": 999, "utc": FUTURE, "status": "SCHEDULED", "home_id": 57, "away_id": 66,
                "home_name": "Arsenal FC", "away_name": "Manchester United FC", "hg": None, "ag": None})
    return out


class FakeFD:
    async def season(self, code):
        return _season(), 1_700_000_000.0

    async def teams(self, code):
        return TEAMS, 1_700_000_000.0


class FakeOdds:
    async def events(self, sport):
        bks = [("b1", "Book One", 2.10, 3.40, 3.50), ("b2", "Book Two", 2.05, 3.45, 3.60),
               ("b3", "Book Three", 2.15, 3.35, 3.45)]
        evs = [{"commence_time": FUTURE, "home_team": "Arsenal", "away_team": "Manchester United",
                "bookmakers": [{"key": k, "title": t, "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Arsenal", "price": h}, {"name": "Draw", "price": d},
                    {"name": "Manchester United", "price": a}]}]} for k, t, h, d, a in bks]}]
        return evs, 1_700_000_100.0


def test_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    with TestClient(main.app) as c:
        main.state["fd"] = FakeFD()
        main.state["odds"] = FakeOdds()

        r = c.get("/api/teams", params={"competition": "PL", "q": "Spurs"})
        assert r.status_code == 200 and r.json()["teams"][0]["id"] == 73

        r = c.get("/api/fixtures", params={"competition": "PL", "team_id": 57})
        assert r.status_code == 200 and r.json()["fixtures"][0]["id"] == 999

        r = c.get("/api/analysis", params={"competition": "PL", "match_id": 999})
        assert r.status_code == 200, r.text
        body = r.json()
        probs = body["prediction"]["probs"]
        assert abs(probs["home"] + probs["draw"] + probs["away"] - 100) < 0.5
        assert body["market"]["available"] is True
        assert body["market"]["books"] == 3
        assert body["lineups"]["status"] == "unavailable"
        assert body["confidence"]["level"] in ("สูง", "ปานกลาง", "ต่ำ")

        # เรียกซ้ำ ควรไม่บันทึกซ้ำเมื่อ timestamp เดิม
        c.get("/api/analysis", params={"competition": "PL", "match_id": 999})
        hist = main.state["store"].history(999)
        assert all(len(rows) == 1 for rows in hist.values())

        r = c.get("/api/analysis", params={"competition": "PL", "match_id": 12345})
        assert r.status_code == 404
