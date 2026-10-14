#!/usr/bin/env python3
"""
ALY MEDIA - Daily Points Agent
Employees ki daily log sheet padhta hai -> points nikalta hai ->
report workbook banata hai:
  Dashboard | Daily (saare din) | har employee ka tab | Weekly | Monthly | Points Rules
Boss ko optional email bhi bhejta hai.
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

# --- Employee files / daily layout ---
BLOCK_ROWS = int(os.getenv("BLOCK_ROWS", "12"))      # roz ke block me kitni khali rows
SKIP_WEEKDAYS = {int(x) for x in os.getenv("SKIP_WEEKDAYS", "6").split(",") if x.strip()}  # 6 = Sunday
STATUSES = ["Pending review", "Approved 1st time", "Minor correction", "Major correction", "Not approved"]

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

DAY_START = re.compile(r"^\W*day\b", re.I)
DATE_RE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?")
SOLO_RE = re.compile(r"^\W*[A-Za-z]{0,12}\W*\d{1,2}[/\-.]\d{1,2}(?:[/\-.]\d{2,4})?\W*$")

def make_date(a, b, today, year=None):
    d, m = (b, a) if DATE_ORDER == "mdy" else (a, b)
    if year:
        y = int(year)
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, m, d)
        except ValueError:
            return None
    try:
        x = dt.date(today.year, m, d)
    except ValueError:
        return None
    if x > today + dt.timedelta(days=2):
        x = x.replace(year=today.year - 1)
    return x

def header_date(row, line, today):
    """Din ka header pehchanta hai: 'Day:1 12/10', 'Day 2 - 13/10/2026', 'Tuesday 13/10', '13/10'.
    Returns (is_header, date)."""
    cells = [c for c in row if c.strip()]
    is_hdr = bool(DAY_START.match(line)) or (len(cells) == 1 and bool(SOLO_RE.match(line)))
    if not is_hdr:
        return False, None
    m = DATE_RE.search(line)
    if not m:
        return False, None
    return True, make_date(int(m.group(1)), int(m.group(2)), today, m.group(3))

def parse_rows(rows, today):
    """Log tab ki rows -> entries. Din ke blocks ('Day:1 12/10') padhta hai."""
    cols = {"category": 1, "task": 2, "status": 3, "number": 4}
    cur, out = None, []
    for r in rows:
        line = " ".join(c for c in r if c).strip()
        if not line:
            continue
        is_hdr, d = header_date(r, line, today)
        if is_hdr:
            cur = d
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
        cat = g("category")
        if cat.lower() in ("other", "others") and g("task"):
            cat = g("task")          # dropdown me 'Other' chuna to task ka naam hi category banega
        out.append(dict(date=cur, category=cat, task=g("task"),
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

def work_summary(rs):
    """Kaam ki list: 'Thumbnail x37, Promo x2' (sirf counted pieces)."""
    c = defaultdict(float)
    for r in rs:
        if r["status"] in COUNTS_BASE:
            c[r["category_rule"]] += r["number"]
    return ", ".join(f"{k} x{v:g}" for k, v in sorted(c.items())) or "-"

# ------------------------- REPORT LAYOUT -------------------------
class Page:
    def __init__(self, ncols, widths=None, wrap=None):
        self.rows, self.hdr, self.sec, self.tot = [], [], [], []
        self.ncols, self.widths, self.wrap = ncols, widths or {}, wrap or []
    def add(self, row=None, kind=None):
        self.rows.append(row or [])
        n = len(self.rows)
        if kind == "hdr": self.hdr.append(n)
        elif kind == "sec": self.sec.append(n)
        elif kind == "tot": self.tot.append(n)

def dashboard(emps, days, recs, rd, last_log=None):
    last_log = last_log or {}
    p = Page(8, widths={1: 200, 2: 90, 3: 90, 4: 100, 5: 150, 6: 110, 7: 220, 8: 480}, wrap=[7, 8])
    p.add(["ALY MEDIA - PERFORMANCE DASHBOARD"])
    p.add([f"Report date: {rd:%d %b %Y}   |   Daily target: {DAILY_TARGET:g} pts   |   "
           f"Updated: {dt.datetime.now(TZ):%d %b %Y %H:%M}"])
    p.add()
    p.add([f"TODAY - {rd:%d %b %Y}"], "sec")
    p.add(["Employee", "Points", "Target %", "Approved", "Not approved / Pending", "On-hold pts", "Remarks", "Work done"], "hdr")
    tw = defaultdict(list)
    for r in recs:
        if r["date"] == rd: tw[r["emp"]].append(r)
    today = sorted(((e, days.get((e, rd))) for e in emps),
                   key=lambda x: -(x[1]["points"] if x[1] else -1e9))
    for i, (e, v) in enumerate(today, 1):
        if not v:
            ll = last_log.get(e)
            rem = f"No log for today (last log: {ll:%d %b})" if ll else "No log submitted"
            p.add([f"{i}. {e}", 0, "0%", 0, 0, 0, rem, "-"]); continue
        flag = "Check entries (unusually high)" if v["points"] > 2 * DAILY_TARGET else ""
        p.add([f"{i}. {e}", r1(v["points"]), pct(v["points"]), v["ok"], v["notok"], r1(v["held"]), flag, work_summary(tw[e])])
    ws = rd - dt.timedelta(days=rd.weekday())
    for title, a in ((f"THIS WEEK (from {ws:%d %b})", ws), (f"THIS MONTH - {rd:%B %Y}", rd.replace(day=1))):
        p.add(); p.add([title], "sec")
        p.add(["Employee", "Points", "Target %", "Days logged", "Avg / day", "On-hold pts", "Total pieces", "Work done"], "hdr")
        rows = []
        for e in emps:
            rs = [r for r in recs if r["emp"] == e and a <= r["date"] <= rd]
            pts, n = period(days, e, a, rd)
            pcs = sum(r["number"] for r in rs if r["status"] in COUNTS_BASE)
            rows.append((e, pts, n, pts / n if n else 0, sum(r["held"] for r in rs), pcs, work_summary(rs)))
        for i, (e, pts, n, avg, held, pcs, wsum) in enumerate(sorted(rows, key=lambda x: -x[1]), 1):
            p.add([f"{i}. {e}", r1(pts), pct(avg), n, r1(avg), r1(held), r1(pcs), wsum])
    return p

def daily_history(emps, days, recs):
    """Saare din ek tab me. Purane din kabhi nahi mitte, naye din NEECHE judte hain."""
    p = Page(7, widths={1: 200, 2: 90, 3: 90, 4: 480, 5: 100, 6: 150, 7: 100}, wrap=[4])
    p.add(["DAILY REPORT - ALL DAYS (naye din neeche)"])
    p.add()
    by = defaultdict(lambda: defaultdict(list))
    for r in recs:
        by[r["date"]][r["emp"]].append(r)
    for d in sorted(by):
        p.add([f"{d:%A, %d %b %Y}"], "sec")
        p.add(["Employee", "Points", "Target %", "Work done (counted pieces)",
               "Approved", "Not approved / Pending", "On-hold pts"], "hdr")
        for e in emps:
            v = days.get((e, d))
            if not v:
                p.add([e, 0, "0%", "No log submitted", 0, 0, 0]); continue
            p.add([e, r1(v["points"]), pct(v["points"]), work_summary(by[d][e]),
                   v["ok"], v["notok"], r1(v["held"])])
        p.add()
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

def period_report(emps, days, recs, key_fn, label_fn, title):
    """Har hafte/mahine ke liye: pehle ranking table, phir har employee ka category-wise kaam."""
    p = Page(6, widths={1: 260, 2: 110, 3: 120, 4: 130, 5: 110, 6: 110})
    p.add([title])
    p.add()
    groups = defaultdict(lambda: defaultdict(list))
    for r in recs:
        groups[key_fn(r["date"])][r["emp"]].append(r)
    for key in sorted(groups):
        g = groups[key]
        st = {}
        for e in emps:
            rs = g.get(e, [])
            n = len({r["date"] for r in rs})
            pts = sum(r["total"] for r in rs)
            pcs = sum(r["number"] for r in rs if r["status"] in COUNTS_BASE)
            st[e] = (pts, n, pts / n if n else 0, pcs, rs)
        ranked = sorted(emps, key=lambda e: -st[e][0])
        p.add([label_fn(key)], "sec")
        p.add(["Employee", "Points", "Days logged", "Avg / day", "Target %", "Total pieces"], "hdr")
        for i, e in enumerate(ranked, 1):
            pts, n, avg, pcs, _ = st[e]
            p.add([f"{i}. {e}", r1(pts), n, r1(avg), pct(avg), r1(pcs)])
        p.add()
        for e in ranked:
            pts, n, avg, pcs, rs = st[e]
            if not rs:
                continue
            p.add([f"{e.upper()} - kaam ki detail"], "sec")
            p.add(["Category", "Pieces done", "Points earned", "Not counted pcs", "On-hold pts", "Entries"], "hdr")
            cat = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])   # done, points, not counted, held, entries
            for r in rs:
                c = cat[r["category_rule"]]
                if r["status"] in COUNTS_BASE: c[0] += r["number"]
                else: c[2] += r["number"]
                c[1] += r["total"]; c[3] += r["held"]; c[4] += 1
            for name, c in sorted(cat.items(), key=lambda kv: -kv[1][1]):
                p.add([name, r1(c[0]), r1(c[1]), r1(c[2]), r1(c[3]), c[4]])
            tot = [sum(c[i] for c in cat.values()) for i in range(5)]
            p.add(["TOTAL", r1(tot[0]), r1(tot[1]), r1(tot[2]), r1(tot[3]), int(tot[4])], "tot")
            p.add()
        p.add()
    return p

def weekly_monthly(emps, days, recs):
    wk = period_report(emps, days, recs,
                       lambda d: d - dt.timedelta(days=d.weekday()),
                       lambda w: f"WEEK: {w:%d %b} - {w + dt.timedelta(days=6):%d %b %Y}",
                       "WEEKLY REPORT - kis ne kya kaam kiya (naye hafte neeche)")
    mo = period_report(emps, days, recs,
                       lambda d: d.replace(day=1),
                       lambda m: f"MONTH: {m:%B %Y}",
                       "MONTHLY REPORT - kis ne kya kaam kiya (naye mahine neeche)")
    return wk, mo

def boss_text(emps, days, recs, rd):
    lines = [f"ALY MEDIA - Daily Report ({rd:%d %b %Y})", ""]
    by = defaultdict(list)
    for r in recs:
        if r["date"] == rd: by[r["emp"]].append(r)
    for e in sorted(emps, key=lambda e: -(days.get((e, rd), {"points": 0})["points"])):
        v = days.get((e, rd))
        if v:
            lines.append(f"{e}: {r1(v['points'])} pts ({pct(v['points'])} of target) - {work_summary(by[e])}")
        else:
            lines.append(f"{e}: no log submitted")
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
TOT = {"backgroundColor": {"red": .90, "green": .93, "blue": .97}, "textFormat": {"bold": True}}
RESET = {"backgroundColor": {"red": 1, "green": 1, "blue": 1}, "wrapStrategy": "OVERFLOW_CELL",
         "verticalAlignment": "BOTTOM",
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
    ws.format("A1:AZ3000", RESET)
    ws.update(values=page.rows, range_name="A1")
    last = len(page.rows) + 1
    fm = []
    if page.wrap:
        fm.append({"range": f"{a1(3, 1)}:{a1(last, page.ncols)}", "format": {"verticalAlignment": "TOP"}})
        fm += [{"range": f"{a1(3, c)}:{a1(last, c)}", "format": {"wrapStrategy": "WRAP"}} for c in page.wrap]
    fm.append({"range": "A1", "format": {"textFormat": {"bold": True, "fontSize": 16, "foregroundColor": NAVY}}})
    fm += [{"range": f"{a1(r, 1)}:{a1(r, page.ncols)}", "format": HDR} for r in page.hdr]
    fm += [{"range": f"{a1(r, 1)}:{a1(r, page.ncols)}", "format": TOT} for r in page.tot]
    fm += [{"range": a1(r, 1), "format": {"textFormat": {"bold": True, "fontSize": 12, "foregroundColor": NAVY}}} for r in page.sec]
    ws.batch_format(fm)
    if page.widths:
        sh.batch_update({"requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": c - 1, "endIndex": c},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}}
            for c, w in page.widths.items()]})
    else:
        try: ws.columns_auto_resize(0, page.ncols - 1)
        except Exception: pass
    time.sleep(3)  # API rate limit
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

# ------------------------- EMPLOYEE FILES / DAILY LAYOUT -------------------------
WHITE = {"red": 1, "green": 1, "blue": 1}

def with_retry(fn, *a, **kw):
    import gspread
    for _ in range(3):
        try:
            return fn(*a, **kw)
        except gspread.exceptions.APIError as ex:
            if "429" in str(ex) or "quota" in str(ex).lower():
                print("Rate limit - 25s wait"); time.sleep(25); continue
            raise
    return fn(*a, **kw)

def employee_files(sh):
    """Report sheet ke 'Employees' tab se: Employee | Log File ID. Tab na ho to bana deta hai."""
    import gspread
    try:
        ws = sh.worksheet("Employees")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Employees", rows=60, cols=4)
        ws.update(values=[["Employee", "Log File ID (ya poora link)", "Status (auto)"]], range_name="A1")
        ws.format("A1:C1", HDR)
        return ws, []
    out = []
    for i, r in enumerate(ws.get_all_values()[1:], start=2):
        name = r[0].strip() if r else ""
        fid = r[1].strip() if len(r) > 1 else ""
        if not name or not fid:
            continue
        m = re.search(r"/d/([A-Za-z0-9_-]+)", fid)
        out.append((i, name, m.group(1) if m else fid))
    return ws, out

def category_names(lookup):
    names = []
    for name, _, _ in lookup.values():
        if name not in names:
            names.append(name)
    return names

def get_daily_ws(f):
    import gspread
    try:
        return f.worksheet("Daily Log")
    except gspread.WorksheetNotFound:
        sheets = f.worksheets()
        if len(sheets) == 1 and not any(any(r) for r in sheets[0].get_all_values()):
            sheets[0].update_title("Daily Log")
            return sheets[0]
        return f.add_worksheet("Daily Log", rows=300, cols=8)

def ensure_lists(f, cats):
    """Employee file me hidden 'Lists' tab: dropdown ki categories + status. Points Rules badle to khud update."""
    import gspread
    n = max(len(cats), len(STATUSES))
    want = [["Category", "Status"]] + [[cats[i] if i < len(cats) else "",
                                         STATUSES[i] if i < len(STATUSES) else ""] for i in range(n)]
    created = False
    try:
        ws = f.worksheet("Lists")
    except gspread.WorksheetNotFound:
        ws = f.add_worksheet("Lists", rows=max(n + 20, 100), cols=3); created = True
    have = [(r + ["", ""])[:2] for r in ws.get_all_values()]
    if have != want:
        ws.clear()
        ws.update(values=want, range_name="A1")
    if created:
        f.batch_update({"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "hidden": True}, "fields": "hidden"}}]})

def block_values(n, d):
    ds = f"{d:%m/%d}" if DATE_ORDER == "mdy" else f"{d:%d/%m}"
    rows = [[f"Day:{n} {ds}"], ["Sr", "Category", "Task", "Status", "Number"]]
    rows += [[i, "", "", STATUSES[0], ""] for i in range(1, BLOCK_ROWS + 1)]
    rows.append([])
    return rows

def block_requests(sheet_id, start, ncols=5):
    """start = 'Day:' header ki 1-based row. Formatting + dropdowns (strict) + borders."""
    h, ch = start - 1, start                 # 0-based: Day row, column-header row
    d0, d1 = start + 1, start + 1 + BLOCK_ROWS
    rng = lambda r0, r1, c0, c1: {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
                                  "startColumnIndex": c0, "endColumnIndex": c1}
    line = {"style": "SOLID", "width": 1, "color": {"red": .75, "green": .75, "blue": .75}}
    val = lambda col, cond, msg: {"setDataValidation": {"range": rng(d0, d1, col, col + 1), "rule": {
        "condition": cond, "strict": True, "showCustomUi": True, "inputMessage": msg}}}
    reqs = [
        {"repeatCell": {"range": rng(h, h + 1, 0, ncols), "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": NAVY}}},
            "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {"range": rng(ch, ch + 1, 0, ncols), "cell": {"userEnteredFormat": {
            "backgroundColor": NAVY, "horizontalAlignment": "CENTER",
            "textFormat": {"bold": True, "foregroundColor": WHITE}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"}},
        {"updateBorders": {"range": rng(ch, d1, 0, ncols), "top": line, "bottom": line, "left": line,
                           "right": line, "innerHorizontal": line, "innerVertical": line}},
        val(1, {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "=Lists!$A$2:$A$500"}]},
            "List me se kaam ka type chuno (spelling type mat karo). List me na ho to 'Other' chuno aur Task me likho."),
        val(3, {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": s} for s in STATUSES]},
            "Status boss/reviewer set karta hai."),
        val(4, {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "0"}]},
            "Number = kitne pieces bane (count)."),
    ]
    for col, w in enumerate([50, 270, 330, 170, 90]):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    return reqs

def ensure_today_block(f, ws, rows, today):
    """Aaj ka din ka ready layout (dropdowns ke saath) na ho to neeche bana deta hai. True = bana."""
    heads = []
    for r in rows:
        line = " ".join(c for c in r if c).strip()
        if line:
            ok, d = header_date(r, line, today)
            if ok: heads.append(d)
    if today in heads or today.weekday() in SKIP_WEEKDAYS:
        return False
    block = block_values(len(heads) + 1, today)
    start = len(rows) + 2 if rows else 1
    need = start + len(block) + 5
    if ws.row_count < need:
        ws.add_rows(need - ws.row_count + 50)
    ws.update(values=block, range_name=f"A{start}")
    f.batch_update({"requests": block_requests(ws.id, start)})
    return True

# ------------------------- MAIN -------------------------
def main():
    gc = client()
    sh = gc.open_by_key(os.environ["REPORT_SHEET_ID"])
    today = dt.datetime.now(TZ).date()
    rd = dt.date.fromisoformat(os.environ["REPORT_DATE"]) if os.getenv("REPORT_DATE") else today
    print(f"Report date: {rd}  (today IST: {today})")

    rules_ws, lookup = load_rules(sh)
    emp_ws, emp_rows = employee_files(sh)
    entries, emps, last_log = [], [], {}

    def collect(name, rows):
        es = parse_rows(rows, today)
        dates = sorted({e["date"] for e in es})
        print(f"[{name}] {len(es)} entries; dates found: "
              f"{', '.join(f'{d:%d %b}' for d in dates) or 'NONE'}")
        ok = [d for d in dates if d <= rd]
        if ok: last_log[name] = max(ok)
        for e in es:
            if e["date"] <= rd: entries.append({**e, "emp": name})

    if emp_rows:        # naya model: har employee ki alag file
        cats = category_names(lookup) + ["Other"]
        status = {}
        for rowno, name, fid in emp_rows:
            emps.append(name)
            try:
                f = with_retry(gc.open_by_key, fid)
                ws = get_daily_ws(f)
                ensure_lists(f, cats)
                rows = with_retry(ws.get_all_values)
                made = ensure_today_block(f, ws, rows, today)
                collect(name, rows)
                status[rowno] = f"OK {dt.datetime.now(TZ):%d %b %H:%M}" + (" - aaj ka layout bana" if made else "")
            except Exception as ex:
                print(f"[{name}] ERROR: {ex}")
                status[rowno] = f"ERROR: {str(ex)[:150]}"
            time.sleep(1.5)
        try:
            emp_ws.batch_update([{"range": f"C{r}", "values": [[t]]} for r, t in status.items()])
        except Exception as ex:
            print("Status write failed:", ex)
    else:               # purana model: ek file, har tab = ek employee
        log = gc.open_by_key(os.environ["LOG_SHEET_ID"])
        for ws in log.worksheets():
            if ws.title.lower() in IGNORE_TABS: continue
            emps.append(ws.title)
            collect(ws.title, ws.get_all_values())

    def new_rule(cat, task):
        mins, why = estimate_minutes(cat, task)
        pts = round(mins / 4, 2)
        rules_ws.append_row([cat, pts, "AUTO-ESTIMATED (confirm)", "", f"{mins:g} min/piece - {why}"])
        return (cat, pts, "AUTO-ESTIMATED")
    recs = score(entries, lookup, new_rule)
    days = day_stats(recs)

    write_tab(sh, "Dashboard", dashboard(emps, days, recs, rd, last_log))
    write_tab(sh, "Daily", daily_history(emps, days, recs))
    for e in emps:
        write_tab(sh, e, employee_page(e, days, recs, rd))
    wk, mo = weekly_monthly(emps, days, recs)
    write_tab(sh, "Weekly", wk); write_tab(sh, "Monthly", mo)

    order = ["Dashboard", "Daily"] + emps + ["Weekly", "Monthly", "Points Rules", "Employees"]
    sh.reorder_worksheets([sh.worksheet(n) for n in order])
    try:
        d = sh.worksheet("Sheet1")
        if not any(any(r) for r in d.get_all_values()): sh.del_worksheet(d)
    except Exception: pass

    text = boss_text(emps, days, recs, rd)
    print(text)
    # Email sirf tab jab SEND_EMAIL=true ho (raat ka run / manual run)
    if (os.getenv("SEND_EMAIL", "").lower() == "true"
            and os.getenv("SMTP_USER") and os.getenv("BOSS_EMAIL")):
        msg = MIMEText(text + f"\n\nFull report: {sh.url}")
        msg["Subject"], msg["From"], msg["To"] = f"Daily Report {rd:%d %b %Y}", os.environ["SMTP_USER"], os.environ["BOSS_EMAIL"]
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)

if __name__ == "__main__":
    main()
