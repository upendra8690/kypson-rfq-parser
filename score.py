"""
STAND-IN scorer - NOT Kypson's score.py.

The brief says score.py is attached, but it was missing from the copy we received.
This file only exists so `python score.py my_tests.jsonl` works locally.
If you have Kypson's original, use it in place of this file.

Scoring here is deliberately strict: every field must match exactly (numbers compared
with ==, so 40000 == 40000.0; strings compared as-is, case-sensitive).
"""
import json
import sys
import time

from parse import parse_rfq

FIELDS = ("category", "quantity", "unit", "gsm", "micron", "location")


def main(path: str) -> None:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    field_ok = {f: 0 for f in FIELDS}
    exact = 0
    failures = []
    start = time.perf_counter()
    for row in rows:
        got = parse_rfq(row["text"])
        exp = row["expected"]
        if set(got) != set(FIELDS):
            raise SystemExit(f"parse_rfq must return exactly {FIELDS}, got {sorted(got)}")
        wrong = [f for f in FIELDS if got.get(f) != exp.get(f)]
        for f in FIELDS:
            field_ok[f] += f not in wrong
        if not wrong:
            exact += 1
        else:
            failures.append((row["text"], {f: (exp.get(f), got.get(f)) for f in wrong}))
    elapsed = time.perf_counter() - start

    n = len(rows)
    print(f"{n} examples in {elapsed:.2f}s")
    for f in FIELDS:
        print(f"  {f:<9} {field_ok[f]:>3}/{n}  {100 * field_ok[f] / n:5.1f}%")
    total = sum(field_ok.values())
    print(f"field accuracy: {total}/{n * len(FIELDS)} = {100 * total / (n * len(FIELDS)):.1f}%")
    print(f"exact match:    {exact}/{n} = {100 * exact / n:.1f}%")
    if failures:
        print("\nmismatches (expected -> got):")
        for text, diff in failures:
            print(f"  {text}")
            for f, (e, g) in diff.items():
                print(f"      {f}: {e!r} -> {g!r}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "my_tests.jsonl")
