"""ตัวเชื่อมต่อผู้ให้ข้อมูล พร้อมแคช

- football-data.org (v4): รายชื่อทีม ผลการแข่งขัน ตารางแข่งขัน  (ต้องมี token)
- The Odds API (v4): ราคา 1X2 จากหลายเจ้ามือ  (ต้องมี key และมีโควตาตามแพ็กเกจ)

ทุกฟังก์ชันคืน (ข้อมูล, เวลาที่ดึงมา) เพื่อให้หน้าเว็บแสดงเวลาอัปเดตได้
ถ้าดึงใหม่ไม่ได้ จะใช้ข้อมูลในแคชเก่าแทน และเวลาจะบอกว่าเก่าแค่ไหน
"""

import asyncio
import time

import httpx

FD_BASE = "https://api.football-data.org/v4"
ODDS_BASE = "https://api.the-odds-api.com/v4"

# รหัสรายการแข่งขันใน football-data -> sport key ใน The Odds API
SPORT_KEYS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CL": "soccer_uefa_champs_league",
}


class UpstreamError(Exception):
    """ข้อผิดพลาดจากผู้ให้ข้อมูลภายนอก ข้อความเป็นภาษาไทยและแสดงให้ผู้ใช้เห็นได้"""


class Cache:
    def __init__(self):
        self._d: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key, ttl, loader):
        hit = self._d.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1], hit[0]
        async with self._lock:
            hit = self._d.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1], hit[0]
            try:
                value = await loader()
            except UpstreamError:
                if hit:  # ดึงใหม่ไม่ได้ ใช้ของเก่าแทน (เวลาจะบอกผู้ใช้)
                    return hit[1], hit[0]
                raise
            now = time.time()
            self._d[key] = (now, value)
            return value, now


def _parse_match(m):
    ft = (m.get("score") or {}).get("fullTime") or {}
    home, away = m["homeTeam"], m["awayTeam"]
    return {
        "id": m["id"], "utc": m["utcDate"], "status": m["status"],
        "home_id": home.get("id"), "away_id": away.get("id"),
        "home_name": home.get("name") or "", "away_name": away.get("name") or "",
        "hg": ft.get("home"), "ag": ft.get("away"),
    }


class FootballData:
    def __init__(self, token: str, client: httpx.AsyncClient, cache: Cache):
        self.token = token
        self.client = client
        self.cache = cache

    async def _get(self, path: str, params=None):
        if not self.token:
            raise UpstreamError("ยังไม่ได้ตั้งค่า FOOTBALL_DATA_TOKEN")
        try:
            r = await self.client.get(FD_BASE + path, params=params,
                                      headers={"X-Auth-Token": self.token}, timeout=20)
        except httpx.HTTPError as e:
            raise UpstreamError("เชื่อมต่อ football-data.org ไม่ได้ ลองใหม่อีกครั้ง") from e
        if r.status_code == 429:
            raise UpstreamError("เรียก football-data.org ถี่เกินกำหนด กรุณารอสักครู่")
        if r.status_code in (401, 403):
            raise UpstreamError("football-data.org ปฏิเสธการเข้าถึง ตรวจสอบ token และสิทธิ์ของแพ็กเกจ")
        if r.status_code >= 400:
            raise UpstreamError(f"football-data.org ตอบกลับผิดปกติ ({r.status_code})")
        return r.json()

    async def teams(self, code: str):
        async def load():
            data = await self._get(f"/competitions/{code}/teams")
            return [{"id": t["id"], "name": t["name"], "short": t.get("shortName"), "tla": t.get("tla")}
                    for t in data.get("teams", [])]
        return await self.cache.get(f"teams:{code}", 86400, load)

    async def season(self, code: str):
        """แมตช์ทั้งฤดูกาลปัจจุบัน (ทั้งที่จบแล้วและยังไม่เริ่ม) ใช้แคช 1 ชั่วโมง"""
        async def load():
            data = await self._get(f"/competitions/{code}/matches")
            parsed = [_parse_match(m) for m in data.get("matches", [])]
            return [p for p in parsed if p["home_id"] and p["away_id"]]
        return await self.cache.get(f"season:{code}", 3600, load)


class OddsAPI:
    def __init__(self, key: str, client: httpx.AsyncClient, cache: Cache):
        self.key = key
        self.client = client
        self.cache = cache

    async def events(self, sport: str):
        async def load():
            if not self.key:
                raise UpstreamError("ยังไม่ได้ตั้งค่า ODDS_API_KEY จึงยังไม่มีข้อมูลราคา")
            try:
                r = await self.client.get(
                    f"{ODDS_BASE}/sports/{sport}/odds",
                    params={"apiKey": self.key, "regions": "eu", "markets": "h2h",
                            "oddsFormat": "decimal", "dateFormat": "iso"},
                    timeout=20)
            except httpx.HTTPError as e:
                raise UpstreamError("เชื่อมต่อ The Odds API ไม่ได้ ลองใหม่อีกครั้ง") from e
            if r.status_code in (401, 403):
                raise UpstreamError("The Odds API key ไม่ถูกต้อง หรือสิทธิ์ไม่ครอบคลุมรายการนี้")
            if r.status_code == 429:
                raise UpstreamError("โควตา The Odds API หมดแล้วในรอบนี้")
            if r.status_code >= 400:
                raise UpstreamError(f"The Odds API ตอบกลับผิดปกติ ({r.status_code})")
            return r.json()
        return await self.cache.get(f"odds:{sport}", 900, load)
