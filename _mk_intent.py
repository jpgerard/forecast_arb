import json,hashlib,datetime,os,glob

# latest run dir
runs=sorted(glob.glob(r"runs\crash_venture_v2\*"), key=os.path.getmtime)
rd=runs[-1]
p=os.path.join(rd,"artifacts","review_candidates.json")
d=json.load(open(p,"r",encoding="utf-8"))

c=d["regimes"]["crash"]["candidates"][0]

def pick(*names):
    for n in names:
        if n in c and c[n] is not None:
            return c[n]
    return None

expiry = pick("expiry","expiration","expiry_yyyymmdd")
symbol = pick("symbol","underlier","underlying","ticker") or "SPY"
debit  = pick("debit_per_contract","debit","max_debit","entry_debit")

# strikes appear under different names in different versions
long_strike  = pick("long_strike","buy_strike","strike_long","k_long","strike_buy")
short_strike = pick("short_strike","sell_strike","strike_short","k_short","strike_sell")

# sometimes legs are already provided
legs = c.get("legs")
if legs is None:
    if long_strike is None or short_strike is None:
        raise KeyError(f"Could not find strikes in candidate. Keys={sorted(list(c.keys()))}")
    legs = [
        {"action":"BUY","right":"P","strike":float(long_strike)},
        {"action":"SELL","right":"P","strike":float(short_strike)},
    ]

intent={
  "strategy":"crash_venture_v2",
  "regime":"crash",
  "symbol":symbol,
  "expiry":expiry,
  "qty":1,
  "limit_start":float(debit),
  "limit_max":round(float(debit)*1.02,4),
  "legs":legs,
  "created_ts_utc":datetime.datetime.utcnow().isoformat()+"Z",
  "source_run_dir":rd,
  "source_candidate":c.get("candidate_id") or c.get("id") or None
}

s=json.dumps({k:intent[k] for k in intent if k!="intent_id"}, sort_keys=True, separators=(",",":"))
intent["intent_id"]=hashlib.sha1(s.encode()).hexdigest()

# filename readable, id is deterministic
fn=f"intents\\\\{intent['symbol']}_{intent['expiry']}_{intent['regime']}_{intent['intent_id'][:8]}.json"
with open(fn,"w",encoding="utf-8") as f:
    f.write(json.dumps(intent,indent=2))

print(fn)
