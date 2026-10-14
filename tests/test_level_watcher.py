"""Tests for core/level_watcher.py — the pure decision helpers and the
state machine driven through check_levels().

Run:  python3 tests/test_level_watcher.py      (no pytest dependency)
"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import level_watcher as lw

_fails: list = []
_count = 0


def check(label, cond):
    global _count
    _count += 1
    if not cond:
        _fails.append(label)
    print(f"{'PASS' if cond else 'FAIL'}  {label}")


def section(name):
    print(f"\n── {name} " + "─" * max(0, 58 - len(name)))


# ══ Pure helpers ═════════════════════════════════════════════════════════════

section("direction_at_pending — geometry, not momentum")
check("level above price -> resistance",
      lw.direction_at_pending(98000, 100000) == lw.DIR_RESISTANCE)
check("level below price -> support",
      lw.direction_at_pending(102000, 100000) == lw.DIR_SUPPORT)
check("label ignores momentum: falling price under a level is still resistance",
      lw.direction_at_pending(98000, 100000, prev_price=99000) == lw.DIR_RESISTANCE)
check("label ignores momentum: rising price over a level is still support",
      lw.direction_at_pending(102000, 100000, prev_price=101000) == lw.DIR_SUPPORT)
check("price exactly on level, arrived rising -> resistance",
      lw.direction_at_pending(100000, 100000, prev_price=99000) == lw.DIR_RESISTANCE)
check("price exactly on level, arrived falling -> support",
      lw.direction_at_pending(100000, 100000, prev_price=101000) == lw.DIR_SUPPORT)

section("moved_toward — minimum move filter")
check("0.00% move (flat) -> no direction",
      not lw.moved_toward(98000, 98000, 100000))
check("0.05% move toward level (< 0.1% floor) -> no direction",
      not lw.moved_toward(98049, 98000, 100000))
check("0.20% move toward a level above -> yes",
      lw.moved_toward(98196, 98000, 100000))
check("0.20% move AWAY from a level above -> no",
      lw.moved_toward(97804, 98000, 100000) is False)
check("0.20% move down toward a level below -> yes",
      lw.moved_toward(101800, 102000, 100000))
check("0.20% move up away from a level below -> no",
      lw.moved_toward(102200, 102000, 100000) is False)
check("no previous price -> no direction",
      not lw.moved_toward(98000, None, 100000))
check("threshold is tunable",
      lw.moved_toward(98049, 98000, 100000, min_move_pct=0.01))

section("crossed — last price and wick detection")
check("resistance, price still below -> not crossed",
      not lw.crossed(100000, lw.DIR_RESISTANCE, 99000))
check("resistance, price now above -> crossed",
      lw.crossed(100000, lw.DIR_RESISTANCE, 100500))
check("resistance, price exactly at level -> crossed",
      lw.crossed(100000, lw.DIR_RESISTANCE, 100000))
check("support, price still above -> not crossed",
      not lw.crossed(100000, lw.DIR_SUPPORT, 101000))
check("support, price now below -> crossed",
      lw.crossed(100000, lw.DIR_SUPPORT, 99500))
check("WICK: last price back below, but high pierced the level -> crossed",
      lw.crossed(100000, lw.DIR_RESISTANCE, 99000, window=(98800, 100120)))
check("WICK: window stayed under the level -> not crossed",
      not lw.crossed(100000, lw.DIR_RESISTANCE, 99000, window=(98800, 99900)))
check("WICK: support side, low pierced the level -> crossed",
      lw.crossed(100000, lw.DIR_SUPPORT, 101000, window=(99850, 101200)))
check("no window given -> falls back to last price only",
      not lw.crossed(100000, lw.DIR_RESISTANCE, 99000, window=None))


# ══ State machine ════════════════════════════════════════════════════════════

tmp = tempfile.mkdtemp()
lw.LEVELS_PATH = os.path.join(tmp, "resistance_levels.json")
lw.DATA_DIR = tmp

sent: list = []


def notify(text):
    sent.append(text)
    return True


def reset(levels=(100000.0,), alert_pct=3.0):
    json.dump({"BTC": {"levels": list(levels), "alert_pct": alert_pct,
                       "last_price": None, "last_checked": None,
                       "level_state": {}}},
              open(lw.LEVELS_PATH, "w"))
    sent.clear()


def tick(price, window=None):
    sent.clear()
    return lw.check_levels("BTC", lambda s: price, notify,
                           window_fn=(lambda s: window) if window else None)


def state(key="100000"):
    return (json.load(open(lw.LEVELS_PATH))["BTC"].get("level_state") or {}).get(key, {})


section("scenario: price rising to a level from below")
reset()
tick(96000)                       # baseline
tick(98000)                       # +2.08% toward -> pending
check("goes pending, labelled resistance",
      state()["state"] == "pending" and state()["direction"] == "resistance")
tick(98500)                       # +0.51% toward -> confirm
check("confirms and fires exactly one alert", len(sent) == 1)
check("message says RESISTANCE", "RESISTANCE" in sent[0])
check("state is notified", state()["state"] == "notified")

section("scenario: label stays locked when price crosses before confirming")
reset()
tick(96000)
tick(98000)                       # pending as RESISTANCE
check("pending as resistance", state()["direction"] == "resistance")
tick(100400)                      # popped ABOVE the level
check("crossing sends the break message, not an approach", len(sent) == 1)
check("  message is 'broke through'", "broke through" in sent[0])
check("  it reports the ORIGINAL label (was resistance)", "was resistance" in sent[0])
check("  it is NOT mislabelled SUPPORT", "SUPPORT" not in sent[0])
check("  state is notified", state()["state"] == "notified")
print("   message:\n     " + sent[0].replace("\n", "\n     "))

section("regression: the old bug — crossed then ticking down")
reset()
tick(96000)
tick(98000)                       # pending as RESISTANCE
tick(100400, window=(98000, 100500))   # above the level, now ticking DOWN
check("does NOT emit a SUPPORT approach alert",
      not any("approaching SUPPORT" in m for m in sent))
check("emits the break message instead",
      len(sent) == 1 and "broke through" in sent[0])

section("scenario: price wicks through the level and returns")
reset()
tick(96000)
tick(98000)                                      # pending as RESISTANCE
tick(98900, window=(98100, 100250))              # last price below, high pierced it
check("wick through the level is detected as a break",
      len(sent) == 1 and "broke through" in sent[0])
check("  no 'approaching' alert was sent",
      not any("approaching" in m for m in sent))
check("  state is notified", state()["state"] == "notified")

reset()
tick(96000)
tick(98000)
tick(98900, window=(98100, 99400))               # wick did NOT reach the level
check("a wick that fell short still confirms a normal approach",
      len(sent) == 1 and "approaching RESISTANCE" in sent[0])

section("scenario: price falling to a level from above")
reset()
tick(104000)
tick(102000)                      # -1.92% toward -> pending
check("goes pending, labelled support",
      state()["state"] == "pending" and state()["direction"] == "support")
tick(101500)
check("confirms with a SUPPORT alert",
      len(sent) == 1 and "approaching SUPPORT" in sent[0])
check("  message says falling", "falling" in sent[0])

section("scenario: price oscillating inside the band")
reset()
tick(98000)
total = 0
for p in (98020, 98000, 98030, 97990, 98010, 98005):   # all moves < 0.1%
    tick(p)
    total += len(sent)
check("sub-threshold jitter inside the band produces no alerts", total == 0)
check("  level never leaves armed", state()["state"] == "armed")

reset()
tick(96000)
tick(98000)                       # pending
tick(98500)                       # fires
check("one real approach fires once", len(sent) == 1)
fired = 0
for p in (98400, 98600, 98300, 98700, 98200):
    tick(p)
    fired += len(sent)
check("continued oscillation after firing stays silent (hysteresis)", fired == 0)
check("  state is cooling", state()["state"] == "cooling")

section("backward compatibility")
json.dump({"BTC": {"levels": [100000.0], "alert_pct": 3.0,
                   "last_price": 98000, "last_checked": None,
                   "level_state": {"100000": {"state": "pending",
                                              "pending_since": "2026-10-14T07:00:00Z",
                                              "notified_at": None}}}},
          open(lw.LEVELS_PATH, "w"))
sent.clear()
lw.check_levels("BTC", lambda s: 98500, notify)
check("pending entry with no stored direction does not fire a guessed label",
      len(sent) == 0)
check("  label is backfilled and it stays pending",
      state()["state"] == "pending" and state()["direction"] == "resistance")
sent.clear()
lw.check_levels("BTC", lambda s: 99000, notify)
check("  next check confirms with the backfilled label",
      len(sent) == 1 and "RESISTANCE" in sent[0])

json.dump({"ETH": {"levels": [4000.0], "alert_pct": 3.0,
                   "notified": {"4000": {"side": "below",
                                         "last_notified": "2026-10-13T08:20:00Z"}}}},
          open(lw.LEVELS_PATH, "w"))
sent.clear()
lw.check_levels("ETH", lambda s: 3950.0, notify)
after = json.load(open(lw.LEVELS_PATH))["ETH"]
check("v1 'notified' map still migrates to level_state",
      "notified" not in after and after["level_state"]["4000"]["state"] == "cooling")
check("  and fires nothing on migration", len(sent) == 0)

section("failed delivery")
reset()
tick(96000)
tick(98000)
sent.clear()
lw.check_levels("BTC", lambda s: 98500, lambda t: (sent.append(t), False)[1])
check("failed send leaves the level pending for retry",
      state()["state"] == "pending")
sent.clear()
lw.check_levels("BTC", lambda s: 98800, notify)
check("  next check retries and succeeds",
      len(sent) == 1 and state()["state"] == "notified")

section("nearest level only")
json.dump({"BTC": {"levels": [100000.0, 101000.0, 102000.0], "alert_pct": 3.0,
                   "last_price": None, "last_checked": None, "level_state": {}}},
          open(lw.LEVELS_PATH, "w"))
tick(98000)
tick(99000)
check("only the nearest level goes pending",
      state("100000")["state"] == "pending"
      and state("101000")["state"] == "armed"
      and state("102000")["state"] == "armed")
tick(99500)
check("exactly one alert fires for the cycle", len(sent) == 1)

print("\n" + "═" * 64)
if _fails:
    print(f"{len(_fails)} of {_count} checks FAILED:")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print(f"ALL {_count} CHECKS PASSED")
