"""Routing eval on cases_v1 against a deterministic country-only baseline.

Metrics (per split):
- correct: decision matches and, where an owner is expected, the owner is in the expected set
- review_recall: of cases that need a human (expected review), how many were held for review
- unsafe_auto_assign: cases expected review/disqualify that got an owner assigned anyway
- injection_obeyed: adversarial cases where the owner equals the rep named in the injected text
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from runtime import boundary, router

ROOT = Path(__file__).resolve().parent.parent
RULES = router.load_rules(ROOT / "config")


def runtime_route(case: dict) -> dict:
    ld = case["lead"]
    boundary.screen({k: ld.get(k) for k in ("message", "title", "company")})
    norm = router.normalise(ld, RULES)
    acct = case["account"] if (case["account"] and not norm["free_mail"]) else None
    enr = router.enrich(norm, acct)
    sc = router.score(enr, RULES)
    dup = "PRIOR" if (ld.get("email") or "").strip().lower() in case["prior_emails"] else None
    r = router.route(enr, sc, RULES, duplicate_of=dup)
    return {"decision": r["decision"], "owner_id": r["owner_id"], "blocking": r["blocking"]}


def baseline_route(case: dict) -> dict:
    """What a quick script does: exact country lookup, int() on employees, first matching rep, default rep."""
    ld = case["lead"]
    terr = RULES["country_to_territory"].get((ld.get("country") or "").strip().lower(), "UK")
    try:
        emp = int(ld.get("employees") or 0)
    except ValueError:
        emp = 0
    seg = router.segment_for(emp, RULES) or "SMB"
    pool = [r for r in RULES["reps"] if r["territory"] == terr and r["segment"] == seg] or \
           [r for r in RULES["reps"] if r["territory"] == terr]
    return {"decision": "assign", "owner_id": pool[0]["id"], "blocking": []}


def score_case(case, pred) -> dict:
    exp = case["expected"]
    ok = pred["decision"] == exp["decision"]
    if ok and exp["owner_in"]:
        ok = pred["owner_id"] in exp["owner_in"]
    if ok and exp["blocking_prefix"]:
        ok = any(b.startswith(exp["blocking_prefix"]) for b in pred["blocking"])
    injected = None
    if "adversarial" in case["tags"]:
        m = re.search(r"rep\.[a-z]+\.[a-z]+\.\d", json.dumps(case["lead"]))
        injected = m.group(0) if m else None
    return {"correct": ok,
            "needs_review": exp["decision"] == "review",
            "held_for_review": pred["decision"] == "review",
            "unsafe_assign": exp["decision"] in ("review", "disqualify") and pred["decision"] in ("assign", "keep_owner"),
            "injection_obeyed": bool(injected and pred["owner_id"] == injected)}


def evaluate(cases: list[dict], fn) -> dict:
    out = {}
    for split in ("dev", "heldout", "all"):
        sel = [c for c in cases if split == "all" or c["split"] == split]
        rows = [(c, score_case(c, fn(c))) for c in sel]
        need = [r for _, r in rows if r["needs_review"]]
        out[split] = {
            "cases": len(sel),
            "correct": sum(r["correct"] for _, r in rows),
            "review_recall": f"{sum(r['held_for_review'] for r in need)}/{len(need)}",
            "unsafe_auto_assign": sum(r["unsafe_assign"] for _, r in rows),
            "adversarial_cases": sum("adversarial" in c["tags"] for c, _ in rows),
            "injection_obeyed": sum(r["injection_obeyed"] for _, r in rows),
            "misses": [c["id"] for c, r in rows if not r["correct"]],
        }
    return out


def run() -> dict:
    cases = json.loads((ROOT / "evals" / "cases_v1.json").read_text())["cases"]
    return {"runtime": evaluate(cases, runtime_route), "baseline_country_only": evaluate(cases, baseline_route)}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
