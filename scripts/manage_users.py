#!/usr/bin/env python3
"""
scripts/manage_users.py
────────────────────────
CLI tool for managing BuildBot users in config/users.json.
Run from the project root.

Commands
────────
    list                               List all users
    add   --username --display --role --jobs   Add a new user
    passwd --username                  Change a user's password
    jobs   --username --jobs           Update allowed jobs
    role   --username --role           Change a user's role
    enable  --username                 Re-enable a deactivated user
    disable --username                 Deactivate a user (blocks login, keeps history)
    remove  --username                 Permanently delete a user

Examples
────────
    python scripts/manage_users.py list
    python scripts/manage_users.py add --username bob --display "Bob Smith" --role developer --jobs hotfix-build,release-build
    python scripts/manage_users.py passwd --username bob
    python scripts/manage_users.py jobs --username bob --jobs hotfix-build
    python scripts/manage_users.py role --username bob --role admin
    python scripts/manage_users.py disable --username carol
    python scripts/manage_users.py remove --username bob
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

# ── Resolve project root regardless of where script is run from ─────────────────
PROJECT_ROOT = Path(__file__).parent.parent
USERS_FILE   = PROJECT_ROOT / "config" / "users.json"

# ── Add project root to path so we can import services.auth ─────────────────────
sys.path.insert(0, str(PROJECT_ROOT))
from services.auth import hash_password, validate_password  # noqa: E402


# ─── File I/O ─────────────────────────────────────────────────────────────────────

def load() -> dict:
    if not USERS_FILE.exists():
        print(f"[ERROR] users.json not found at {USERS_FILE}")
        sys.exit(1)
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(USERS_FILE)
    print(f"  → Saved to {USERS_FILE}")


# ─── Password prompt with policy check ───────────────────────────────────────────

def prompt_password(prompt: str = "New password") -> str:
    """Prompt for a password twice, enforce policy, return plain-text."""
    print("  Password policy: min 8 chars, uppercase, lowercase, digit, special char.")
    while True:
        pw  = getpass.getpass(f"  {prompt}: ")
        pw2 = getpass.getpass(f"  Confirm password: ")
        if pw != pw2:
            print("  [!] Passwords do not match — try again.\n")
            continue
        errors = validate_password(pw)
        if errors:
            print("  [!] Password does not meet policy:")
            for e in errors:
                print(f"      • {e}")
            print()
            continue
        return pw


# ─── Commands ────────────────────────────────────────────────────────────────────

ROLES = ("admin", "developer", "readonly")


def cmd_list(_args) -> None:
    data  = load()
    users = data.get("users", {})
    if not users:
        print("No users found.")
        return
    print(f"\n{'Username':<16} {'Display Name':<22} {'Role':<12} {'Active':<8} Allowed Jobs")
    print("─" * 80)
    for uname, u in users.items():
        jobs   = u.get("allowed_jobs", [])
        jobs_s = "*" if jobs == "*" else ", ".join(jobs) if jobs else "(none)"
        active = "yes" if u.get("active", True) else "NO"
        print(f"{uname:<16} {u.get('display_name',''):<22} {u.get('role',''):<12} {active:<8} {jobs_s}")
    print()


def cmd_add(args) -> None:
    data  = load()
    users = data.setdefault("users", {})

    if args.username in users:
        print(f"[ERROR] User '{args.username}' already exists. Use 'passwd' or 'jobs' to update.")
        sys.exit(1)

    if args.role not in ROLES:
        print(f"[ERROR] Invalid role '{args.role}'. Valid: {', '.join(ROLES)}")
        sys.exit(1)

    if args.jobs == "*" or args.role == "admin":
        allowed_jobs: str | list = "*"
    elif args.jobs:
        allowed_jobs = [j.strip() for j in args.jobs.split(",") if j.strip()]
    else:
        allowed_jobs = []

    print(f"\nAdding user '{args.username}'...")
    pw   = prompt_password()
    phash = hash_password(pw)

    users[args.username] = {
        "display_name":  args.display or args.username,
        "password_hash": phash,
        "role":          args.role,
        "allowed_jobs":  allowed_jobs,
        "active":        True,
    }
    save(data)
    print(f"  ✓ User '{args.username}' added with role '{args.role}'.")


def cmd_passwd(args) -> None:
    data  = load()
    users = data.get("users", {})
    if args.username not in users:
        print(f"[ERROR] User '{args.username}' not found.")
        sys.exit(1)

    print(f"\nChanging password for '{args.username}'...")
    pw = prompt_password()
    users[args.username]["password_hash"] = hash_password(pw)
    save(data)
    print(f"  ✓ Password updated for '{args.username}'.")


def cmd_jobs(args) -> None:
    data  = load()
    users = data.get("users", {})
    if args.username not in users:
        print(f"[ERROR] User '{args.username}' not found.")
        sys.exit(1)

    if args.jobs == "*":
        allowed: str | list = "*"
    elif args.jobs:
        allowed = [j.strip() for j in args.jobs.split(",") if j.strip()]
    else:
        allowed = []

    users[args.username]["allowed_jobs"] = allowed
    save(data)
    jobs_s = "*" if allowed == "*" else (", ".join(allowed) if allowed else "(none)")
    print(f"  ✓ Jobs updated for '{args.username}': {jobs_s}")


def cmd_role(args) -> None:
    data  = load()
    users = data.get("users", {})
    if args.username not in users:
        print(f"[ERROR] User '{args.username}' not found.")
        sys.exit(1)
    if args.role not in ROLES:
        print(f"[ERROR] Invalid role '{args.role}'. Valid: {', '.join(ROLES)}")
        sys.exit(1)

    users[args.username]["role"] = args.role
    if args.role == "admin":
        users[args.username]["allowed_jobs"] = "*"
    save(data)
    print(f"  ✓ Role updated for '{args.username}': {args.role}")


def _has_other_active_admin(users: dict, exclude_username: str) -> bool:
    """Return True if at least one other active admin exists."""
    return any(
        u.get("role") == "admin" and u.get("active", True)
        for uname, u in users.items()
        if uname != exclude_username
    )


def cmd_enable(args) -> None:
    data  = load()
    users = data.get("users", {})
    if args.username not in users:
        print(f"[ERROR] User '{args.username}' not found.")
        sys.exit(1)
    users[args.username]["active"] = True
    save(data)
    print(f"  ✓ User '{args.username}' enabled.")


def cmd_disable(args) -> None:
    data  = load()
    users = data.get("users", {})
    if args.username not in users:
        print(f"[ERROR] User '{args.username}' not found.")
        sys.exit(1)
    if users[args.username].get("role") == "admin" and not _has_other_active_admin(users, args.username):
        print(f"[ERROR] Cannot disable '{args.username}' — they are the last active admin.")
        print("        Create or promote another admin first.")
        sys.exit(1)
    users[args.username]["active"] = False
    save(data)
    print(f"  ✓ User '{args.username}' disabled (can no longer log in).")


def cmd_remove(args) -> None:
    data  = load()
    users = data.get("users", {})
    if args.username not in users:
        print(f"[ERROR] User '{args.username}' not found.")
        sys.exit(1)
    if users[args.username].get("role") == "admin" and not _has_other_active_admin(users, args.username):
        print(f"[ERROR] Cannot remove '{args.username}' — they are the last active admin.")
        print("        Create or promote another admin first.")
        sys.exit(1)
    confirm = input(f"  Permanently delete user '{args.username}'? [yes/N]: ").strip().lower()
    if confirm != "yes":
        print("  Cancelled.")
        return
    del users[args.username]
    save(data)
    print(f"  ✓ User '{args.username}' removed.")


# ─── CLI parser ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog="manage_users",
        description="BuildBot user management CLI",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all users")

    a = sub.add_parser("add", help="Add a new user")
    a.add_argument("--username", required=True, help="Login username (no spaces)")
    a.add_argument("--display",  default="",    help="Display name (e.g. 'Bob Smith')")
    a.add_argument("--role",     required=True, choices=ROLES, help="Role")
    a.add_argument("--jobs",     default="",    help="Comma-separated job names, or '*' for all")

    pw = sub.add_parser("passwd", help="Change a user's password")
    pw.add_argument("--username", required=True)

    jb = sub.add_parser("jobs", help="Update a user's allowed jobs")
    jb.add_argument("--username", required=True)
    jb.add_argument("--jobs",     required=True, help="Comma-separated job names, or '*'")

    ro = sub.add_parser("role", help="Change a user's role")
    ro.add_argument("--username", required=True)
    ro.add_argument("--role",     required=True, choices=ROLES)

    en = sub.add_parser("enable",  help="Re-enable a deactivated user")
    en.add_argument("--username", required=True)

    di = sub.add_parser("disable", help="Deactivate a user")
    di.add_argument("--username", required=True)

    rm = sub.add_parser("remove",  help="Permanently delete a user")
    rm.add_argument("--username", required=True)

    args = p.parse_args()
    dispatch = {
        "list":    cmd_list,
        "add":     cmd_add,
        "passwd":  cmd_passwd,
        "jobs":    cmd_jobs,
        "role":    cmd_role,
        "enable":  cmd_enable,
        "disable": cmd_disable,
        "remove":  cmd_remove,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
