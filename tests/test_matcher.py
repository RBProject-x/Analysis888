from app.matcher import Team, TeamIndex, normalize

TEAMS = [
    Team(57, "Arsenal FC", "Arsenal", "ARS"),
    Team(66, "Manchester United FC", "Man United", "MUN"),
    Team(65, "Manchester City FC", "Manchester City", "MCI"),
    Team(73, "Tottenham Hotspur FC", "Tottenham Hotspur", "TOT"),
    Team(67, "Newcastle United FC", "Newcastle United", "NEW"),
    Team(76, "Wolverhampton Wanderers FC", "Wolves", "WOL"),
    Team(78, "Brighton & Hove Albion FC", "Brighton & Hove Albion", "BHA"),
    Team(351, "Nottingham Forest FC", "Nottingham Forest", "NFO"),
    Team(64, "Liverpool FC", "Liverpool", "LIV"),
    Team(61, "Chelsea FC", "Chelsea", "CHE"),
]
IDX = TeamIndex(TEAMS)


def test_normalize_strips_suffixes_and_accents():
    assert normalize("Arsenal FC") == normalize("Arsenal") == "arsenal"
    assert normalize("Atlético de Madrid") == normalize("Atletico Madrid")
    assert normalize("Brighton & Hove Albion") == normalize("Brighton and Hove Albion")


def test_exact_and_suffix_variants():
    assert IDX.match("Arsenal").team.id == 57
    assert IDX.match("Arsenal FC").team.id == 57
    assert IDX.match("Liverpool FC").team.id == 64


def test_abbreviation_expansion():
    assert IDX.match("Man Utd").team.id == 66
    assert IDX.match("Manchester Utd").team.id == 66
    assert IDX.match("Man City").team.id == 65


def test_alias_table():
    assert IDX.match("Spurs").team.id == 73
    assert IDX.match("Wolves").team.id == 76
    assert IDX.match("Nott'm Forest").team.id == 351


def test_thai_alias():
    assert IDX.match("อาร์เซนอล").team.id == 57
    assert IDX.match("แมนยู").team.id == 66
    assert IDX.match("เชลซี").team.id == 61


def test_ambiguous_returns_none():
    # "Manchester" ตรงทั้งยูไนเต็ดและซิตี้ ต้องไม่เดา
    assert IDX.match("Manchester") is None


def test_unknown_team_returns_none():
    assert IDX.match("Real Madrid") is None
    assert IDX.match("") is None


def test_search_prefix_and_alias():
    ids = [t.id for t in IDX.search("ars")]
    assert 57 in ids
    assert [t.id for t in IDX.search("Spurs")][0] == 73
    assert [t.id for t in IDX.search("อาร์เซนอล")][0] == 57
