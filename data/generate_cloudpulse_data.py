#!/usr/bin/env python3
"""
CloudPulse Console - synthetic dataset generator
=================================================
Generates 4 CSVs that match the project schema exactly:

  organizations        (500 rows)
  users                (1,500 rows)
  api_usage_logs       (20,000 rows)
  subscription_events  (3,000 rows)

Requirements: Python 3.9+, pandas, numpy.   Run:  python generate_cloudpulse_data.py

Patterns deliberately baked in (all tunable in CFG below)
---------------------------------------------------------
1. ~60% onboarding drop-off: only ~38% of orgs make a successful (HTTP 200) call
   within 7 days of signup; a few more activate late; the rest only ever produce
   401/403 noise or nothing at all.
2. HTTP 429 spike: only Free-tier orgs that hit quota_limit = 1000 get 429s. They
   cluster in the days after a `Quota_100_Blocked` event, so 429s rise as the
   monthly cycle progresses and as the Free base grows. Pro/Enterprise: near zero.
3. Warned vs abrupt: "warned" orgs receive `Quota_80_Reached` before blocking;
   "abrupt" orgs jump straight to `Quota_100_Blocked`. Warned orgs convert ~4x more.

Modelling notes (read before analysing)
---------------------------------------
* api_usage_logs is a 20,000-row SAMPLE of request traffic. A real 1,000-call quota
  cannot be reproduced row-for-row with that budget, so the metering signal
  (utilisation state) lives in subscription_events and the logs carry the 429 /
  200 / error traffic around those events.
* Quota cycles are calendar months (reset on the 1st).
* No 200 logs are written for a Free org inside its blocked window (it would get 429).
* Conversion is deliberately oversampled (~5% of orgs are paid vs. a real-world 2.1%)
  so that the 80% vs 100% comparison has enough rows. Lower conv_warned /
  conv_abrupt in CFG for a more conservative dataset.
* Payment_Failed -> `Past_Due` (3-day grace) -> `Suspended` if never recovered.
"""
import argparse
import os

import numpy as np
import pandas as pd

CFG = dict(
    seed=42,
    n_orgs=500, n_users=1500, n_logs=20000, n_events=3000,
    as_of="2026-11-15 12:00:00",        # "now" for the dataset
    start="2025-04-01",                 # earliest possible signup
    p_activate_7d=0.355,                # activation within 7 days (=> 62% drop-off)
    p_activate_late=0.045,              # activates after day 7 (still a "late" win)
    p_hitter=0.95,                      # share of activated orgs that burn through 1,000 calls
    share_warned=0.60,                  # hitters that get an 80% warning first
    conv_warned=0.14, conv_abrupt=0.05, # Free -> Pro conversion by trigger type
    p_progress_100=0.60,                # warned org keeps going and hits 100% in same cycle
    n_direct_enterprise=5, n_expansion=3, n_forced_past_due=4, n_forced_suspended=2,
    pro_price=99.0,
)

HOUR_P = np.array([1, 1, 1, 1, 1, 2, 3, 5, 7, 9, 10, 10, 9, 9, 10, 10, 9, 8, 6, 4, 3, 2, 1, 1], float)
HOUR_P /= HOUR_P.sum()
ENDPOINTS = ["/v1/data/query", "/v1/data/stream", "/v1/reports", "/v1/datasets", "/v1/export", "/v1/webhooks"]
ENDPOINT_P = [0.40, 0.12, 0.18, 0.15, 0.10, 0.05]
ADJ = ["Northwind", "Bluepeak", "Quantum", "Helio", "Vertex", "Lumina", "Ironclad", "Nimbus", "Orbital", "Cobalt",
       "Fathom", "Granite", "Harbor", "Kestrel", "Meridian", "Nova", "Onyx", "Pioneer", "Quartz", "Relay",
       "Summit", "Tidal", "Umbra", "Verdant", "Willow", "Zephyr", "Apex", "Brightline", "Crestview", "Driftwood"]
