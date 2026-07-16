"""
generate_password_hash.py — Run this locally to create a secrets.toml
entry for a new user, without ever writing their real password into a
file or committing it anywhere.

Usage:
    python3 modules/generate_password_hash.py

It will prompt for a password (hidden, not echoed to the terminal), then
print the exact line to paste into `.streamlit/secrets.toml` (locally) or
the "Secrets" box in Streamlit Cloud's app settings (when deployed) under
a `[auth.users]` section, e.g.:

    [auth]
    [auth.users]
    alice = "3f2a9c1e...$9b7d4e2a..."

The printed value is a salt and a SHA-256 hash separated by "$" — never
the plaintext password. This matches what modules/auth.py expects.
"""

from __future__ import annotations

import getpass
import hashlib
import secrets
import sys


def generate_entry(username: str, password: str) -> str:
    salt = secrets.token_hex(16)
    password_hash = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f'{username} = "{salt}${password_hash}"'


def main() -> None:
    username = input("Username (no spaces, e.g. firstname.lastname): ").strip()
    if not username or " " in username:
        print("Username can't be blank or contain spaces.", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Password (won't be shown): ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords didn't match — try again.", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("Use at least 8 characters for a real deployment.", file=sys.stderr)

    print()
    print("Add this line under [auth.users] in your secrets:")
    print()
    print(generate_entry(username, password))
    print()
    print(
        "If [auth] / [auth.users] sections don't exist yet, the full block "
        "looks like:"
    )
    print()
    print("[auth]")
    print("[auth.users]")
    print(generate_entry(username, password))


if __name__ == "__main__":
    main()
