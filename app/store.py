"""เก็บ snapshot ราคาแต่ละครั้งที่ดึงมา เพื่อดูการเคลื่อนไหวของราคาเอง

หมายเหตุ: แอปไม่มีราคาเปิดจริงจากเจ้ามือ จึงใช้ "ราคาแรกที่แอปบันทึก" เป็นจุดเริ่มต้น
"""

import sqlite3


class Store:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.executescript("""
            create table if not exists odds_snapshots (
                match_id integer not null, fetched_at real not null, book text not null,
                h real not null, d real not null, a real not null,
                primary key (match_id, fetched_at, book));
            create table if not exists spread_snapshots (
                match_id integer not null, fetched_at real not null, book text not null,
                line real not null, ph real not null, pa real not null,
                primary key (match_id, fetched_at, book));
            create table if not exists watch (
                match_id integer primary key, code text not null, utc text not null);
        """)
        self.con.commit()

    def record(self, match_id: int, fetched_at: float, rows):
        """rows: iterable ของ (book, h, d, a) ซ้ำกันจะถูกข้าม"""
        self.con.executemany("insert or ignore into odds_snapshots values (?,?,?,?,?,?)",
                             [(match_id, fetched_at, b, h, d, a) for b, h, d, a in rows])
        self.con.commit()

    def history(self, match_id: int) -> dict[str, list[tuple]]:
        out: dict[str, list[tuple]] = {}
        for book, t, h, d, a in self.con.execute(
                "select book, fetched_at, h, d, a from odds_snapshots "
                "where match_id=? order by fetched_at", (match_id,)):
            out.setdefault(book, []).append((t, h, d, a))
        return out

    def record_spreads(self, match_id: int, fetched_at: float, rows):
        """rows: iterable ของ (book, line_เหย้า, ราคาเหย้า, ราคาเยือน)"""
        self.con.executemany("insert or ignore into spread_snapshots values (?,?,?,?,?,?)",
                             [(match_id, fetched_at, b, ln, ph, pa) for b, ln, ph, pa in rows])
        self.con.commit()

    def spread_history(self, match_id: int) -> dict[str, list[tuple]]:
        out: dict[str, list[tuple]] = {}
        for book, t, ln, ph, pa in self.con.execute(
                "select book, fetched_at, line, ph, pa from spread_snapshots "
                "where match_id=? order by fetched_at", (match_id,)):
            out.setdefault(book, []).append((t, ln, ph, pa))
        return out

    def watch(self, match_id: int, code: str, utc: str):
        self.con.execute("insert or replace into watch values (?,?,?)", (match_id, code, utc))
        self.con.commit()

    def watched(self):
        return list(self.con.execute("select match_id, code, utc from watch"))
