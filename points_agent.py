#!/usr/bin/env python3
"""
ALY MEDIA - Daily Points Agent
Roz raat ko: employees ki daily log sheet padhta hai -> points nikalta hai ->
professional report workbook banata hai (Dashboard + har employee ka alag tab +
Weekly + Monthly). Boss ko optional email bhi bhejta hai.
"""
import os, re, json, time, smtplib, datetime as dt
from collections import defaultdict
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

# ------------------------- SETTINGS -------------------------
DAILY_TARGET = float(os.getenv("DAILY_TARGET", "100"))
DATE_ORDER = os.getenv("DATE_ORDER", "dmy")          # "Day:1 12/10" = 12 Oct -> dmy
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
IGNORE_TABS = {t.strip().lower() for t in os.getenv("IGNORE_TABS", "").split(",") if t.strip()}
FALLBACK_MINUTES = 10                                 # AI key na ho to unlisted kaam ka estimate
COUNTS_BASE = {"approved", "corrected", "major"}     # in status par base points count hote hain
ADJUSTMENT = {"approved": 1, "corrected": 0, "major": -1, "rejected": -3, "pending": 0}

# (Category, points per piece, aliases)  <- POINT SYSTEM sheet se
SEED_RULES = [
    ("Thumbnail", 5, ""), ("Redesign", 4, ""), ("Islamic Post", 4, ""),
    ("Book Cover", 11, "Book"), ("Post Animation", 23, ""), ("Study Guide", 240, ""),
    ("Montage", 840, ""), ("Reel Animation", 23, ""), ("Reel Layout", 15, ""),
    ("Clip & Reel Video Making & Export", 3, "Video Export, Export, Reel Export, Clip Export"),
    ("Urdu Post Making", 7, "Urdu Post"), ("Islamic Post Making", 7, ""),
    ("New Layout Design", 35, "New Layout"), ("Islamic Video", 50, ""),
    ("Promo", 75, ""), ("Teaser", 60, ""),
]

# ------------------------- HELPERS -------------------------
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()

def classify(status):
    s = status.lower().strip()
    if any(w in s for w in ("not", "reject", "redo")): return "rejected"
    if "major" in s: return "major"
    if "correction" in s or "minor" in s: return "corrected"
    if "approved" in s: return "approved"
    return "pending"

DAY_RE = re.compile(r"\s*day\s*:?\s*(?:\d+\s+)?(\d{1,2})[/\-.](\d{1,2})", re.I)

def make_date(a, b, today):
    d, m = (b, a) if DATE_ORDER == "mdy" else (a, b)
    try:
        x = dt.date(today.year, m, d)
    except ValueError:
        return None
    if x > today + dt.timedelta(days=2):
        x = x.replace(year=today.year - 1)
    return x

def parse_rows(rows, today):
    """Log tab ki rows -> entries. 'Day:1 12/10' wale blocks padhta hai."""
    cols = {"category": 1, "task": 2, "status": 3, "number": 4}
    cur, out = None, []
    for r in rows:
        line = " ".join(c for c in r if c).strip()
        if not line:
            continue
        m = DAY_RE.match(line)
        if m:
            cur = make_date(int(m.group(1)), int(m.group(2)), today)
            continue
        low = [c.strip().lower() for c in r]
        if "category" in low:
            for k in cols:
                if k in low:
                    cols[k] = low.index(k)
            continue
        if cur is None:
            continue
        g = lambda k: r[cols[k]].strip() if cols[k] < len(r) else ""
        if not g("category"):
            continue
        try:
            num = float(g("number") or 0)
        except ValueError:
            num = 0.0
        out.append(dict(date=cur, category=g("category"), task=g("task"),
                        status_raw=g("status"), number=num))
    return out

def build_lookup(values):
    lk = {}
    for r in values[1:]:
        if len(r) < 2 or not r[0].strip():
            continue
        try:
            p = float(r[1])
        except ValueError:
            continue
        src = r[2] if len(r) > 2 else ""
        aliases = r[3].split(",") if len(r) > 3 else []
        for n in [r[0]] + aliases:
            if n.strip():
                lk[norm(n)] = (r[0].strip(), p, src)
    return lk

