import os, json, time, sqlite3, asyncio, logging, itertools, hashlib
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ============================================================
# XHAWON SELF-LEARNING PATTERN AI v5
# Autonomous pattern discovery + failure learning + evolution
# Only TWO hosting files: bot.py + requirements.txt
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8777437712:AAGzqZ3ABYur-NpiFdl_3gkA0ApBFyZhkIo").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "8746661403").strip()
API_URL = os.getenv(
    "WINGO_API_URL",
    "https://draw.ar-lottery01.com/WinGo/WinGo_30S/GetHistoryIssuePage.json"
).strip()
DB_PATH = os.getenv("XHAWON_DB", "xhawon_self_learning_v5.db").strip()
POLL_SECONDS = 4
LEARN_SECONDS = 15
WINDOW = 10
QUALIFY = 60.0
MIN_SAMPLES = 40
MAX_ROWS = 8000
TZ = timezone(timedelta(hours=6))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("xhawon-v5")


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS results(
        issue TEXT PRIMARY KEY,
        number INTEGER NOT NULL,
        kind TEXT NOT NULL,
        ts TEXT NOT NULL,
        collected_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS systems(
        system_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        family TEXT NOT NULL,
        definition TEXT NOT NULL,
        params TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        parent_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS evaluations(
        system_id TEXT NOT NULL,
        dataset TEXT NOT NULL,
        samples INTEGER NOT NULL,
        wins INTEGER NOT NULL,
        losses INTEGER NOT NULL,
        accuracy REAL NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(system_id,dataset)
    );
    CREATE TABLE IF NOT EXISTS time_stats(
        system_id TEXT NOT NULL,
        hour INTEGER NOT NULL,
        samples INTEGER NOT NULL,
        wins INTEGER NOT NULL,
        accuracy REAL NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(system_id,hour)
    );
    CREATE TABLE IF NOT EXISTS failures(
        system_id TEXT PRIMARY KEY,
        failure_count INTEGER NOT NULL,
        reasons TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS evolution(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        change_text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS research_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_results_issue ON results(issue);
    """)
    c.commit(); c.close()


def event(kind, message):
    try:
        c = db(); c.execute(
            "INSERT INTO research_events(event_type,message,created_at) VALUES(?,?,?)",
            (kind, message, now())
        ); c.commit(); c.close()
    except Exception:
        log.exception("event failed")


def pick(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def parse_time(v):
    if v is None: return now()
    try:
        if isinstance(v, (int,float)):
            x=float(v); x=x/1000 if x>10_000_000_000 else x
            return datetime.fromtimestamp(x,TZ).isoformat(timespec="seconds")
        s=str(v).replace("Z","+00:00")
        d=datetime.fromisoformat(s)
        if d.tzinfo is None: d=d.replace(tzinfo=TZ)
        return d.astimezone(TZ).isoformat(timespec="seconds")
    except Exception:
        return now()


def normalize(payload):
    data=payload.get("data",payload) if isinstance(payload,dict) else payload
    source_rows=[]
    if isinstance(data,dict): source_rows=data.get("list") or data.get("data") or data.get("records") or []
    elif isinstance(data,list): source_rows=data
    rows = []  # Collect only valid, normalized history rows.
    for r in source_rows:
        if not isinstance(r,dict): continue
        issue=pick(r,["issueNumber","issue","period","periodNumber","issueNo"])
        num=pick(r,["number","result","openNumber","num"])
        if issue is None or num is None: continue
        try: n=int(str(num).strip())
        except Exception: continue
        if 0<=n<=9:
            ts=parse_time(pick(r,["time","timestamp","drawTime","createdAt","date"]))
            rows.append((str(issue),n,"BIG" if n>=5 else "SMALL",ts))
    return rows


async def fetch(session):
    params={"pageNo":1,"pageSize":50,"t":int(time.time()*1000)}
    async with session.get(API_URL,params=params,timeout=aiohttp.ClientTimeout(total=12)) as r:
        r.raise_for_status(); return await r.json(content_type=None)


def save(rows):
    c=db(); n=0
    for issue,num,k,ts in rows:
        cur=c.execute(
            "INSERT OR IGNORE INTO results(issue,number,kind,ts,collected_at) VALUES(?,?,?,?,?)",
            (issue,num,k,ts,now())
        ); n += max(cur.rowcount,0)
    c.execute("""DELETE FROM results WHERE issue NOT IN
                 (SELECT issue FROM results ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?)""",(MAX_ROWS,))
    c.commit(); c.close(); return n


def history():
    c=db(); r=c.execute(
        "SELECT issue,number,kind,ts FROM results ORDER BY CAST(issue AS INTEGER) ASC"
    ).fetchall(); c.close(); return [dict(x) for x in r]


def knd(n): return 1 if n>=5 else 0

# -------------------- Seed patterns --------------------

def seed_number(nums):
    if len(nums)<10: return None
    first=nums[-10]
    mapping={1:1,2:0,3:1,4:1,5:0,6:1,7:0,8:0,9:0}
    if first==0: return 1 if nums[-1] in (1,3,6,8) else 0
    base=mapping.get(first)
    if base is None: return None
    return 1-base if nums[-10:].count(first)>1 else base


def seed_plus(nums):
    if len(nums)<10: return None
    a,b=nums[-10],nums[-1]
    if a==0 or b==0: return None
    s=a+b
    if 1<=s<=4:return 0
    if 5<=s<=9:return 1
    if s==10:return 0
    return None

# -------------------- Behavioral feature engine --------------------
# These are interpretable behaviours, not arbitrary mathematical formulas.


def bits(nums): return [knd(x) for x in nums]


def predict_behavior(nums, p):
    fam=p["family"]; w=p.get("window",3)
    if len(nums)<max(3,w): return None
    b=bits(nums)
    if fam=="majority":
        return 1 if sum(b[-w:])*2>=w else 0
    if fam=="minority":
        return 0 if sum(b[-w:])*2>=w else 1
    if fam=="streak_reverse":
        s=b[-w:]
        return 1-s[-1] if len(set(s))==1 else None
    if fam=="streak_follow":
        s=b[-w:]
        return s[-1] if len(set(s))==1 else None
    if fam=="alternation":
        s=b[-w:]
        if all(s[i]!=s[i-1] for i in range(1,len(s))): return 1-s[-1]
        return None
    if fam=="alternation_follow":
        s=b[-w:]
        if all(s[i]!=s[i-1] for i in range(1,len(s))): return s[-1]
        return None
    if fam=="last_repeat_reverse":
        x=nums[-1]
        return 1-knd(x) if nums[-w:].count(x)>=p.get("repeat",2) else None
    if fam=="first_repeat_reverse":
        x=nums[-w]
        return 1-knd(x) if nums[-w:].count(x)>=p.get("repeat",2) else None
    if fam=="recent_pressure":
        s=sum(b[-w:]);
        if s>=p.get("high",w-1): return 1
        if s<=p.get("low",1): return 0
        return None
    if fam=="balance_reversal":
        s=sum(b[-w:])
        if s==0:return 1
        if s==w:return 0
        return None
    if fam=="last_digit_follow_group":
        x=nums[-1]; group=p["group"]
        return p["outcome"] if x in group else None
    if fam=="first_digit_follow_group":
        x=nums[-w]; group=p["group"]
        return p["outcome"] if x in group else None
    if fam=="position_relation":
        a=nums[-w]; b2=nums[-1]
        if p["relation"]=="same" and a==b2:return p["outcome"]
        if p["relation"]=="different" and a!=b2:return p["outcome"]
        return None
    if fam=="gap_reappearance":
        x=nums[-1]; gaps=[]
        for i in range(len(nums)-2,-1,-1):
            if nums[i]==x:
                gaps.append(len(nums)-1-i); break
        if gaps and gaps[0] in p["gaps"]: return p["outcome"]
        return None
    if fam=="digit_pressure":
        c=Counter(nums[-w:]); x=p["digit"]
        if c[x]>=p["count"]: return p["outcome"]
        return None
    if fam=="odd_even_behavior":
        # number-category behaviour, not a numeric formula
        ev=sum(1 for x in nums[-w:] if x%2==0)
        if ev>=p["count"]: return p["outcome"]
        return None
    if fam=="edge_behavior":
        # low digits 0-2 / high digits 7-9
        c=sum(1 for x in nums[-w:] if x in p["digits"])
        return p["outcome"] if c>=p["count"] else None
    if fam=="mirror_behavior":
        a=nums[-w]; z=nums[-1]
        if (a+z)==9: return p["outcome"]
        return None
    if fam=="middle_pressure":
        mid=set(p["digits"]); c=sum(1 for x in nums[-w:] if x in mid)
        return p["outcome"] if c>=p["count"] else None
    if fam=="kind_run_context":
        # richer than simple transition: run length before the last draw
        last=b[-1]; run=1
        for x in reversed(b[:-1]):
            if x==last: run+=1
            else: break
        if run==p["run"]: return p["outcome"]
        return None
    if fam=="window_shape":
        s=b[-w:]
        ones=sum(s)
        if ones==p["ones"]: return p["outcome"]
        return None
    return None


def predict(nums,p):
    fam=p.get("family")
    if fam=="seed_number": return seed_number(nums)
    if fam=="seed_plus": return seed_plus(nums)
    return predict_behavior(nums,p)


def description(p):
    fam=p["family"]
    if fam=="seed_number": return "User seed: 10-period first-number mapping with repeat reversal and zero rule."
    if fam=="seed_plus": return "User seed: first + 10th number mapping with zero/no-trade conditions."
    return p.get("text",fam)

# -------------------- Discovery --------------------

def candidate_pool():
    c=[
        ("SEED_NUMBER","Seed Number Pattern","seed_number",{"family":"seed_number"}),
        ("SEED_PLUS","Seed Plus Pattern","seed_plus",{"family":"seed_plus"}),
    ]
    for w in range(3,11):
        c += [
            (f"MAJ_{w}",f"Majority behaviour / {w} periods","sequence",{"family":"majority","window":w}),
            (f"MIN_{w}",f"Minority behaviour / {w} periods","sequence",{"family":"minority","window":w}),
            (f"SR_{w}",f"Streak reversal / {w} same kinds","streak",{"family":"streak_reverse","window":w}),
            (f"SF_{w}",f"Streak continuation / {w} same kinds","streak",{"family":"streak_follow","window":w}),
            (f"ALT_{w}",f"Alternation behaviour / {w} periods","sequence",{"family":"alternation","window":w}),
            (f"ALTF_{w}",f"Alternation continuation / {w} periods","sequence",{"family":"alternation_follow","window":w}),
        ]
    # Repeat and pressure candidates
    for w in range(4,11):
        for repeat in (2,3):
            c.append((f"LR_{w}_{repeat}",f"Last-digit repeat/reversal w{w} x{repeat}","repeat",{"family":"last_repeat_reverse","window":w,"repeat":repeat}))
        for repeat in (2,3):
            c.append((f"FR_{w}_{repeat}",f"First-digit repeat/reversal w{w} x{repeat}","repeat",{"family":"first_repeat_reverse","window":w,"repeat":repeat}))
        for high in (w-1,w):
            c.append((f"RP_{w}_{high}",f"Recent BIG pressure w{w}","pressure",{"family":"recent_pressure","window":w,"high":high,"low":1}))
        c.append((f"BR_{w}",f"Full imbalance reversal w{w}","pressure",{"family":"balance_reversal","window":w}))
        for count in (2,3):
            for out in (0,1):
                c.append((f"OE_{w}_{count}_{out}",f"Even-digit pressure w{w} count{count}","number_behavior",{"family":"odd_even_behavior","window":w,"count":count,"outcome":out}))
                c.append((f"EDGE_{w}_{count}_{out}",f"Edge-digit pressure w{w}","number_behavior",{"family":"edge_behavior","window":w,"count":count,"digits":[0,1,2,7,8,9],"outcome":out}))
    for digit in range(10):
        for count in (2,3):
            for out in (0,1):
                c.append((f"DIG_{digit}_{count}_{out}",f"Digit {digit} repetition x{count} -> {'BIG' if out else 'SMALL'}","number_behavior",{"family":"digit_pressure","window":10,"digit":digit,"count":count,"outcome":out}))
    groups=[[0,1,2],[3,4,5],[6,7,8],[7,8,9],[1,2,3],[4,5,6]]
    for gi,g in enumerate(groups):
        for out in (0,1):
            c.append((f"GRP_{gi}_{out}",f"Digit-group recurrence {g}","number_behavior",{"family":"last_digit_follow_group","group":g,"outcome":out}))
            c.append((f"FGRP_{gi}_{out}",f"First-position digit-group {g}","number_behavior",{"family":"first_digit_follow_group","window":10,"group":g,"outcome":out}))
    for rel in ("same","different"):
        for w in (5,10):
            for out in (0,1):
                c.append((f"POS_{rel}_{w}_{out}",f"Position relation {rel} ({w})","position",{"family":"position_relation","window":w,"relation":rel,"outcome":out}))
    for gapset in ([1],[2],[3],[4],[2,3],[3,4]):
        for out in (0,1):
            c.append(("GAP_"+"_".join(map(str,gapset))+f"_{out}",f"Digit reappearance gap {gapset}","gap",{"family":"gap_reappearance","gaps":gapset,"outcome":out}))
    for run in (2,3,4,5):
        for out in (0,1):
            c.append((f"RUN_{run}_{out}",f"Run-length context {run}","context",{"family":"kind_run_context","run":run,"outcome":out}))
    for w in (4,5,6,7,8,9,10):
        for ones in range(w+1):
            for out in (0,1):
                if ones in (0,w): continue
                c.append((f"SHAPE_{w}_{ones}_{out}",f"Window shape w{w}, BIG count {ones}","shape",{"family":"window_shape","window":w,"ones":ones,"outcome":out}))
    # mirror is a behavioural digit relationship, retained as an interpretable feature
    for w in (3,5,10):
        for out in (0,1): c.append((f"MIRROR_{w}_{out}",f"Complementary digit relation w{w}","relationship",{"family":"mirror_behavior","window":w,"outcome":out}))
    # de-duplicate by id
    seen=set(); out=[]
    for x in c:
        if x[0] not in seen: seen.add(x[0]); out.append(x)
    return out


def sid_for(cid,params):
    return "SYS_"+hashlib.sha1((cid+json.dumps(params,sort_keys=True)).encode()).hexdigest()[:12]


def register_candidates():
    c=db(); t=now()
    for cid,name,fam,p in candidate_pool():
        sid=sid_for(cid,p)
        exists=c.execute("SELECT 1 FROM systems WHERE system_id=?",(sid,)).fetchone()
        if not exists:
            c.execute("""INSERT INTO systems(system_id,name,family,definition,params,status,version,parent_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,'TESTING',1,NULL,?,?)""",
                      (sid,name,fam,description(p),json.dumps(p),t,t))
    c.commit(); c.close()


def evaluate_dataset(nums, rows, p):
    wins=losses=0
    start=WINDOW
    for i in range(start,len(rows)):
        context=nums[:i]
        pred=predict(context,p)
        if pred is None: continue
        actual=knd(rows[i]["number"])
        if pred==actual: wins+=1
        else: losses+=1
    n=wins+losses
    return wins,losses,(wins/n*100 if n else 0.0)


def walk_eval(rows,p,start_idx=WINDOW,end_idx=None):
    if end_idx is None: end_idx=len(rows)
    wins=losses=0
    nums=[r["number"] for r in rows]
    for i in range(max(start_idx,WINDOW),end_idx):
        pred=predict(nums[:i],p)
        if pred is None: continue
        if pred==knd(nums[i]): wins+=1
        else: losses+=1
    n=wins+losses
    return wins,losses,(wins/n*100 if n else 0.0)


def store_eval(sid,d,w,l,a):
    c=db(); c.execute("""INSERT INTO evaluations(system_id,dataset,samples,wins,losses,accuracy,updated_at)
                      VALUES(?,?,?,?,?,?,?) ON CONFLICT(system_id,dataset) DO UPDATE SET samples=excluded.samples,wins=excluded.wins,losses=excluded.losses,accuracy=excluded.accuracy,updated_at=excluded.updated_at""",
                   (sid,d,w+l,w,l,a,now())); c.commit(); c.close()


def time_eval(sid,rows,p):
    buckets={h:[0,0] for h in range(24)}; nums=[r["number"] for r in rows]
    for i in range(WINDOW,len(rows)):
        pred=predict(nums[:i],p)
        if pred is None: continue
        try: h=datetime.fromisoformat(rows[i]["ts"]).hour
        except Exception: continue
        buckets[h][0]+=1
        buckets[h][1]+=int(pred==knd(nums[i]))
    c=db()
    for h,(n,w) in buckets.items():
        if n:
            c.execute("""INSERT INTO time_stats(system_id,hour,samples,wins,accuracy,updated_at) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(system_id,hour) DO UPDATE SET samples=excluded.samples,wins=excluded.wins,accuracy=excluded.accuracy,updated_at=excluded.updated_at""",
                      (sid,h,n,w,w*100/n,now()))
    c.commit(); c.close()


def failure_reason(rows,p):
    nums=[r["number"] for r in rows]
    failures=Counter(); contexts=Counter(); total=0
    for i in range(WINDOW,len(rows)):
        pred=predict(nums[:i],p)
        if pred is None: continue
        total+=1
        if pred!=knd(nums[i]):
            b=bits(nums[:i])
            if len(set(b[-3:]))==1: failures["3-period same-kind state"]+=1
            elif b[-1]!=b[-2] and b[-2]!=b[-3]: failures["alternating state"]+=1
            if i > 0 and nums[i-1] == nums[i]: contexts["same-digit context"]+=1
            else: contexts["digit-change context"]+=1
    if not total:return {}
    out=failures+contexts
    return {k:int(v) for k,v in out.most_common(5)}


def analyze_and_update():
    rows=history()
    if len(rows)<MIN_SAMPLES+WINDOW:return
    nums=[r["number"] for r in rows]
    n=len(rows); a=int(n*.60); b=int(n*.80)
    c=db(); systems=c.execute("SELECT * FROM systems").fetchall(); c.close()
    for s in systems:
        p=json.loads(s["params"])
        tw,tl,ta=walk_eval(rows,p,WINDOW,a)
        vw,vl,va=walk_eval(rows,p,a,b)
        hw,hl,ha=walk_eval(rows,p,b,n)
        store_eval(s["system_id"],"train",tw,tl,ta)
        store_eval(s["system_id"],"validation",vw,vl,va)
        store_eval(s["system_id"],"holdout",hw,hl,ha)
        time_eval(s["system_id"],rows,p)
        support=tw+tl+vw+vl+hw+hl
        if hw+hl>=MIN_SAMPLES and ha>=QUALIFY and va>=QUALIFY:
            status="QUALIFIED"
        elif support>=MIN_SAMPLES and (ha<50 or va<50):
            status="EVOLVING"
        else:
            status="TESTING"
        c=db(); c.execute("UPDATE systems SET status=?,updated_at=? WHERE system_id=?",(status,now(),s["system_id"])); c.commit(); c.close()
        if status=="EVOLVING":
            reasons=failure_reason(rows,p)
            if reasons:
                c=db(); c.execute("""INSERT INTO failures(system_id,failure_count,reasons,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(system_id) DO UPDATE SET failure_count=failures.failure_count+1,reasons=excluded.reasons,updated_at=excluded.updated_at""",
                    (s["system_id"],1,json.dumps(reasons),now())); c.commit(); c.close()


def best_conditions():
    # Learn new interpretable group conditions from observed outcome rates.
    rows=history()
    if len(rows)<100:return 0
    nums=[r["number"] for r in rows]; created=0
    c=db()
    # discover high-support digit -> outcome behaviours from previous windows.
    for digit in range(10):
        for count in range(2,6):
            for out in (0,1):
                p={"family":"digit_pressure","window":10,"digit":digit,"count":count,"outcome":out}
                sid=sid_for(f"AUTO_DIG_{digit}_{count}_{out}",p)
                if c.execute("SELECT 1 FROM systems WHERE system_id=?",(sid,)).fetchone():continue
                w,l,a=walk_eval(rows,p,WINDOW,len(rows))
                if w+l>=MIN_SAMPLES and a>=QUALIFY:
                    c.execute("""INSERT INTO systems(system_id,name,family,definition,params,status,version,parent_id,created_at,updated_at)
                        VALUES(?,?,?,?,?,'EVOLVING',1,NULL,?,?)""",
                        (sid,f"Auto-discovered digit pressure {digit} x{count}","auto_number_behavior",
                         f"When digit {digit} appears at least {count} times in the latest 10 periods, outcome tends toward {'BIG' if out else 'SMALL'}.",json.dumps(p),now(),now())); created+=1
    c.commit(); c.close()
    return created


def mutate_parent(s):
    p=json.loads(s["params"]); fam=p.get("family")
    variants=[]
    if "window" in p:
        for d in (-1,1):
            q=dict(p); q["window"]=max(3,min(10,p["window"]+d)); variants.append((q,f"window changed {p['window']} -> {q['window']}"))
    if fam in ("digit_pressure","odd_even_behavior","edge_behavior") and "count" in p:
        for d in (-1,1):
            q=dict(p); q["count"]=max(2,min(5,p["count"]+d)); variants.append((q,f"condition count changed {p['count']} -> {q['count']}"))
    if fam in ("streak_reverse","streak_follow"):
        q=dict(p); q["family"]="streak_follow" if fam=="streak_reverse" else "streak_reverse"; variants.append((q,"reversed streak response"))
    return variants[:3]


def evolve():
    c=db(); parents=c.execute("SELECT * FROM systems WHERE status='EVOLVING' ORDER BY updated_at DESC LIMIT 20").fetchall(); c.close()
    made=0
    for s in parents:
        for p,change in mutate_parent(s):
            cid=s["system_id"]+"_V"+str(int(s["version"])+1)+"_"+hashlib.sha1(json.dumps(p,sort_keys=True).encode()).hexdigest()[:5]
            c=db(); exists=c.execute("SELECT 1 FROM systems WHERE system_id=?",(cid,)).fetchone()
            if not exists:
                c.execute("""INSERT INTO systems(system_id,name,family,definition,params,status,version,parent_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,'TESTING',?,?,?,?)""",
                    (cid,s["name"]+" → evolved",s["family"],description(p),json.dumps(p),int(s["version"])+1,s["system_id"],now(),now()))
                c.execute("INSERT INTO evolution(parent_id,child_id,reason,change_text,created_at) VALUES(?,?,?,?,?)",
                          (s["system_id"],cid,"Failure analysis / mutation",change,now()))
                c.commit(); made+=1
            c.close()
    return made


def qualify_report(limit=8):
    c=db(); rows=c.execute("""SELECT s.*, e.accuracy hold_acc,e.samples hold_n,
                     v.accuracy val_acc,v.samples val_n,
                     (SELECT ts.accuracy FROM time_stats ts WHERE ts.system_id=s.system_id AND ts.samples>=10 ORDER BY ts.accuracy DESC, ts.samples DESC LIMIT 1) best_time_acc,
                     (SELECT ts.hour FROM time_stats ts WHERE ts.system_id=s.system_id AND ts.samples>=10 ORDER BY ts.accuracy DESC, ts.samples DESC LIMIT 1) best_hour
                     FROM systems s
                     LEFT JOIN evaluations e ON s.system_id=e.system_id AND e.dataset='holdout'
                     LEFT JOIN evaluations v ON s.system_id=v.system_id AND v.dataset='validation'
                     WHERE s.status='QUALIFIED' ORDER BY COALESCE(hold_acc,0) DESC LIMIT ?""",(limit,)).fetchall(); c.close()
    return rows


def detailed_report(sid):
    c=db(); s=c.execute("SELECT * FROM systems WHERE system_id=?",(sid,)).fetchone()
    if not s:return "Logic not found."
    ev=c.execute("SELECT dataset,samples,wins,losses,accuracy FROM evaluations WHERE system_id=?",(sid,)).fetchall()
    times=c.execute("SELECT hour,samples,accuracy FROM time_stats WHERE system_id=? AND samples>=10 ORDER BY accuracy DESC",(sid,)).fetchall()
    evo=c.execute("SELECT parent_id,change_text,created_at FROM evolution WHERE child_id=?",(sid,)).fetchall()
    fail=c.execute("SELECT reasons FROM failures WHERE system_id=?",(sid,)).fetchone(); c.close()
    p=json.loads(s["params"])
    lines=[f"🧠 {s['name']}",f"ID: {sid}",f"Family: {s['family']}",f"Version: v{s['version']}","", "📌 COMPLETE LOGIC",description(p),""]
    for e in ev: lines.append(f"{e['dataset'].upper()}: {e['accuracy']:.1f}% | n={e['samples']} | W={e['wins']} L={e['losses']}")
    if times:
        lines += ["",f"⏰ Best observed time: {times[0]['hour']:02d}:00 ({times[0]['accuracy']:.1f}%, n={times[0]['samples']})"]
        if len(times)>1: lines.append(f"⏰ Second best: {times[1]['hour']:02d}:00 ({times[1]['accuracy']:.1f}%)")
    if fail:
        try:
            rr=json.loads(fail["reasons"]); lines += ["","🔎 FAILURE LEARNING", "; ".join(f"{k}: {v}" for k,v in rr.items())]
        except Exception: pass
    if evo:
        lines += ["","🧬 EVOLUTION"]+[f"{x['created_at']}: {x['change_text']}" for x in evo[-3:]]
    lines += ["","⚠️ Historical/paper research only; future outcomes are not guaranteed."]
    return "\n".join(lines)

# -------------------- Learning cycle --------------------

async def learner():
    register_candidates(); last=-1
    while True:
        try:
            rows=history(); n=len(rows)
            if n>=MIN_SAMPLES+WINDOW and n!=last:
                analyze_and_update()
                auto=best_conditions()
                ev=evolve()
                if auto: event("DISCOVERY",f"Auto-discovered {auto} strong behavioural candidates.")
                if ev: event("EVOLUTION",f"Created {ev} mutated logic versions from failure analysis.")
                last=n; log.info("research cycle rows=%s auto=%s evolved=%s",n,auto,ev)
        except Exception: log.exception("learner cycle failed")
        await asyncio.sleep(LEARN_SECONDS)

# -------------------- Telegram --------------------

def allowed(update):
    return (not ADMIN_CHAT_ID) or str(update.effective_chat.id)==ADMIN_CHAT_ID


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Status",callback_data="status"),InlineKeyboardButton("🧠 Logic",callback_data="logic")],
        [InlineKeyboardButton("📚 Learning Report",callback_data="report"),InlineKeyboardButton("🔬 Research",callback_data="activity")],
        [InlineKeyboardButton("⏰ Best Time",callback_data="time")]
    ])

async def start(update,context):
    if not allowed(update):return
    await update.message.reply_text("🧠 XHAWON SELF-LEARNING PATTERN AI v5\n\nনিজে pattern বানায় → test করে → failure শেখে → logic evolve করে।\n\nনিচের menu থেকে Logic/Report নাও।",reply_markup=main_menu())

async def status_text():
    r=history(); c=db()
    total=c.execute("SELECT COUNT(*) FROM systems").fetchone()[0]
    q=c.execute("SELECT COUNT(*) FROM systems WHERE status='QUALIFIED'").fetchone()[0]
    ev=c.execute("SELECT COUNT(*) FROM systems WHERE status='EVOLVING'").fetchone()[0]
    te=c.execute("SELECT COUNT(*) FROM systems WHERE status='TESTING'").fetchone()[0]
    c.close(); last=r[-1] if r else None
    last_text = f"{last['number']} / {last['kind']} (issue {last['issue']})" if last else "Waiting for market data"
    return (f"🧠 XHAWON v5.1.1\n\n📡 Periods: {len(r)}\n🔬 Patterns/logic tested: {total}\n"
            f"✅ Qualified ≥60%: {q}\n🧬 Evolving: {ev}\n🧪 Testing: {te}\n"
            f"📌 Last: {last_text}\n\nAI: discover → test → learn failure → evolve")

async def logic_text():
    rs=qualify_report(10)
    if not rs:return "🧠 QUALIFIED LOGIC\n\nএখনও 60%+ validation + holdout পাওয়া যায়নি। AI গবেষণা করছে।"
    out=["🧠 QUALIFIED LOGICS\n"]
    for i,s in enumerate(rs,1):
        out.append(f"{i}. {s['name']}\nID: {s['system_id']}\nHoldout: {s['hold_acc']:.1f}% | Validation: {s['val_acc']:.1f}% | n={s['hold_n']}")
    return "\n\n".join(out)

async def report_text():
    rs=qualify_report(5)
    if not rs:return "📚 LEARNING REPORT\n\nকোনো qualified logic নেই। Failure learning ও evolution চলতে থাকবে।"
    return "📚 LEARNING REPORT\n\n"+"\n\n────────────\n\n".join(detailed_report(s["system_id"]) for s in rs)

async def time_text():
    c=db(); r=c.execute("""SELECT s.name,t.hour,t.accuracy,t.samples FROM time_stats t JOIN systems s ON s.system_id=t.system_id
                       WHERE s.status='QUALIFIED' AND t.samples>=10 ORDER BY t.accuracy DESC LIMIT 12""").fetchall(); c.close()
    if not r:return "⏰ Qualified logic-এর জন্য পর্যাপ্ত time sample নেই।"
    return "⏰ BEST OBSERVED WINDOWS\n\n"+"\n".join(f"{x['name']} | {x['hour']:02d}:00 | {x['accuracy']:.1f}% | n={x['samples']}" for x in r)

async def activity_text():
    c=db(); r=c.execute("SELECT event_type,message,created_at FROM research_events ORDER BY id DESC LIMIT 15").fetchall(); c.close()
    if not r:return "🔬 Research activity এখনো তৈরি হয়নি।"
    return "🔬 RESEARCH ACTIVITY\n\n"+"\n\n".join(f"{x['created_at']} | {x['event_type']}\n{x['message']}" for x in r)

async def send_chunks(msg,text):
    for i in range(0,len(text),3900): await msg.reply_text(text[i:i+3900])

async def telegram_error_handler(update, context):
    """Log Telegram handler errors without crashing the polling application."""
    log.error("Unhandled Telegram update error: %s", context.error, exc_info=context.error)


async def callback(update,context):
    q=update.callback_query; await q.answer()
    if not allowed(update):return
    if q.data=="status": t=await status_text()
    elif q.data=="logic": t=await logic_text()
    elif q.data=="report": t=await report_text()
    elif q.data=="time": t=await time_text()
    else: t=await activity_text()
    await send_chunks(q.message,t)

async def cmd_status(u,c):
    if allowed(u): await send_chunks(u.message,await status_text())
async def cmd_logic(u,c):
    if allowed(u): await send_chunks(u.message,await logic_text())
async def cmd_report(u,c):
    if allowed(u): await send_chunks(u.message,await report_text())

async def collector(app):
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                n=save(normalize(await fetch(s)))
                if n: log.info("new periods=%s",n)
            except Exception as e: log.warning("collector: %s",e)
            await asyncio.sleep(POLL_SECONDS)

async def post_init(app):
    init_db(); register_candidates()
    app.create_task(collector(app)); app.create_task(learner())


def main():
    if not BOT_TOKEN or BOT_TOKEN=="PASTE_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN is not configured. Set BOT_TOKEN in hosting environment.")
    application=Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start",start))
    application.add_handler(CommandHandler("status",cmd_status))
    application.add_handler(CommandHandler("logic",cmd_logic))
    application.add_handler(CommandHandler("report",cmd_report))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_error_handler(telegram_error_handler)
    log.info("XHAWON SELF-LEARNING PATTERN AI v5 starting")
    application.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
