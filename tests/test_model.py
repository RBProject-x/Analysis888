from app import analysis, model


def _season():
    """ทีม 1 แข่งชนะทุกนัด 3-0 ทีม 2 แพ้ทุกนัด 0-3 (สลับเหย้า/เยือน)"""
    out = []
    for i in range(40):
        if i % 2 == 0:
            hg, ag, h, a = 3, 0, 1, 2
        else:
            hg, ag, h, a = 0, 3, 2, 1
        out.append({"id": i, "utc": f"2025-01-{(i // 2) % 28 + 1:02d}T15:00:00Z",
                    "status": "FINISHED", "home_id": h, "away_id": a,
                    "home_name": f"T{h}", "away_name": f"T{a}", "hg": hg, "ag": ag})
    return out


def test_score_matrix_normalized():
    M = model.score_matrix(1.4, 1.1)
    assert abs(sum(map(sum, M)) - 1) < 1e-9
    s = model.summarize(M)
    assert abs(s["home"] + s["draw"] + s["away"] - 1) < 1e-9
    assert 0 < s["btts"] < 1
    assert s["over"][1.5] > s["over"][2.5] > s["over"][3.5]


def test_stronger_team_is_favoured():
    pred = analysis.predict(_season(), home_id=1, away_id=2)
    assert pred["probs"][0] > 0.8
    assert pred["lam_h"] > pred["lam_a"]


def test_league_averages_default_when_small_sample():
    assert model.league_averages([])[2] is False
    h, a, ok = model.league_averages(_season())
    assert ok and abs(h - 1.5) < 1e-9 and abs(a - 1.5) < 1e-9


def test_team_rows_most_recent_first():
    rows = model.team_rows(_season(), team_id=1, n=10)
    assert len(rows) == 10
    assert rows[0]["id"] > rows[1]["id"]
    assert rows[0]["w"] == 1.0


def test_form_letters():
    f = analysis.form(model.team_rows(_season(), team_id=1))
    assert f[0]["result"] == "W" and f[0]["score"] == "3-0"


def test_implied_removes_overround():
    probs, overround = analysis.implied([2.0, 3.5, 4.0])
    assert abs(sum(probs) - 1) < 1e-9
    assert overround > 0


def test_confidence_levels():
    assert analysis.confidence(3, 3, False, None)["level"] == "ต่ำ"
    assert analysis.confidence(10, 10, True, {"available": True, "books": 3,
                                              "edge_points": [1, 2, -1]})["level"] == "สูง"
