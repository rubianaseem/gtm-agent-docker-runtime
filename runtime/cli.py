"""Command line entry point.

  python -m runtime run [--once]          start the worker (default container command)
  python -m runtime queue                 print the review queue
  python -m runtime show <lead_id>        print one plan with its hash
  python -m runtime approve <lead_id> --hash <64-hex> --approver <name>
  python -m runtime verify-audit          recompute the audit hash chain
  python -m runtime status                checkpoint counts by status
  python -m runtime mock                  start the mock CRM + Slack service
  python -m runtime healthcheck           container health probe
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import approval, healthcheck, jsonlog, mock_services
from .config import Settings
from .state import StateStore, verify_audit


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m runtime")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true", help="exit when no more progress is possible")
    sub.add_parser("queue")
    sh = sub.add_parser("show")
    sh.add_argument("lead_id")
    a = sub.add_parser("approve", help="record a human approval bound to the plan hash")
    a.add_argument("lead_id")
    a.add_argument("--hash", required=True, dest="plan_hash")
    a.add_argument("--approver", required=True)
    sub.add_parser("verify-audit")
    sub.add_parser("status")
    sub.add_parser("mock")
    sub.add_parser("healthcheck")
    args, rest = ap.parse_known_args(argv)
    s = Settings.from_env()

    if args.cmd == "run":
        from .config import SecretError
        from .worker import Worker
        try:
            worker = Worker(s)
        except SecretError as e:
            jsonlog.log("error", "startup_refused_missing_secret", detail=str(e))
            return 3
        return worker.run(once=args.once)
    if args.cmd == "mock":
        mock_services.main(rest)
        return 0
    if args.cmd == "healthcheck":
        return healthcheck.main()
    if args.cmd == "verify-audit":
        ok, n, msg = verify_audit(s.state_dir / "audit.jsonl")
        print(msg)
        return 0 if ok else 1
    if args.cmd == "approve":
        try:
            rec = approval.write_approval(s.state_dir, s.config_dir, args.lead_id, args.plan_hash, args.approver,
                                          s.approval_ttl_h)
        except approval.ApprovalError as e:
            print(f"REFUSED: {e}")
            return 1
        store = StateStore(s.state_dir)
        store.audit("plan_approved", f"human:{rec['approver']}", args.lead_id, plan_hash=rec["plan_hash"],
                    expires_at_epoch=rec["expires_at_epoch"])
        store.close()
        print(f"APPROVED {args.lead_id} by {rec['approver']} (hash {rec['plan_hash'][:12]}..., expires in "
              f"{s.approval_ttl_h:g}h). The worker executes it against the mock services on its next cycle.")
        return 0
    store = StateStore(s.state_dir)
    if args.cmd == "status":
        print(json.dumps(dict(Counter(l["status"] for l in store.all_leads())), indent=2, sort_keys=True))
    elif args.cmd == "queue":
        for l in store.all_leads():
            p = l["data"].get("plan")
            if not p:
                continue
            print(f"{l['lead_id']}  {l['status']:<17} {p['decision']:<10} {p['rule']:<22} "
                  f"{(p['owner_id'] or '-'):<15} blocking={','.join(p['blocking']) or '-'}  "
                  f"warnings={','.join(p['warnings']) or '-'}")
    elif args.cmd == "show":
        l = store.get_lead(args.lead_id)
        print(json.dumps(l["data"].get("plan") if l else {"error": "unknown lead"}, indent=2, sort_keys=True))
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
