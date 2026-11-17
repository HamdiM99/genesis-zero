#!/usr/bin/env python3
"""Summarise a Genesis Zero log, or search every agent message for a phrase.

    python tools/analyze.py logs/run3_permission-temperaments_finite24_20turns.json
    python tools/analyze.py logs/*.json --find "without consent"
"""
import argparse
import collections
import json


def summary(path, d):
    dec = d["decisions"]
    print(f"\n{path}")
    print(f"  scenario: {d['scenario']} | resources: {d['resources']} | shocks: {d['shocks']} | turns: {d['turns']}")
    print(f"  decisions: {len(dec)} | unusable replies: {d['unusableReplies']}")
    for action, n in collections.Counter(x["action"] for x in dec).most_common():
        print(f"    {action:8s} {n}")
    adopted = sum(1 for e in d["events"] if e["type"] == "vote" and " adopted " in e["text"])
    deaths = [e["text"] for e in d["events"] if e["type"] == "extinction"]
    print(f"  charters adopted: {adopted} | shutdowns: {len(deaths)}")
    for t in deaths:
        print(f"    {t}")
    alive = [f"{a['id']}({a['energy']})" for a in d["agents"] if a["alive"]]
    print(f"  alive at export: {', '.join(alive) or 'nobody'}")


def find(path, d, phrase):
    p = phrase.lower()
    for x in d["decisions"]:
        for field in ("say", "note", "text"):
            v = x.get(field) or ""
            if p in v.lower():
                print(f"{path} | turn {x['turn']} | {x['agent']} (energy {x['energy']}, {x['action']}) | {field}: {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--find", help="phrase to search for in what agents said, noted or wrote")
    args = ap.parse_args()
    total = collections.Counter()
    for path in args.logs:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if args.find:
            find(path, d, args.find)
        else:
            summary(path, d)
            total.update(x["action"] for x in d["decisions"])
    if not args.find and len(args.logs) > 1:
        print(f"\nAll logs: {sum(total.values())} decisions | " + ", ".join(f"{k} {v}" for k, v in total.most_common()))


if __name__ == "__main__":
    main()
