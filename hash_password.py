"""
hash_password.py
================
Utility scripts for password management.

Usage:
    # Generate a bcrypt hash for any password (e.g. to create the first admin)
    python hash_password.py hash "mypassword"

    # Migrate ALL family plaintext passwords to bcrypt in-place
    python hash_password.py migrate_families

The migrate_families command:
  - Reads every row in the family table where password_hash is empty
  - bcrypt-hashes the plaintext password column
  - Writes the hash into the password_hash column
  - Does NOT touch the original password column (safe / reversible)
"""

import sys
import bcrypt
from db import get_db_connection


def make_hash(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()


def cmd_hash(plain: str):
    print(make_hash(plain))


def cmd_migrate_families():
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)

    # Find families that haven't been hashed yet
    cur.execute(
        "SELECT id, password FROM family "
        "WHERE (password_hash IS NULL OR password_hash = '') "
        "AND password != '' AND password != '?'"
    )
    rows = cur.fetchall()
    print(f"Found {len(rows)} family rows to hash …")

    updated = 0
    for row in rows:
        hashed = make_hash(row['password'])
        cur.execute(
            "UPDATE family SET password_hash = %s WHERE id = %s",
            (hashed, row['id'])
        )
        updated += 1
        if updated % 50 == 0:
            conn.commit()
            print(f"  {updated} done …")

    conn.commit()
    conn.close()
    print(f"Done. {updated} passwords hashed.")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == 'hash':
        if len(sys.argv) < 3:
            print("Usage: python hash_password.py hash <password>")
            sys.exit(1)
        cmd_hash(sys.argv[2])

    elif cmd == 'migrate_families':
        cmd_migrate_families()

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: hash, migrate_families")
        sys.exit(1)
