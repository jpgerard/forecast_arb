"""
Demo script: shows CCC v1 hardened allocator output with HARVEST_CLOSE + OPEN.
Run: python demo_ccc_v1_hardening.py
"""
import json
import datetime
import tempfile
from pathlib import Path

from forecast_arb.allocator.budget import append_ledger_record
from forecast_arb.allocator.plan import run_allocator_plan

tmp = Path(tempfile.mkdtemp())
today = datetime.date.today().isoformat()

# Write policy file
policy_yaml = f"""policy_id: ccc_v1
budgets:
  monthly_baseline: 1000.0
  monthly_max: 2000.0
  weekly_baseline: 250.0
  daily_baseline: 50.0
  weekly_kicker: 500.0
  daily_kicker: 100.0
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    ev_per_dollar_implied: 1.6
    ev_per_dollar_external: 0.5
    convexity_multiple: 25.0
  selloff:
    ev_per_dollar_implied: 1.3
    ev_per_dollar_external: 0.3
    convexity_multiple: 15.0
harvest:
  partial_close_multiple: 2.0
  full_close_multiple: 3.0
  partial_close_fraction: 0.5
  time_stop_dte: 14
  time_stop_min_multiple: 1.2
close_liquidity_guard:
  max_width_pct: 0.25
limits:
  max_open_actions_per_day: 1
  max_close_actions_per_day: 2
sizing:
  max_qty_per_trade: 10
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
ledger_dir: {tmp}
output_dir: {tmp}
intents_dir: {tmp / 'intents'}
"""
pf = tmp / "policy.yaml"
pf.write_text(policy_yaml)

# Write ledger: crash position open, $49 spent
ledger = tmp / "allocator_ledger.jsonl"
append_ledger_record(ledger, {
    "date": today, "action": "OPEN",
    "trade_id": "trade_crash_abc123", "regime": "crash",
    "candidate_id": "crash_venture_v2_abc:SPY:crash:20260402:570/550",
    "underlier": "SPY", "expiry": "20261001",
    "strikes": [570.0, 550.0], "qty": 1,
    "premium_per_contract": 49.0, "premium_spent": 49.0,
})

# Candidates:
#  - crash 20261001: mark at $151 = 3.08x → HARVEST_CLOSE (full)
#  - selloff 20261201: ev=30 >> 1.3 threshold → OPEN
candidates = {"candidates": [
    {
        "candidate_id": "crash_venture_v2_abc:SPY:crash:20261001:570/550",
        "regime": "crash", "underlier": "SPY", "expiry": "20261001",
        "debit_per_contract": 151.0,
        "max_gain_per_contract": 1849.0,
        "ev_per_dollar": 1.0,   # too low for open gate
        "strikes": {"long_put": 570.0, "short_put": 550.0},
    },
    {
        "candidate_id": "crash_venture_v2_xyz:SPY:selloff:20261201:540/520",
        "regime": "selloff", "underlier": "SPY", "expiry": "20261201",
        "debit_per_contract": 15.0,
        "max_gain_per_contract": 1985.0,  # 15x spread → 132x convexity >> 15x threshold
        "ev_per_dollar": 30.0,
        "strikes": {"long_put": 540.0, "short_put": 520.0},
        "run_id": "ccc_v2_run_20260228",
        "rank": 1,
    },
]}
cpath = tmp / "candidates.json"
cpath.write_text(json.dumps(candidates, indent=2))

# Run plan
plan = run_allocator_plan(str(pf), candidates_path=str(cpath), dry_run=False)

# Show structured output
d = plan.to_dict()
print()
print("=" * 60)
print("  allocator_actions.json — key fields")
print("=" * 60)

b = d["budgets"]
print("\nBUDGET BEFORE/AFTER:")
print(f"  spent_today_before:      ${b['spent_today_before']:.2f}")
print(f"  remaining_today_before:  ${b['remaining_today_before']:.2f}")
print(f"  planned_spend_today:     ${b['planned_spend_today']:.2f}")
print(f"  remaining_today_after:   ${b['remaining_today_after']:.2f}")
print(f"  spent_week_before:       ${b['spent_week_before']:.2f}")
print(f"  remaining_week_before:   ${b['remaining_week_before']:.2f}")
print(f"  planned_spend_week:      ${b['planned_spend_week']:.2f}")
print(f"  remaining_week_after:    ${b['remaining_week_after']:.2f}")
print(f"  kicker_enabled:          {b['kicker_enabled']}")

print("\nINVENTORY BEFORE/AFTER:")
inv = d["inventory"]
print(f"  before: {inv['before']}")
print(f"  after:  {inv['after']}")

print("\nACTIONS:")
close_count = sum(1 for a in d["actions"] if a["type"] in ("HARVEST_CLOSE", "ROLL_CLOSE"))
open_count = sum(1 for a in d["actions"] if a["type"] == "OPEN")
hold_count = sum(1 for a in d["actions"] if a["type"] == "HOLD")
print(f"  total={len(d['actions'])}  open={open_count}  close={close_count}  hold={hold_count}")

for a in d["actions"]:
    print(f"\n  type: {a['type']}")
    print(f"  reason_codes: {a['reason_codes'][:3]}")
    if a.get("candidate_id"):
        print(f"  candidate_id: {a['candidate_id']}")
    if a.get("run_id"):
        print(f"  run_id: {a['run_id']}")
    if a.get("candidate_rank") is not None:
        print(f"  candidate_rank: {a['candidate_rank']}")
    if a.get("convexity"):
        c = a["convexity"]
        print(f"  convexity: width=${c['width']}  debit=${c['debit']}/share  "
              f"premium=${c['premium_per_contract']}/contract  "
              f"max_gain=${c['max_gain_per_contract']}/contract  "
              f"multiple={c['multiple']}x")
    if a.get("intent_path"):
        print(f"  intent_path: {a['intent_path']}")

# Show intent file
intents_dir = tmp / "intents"
intent_files = sorted(intents_dir.glob("*.json")) if intents_dir.exists() else []
if intent_files:
    print()
    print("=" * 60)
    print("  Close intent file")
    print("=" * 60)
    f = intent_files[0]
    print(f"\nFile: {f.name}")
    intent = json.loads(f.read_text())
    for k in ("intent_type", "action_type", "policy_id",
              "trade_id", "underlier", "expiry", "strikes",
              "qty", "estimated_credit_per_contract", "manual_close_required"):
        print(f"  {k}: {intent.get(k)}")
