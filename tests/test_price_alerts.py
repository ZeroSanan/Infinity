"""Tests for core/price_alerts.py — the pure helpers and the one-shot checker.

Run:  python3 tests/test_price_alerts.py      (no pytest dependency)
"""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Point the DB at a throwaway file BEFORE anything opens it.
_tmp = tempfile.mkdtemp()
from core import db as _db
_db.DATA_DIR = _tmp
_db.DB_PATH  = os.path.join(_tmp, "infinity.db")
_db.init_db()

from core import price_alerts as pa

_fails: list = []
_count = 0


def check(label, cond):
    global _count
    _count += 1
    if not cond:
        _fails.append(label)
    print(f"{'PASS' if cond else 'FAIL'}  {label}")


def section(name):
    print(f"\n── {name} " + "─" * max(0, 56 - len(name)))


sent: list = []
def notify(text):
    sent.append(text); return True
def notify_fail(text):
    sent.append(text); return False


def wipe():
    for row in _db.price_alerts_get_all():
        _db.price_alerts_delete(row["id"])
    sent.clear()


def mk(target=76000.0, pct=1.0, **kw):
    return _db.price_alerts_insert("BTC", target, pct,
                                   kw.get("note"), kw.get("notify_on_touch", False),
                                   kw.get("expires_at"))


def run(price, window=None, n=notify):
    sent.clear()
    return pa.check_price_alerts({"BTCUSDT": price},
                                 {"BTCUSDT": window} if window else {},
                                 notify_fn=n)


def get(aid):
    return _db.price_alerts_get(aid)


# ══ Pure helpers ═════════════════════════════════════════════════════════════

section("gap_pct / side_label")
check("gap below target", abs(pa.gap_pct(75400, 76000) - 0.7894) < 0.001)
check("gap above target", abs(pa.gap_pct(76600, 76000) - 0.7894) < 0.001)
check("below target wording", pa.side_label(75400, 76000) == "below target")
check("above target wording", pa.side_label(76600, 76000) == "above target")
check("no support/resistance wording in approach message",
      not any(w in pa.format_approach("BTC", 76000, 75400, 0.79).lower()
              for w in ("support", "resistance")))

section("touched")
check("window straddles target -> touched",
      pa.touched(76000, 75400, window=(75350, 76010)))
check("window short of target -> not touched",
      not pa.touched(76000, 75400, window=(75350, 75900)))
check("price exactly on target -> touched", pa.touched(76000, 76000))
check("crossed between checks (no window) -> touched",
      pa.touched(76000, 76100, last_price=75900))
check("same side both checks -> not touched",
      not pa.touched(76000, 75900, last_price=75800))
check("no window and no last_price -> not touched",
      not pa.touched(76000, 75400))

section("validate")
def raises(fn, frag):
    try:
        fn(); return False
    except ValueError as e:
        return frag.lower() in str(e).lower()
check("empty coin rejected", raises(lambda: pa.validate("", 100), "coin is required"))
check("negative target rejected", raises(lambda: pa.validate("BTC", -1), "positive"))
check("zero target rejected", raises(lambda: pa.validate("BTC", 0), "positive"))
check("non-numeric target rejected", raises(lambda: pa.validate("BTC", "abc"), "positive number"))
check("alert_pct 25 rejected", raises(lambda: pa.validate("BTC", 100, 25), "between 0.1 and 20"))
check("alert_pct 0.05 rejected", raises(lambda: pa.validate("BTC", 100, 0.05), "between 0.1 and 20"))
check("past expires_at rejected",
      raises(lambda: pa.validate("BTC", 100, 1, expires_at="2020-01-01T00:00:00Z"), "future"))
check("bad expires_at rejected",
      raises(lambda: pa.validate("BTC", 100, 1, expires_at="not-a-date"), "ISO-8601"))
check("alert_pct defaults when omitted",
      pa.validate("BTC", 100)["alert_pct"] == pa.DEFAULT_ALERT_PCT)
check("coin upper-cased", pa.validate("btc", 100)["coin"] == "BTC")


# ══ Checker ══════════════════════════════════════════════════════════════════

section("approach from below")
wipe(); a = mk(76000.0, 1.0)
run(74000.0)                                  # 2.63% away — outside the band
check("outside band -> no alert", not sent and get(a["id"])["status"] == "active")
check("  last_price still recorded", get(a["id"])["last_price"] == 74000.0)
r = run(75400.0)                              # 0.79% away — inside
check("inside band -> fires immediately (no confirmation wait)", len(sent) == 1)
print("   message:\n     " + sent[0].replace("\n", "\n     "))
check("  says below target", "below target" in sent[0])
check("  one-shot: status triggered", get(a["id"])["status"] == "triggered")
check("  triggered_at set", get(a["id"])["triggered_at"])

section("approach from above")
wipe(); a = mk(76000.0, 1.0)
run(78000.0)
check("outside band above -> no alert", not sent)
run(76600.0)
check("inside band above -> fires", len(sent) == 1)
check("  says above target", "above target" in sent[0])

