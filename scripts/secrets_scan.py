"""Secrets and personal-data scan: run before every commit and before any push.

It reads the real values from .env (never printed) and looks for them in everything git tracks or
would track, and, with --history, in every commit ever made. It also looks for well-known key shapes.
Only the file, line and the NAME of the matching setting are printed.

    uv run python scripts/secrets_scan.py            # working tree + untracked files
    uv run python scripts/secrets_scan.py --history  # also every past commit (needed before a push)
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENSITIVE_KEY = re.compile(r"(SCAN_EXTRA_TERMS|TOKEN|SECRET|PASSWORD|KEY|SID|_ID|ALLOWED_TO|_FROM|EMAIL|CREDENTIALS)", re.I)
SAFE_KEYS = {"POSTGRES_PORT", "LOG_LEVEL", "DRY_RUN", "LANGFUSE_HOST", "GOOGLE_CREDENTIALS_FILE"}
SHAPES = {
    "anthropic key": r"sk-ant-[A-Za-z0-9_\-]{20,}",
    "twilio sid": r"\bAC[0-9a-f]{32}\b",
    "telegram token": r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}",
    "hubspot token": r"\bpat-[a-z0-9]+-[0-9a-f\-]{30,}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "langfuse key": r"\b(sk|pk)-lf-[0-9a-f\-]{20,}",
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


def env_values() -> dict[str, str]:
    out: dict[str, str] = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if SENSITIVE_KEY.search(k) and k not in SAFE_KEYS and len(v) >= 6 and v.lower() not in {"true", "false", "changeme"}:
                    out[k] = v
    # Personal terms (your email, your own phone number...) live in .env, never in the repo:
    # SCAN_EXTRA_TERMS=term1,term2
    extra = out.pop("SCAN_EXTRA_TERMS", "")
    for i, term in enumerate(t.strip() for t in extra.split(",") if t.strip()):
        out[f"PERSONAL_{i}"] = term
    return out


def main() -> int:
    history = "--history" in sys.argv
    problems = 0
    for name, value in env_values().items():
        hits = git("grep", "-n", "-F", "--untracked", "-I", "-e", value, "--", ":!.env").splitlines()
        for h in hits:
            print(f"WORKING TREE  {name}: {h.split(':', 2)[0]}:{h.split(':', 2)[1]}")
            problems += 1
        if history:
            for c in git("log", "--all", "--format=%h", "-S", value).split():
                print(f"HISTORY       {name}: commit {c}")
                problems += 1
    for label, pat in SHAPES.items():
        for h in git("grep", "-n", "-E", "--untracked", "-I", "-e", pat, "--", ":!.env", ":!uv.lock").splitlines():
            print(f"SHAPE         {label}: {h.split(':', 2)[0]}:{h.split(':', 2)[1]}")
            problems += 1
        if history:
            for c in git("log", "--all", "--format=%h", "-G", pat).split():
                print(f"HISTORY SHAPE {label}: commit {c}")
                problems += 1
    if ".env" in git("ls-files").split():
        print(".env IS TRACKED BY GIT")
        problems += 1
    print("scan finished:", "CLEAN" if problems == 0 else f"{problems} finding(s) - do not commit/push")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
