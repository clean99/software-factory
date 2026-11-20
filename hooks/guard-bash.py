#!/usr/bin/env python3
"""PreToolUse guard: writes to other people's GitHub repos go through bin/factory only.

A guardrail against shortcuts, not a security boundary: the factory commands carry the gates,
approvals and idempotency checks that a raw `gh pr create` skips.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WRITE = re.compile(r"\b(gh\s+(pr\s+(create|comment|review|merge|edit|close|reopen|ready)|issue\s+(create|comment|edit|close)|repo\s+(fork|create)|api\s+[^|;&]*-X\s*(POST|PATCH|PUT|DELETE))|git\s+(-C\s+\S+\s+)?push)\b")


def main():
    reason = "Writes to GitHub go through bin/factory (submit / propose), which enforce the gate, the user's approval and idempotency. Use those commands."
    try:
        sys.path.insert(0, str(ROOT))
        from factory.core.stopcheck import deadline

        with deadline(10):
            data = json.load(sys.stdin)
            if not isinstance(data, dict) or not isinstance(data.get("tool_input"), dict):
                raise ValueError("invalid Bash hook payload")
            cmd = data["tool_input"].get("command")
            if not isinstance(cmd, str):
                raise ValueError("missing Bash command")
            if not WRITE.search(cmd):
                return 0
            stripped = cmd.strip()
            if stripped.startswith(("bin/factory", "./bin/factory", str(ROOT / "bin" / "factory"))):
                return 0
            own_push = re.fullmatch(r"git\s+push(\s+origin)?(\s+main)?", stripped) and pathlib.Path(data.get("cwd", "")).resolve() == ROOT
            if own_push:
                return 0
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        reason = f"Cannot validate Bash hook: {error}"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
