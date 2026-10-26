#!/usr/bin/env python3
"""Entry point for the web account checker.

Usage: web_account_checker.py <checker> <user> <pass> [proxy] [budget_sec]

Always prints exactly one JSON object on stdout so the server can parse the
result even when the check failed.
"""
import sys
import json
import os

sys.path.insert(0, os.path.dirname(__file__))
from gates.account_checkers import run_check, SUPPORTED_CHECKERS


def main():
    if len(sys.argv) < 4:
        print(json.dumps({"status": "error", "message": "Usage: web_account_checker.py <checker> <user> <pass> [proxy] [budget_sec]"}))
        return

    checker_type = sys.argv[1].lower()
    user = sys.argv[2]
    password = sys.argv[3]
    proxy = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    budget_sec = int(sys.argv[5]) if len(sys.argv) > 5 else None

    if checker_type not in SUPPORTED_CHECKERS:
        print(json.dumps({"status": "error", "message": f"Unknown checker. Supported: {', '.join(SUPPORTED_CHECKERS.keys())}"}))
        return

    try:
        result = run_check(checker_type, user, password, proxy, budget_sec=budget_sec)
    except Exception as e:
        result = {"status": "error", "message": f"Check failed: {type(e).__name__}: {str(e)[:150]}"}

    if not isinstance(result, dict):
        result = {"status": "error", "message": "Checker returned an invalid result"}

    result.setdefault("status", "error")
    result.setdefault("capture", {})
    print(json.dumps(result))


if __name__ == "__main__":
    main()
