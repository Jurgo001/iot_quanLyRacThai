"""Create P3 dashboard accounts locally (no network, no default password).

Run from the repository root:
    python backend_server/p3_setup.py init-admin
    python backend_server/p3_setup.py add-user --username driver01 --role operator
    python backend_server/p3_setup.py disable-user --username driver01
"""

import argparse
import getpass
import os
import sqlite3
from contextlib import closing

from p3_auth import ROLES, add_user, bootstrap_signing_secret, init_auth_tables

DB_FILE = os.getenv("IOT_DB_PATH", os.path.join(os.path.dirname(__file__), "smart_waste.db"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline setup and management for P3 JWT users")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init-admin")
    create = sub.add_parser("add-user")
    create.add_argument("--username", required=True)
    create.add_argument("--role", choices=sorted(ROLES), required=True)
    sub.add_parser("init-secret")
    disable = sub.add_parser("disable-user")
    disable.add_argument("--username", required=True)
    revoke = sub.add_parser("revoke-tokens")
    revoke.add_argument("--username", required=True)
    args = parser.parse_args()
    init_auth_tables(DB_FILE)
    if args.action == "init-secret":
        bootstrap_signing_secret()
        print("JWT signing secret is ready. Do not commit p3_local_auth.json.")
    elif args.action in ("init-admin", "add-user"):
        username = input("Username (3-32 letters, numbers, '.', '_', '-'): ").strip() if args.action == "init-admin" else args.username
        role = "admin" if args.action == "init-admin" else args.role
        password = getpass.getpass("Password (at least 12 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            parser.error("Passwords do not match")
        try:
            # A new admin account and its secret should be initialized together.
            # Existing secret is never overwritten.
            bootstrap_signing_secret()
            add_user(DB_FILE, username, password, role)
        except (ValueError, sqlite3.IntegrityError) as exc:
            parser.error(str(exc))
        print(f"Created {role} user: {username}. Keep p3_local_auth.json PRIVATE.")
    else:
        with closing(sqlite3.connect(DB_FILE)) as db:
            if args.action == "disable-user":
                changed = db.execute("UPDATE dashboard_users SET disabled=1, token_version=token_version+1 WHERE username=?", (args.username,)).rowcount
            else:
                changed = db.execute("UPDATE dashboard_users SET token_version=token_version+1 WHERE username=?", (args.username,)).rowcount
            db.commit()
        if changed == 0:
            parser.error("User not found")
        print(f"User updated; existing JWT sessions revoked: {args.username}")


if __name__ == "__main__":
    main()
