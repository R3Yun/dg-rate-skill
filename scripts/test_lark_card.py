# mock test for Lark card rendering
import sys
import datetime
import json
sys.path.insert(0, '.')
from expiry_reminder import build_summary, aggregate, render_lark_card
from rate_io import NormalizedRateEntry

today = datetime.date(2026, 7, 10)
scenarios = [
    ("CNSHA", "THBKK", "SITC",  500, 950,  "2026-01-01", "2026-07-05"),  # expired
    ("CNSHA", "VNSGN", "MSC",   600, 1100, "2026-02-01", "2026-07-12"),  # 1-7d
    ("CNSHA", "SGSIN", "COSCO", 800, 1500, "2026-02-15", "2026-07-20"),  # 8-14d
]
entries = []
for pol, pod, carrier, of20, of40hq, vf, vt in scenarios:
    e = NormalizedRateEntry()
    e.pol = pol
    e.pod = pod
    e.carrier = carrier
    e.of_20 = of20
    e.of_40hq = of40hq
    e.valid_from = vf
    e.valid_to = vt
    e._record_id = "rec_" + pol + pod
    entries.append(e)

items, _ = build_summary(entries, today)
agg = aggregate(items)
card = render_lark_card(items, agg, today)
print(json.dumps(card, ensure_ascii=False, indent=2))
