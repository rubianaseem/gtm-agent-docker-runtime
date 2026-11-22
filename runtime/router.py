"""Deterministic normalise -> enrich -> score -> route.

Nothing in this module reads free text written by the lead (message, title).
Owner choice is a pure function of structured fields, the account record and
the rules file, so replaying a step after a crash gives the same answer.
Round robin uses a hash of the lead id instead of a counter for that reason:
a counter advanced before a crash would hand the replayed lead to a different rep.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def load_rules(config_dir: Path) -> dict:
    rules = json.loads((Path(config_dir) / "routing_rules.json").read_text())
    rules["reps"] = json.loads((Path(config_dir) / "reps.json").read_text())["reps"]
    return rules


def parse_employees(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(",", "")
    if not s:
        return None
    m = re.fullmatch(r"(?:approx\.?|about|~|c\.?)?\s*(\d+)\s*(k)?\+?", s)
    if m:
        n = int(m.group(1)) * (1000 if m.group(2) else 1)
        return n if n > 0 else None
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", s)
    if m:  # a band like 50-200: take the upper bound, flagged by the caller
        return int(m.group(2))
    return None


def normalise(lead: dict, rules: dict) -> dict:
    email = (lead.get("email") or "").strip().lower()
    domain = email.split("@", 1)[1] if "@" in email else (lead.get("domain") or "").strip().lower()
    country_raw = (lead.get("country") or "").strip()
    territory = rules["country_to_territory"].get(country_raw.lower())
    emp_raw = lead.get("employees")
    employees = parse_employees(emp_raw)
    first, last = (lead.get("first_name") or "").strip(), (lead.get("last_name") or "").strip()
    return {
        "lead_id": lead["lead_id"], "email_domain": domain, "email_valid": bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email)),
        "country_raw": country_raw, "territory": territory, "employees": employees,
        "employees_is_band": bool(emp_raw and "-" in str(emp_raw)),
        "free_mail": domain in rules["free_mail_domains"],
        "disposable": domain in rules["disposable_domains"],
        "test_name": any(tok == first.lower() or tok == last.lower() for tok in rules["test_name_tokens"]),
        "existing_owner": (lead.get("owner_id") or "").strip() or None,
        "consent": (lead.get("consent") or "").strip().lower(),
        "source": (lead.get("source") or "").strip().lower(),
    }


def enrich(norm: dict, account: dict | None) -> dict:
    out = dict(norm)
    out["account"] = None
    if account and not norm["free_mail"]:
        out["account"] = {k: account.get(k) for k in ("account_id", "name", "domain", "employees", "owner_id",
                                                       "named_account", "open_opportunity", "territory")}
        if out["employees"] is None and account.get("employees"):
            out["employees"] = int(account["employees"])
            out["employees_source"] = "account"
        if out["territory"] is None and account.get("territory"):
            out["territory"] = account["territory"]
            out["territory_source"] = "account"
    return out


def segment_for(employees: int | None, rules: dict) -> str | None:
    if employees is None:
        return None
    for seg in rules["segments"]:
        if seg["min"] <= employees <= seg["max"]:
            return seg["name"]
    return None


def score(enriched: dict, rules: dict) -> dict:
    pts, why = 0, []
    seg = segment_for(enriched["employees"], rules)
    if seg == "ENT":
        pts, why = pts + 40, why + ["enterprise size +40"]
    elif seg == "MM":
        pts, why = pts + 25, why + ["mid-market size +25"]
    elif seg == "SMB":
        pts, why = pts + 10, why + ["smb size +10"]
    if enriched["account"]:
        pts, why = pts + 15, why + ["known account +15"]
    if enriched["source"] in ("demo_request", "web_form", "pricing_page"):
        pts, why = pts + 15, why + [f"source {enriched['source']} +15"]
    if enriched["free_mail"]:
        pts, why = pts - 20, why + ["free mail -20"]
    if enriched["disposable"] or enriched["test_name"] or not enriched["email_valid"]:
        pts, why = pts - 60, why + ["spam/test signal -60"]
    return {"score": max(0, min(100, pts)), "segment": seg, "reasons": why}


def _rep(rules, rep_id):
    return next((r for r in rules["reps"] if r["id"] == rep_id), None)


def route(enriched: dict, scored: dict, rules: dict, duplicate_of: str | None = None) -> dict:
    """Returns {'decision', 'owner_id', 'rule', 'blocking': [...], 'warnings': [...]}."""
    blocking, warnings = [], []
    acct = enriched.get("account") or {}
    seg, terr = scored["segment"], enriched["territory"]
    if duplicate_of:
        return {"decision": "review", "owner_id": None, "rule": "R00_DUPLICATE",
                "blocking": [f"duplicate_of:{duplicate_of}"], "warnings": warnings}
    if enriched["disposable"] or enriched["test_name"] or not enriched["email_valid"]:
        return {"decision": "disqualify", "owner_id": None, "rule": "R01_SPAM_OR_TEST",
                "blocking": [], "warnings": ["spam_or_test"]}
    if enriched["free_mail"]:
        warnings.append("free_mail_domain")
    if enriched.get("employees_is_band"):
        warnings.append("employee_band_upper_bound_used")
    if enriched.get("consent") not in ("yes", "true", "opt_in"):
        warnings.append("no_marketing_consent_sales_follow_up_only")
    if acct.get("open_opportunity") and acct.get("owner_id"):
        rep = _rep(rules, acct["owner_id"])
        if rep and rep["active"]:
            return {"decision": "assign", "owner_id": rep["id"], "rule": "R02_OPEN_OPP_OWNER",
                    "blocking": [], "warnings": warnings}
        blocking.append(f"account_owner_inactive:{acct['owner_id']}")
    elif acct.get("named_account") and acct.get("owner_id"):
        rep = _rep(rules, acct["owner_id"])
        if rep and rep["active"]:
            return {"decision": "assign", "owner_id": rep["id"], "rule": "R03_NAMED_ACCOUNT",
                    "blocking": [], "warnings": warnings}
        blocking.append(f"account_owner_inactive:{acct['owner_id']}")
    if enriched["existing_owner"] and not blocking:
        rep = _rep(rules, enriched["existing_owner"])
        if rep and rep["active"]:
            return {"decision": "keep_owner", "owner_id": rep["id"], "rule": "R04_EXISTING_OWNER",
                    "blocking": [], "warnings": warnings}
        warnings.append(f"existing_owner_inactive_or_unknown:{enriched['existing_owner']}")
    if terr is None:
        blocking.append(f"unknown_territory:{enriched['country_raw'] or 'blank'}")
    if seg is None:
        blocking.append("unknown_segment")
    if blocking:
        return {"decision": "review", "owner_id": None, "rule": "R09_NEEDS_HUMAN", "blocking": blocking,
                "warnings": warnings}
    pool = [r for r in rules["reps"] if r["territory"] == terr and r["segment"] == seg and r["active"]]
    if not pool:
        return {"decision": "review", "owner_id": None, "rule": "R09_NEEDS_HUMAN",
                "blocking": [f"no_active_rep:{terr}/{seg}"], "warnings": warnings}
    idx = int(hashlib.sha256(enriched["lead_id"].encode()).hexdigest(), 16) % len(pool)
    return {"decision": "assign", "owner_id": pool[idx]["id"], "rule": f"R05_TERRITORY_{terr}_{seg}",
            "blocking": [], "warnings": warnings}