def estimate_minutes(category, task):
    """Unlisted kaam: Claude se estimate (key ho to), warna 10 min. 4 min = 1 point."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        try:
            import anthropic
            prompt = (f"A design/video media company logged this work item. Category: '{category}'. "
                      f"Task: '{task}'. Estimate the average focused working minutes needed for ONE piece. "
                      'Reply ONLY JSON: {"minutes": <number>, "reason": "<max 12 words>"}')
            msg = anthropic.Anthropic(api_key=key).messages.create(
                model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=200,
                messages=[{"role": "user", "content": prompt}])
            data = json.loads(re.sub(r"```(json)?", "", msg.content[0].text).strip())
            return max(2.0, min(float(data["minutes"]), 2880.0)), str(data.get("reason", ""))
        except Exception as ex:
            print("AI estimate failed:", ex)
    return float(FALLBACK_MINUTES), "no AI estimate - default"

def score(entries, lookup, on_new_rule):
    recs = []
    for e in entries:
        key = norm(e["category"])
        if key not in lookup:
            lookup[key] = on_new_rule(e["category"], e["task"])
        name, ppp, src = lookup[key]
        st = classify(e["status_raw"])
        base = ppp * e["number"]
        counted = base if st in COUNTS_BASE else 0.0
        adj = ADJUSTMENT[st] if e["number"] > 0 else 0
        notes = []
        if "AUTO" in src.upper(): notes.append("Estimated rate - confirm in Points Rules")
        if st in ("rejected", "pending"): notes.append(f"On hold: {base:g} pts after approval")
        recs.append({**e, "category_rule": name, "status": st, "ppp": ppp, "base": counted,
                     "held": base - counted, "adj": adj, "total": counted + adj,
                     "note": "; ".join(notes)})
    return recs

def day_stats(recs):
    d = defaultdict(lambda: dict(points=0.0, held=0.0, ok=0, notok=0, n=0))
    for r in recs:
        s = d[(r["emp"], r["date"])]
        s["points"] += r["total"]; s["held"] += r["held"]; s["n"] += 1
        if r["status"] in COUNTS_BASE: s["ok"] += 1
        else: s["notok"] += 1
    return d

def period(days, emp, a, b):
    pts, n = 0.0, 0
    for (e, d), v in days.items():
        if e == emp and a <= d <= b:
            pts += v["points"]; n += 1
    return pts, n

def pct(avg): return f"{avg / DAILY_TARGET * 100:.0f}%"
def r1(x): return round(x, 1)

# ------------------------- REPORT LAYOUT -------------------------
class Page:
    def __init__(self, ncols): self.rows, self.hdr, self.sec, self.ncols = [], [], [], ncols
    def add(self, row=None, kind=None):
        self.rows.append(row or [])
        n = len(self.rows)
        if kind == "hdr": self.hdr.append(n)
        if kind == "sec": self.sec.append(n)

def dashboard(emps, days, recs, rd):
    p = Page(8)
    p.add(["ALY MEDIA - PERFORMANCE DASHBOARD"])
    p.add([f"Report date: {rd:%d %b %Y}   |   Daily target: {DAILY_TARGET:g} pts   |   "
           f"Updated: {dt.datetime.now(TZ):%d %b %Y %H:%M}"])
    p.add()
    p.add([f"TODAY - {rd:%d %b %Y}"], "sec")
    p.add(["Rank", "Employee", "Points", "Target %", "Approved", "Not approved / Pending", "On-hold pts", "Remarks"], "hdr")
    today = sorted(((e, days.get((e, rd))) for e in emps),
                   key=lambda x: -(x[1]["points"] if x[1] else -1e9))
    for i, (e, v) in enumerate(today, 1):
        if not v:
            p.add([i, e, 0, "0%", 0, 0, 0, "No log submitted"]); continue
        flag = "Check entries (unusually high)" if v["points"] > 2 * DAILY_TARGET else ""
        p.add([i, e, r1(v["points"]), pct(v["points"]), v["ok"], v["notok"], r1(v["held"]), flag])
    ws = rd - dt.timedelta(days=rd.weekday())
    for title, a in ((f"THIS WEEK (from {ws:%d %b})", ws), (f"THIS MONTH - {rd:%B %Y}", rd.replace(day=1))):
        p.add(); p.add([title], "sec")
        p.add(["Rank", "Employee", "Points", "Days logged", "Avg / day", "Target %"], "hdr")
        rows = []
        for e in emps:
            pts, n = period(days, e, a, rd)
            rows.append((e, pts, n, pts / n if n else 0))
        for i, (e, pts, n, avg) in enumerate(sorted(rows, key=lambda x: -x[1]), 1):
            p.add([i, e, r1(pts), n, r1(avg), pct(avg)])
    return p

def employee_page(emp, days, recs, rd):
    p = Page(10)
    ws, ms = rd - dt.timedelta(days=rd.weekday()), rd.replace(day=1)
    p.add([emp.upper()])
    p.add([f"Performance report  |  Updated: {dt.datetime.now(TZ):%d %b %Y %H:%M}"])
    p.add()
    p.add(["Today", "This week", "This month", "All time"], "hdr")
    p.add([r1(period(days, emp, rd, rd)[0]), r1(period(days, emp, ws, rd)[0]),
           r1(period(days, emp, ms, rd)[0]), r1(period(days, emp, dt.date.min, rd)[0])])
    p.add(); p.add(["DAILY SUMMARY"], "sec")
    p.add(["Date", "Entries", "Points", "Target %", "Approved", "Not approved / Pending", "On-hold pts"], "hdr")
    for (e, d), v in sorted(((k, v) for k, v in days.items() if k[0] == emp), key=lambda x: x[0][1], reverse=True):
        p.add([f"{d:%d %b %Y}", v["n"], r1(v["points"]), pct(v["points"]), v["ok"], v["notok"], r1(v["held"])])
    p.add(); p.add(["DETAIL"], "sec")
    p.add(["Date", "Category", "Task", "Status", "Number", "Pts / piece", "Base counted", "Adjustment", "Total", "Note"], "hdr")
    for r in sorted((r for r in recs if r["emp"] == emp), key=lambda r: r["date"], reverse=True):
        p.add([f"{r['date']:%d %b %Y}", r["category"], r["task"], r["status_raw"], r["number"],
               r["ppp"], r1(r["base"]), f"{r['adj']:+d}", r1(r["total"]), r["note"]])
    return p

def weekly_monthly(emps, days, recs):
    wk, mo = Page(6), Page(8)
    wk.add(["WEEKLY REPORT"], "sec")
    wk.add(["Week starting (Mon)", "Employee", "Points", "Days logged", "Avg / day", "Target %"], "hdr")
    mo.add(["MONTHLY REPORT"], "sec")
    mo.add(["Month", "Employee", "Points", "Days logged", "Avg / day", "Target %", "Approval rate", "Rank"], "hdr")
    W, M = defaultdict(lambda: [0.0, 0]), defaultdict(lambda: [0.0, 0, 0, 0])
    for (e, d), v in days.items():
        w = W[(d - dt.timedelta(days=d.weekday()), e)]; w[0] += v["points"]; w[1] += 1
        m = M[(d.replace(day=1), e)]; m[0] += v["points"]; m[1] += 1; m[2] += v["ok"]; m[3] += v["n"]
    for (w, e), (pts, n) in sorted(W.items(), key=lambda x: (-x[0][0].toordinal(), -x[1][0])):
        wk.add([f"{w:%d %b %Y}", e, r1(pts), n, r1(pts / n), pct(pts / n)])
    for month in sorted({k[0] for k in M}, reverse=True):
        ranked = sorted(((e, v) for (m, e), v in M.items() if m == month), key=lambda x: -x[1][0])
        for i, (e, (pts, n, ok, tot)) in enumerate(ranked, 1):
            mo.add([f"{month:%b %Y}", e, r1(pts), n, r1(pts / n), pct(pts / n), f"{ok / tot * 100:.0f}%", i])
    return wk, mo

def boss_text(emps, days, rd):
    lines = [f"ALY MEDIA - Daily Report ({rd:%d %b %Y})", ""]
    for e in sorted(emps, key=lambda e: -(days.get((e, rd), {"points": 0})["points"])):
        v = days.get((e, rd))
        lines.append(f"{e}: {r1(v['points'])} pts ({pct(v['points'])} of target)" if v else f"{e}: no log submitted")
    return "\n".join(lines)

# ------------------------- GOOGLE SHEETS -------------------------
def client():
    import gspread
    from google.oauth2.service_account import Credentials
    raw = os.getenv("GOOGLE_CREDENTIALS")
    info = json.loads(raw) if raw else json.load(open("service_account.json"))
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.readonly"]
    return gspread.authorize(Credentials.from_service_account_info(info, scopes=scopes))

NAVY = {"red": .12, "green": .23, "blue": .37}
HDR = {"backgroundColor": NAVY, "horizontalAlignment": "CENTER",
       "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}
RESET = {"backgroundColor": {"red": 1, "green": 1, "blue": 1},
         "textFormat": {"bold": False, "fontSize": 10, "foregroundColor": {"red": 0, "green": 0, "blue": 0}}}

def write_tab(sh, name, page):
    import gspread
    from gspread.utils import rowcol_to_a1 as a1
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(name, rows=100, cols=12)
    ws.clear()
    ws.resize(rows=max(len(page.rows) + 20, 60), cols=max(page.ncols, 8))
    ws.format("A1:Z2000", RESET)
    ws.update(values=page.rows, range_name="A1")
    fm = [{"range": "A1", "format": {"textFormat": {"bold": True, "fontSize": 16, "foregroundColor": NAVY}}}]
    fm += [{"range": f"{a1(r, 1)}:{a1(r, page.ncols)}", "format": HDR} for r in page.hdr]
    fm += [{"range": a1(r, 1), "format": {"textFormat": {"bold": True, "fontSize": 12, "foregroundColor": NAVY}}} for r in page.sec]
    ws.batch_format(fm)
    try: ws.columns_auto_resize(0, page.ncols - 1)
    except Exception: pass
    time.sleep(2)  # API rate limit
    return ws

def load_rules(sh):
    import gspread
    try:
        ws = sh.worksheet("Points Rules")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Points Rules", rows=300, cols=5)
        ws.update(values=[["Category", "Points per piece", "Source", "Aliases (comma separated)", "Note"]] +
                  [[c, p, "Point System", a, ""] for c, p, a in SEED_RULES], range_name="A1")
        ws.format("A1:E1", HDR)
    return ws, build_lookup(ws.get_all_values())

# ------------------------- MAIN -------------------------
def main():
    gc = client()
    log, sh = gc.open_by_key(os.environ["LOG_SHEET_ID"]), gc.open_by_key(os.environ["REPORT_SHEET_ID"])
    today = dt.datetime.now(TZ).date()
    rd = dt.date.fromisoformat(os.environ["REPORT_DATE"]) if os.getenv("REPORT_DATE") else today

    entries, emps = [], []
    for ws in log.worksheets():
        if ws.title.lower() in IGNORE_TABS: continue
        emps.append(ws.title)
        for e in parse_rows(ws.get_all_values(), today):
            if e["date"] <= rd: entries.append({**e, "emp": ws.title})

    rules_ws, lookup = load_rules(sh)
    def new_rule(cat, task):
        mins, why = estimate_minutes(cat, task)
        pts = round(mins / 4, 2)
        rules_ws.append_row([cat, pts, "AUTO-ESTIMATED (confirm)", "", f"{mins:g} min/piece - {why}"])
        return (cat, pts, "AUTO-ESTIMATED")
    recs = score(entries, lookup, new_rule)
    days = day_stats(recs)

    write_tab(sh, "Dashboard", dashboard(emps, days, recs, rd))
    for e in emps:
        write_tab(sh, e, employee_page(e, days, recs, rd))
    wk, mo = weekly_monthly(emps, days, recs)
    write_tab(sh, "Weekly", wk); write_tab(sh, "Monthly", mo)

    order = ["Dashboard"] + emps + ["Weekly", "Monthly", "Points Rules"]
    sh.reorder_worksheets([sh.worksheet(n) for n in order])
    try:
        d = sh.worksheet("Sheet1")
        if not any(any(r) for r in d.get_all_values()): sh.del_worksheet(d)
    except Exception: pass

    text = boss_text(emps, days, rd)
    print(text)
    if os.getenv("SMTP_USER") and os.getenv("BOSS_EMAIL"):
        msg = MIMEText(text + f"\n\nFull report: {sh.url}")
        msg["Subject"], msg["From"], msg["To"] = f"Daily Report {rd:%d %b %Y}", os.environ["SMTP_USER"], os.environ["BOSS_EMAIL"]
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)

if __name__ == "__main__":
    main()