NOUN = ["Analytics", "Labs", "Systems", "Logistics", "Health", "Retail", "Finance", "Robotics", "Media", "Energy",
        "Software", "Networks", "Data", "Cloud", "Mobility", "Foods", "Security", "Education", "Travel", "Studios"]
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Riley", "Casey", "Jamie", "Avery", "Quinn", "Priya", "Wei",
         "Mai", "Linh", "Hiro", "Sofia", "Mateo", "Amara", "Noah", "Elena", "Omar", "Chloe", "Ravi", "Anh", "Lucas"]
LAST = ["Nguyen", "Tran", "Smith", "Patel", "Kim", "Garcia", "Tanaka", "Silva", "Cohen", "Lopez", "Brown", "Chen",
        "Singh", "Muller", "Rossi", "Ivanov", "Okafor", "Haddad", "Larsen", "Dubois"]

D = pd.Timedelta


def run(cfg, out_dir):
    rng = np.random.default_rng(cfg["seed"])
    AS_OF = pd.Timestamp(cfg["as_of"])
    START = pd.Timestamp(cfg["start"])
    N = cfg["n_orgs"]

    def mfloor(ts):
        return pd.Timestamp(year=ts.year, month=ts.month, day=1)

    def mnext(ts):
        return mfloor(ts) + pd.DateOffset(months=1)

    def months_between(a, b):
        n = (b.year - a.year) * 12 + (b.month - a.month)
        return n - (1 if b.day < a.day else 0)

    # ------------------------------------------------------------------ ORGANIZATIONS
    last_signup = AS_OF - D(days=7)                       # cohorts must be old enough to judge 7-day activation
    span = (last_signup.normalize() - START).days
    w = np.linspace(1.0, 3.0, span + 1)                   # signups grow over time
    day_idx = rng.choice(span + 1, size=N, p=w / w.sum())
    created = sorted(START + D(days=int(d)) + D(seconds=int(s))
                     for d, s in zip(day_idx, rng.integers(0, 86400, size=N)))

    names, seen = [], set()
    for i in range(N):
        while True:
            nm = f"{rng.choice(ADJ)} {rng.choice(NOUN)}"
            if nm not in seen:
                seen.add(nm)
                break
            if len(seen) > len(ADJ) * len(NOUN) - 20:
                nm = f"{nm} {i}"
                seen.add(nm)
                break
        names.append(nm)

    orgs = []
    for i in range(N):
        orgs.append(dict(idx=i, org_id=f"org_{i + 1:04d}", name=names[i], created=created[i],
                         activated=False, first_call=None, kind=None, hitter=False, converter=False,
                         direct_ent=False, forced=None, expander=False, anchor=None, t_trig=None,
                         t_up=None, p_org=float(rng.beta(4, 2.5)), plan="Free", status="Active",
                         ent_mrr=None, ent_quota=None, churn_ts=None, susp_ts=None))

    # Activation: ~38% inside 7 days (log-normal hours), ~4.5% later, the rest never
    for o in orgs:
        r = rng.random()
        if r < cfg["p_activate_7d"]:
            hrs = float(np.clip(rng.lognormal(np.log(18), 1.0), 0.08, 160))
            o["first_call"] = o["created"] + D(hours=hrs)
        elif r < cfg["p_activate_7d"] + cfg["p_activate_late"] and o["created"] <= AS_OF - D(days=50):
            o["first_call"] = o["created"] + D(days=float(rng.uniform(8, 45)))
        o["activated"] = o["first_call"] is not None

    # Quota hitters and their type
    elig = [o for o in orgs if o["activated"] and o["first_call"] <= AS_OF - D(days=20)]
    for o in elig:
        if rng.random() < cfg["p_hitter"]:
            o["hitter"] = True
            o["kind"] = "A" if rng.random() < cfg["share_warned"] else "B"
    A = [o["idx"] for o in orgs if o["kind"] == "A"]
    B = [o["idx"] for o in orgs if o["kind"] == "B"]
    for idxs, p in ((A, cfg["conv_warned"]), (B, cfg["conv_abrupt"])):
        for i in rng.choice(idxs, size=max(2, round(p * len(idxs))), replace=False):
            orgs[int(i)]["converter"] = True

    # Sales-led Enterprise (never hit the Free wall)
    pool = [o["idx"] for o in orgs if o["activated"] and not o["hitter"] and o["created"] <= AS_OF - D(days=80)]
    for i in rng.choice(pool, size=cfg["n_direct_enterprise"], replace=False):
        o = orgs[int(i)]
        o["direct_ent"] = True
        o["ent_mrr"] = float(rng.integers(30, 100) * 50)
        o["ent_quota"] = int(rng.choice([250000, 500000, 1000000]))
        lb = max(o["created"] + D(days=10), o["first_call"] + D(days=5))
        o["anchor"] = lb + (AS_OF - D(days=6) - lb) * float(rng.random())

    # Converters: decision time, forced Past_Due / Suspended, expansion
    def lower_bound(o):
        return max(o["created"] + D(days=25), o["first_call"] + D(days=14))

    conv = [o for o in orgs if o["converter"]]
    rng.shuffle(conv)
    want = ["pd"] * cfg["n_forced_past_due"] + ["sus"] * cfg["n_forced_suspended"]
    for o in conv:
        if want:
            kind = want[0]
            L = AS_OF - D(days=float(rng.uniform(0.15, 2.8) if kind == "pd" else rng.uniform(6, 40)))
            lb = lower_bound(o)
            nmax = months_between(lb, L)
            if nmax >= 0:
                n = int(rng.integers(0, min(nmax, 9) + 1))
                o["anchor"] = L - pd.DateOffset(months=n)
                o["forced"], o["forced_k"] = kind, n
                want.pop(0)
                continue
        lb = lower_bound(o)
        o["anchor"] = lb + (AS_OF - D(days=1) - lb) * float(rng.random())
    for o in conv:
        o["t_up"] = o["anchor"] - D(minutes=8)
        lag = rng.uniform(0.3, 8) if o["kind"] == "A" else rng.uniform(0.1, 5)
        o["t_trig"] = o["t_up"] - D(days=float(lag))
    exp_pool = [o for o in conv if o["forced"] is None and o["anchor"] <= AS_OF - D(days=75)]
    for i in rng.choice(len(exp_pool), size=min(cfg["n_expansion"], len(exp_pool)), replace=False):
        o = exp_pool[int(i)]
        o["expander"] = True
        o["ent_mrr"] = float(rng.integers(16, 50) * 50)
        o["ent_quota"] = int(rng.choice([250000, 500000]))
    paid_orgs = [o for o in orgs if o["converter"] or o["direct_ent"]]

    # ------------------------------------------------------------------ BILLING EVENTS
    ev = []   # (org_idx, type, ts, mrr)
    for o in orgs:                                   # every org starts a trial at signup
        ev.append((o["idx"], "Trial_Started", o["created"] + D(seconds=int(rng.integers(20, 400))), 0.0))

    for o in paid_orgs:
        base = o["ent_mrr"] if o["direct_ent"] else cfg["pro_price"]
        bills = []
        k = 0
        while True:
            b = o["anchor"] + pd.DateOffset(months=k)
            if b > AS_OF:
                break
            bills.append(b)
            k += 1
        k_exp = int(rng.integers(2, max(3, len(bills)))) if o["expander"] else None
        churned, last_fail, ever_success = False, None, False
        for k, b in enumerate(bills):
            mrr = o["ent_mrr"] if (o["expander"] and k >= k_exp) else base
            if k == 0 or (o["expander"] and k == k_exp):
                ev.append((o["idx"], "Upgrade_Initiated", b - D(minutes=float(rng.uniform(1.5, 12))), mrr))
            forced_bill = o["forced"] is not None and k == o["forced_k"]
            pf = 0.07 if k == 0 else 0.04
            for off, ps in ((D(0), 1 - pf), (D(days=1), 0.6), (D(days=3), 0.5)):
                t = b + off + D(minutes=float(rng.uniform(1, 25)))
                if t > AS_OF:
                    break
                ok = False if forced_bill else rng.random() < ps
                ev.append((o["idx"], "Payment_Succeeded" if ok else "Payment_Failed", t, mrr))
                if ok:
                    last_fail = None
                    ever_success = True
                    break
                last_fail = t
                if off == D(days=3):
                    churned = True
            if churned:
                break
        if last_fail is not None:
            if churned or (AS_OF - last_fail) > D(days=3):
                o["status"], o["susp_ts"] = "Suspended", last_fail
            else:
                o["status"] = "Past_Due"
        o["churn_ts"] = o["susp_ts"]
        o["plan"] = "Enterprise" if (o["direct_ent"] or o["expander"]) else "Pro"

    # ------------------------------------------------------------------ QUOTA EPISODES
    episodes, ep_seq = [], [0]

    def new_ep(o, kind, month, events, t429, block, trigger=False):
        ep_seq[0] += 1
        return dict(id=ep_seq[0], org=o["idx"], kind=kind, month=month, events=events,
                    t429=t429, block=block, trigger=trigger)

    def times429(t100, cycle_end, lam):
        n = 1 + int(rng.poisson(lam))
        return [t100 + (cycle_end - t100) * float(x) for x in rng.beta(1, 2.0, size=n)]

    def free_episode(o, m, max_events=None):
        m_next = mnext(m)
        start = max(m + D(days=1), o["first_call"] + D(days=2))
        end = min(m_next - D(days=1), AS_OF - D(hours=2))
        if o["converter"]:
            end = min(end, o["t_trig"] - D(days=1))
        if (end - start) < D(days=4):
            return None
        span_ = end - start
        events, t100, trig = [], None, None
        if o["kind"] == "A":
            t80 = start + span_ * float(rng.uniform(0.1, 0.6))
            events.append(("Quota_80_Reached", t80, 0.0))
            trig = t80
            if (max_events is None or max_events >= 2) and rng.random() < cfg["p_progress_100"]:
                t = t80 + D(days=float(rng.uniform(0.5, 6)))
                if t <= end:
                    events.append(("Quota_100_Blocked", t, 0.0))
                    t100 = t
        else:
            t100 = start + span_ * float(rng.uniform(0.25, 0.95))
            events.append(("Quota_100_Blocked", t100, 0.0))
            trig = t100
        p_ab = 0.12 if o["kind"] == "A" else 0.03
        if (max_events is None or len(events) < max_events) and rng.random() < p_ab:
            tu = trig + D(days=float(rng.uniform(0.05, 2)))
            if tu <= AS_OF - D(hours=1):
                events.append(("Upgrade_Initiated", tu, cfg["pro_price"]))
        t429, block = [], []
        if t100 is not None:
            cyc_end = min(m_next - D(minutes=1), AS_OF)
            t429 = times429(t100, cyc_end, 1.0 if o["kind"] == "A" else 2.2)
            block = [(t100, m_next)]
        return new_ep(o, o["kind"], m, events, t429, block)

    def trigger_episode(o):
        t_trig, t_up = o["t_trig"], o["t_up"]
        events, t100 = [], None
        if o["kind"] == "A":
            events.append(("Quota_80_Reached", t_trig, 0.0))
            if (t_up - t_trig) > D(days=1.5) and rng.random() < 0.45:
                t100 = t_trig + D(days=float(rng.uniform(0.3, (t_up - t_trig).total_seconds() / 86400 - 0.3)))
                events.append(("Quota_100_Blocked", t100, 0.0))
        else:
            t100 = t_trig
            events.append(("Quota_100_Blocked", t_trig, 0.0))
        t429, block = [], []
        if t100 is not None:
            t429 = times429(t100, t_up - D(minutes=2), 1.0 if o["kind"] == "A" else 1.5)
            block = [(t100, t_up)]
        return new_ep(o, o["kind"], mfloor(t_trig), events, t429, block, trigger=True)

    def pro_episode(o, m):
        m_next = mnext(m)
        start = max(m + D(days=1), o["anchor"] + D(days=3))
        end = min(m_next - D(days=1), AS_OF - D(hours=2))
        if o["churn_ts"] is not None:
            end = min(end, o["churn_ts"])
        if (end - start) < D(days=4):
            return None
        t80 = start + (end - start) * float(rng.uniform(0.1, 0.8))
        events, t429, block = [("Quota_80_Reached", t80, 0.0)], [], []
        if rng.random() < 0.15:
            t100 = t80 + D(days=float(rng.uniform(1, 6)))
            if t100 <= end:
                events.append(("Quota_100_Blocked", t100, 0.0))
                t429 = times429(t100, min(m_next - D(minutes=1), AS_OF), 0.6)
                block = [(t100, m_next)]
        return new_ep(o, "P", m, events, t429, block)

    def free_months(o):
        last = mfloor(o["t_trig"]) if o["converter"] else mfloor(AS_OF)
        m, out = mfloor(o["first_call"]), []
        while m <= last:
            out.append(m)
            m = mnext(m)
        return out

    for o in orgs:
        if o["hitter"]:
            for m in free_months(o):
                if o["converter"] and m == mfloor(o["t_trig"]):
                    episodes.append(trigger_episode(o))
                elif rng.random() < o["p_org"]:
                    e = free_episode(o, m)
                    if e:
                        episodes.append(e)
        if o["converter"]:
            m = mnext(o["anchor"])
            while m <= mfloor(AS_OF):
                if rng.random() < 0.10:
                    e = pro_episode(o, m)
                    if e:
                        episodes.append(e)
                m = mnext(m)

    def total_events():
        return len(ev) + sum(len(e["events"]) for e in episodes)

    # Top-up / trim so the table has exactly n_events rows
    target = cfg["n_events"]
    guard = 0
    while total_events() > target and guard < 5000:
        guard += 1
        removable = [e for e in episodes if not e["trigger"]]
        episodes.remove(removable[int(rng.integers(len(removable)))])
    guard = 0
    while total_events() < target and guard < 20000:
        guard += 1
        o = orgs[int(rng.choice([x["idx"] for x in orgs if x["hitter"]]))]
        used = {e["month"] for e in episodes if e["org"] == o["idx"]}
        cands = [m for m in free_months(o) if m not in used and
                 not (o["converter"] and m == mfloor(o["t_trig"]))]
        if not cands:
            continue
        e = free_episode(o, cands[int(rng.integers(len(cands)))], max_events=target - total_events())
        if e:
            episodes.append(e)
    assert total_events() == target, f"events {total_events()} != {target}"

    for e in episodes:
        for (t, ts, mrr) in e["events"]:
            ev.append((e["org"], t, ts, mrr))

    # Finalise organisations
    for o in orgs:
        if o["plan"] == "Free":
            o["quota"] = 1000
        elif o["plan"] == "Pro":
            o["quota"] = 50000
        else:
            o["quota"] = o["ent_quota"]

    # ------------------------------------------------------------------ USERS
    n_inv = cfg["n_users"] - N
    wt = np.array([2.4 if o["activated"] else 0.4 for o in orgs])
    wt = wt * np.array([1.6 if o["plan"] != "Free" else 1.0 for o in orgs])
    inv_counts = rng.multinomial(n_inv, wt / wt.sum())
    users, emails = [], set()

    def make_email(first, last, org):
        dom = org["name"].lower().replace(" ", "") + ".com"
        base = f"{first}.{last}".lower()
        e, k = f"{base}@{dom}", 1
        while e in emails:
            k += 1
            e = f"{base}{k}@{dom}"
        emails.add(e)
        return e

    for o in orgs:
        f, l = rng.choice(FIRST), rng.choice(LAST)
        users.append(dict(org_id=o["org_id"], email=make_email(f, l, o), role="Admin",
                          onboarded_at=o["created"] + D(minutes=float(rng.uniform(1, 45))), _c=o["created"]))
        for _ in range(int(inv_counts[o["idx"]])):
            f, l = rng.choice(FIRST), rng.choice(LAST)
            role = rng.choice(["Developer", "Billing_Viewer", "Admin"], p=[0.72, 0.13, 0.15])
            ob = o["created"] + D(days=float(rng.uniform(0.1, 12)))
            ob = pd.NaT if (ob > AS_OF or rng.random() < 0.12) else ob   # pending invite
            users.append(dict(org_id=o["org_id"], email=make_email(f, l, o), role=role, onboarded_at=ob, _c=o["created"]))
    users_df = pd.DataFrame(users).sort_values(["_c", "org_id"], kind="stable").reset_index(drop=True)
    users_df.insert(0, "user_id", [f"usr_{i + 1:05d}" for i in range(len(users_df))])
    users_df = users_df[["user_id", "org_id", "email", "role", "onboarded_at"]]

    # ------------------------------------------------------------------ API USAGE LOGS
    L = []  # (org_idx, ts, status)

    def blocks_for(oidx):
        out = []
        for e in episodes:
            if e["org"] == oidx:
                out.extend(e["block"])
        return [(a.timestamp(), b.timestamp()) for a, b in out]

    block_map = {o["idx"]: blocks_for(o["idx"]) for o in orgs}
    log_end = {}
    for o in orgs:
        log_end[o["idx"]] = (o["susp_ts"] if o["susp_ts"] is not None else AS_OF)

    # Protected logs: first successful call, onboarding errors, never-activated noise, 429s
    for o in orgs:
        if o["activated"]:
            L.append((o["idx"], o["first_call"], 200))
            if rng.random() < 0.35:                      # misconfigured key before first success
                for _ in range(int(rng.integers(1, 4))):
                    ts = o["created"] + (o["first_call"] - o["created"]) * float(rng.uniform(0.05, 0.95))
                    L.append((o["idx"], ts, int(rng.choice([401, 403], p=[0.75, 0.25]))))
        elif rng.random() < 0.25:                        # tried, never succeeded
            for _ in range(int(rng.integers(1, 4))):
                ts = o["created"] + D(days=float(rng.uniform(0.01, 6)))
                L.append((o["idx"], ts, int(rng.choice([401, 403], p=[0.75, 0.25]))))
    for e in episodes:
        for ts in e["t429"]:
            L.append((e["org"], ts, 429))
    n_protected = len(L)
    assert n_protected < cfg["n_logs"], "protected logs exceed the log budget - lower episode volume"

    act = [o for o in orgs if o["activated"]]
    vol_w = []
    for o in act:
        wv = float(rng.lognormal(0, 0.9))
        wv *= 3.0 if o["hitter"] else 1.0
        wv *= 2.5 if o["converter"] else 1.0
        wv *= 5.0 if o["direct_ent"] else 1.0
        wv *= 4.0 if o["forced"] == "pd" else 1.0
        wv *= max((log_end[o["idx"]] - o["first_call"]).days, 5) ** 0.5
        vol_w.append(wv)
    vol_w = np.array(vol_w) / sum(vol_w)

    def gen_base(o, n):
        fs, es = o["first_call"].timestamp(), log_end[o["idx"]].timestamp()
        got, need = [], n
        for _ in range(12):
            if need <= 0:
                break
            cand = fs + (es - fs) * rng.beta(1.4, 1.0, size=need * 2)
            cand = np.floor(cand / 86400.0) * 86400.0 + rng.choice(24, size=len(cand), p=HOUR_P) * 3600 \
                   + rng.integers(0, 3600, size=len(cand))
            ok = (cand >= fs) & (cand <= es)
            for a, b in block_map[o["idx"]]:
                ok &= ~((cand >= a) & (cand <= b))
            good = cand[ok][:need]
            got.extend(good.tolist())
            need -= len(good)
        return got

    base_rows = []
    for _ in range(30):
        deficit = cfg["n_logs"] - n_protected - len(base_rows)
        if deficit <= 0:
            break
        counts = rng.multinomial(deficit, vol_w)
        for o, c in zip(act, counts):
            if c:
                for s in gen_base(o, int(c)):
                    st = int(rng.choice([200, 401, 403, 500], p=[0.955, 0.02, 0.01, 0.015]))
                    base_rows.append((o["idx"], pd.Timestamp(s, unit="s"), st))
    base_rows = base_rows[: cfg["n_logs"] - n_protected]
    L.extend(base_rows)
    assert len(L) == cfg["n_logs"], f"logs {len(L)} != {cfg['n_logs']}"

    logs = pd.DataFrame(L, columns=["oi", "timestamp", "status_code"])
    logs["timestamp"] = pd.to_datetime(logs["timestamp"]).dt.floor("s")
    logs = logs.sort_values("timestamp", kind="stable").reset_index(drop=True)
    plan_of = np.array([o["plan"] for o in orgs])
    n = len(logs)
    sc = logs["status_code"].to_numpy()
    lat = rng.lognormal(np.log(140), 0.55, size=n)
    lat = np.where(np.isin(plan_of[logs["oi"].to_numpy()], ["Pro", "Enterprise"]), lat * 0.8, lat)
    lat = np.where(sc == 500, lat * 6, lat)
    lat = np.where(sc == 429, rng.uniform(8, 30, size=n), lat)
    lat = np.where((sc == 401) | (sc == 403), rng.uniform(10, 40, size=n), lat)
    logs["latency_ms"] = np.maximum(lat.round().astype(int), 5)
    logs["endpoint_path"] = rng.choice(ENDPOINTS, size=n, p=ENDPOINT_P)
    logs["org_id"] = [orgs[i]["org_id"] for i in logs["oi"]]
    logs.insert(0, "log_id", [f"log_{i + 1:07d}" for i in range(n)])
    logs = logs[["log_id", "org_id", "endpoint_path", "status_code", "latency_ms", "timestamp"]]

    # ------------------------------------------------------------------ WRITE
    ev_df = pd.DataFrame(ev, columns=["oi", "event_type", "event_timestamp", "mrr_amount"])
    ev_df["event_timestamp"] = pd.to_datetime(ev_df["event_timestamp"]).dt.floor("s")
    ev_df = ev_df.sort_values("event_timestamp", kind="stable").reset_index(drop=True)
    ev_df["org_id"] = [orgs[i]["org_id"] for i in ev_df["oi"]]
    ev_df["mrr_amount"] = ev_df["mrr_amount"].round(2)
    ev_df.insert(0, "event_id", [f"evt_{i + 1:06d}" for i in range(len(ev_df))])
    ev_df = ev_df[["event_id", "org_id", "event_type", "mrr_amount", "event_timestamp"]]

    org_df = pd.DataFrame([dict(org_id=o["org_id"], org_name=o["name"], plan_tier=o["plan"],
                                quota_limit=o["quota"], created_at=o["created"].floor("s"), status=o["status"])
                           for o in orgs])

    os.makedirs(out_dir, exist_ok=True)
    fmt = "%Y-%m-%d %H:%M:%S"
    org_df.to_csv(os.path.join(out_dir, "organizations.csv"), index=False, date_format=fmt)
    users_df.to_csv(os.path.join(out_dir, "users.csv"), index=False, date_format=fmt)
    logs.to_csv(os.path.join(out_dir, "api_usage_logs.csv"), index=False, date_format=fmt)
    ev_df.to_csv(os.path.join(out_dir, "subscription_events.csv"), index=False, date_format=fmt)

    # ------------------------------------------------------------------ SANITY REPORT
    assert (len(org_df), len(users_df), len(logs), len(ev_df)) == (N, cfg["n_users"], cfg["n_logs"], cfg["n_events"])
    assert users_df["email"].is_unique and org_df["org_id"].is_unique
    first_ok = logs[logs.status_code == 200].groupby("org_id")["timestamp"].min()
    c = org_df.set_index("org_id")["created_at"]
    ttv = (first_ok - c.reindex(first_ok.index)).dt.total_seconds() / 3600
    print(f"rows: orgs={len(org_df)} users={len(users_df)} logs={len(logs)} events={len(ev_df)}")
    print(f"activated within 7d : {(ttv <= 168).sum() / N:.1%}   (never/late: {1 - (ttv <= 168).sum() / N:.1%})")
    print(f"median TTV hours    : {ttv.median():.1f}")
    print("plan_tier           :", org_df.plan_tier.value_counts().to_dict())
    print("status              :", org_df.status.value_counts().to_dict())
    print("event types         :", ev_df.event_type.value_counts().to_dict())
    print("status codes        :", logs.status_code.value_counts().to_dict())
    lp = logs.merge(org_df[["org_id", "plan_tier"]], on="org_id")
    print("429 rate by plan    :", (lp.assign(x=lp.status_code == 429).groupby("plan_tier")["x"].mean() * 100).round(2).to_dict())
    print(f"warned orgs={len(A)} (converted {sum(orgs[i]['converter'] for i in A)}) | "
          f"abrupt orgs={len(B)} (converted {sum(orgs[i]['converter'] for i in B)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate CloudPulse synthetic CSVs")
    ap.add_argument("--out-dir", default="cloudpulse_data")
    ap.add_argument("--seed", type=int, default=CFG["seed"])
    ap.add_argument("--as-of", default=CFG["as_of"], help="dataset 'now', e.g. '2026-11-15 12:00:00'")
    a = ap.parse_args()
    CFG.update(seed=a.seed, as_of=a.as_of)
    run(CFG, a.out_dir)
