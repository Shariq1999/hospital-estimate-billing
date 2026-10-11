#!/usr/bin/env python3
"""
reset_admin.py - Emergency Command-Line Administrator Password Reset Tool.

Usage:
    python reset_admin.py [username]

Description:
    Prompts for a new password using getpass (min 8 characters, confirmed twice),
    requires explicit typed confirmation ('RESET'), and updates the administrator password
    in the database using PBKDF2 with 600,000 iterations.
    Never prints or logs the password or hash.
    Run this tool when the Streamlit application is stopped.
"""

from __future__ import annotations

import getpass
import sys
from typing import Optional

import database


def reset_password(username: str = "admin", db_path: str = "hospital.db") -> bool:
    """Prompt for confirmation and new password, then update administrator credentials."""
    print("=== Hospital Billing: Emergency Admin Password Reset ===")
    print(f"Target account: {username}")
    print(f"Target database: {db_path}\n")

    # Ask for typed confirmation before changing anything
    confirm_text = input("Type 'RESET' to confirm you wish to overwrite this password: ").strip()
    if confirm_text != "RESET":
        print("Reset aborted. Confirmation code did not match 'RESET'.")
        return False

    # Prompt for new password with getpass (min 8 characters, ask twice)
    pwd1 = getpass.getpass("Enter new administrator password (min 8 characters): ")
    if len(pwd1) < 8:
        print("Error: Password must be at least 8 characters long.")
        return False

    pwd2 = getpass.getpass("Confirm new administrator password: ")
    if pwd1 != pwd2:
        print("Error: Passwords do not match.")
        return False

    conn = database.get_connection(db_path)
    try:
        database.init_db(conn)
        database.set_admin_password(conn, username, pwd1)
        print("\nSuccess: Administrator password has been updated securely.")
        return True
    finally:
        conn.close()


def main() -> None:
    username = sys.argv[1] if len(sys.argv) > 1 else "admin"
    success = reset_password(username=username)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
