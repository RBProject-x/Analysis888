"""โมเดลคาดการณ์: ความแข็งแกร่งเหย้า/เยือน + Poisson + การแก้สกอร์ต่ำแบบ Dixon-Coles

ไฟล์นี้ไม่เรียกเครือข่าย ทุกฟังก์ชันรับข้อมูลเข้าแล้วคืนผลลัพธ์ จึงทดสอบได้ง่าย
ค่าคงที่ทั้งหมดเป็นค่าตั้งต้นที่ยังไม่ได้ปรับเทียบ (calibrate) กับผลย้อนหลังจริง
"""

import math

RHO = -0.08            # Dixon-Coles: ปรับความน่าจะเป็นสกอร์ 0-0, 1-0, 0-1, 1-1
DECAY = 0.9            # นัดล่าสุดมีน้ำหนักมากกว่านัดเก่า (0.9^i)
SHRINK = 4.0           # จำนวนนัดสมมติที่ดึงค่าความแข็งแกร่งเข้าหาค่ากลางลีก
N_GOALS = 11           # นับ 0..10 ประตูต่อทีม
DEFAULT_LEAGUE = (1.50, 1.20)  # (เหย้า, เยือน) ใช้เมื่อข้อมูลลีกไม่พอ
MIN_LEAGUE_MATCHES = 30
LAM_MIN, LAM_MAX = 0.1, 5.0


def league_averages(finished):
    """ค่าเฉลี่ยประตูทีมเหย้าและทีมเยือนของลีก (finished ต้องเป็นแมตช์ที่จบแล้ว)"""
    if len(finished) < MIN_LEAGUE_MATCHES:
        return DEFAULT_LEAGUE[0], DEFAULT_LEAGUE[1], False
    n = len(finished)
    return (sum(m["hg"] for m in finished) / n,
            sum(m["ag"] for m in finished) / n, True)


def team_rows(finished, team_id, n=10):
    """นัดล่าสุดของทีม เรียงใหม่สุดก่อน พร้อมน้ำหนักและสถานะเหย้า/เยือน

    finished ต้องเรียงตามเวลาจากเก่าไปใหม่
    """
    mine = [m for m in finished if team_id in (m["home_id"], m["away_id"])][-n:]
    rows = []
    for i, m in enumerate(reversed(mine)):
        home = m["home_id"] == team_id
        gf, ga = (m["hg"], m["ag"]) if home else (m["ag"], m["hg"])
        rows.append({
            "w": DECAY ** i, "gf": gf, "ga": ga, "home": home,
            "opp": m["away_name"] if home else m["home_name"],
            "utc": m["utc"], "id": m["id"],
        })
    return rows


def _strength(rows, lh, la, k=SHRINK):
    """คืน (ความสามารถในการยิง, ความอ่อนแอในการรับ) เทียบกับค่าเฉลี่ยลีกตามบทบาทเหย้า/เยือน"""
    if not rows:
        return 1.0, 1.0
    sw = sum(r["w"] for r in rows)
    att = sum(r["w"] * r["gf"] / (lh if r["home"] else la) for r in rows)
    conc = sum(r["w"] * r["ga"] / (la if r["home"] else lh) for r in rows)
    return (att + k) / (sw + k), (conc + k) / (sw + k)


def _venue(rows, home):
    """ใช้เฉพาะนัดเหย้าหรือเยือนถ้ามีอย่างน้อย 3 นัด มิฉะนั้นใช้ทุกนัด"""
    v = [r for r in rows if r["home"] == home]
    return v if len(v) >= 3 else rows


def expected_goals(home_rows, away_rows, lh, la):
    """ประตูคาดหมาย (λ) ของทีมเหย้าและเยือน"""
    att_h, conc_h = _strength(_venue(home_rows, True), lh, la)
    att_a, conc_a = _strength(_venue(away_rows, False), lh, la)
    lam_h = att_h * conc_a * lh
    lam_a = att_a * conc_h * la
    return min(max(lam_h, LAM_MIN), LAM_MAX), min(max(lam_a, LAM_MIN), LAM_MAX)


def _poisson(lam, n=N_GOALS):
    p = [math.exp(-lam)]
    for k in range(1, n):
        p.append(p[-1] * lam / k)
    return p


def _tau(i, j, lh, la, rho):
    if i == 0 and j == 0:
        t = 1 - lh * la * rho
    elif i == 0 and j == 1:
        t = 1 + lh * rho
    elif i == 1 and j == 0:
        t = 1 + la * rho
    elif i == 1 and j == 1:
        t = 1 - rho
    else:
        t = 1.0
    return max(t, 0.0)


def score_matrix(lh, la, rho=RHO):
    """เมทริกซ์ความน่าจะเป็นของสกอร์ M[i][j] = P(เหย้า i ประตู, เยือน j ประตู) ที่ normalize แล้ว"""
    ph, pa = _poisson(lh), _poisson(la)
    m = [[ph[i] * pa[j] * _tau(i, j, lh, la, rho) for j in range(N_GOALS)]
         for i in range(N_GOALS)]
    s = sum(map(sum, m))
    return [[v / s for v in row] for row in m]


def summarize(M):
    """สรุปจากเมทริกซ์: 1X2, BTTS, สูง/ต่ำ, สกอร์เด่น, จำนวนประตูที่น่าจะเป็นที่สุด"""
    n = len(M)
    home = draw = away = btts = 0.0
    total = [0.0] * (2 * n - 1)
    cells = []
    for i in range(n):
        for j in range(n):
            p = M[i][j]
            if i > j:
                home += p
            elif i < j:
                away += p
            else:
                draw += p
            if i and j:
                btts += p
            total[i + j] += p
            cells.append((p, i, j))
    cells.sort(reverse=True)
    over = {x: sum(total[t] for t in range(len(total)) if t > x) for x in (1.5, 2.5, 3.5)}
    return {
        "home": home, "draw": draw, "away": away, "btts": btts, "over": over,
        "top_scores": [{"score": f"{i}-{j}", "p": p} for p, i, j in cells[:5]],
        "total_mode": max(range(len(total)), key=lambda t: total[t]),
    }