section("note is included")
wipe(); a = mk(76000.0, 1.0, note="scale out half here")
run(75500.0)
check("note appears in the message", "Note: scale out half here" in sent[0])

section("wick through target, price back outside the band")
wipe(); a = mk(76000.0, 0.2)                  # tight band: 75,848 - 76,152
run(75000.0)                                  # far away, establishes last_price
r = run(75500.0, window=(75480, 76030))       # last price 0.66% away (outside 0.2%)
check("wick that reached the target fires despite price outside the band",
      len(sent) == 1)
check("  status triggered", get(a["id"])["status"] == "triggered")
wipe(); a = mk(76000.0, 0.2)
run(75000.0)
run(75500.0, window=(75480, 75700))           # wick fell short
check("wick short of the target does not fire", not sent)
check("  stays active", get(a["id"])["status"] == "active")

section("price already inside the band at creation")
wipe(); a = mk(76000.0, 1.0)                  # created with no last_price
r = run(75700.0)                              # first ever check is already inside
check("fires on the first check after creation", len(sent) == 1)
check("  no second alert on later checks", len(run(75650.0)) * 0 == 0 and not sent)
check("  status triggered", get(a["id"])["status"] == "triggered")

section("one-shot: no second alert after triggering")
wipe(); a = mk(76000.0, 1.0)
run(75500.0)
check("first check fires", len(sent) == 1)
total = 0
for p in (75600.0, 75800.0, 76000.0, 75400.0, 75900.0):
    run(p); total += len(sent)
check("no further alerts across 5 more checks", total == 0)
check("  checker no longer considers it (0 active rows)",
      len(_db.price_alerts_get_all("active")) == 0)

section("notify_on_touch — two-stage flow")
wipe(); a = mk(76000.0, 1.0, notify_on_touch=True)
run(75400.0)
check("stage 1 sends the approach message",
      len(sent) == 1 and "price alert" in sent[0])
check("  stays ACTIVE for stage 2", get(a["id"])["status"] == "active")
check("  approach_notified_at set", get(a["id"])["approach_notified_at"])
run(75500.0)
check("no repeat approach message while waiting for the touch", not sent)
run(75990.0, window=(75900, 76005))
check("stage 2 sends the touch message",
      len(sent) == 1 and sent[0].startswith("✅"))
print("   message:\n     " + sent[0].replace("\n", "\n     "))
check("  reached wording", "reached" in sent[0])
check("  now triggered", get(a["id"])["status"] == "triggered")
run(76100.0)
check("nothing more after the touch", not sent)

section("failed delivery retries")
wipe(); a = mk(76000.0, 1.0)
run(75400.0, n=notify_fail)
check("failed send attempted", len(sent) == 1)
check("  alert stays ACTIVE", get(a["id"])["status"] == "active")
check("  approach_notified_at NOT recorded", not get(a["id"])["approach_notified_at"])
run(75450.0)
check("next check retries and succeeds",
      len(sent) == 1 and get(a["id"])["status"] == "triggered")

wipe(); a = mk(76000.0, 1.0, notify_on_touch=True)
run(75400.0)
run(75990.0, window=(75900, 76005), n=notify_fail)
check("failed TOUCH send leaves it active", get(a["id"])["status"] == "active")
check("  touched_notified_at not recorded", not get(a["id"])["touched_notified_at"])
run(75995.0, window=(75900, 76005))
check("  touch retries and succeeds", len(sent) == 1 and get(a["id"])["status"] == "triggered")

section("expiry")
future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
past   = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
wipe(); a = mk(76000.0, 1.0, expires_at=future)
run(75400.0)
check("unexpired alert still fires", len(sent) == 1)
wipe(); a = _db.price_alerts_insert("BTC", 76000.0, 1.0, None, False, past)
r = run(75400.0)
check("expired alert is cancelled silently (no message)", not sent)
check("  status cancelled", get(a["id"])["status"] == "cancelled")
check("  reported as expired", len(r["expired"]) == 1)

section("multiple alerts per coin")
wipe()
a1 = mk(76000.0, 1.0)
a2 = mk(80000.0, 1.0)
a3 = mk(70000.0, 1.0)
run(75400.0)
check("only the in-band alert fires", len(sent) == 1)
check("  the other two stay active",
      get(a2["id"])["status"] == "active" and get(a3["id"])["status"] == "active")
check("  the fired one is triggered", get(a1["id"])["status"] == "triggered")

section("missing price / bad row isolation")
wipe(); a = mk(76000.0, 1.0)
r = pa.check_price_alerts({}, {}, notify_fn=notify)      # no price for BTCUSDT
check("missing price -> skipped, stays active",
      r["skipped"] == 1 and get(a["id"])["status"] == "active")
r = pa.check_price_alerts({"BTCUSDT": 75400.0}, {}, notify_fn=notify,
                           alerts=[{"id": None, "coin": "", "target_price": None}])
check("malformed row is skipped without raising", r["skipped"] == 1)

print("\n" + "═" * 62)
if _fails:
    print(f"{len(_fails)} of {_count} checks FAILED:")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print(f"ALL {_count} CHECKS PASSED")
