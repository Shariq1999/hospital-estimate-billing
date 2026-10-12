#!/usr/bin/env python3
"""
reset_admin.py - Emergency Command-Line Administrator Password Reset Tool.

Usage:
    python reset_admin.py [username]

Description:
    Resolves hospital.db relative to this script's directory.
    Aborts if the database file does not exist.
    Lists existing usernames in admin_auth and requires choosing one.
    Never creates a new account.
    Prompts for a new password using getpass (min 8 characters, confirmed twice),
    requires explicit typed confirmation ('RESET'), and updates the administrator password
    in the database using PBKDF2 with 600,000 iterations.
    Never prints or logs the password or hash.
    Run this tool when the Streamlit application is stopped.
"""

from __future__ import annotations

import getpass
import os
import sys
from datetime import datetime

import database

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(SCRIPT_DIR, "hospital.db")


def reset_password(username: str | None = None, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Prompt for confirmation and new password, then update administrator credentials."""
    # 1. Resolve database path and abort if file does not exist
    if not os.path.exists(db_path):
        print(f"Error: Database file '{db_path}' does not exist.")
        return False

    print("=== Hospital Billing: Emergency Admin Password Reset ===")
    print(f"Target database: {db_path}\n")

    conn = database.get_connection(db_path)
    try:
        # 2. List existing usernames and require choosing one; never create a new account
        cursor = conn.cursor()
        rows = cursor.execute("SELECT username FROM admin_auth ORDER BY username ASC").fetchall()
        existing_users = [r["username"] for r in rows]

        if not existing_users:
            print("Error: No administrator accounts found in the database.")
            print("Please run the web application first to complete initial onboarding.")
            return False

        print("Existing administrator accounts:")
        for u in existing_users:
            print(f"  • {u}")
        print()

        target_user = username
        if target_user is not None and target_user not in existing_users:
            print(f"Error: User '{target_user}' does not exist in admin_auth. Aborting (will not create new account).")
            return False

        if not target_user:
            chosen = input(f"Enter username to reset [{existing_users[0]}]: ").strip()
            target_user = chosen if chosen else existing_users[0]

        if target_user not in existing_users:
            print(f"Error: User '{target_user}' does not exist in admin_auth. Aborting (will not create new account).")
            return False

        # 3. Ask for typed confirmation before changing anything
        confirm_text = input(f"Type 'RESET' to confirm you wish to overwrite password for '{target_user}': ").strip()
        if confirm_text != "RESET":
            print("Reset aborted. Confirmation code did not match 'RESET'.")
            return False

        # 4. Prompt for new password with getpass (min 8 characters, ask twice)
        pwd1 = getpass.getpass("Enter new administrator password (min 8 characters): ")
        if len(pwd1) < 8:
            print("Error: Password must be at least 8 characters long.")
            return False

        pwd2 = getpass.getpass("Confirm new administrator password: ")
        if pwd1 != pwd2:
            print("Error: Passwords do not match.")
            return False

        # 5. Update existing account only
        pwd_hash, salt = database.hash_password(pwd1)
        now = datetime.now().isoformat()
        with conn:
            conn.execute(
                "UPDATE admin_auth SET password_hash = ?, salt = ?, updated_at = ? WHERE username = ?",
                (pwd_hash, salt, now, target_user),
            )
        print(f"\nSuccess: Password for '{target_user}' has been updated securely.")
        return True
    finally:
        conn.close()


def main() -> None:
    target_user = sys.argv[1] if len(sys.argv) > 1 else None
    success = reset_password(username=target_user)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
