"""จับคู่ชื่อทีมจากหลายแหล่งข้อมูลให้เป็นทีมเดียวกัน

ลำดับการจับคู่:
1. ชื่อตรงกันหลัง normalize (ตัด FC/CF/AC ฯลฯ, ตัดเครื่องหมาย, ขยาย Man -> Manchester)
2. ตารางชื่อเรียก (app/data/aliases.json) เช่น Spurs, Man Utd, อาร์เซนอล
3. fuzzy matching พร้อมตรวจความกำกวม ถ้าคะแนนสองอันดับแรกใกล้กันจะคืน None ไม่เดา
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

ALIAS_FILE = Path(__file__).parent / "data" / "aliases.json"
STOPWORDS = {"fc", "afc", "cf", "sc", "ac", "as", "ss", "ssc", "ud", "cd", "sad",
             "club", "the", "de", "sv", "fk", "sk"}
EXPAND = {"man": "manchester", "utd": "united", "st": "saint"}


def normalize(name: str) -> str:
    """ทำให้ชื่อทีมอยู่ในรูปเดียวกันเพื่อเทียบ (ใช้ทั้งฝั่งข้อมูลและฝั่งผู้ใช้)"""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[.\-'’`,()/:]+", " ", s)
    return " ".join(EXPAND.get(t, t) for t in s.split() if t not in STOPWORDS)


@dataclass(frozen=True)
class Team:
    id: int
    name: str
    short: str | None = None
    tla: str | None = None


@dataclass(frozen=True)
class MatchResult:
    team: Team
    score: float
    method: str  # exact | alias | fuzzy


def _load_aliases() -> dict[str, str]:
    data = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    table: dict[str, str] = {}
    for canonical, alts in data.items():
        target = normalize(canonical)
        for alt in alts:
            table[normalize(alt)] = target
    return table


class TeamIndex:
    def __init__(self, teams, min_score: float = 0.9, margin: float = 0.04):
        self.teams = list(teams)
        self.min_score = min_score
        self.margin = margin
        self.by_key: dict[str, Team] = {}
        for t in self.teams:
            for name in (t.name, t.short):
                if name:
                    self.by_key.setdefault(normalize(name), t)
        self.aliases = _load_aliases()

    def match(self, external: str) -> MatchResult | None:
        q = normalize(external)
        if not q:
            return None
        if q in self.by_key:
            return MatchResult(self.by_key[q], 1.0, "exact")
        target = self.aliases.get(q)
        if target and target in self.by_key:
            return MatchResult(self.by_key[target], 1.0, "alias")

        best: dict[int, tuple[float, Team]] = {}
        for key, t in self.by_key.items():
            s = max(fuzz.token_set_ratio(q, key), fuzz.token_sort_ratio(q, key)) / 100
            if s > best.get(t.id, (0.0, t))[0]:
                best[t.id] = (s, t)
        ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)
        if not ranked:
            return None
        top_s, top_t = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        if top_s >= self.min_score and top_s - second >= self.margin:
            return MatchResult(top_t, top_s, "fuzzy")
        return None

    def search(self, query: str, limit: int = 8) -> list[Team]:
        """ค้นหาทีมสำหรับช่องพิมพ์ (รองรับพิมพ์บางส่วนและชื่อเรียก)"""
        nq = normalize(query)
        if not nq:
            return []
        alias_target = self.aliases.get(nq)
        scored = []
        for t in self.teams:
            keys = [normalize(t.name)] + ([normalize(t.short)] if t.short else [])
            sc = max(max(fuzz.partial_ratio(nq, k), fuzz.token_set_ratio(nq, k)) for k in keys)
            if alias_target and alias_target in keys:
                sc = 101
            if sc >= 70:
                scored.append((sc, t))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [t for _, t in scored[:limit]]
