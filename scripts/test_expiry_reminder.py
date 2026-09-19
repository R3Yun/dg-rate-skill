# mock test for expiry_reminder
import sys
import datetime
sys.path.insert(0, '/mnt/claw1/workspace/skills/dg-rate-query/scripts')
from expiry_reminder import bucket_of, _to_date, build_summary, aggregate, BUCKETS
from rate_io import NormalizedRateEntry

today = datetime.date(2026, 7, 10)
scenarios = [
    ("CNSHA", "THBKK", "SITC",  500, 950,  "2026-01-01", "2026-07-05"),
    ("CNSHA", "VNSGN", "MSC",   600, 1100, "2026-02-01", "2026-07-12"),
    ("CNSHA", "SGSIN", "COSCO", 800, 1500, "2026-02-15", "2026-07-20"),
    ("CNNGB", "JPYOK", "ONE",   700, 1300, "2026-03-01", "2026-07-25"),
    ("CNSHA", "MYPKG", "YML",   650, 1200, "2026-03-10", "2026-08-20"),
    ("CNSHA", "INMUN", "IAL",   900, 1700, "2026-04-01", "2027-01-15"),
    ("CNSHA", "AEJEA", "HMM",   None,None,"2026-04-15", None),
]
entries = []
for pol, pod, carrier, of20, of40hq, vf, vt in scenarios:
    e = NormalizedRateEntry()
    e.pol = pol; e.pod = pod; e.carrier = carrier
    e.of_20 = of20; e.of_40hq = of40hq
    e.valid_from = vf; e.valid_to = vt
    e._record_id = "rec_test_" + pol + pod
    entries.append(e)

items, _ = build_summary(entries, today)
agg = aggregate(items)
print("=== MOCK TEST ===")
print("buckets expected:")
for e in entries:
    rd = (datetime.datetime.strptime(e.valid_to, '%Y-%m-%d').date() - today).days if e.valid_to else None
    print(" ", e.pol, "->", e.pod, " valid_to=", e.valid_to, " rd=", rd)
print()
print("=== aggregate ===")
for k in agg:
    print(" ", k.ljust(10), agg[k]['count'])
print()
print("=== items detail ===")
for it in items:
    print(" ", it['bucket'].ljust(8), str(it['remaining_days']).rjust(5), it['pol'], '->', it['pod'])
