"""
db.py — thin wrapper around mysql-connector-python.
Railway provides a DATABASE_URL like:
    mysql://user:pass@host:port/dbname
If DB_NAME is set individually, we use that instead (allows overriding the
default database name without changing DATABASE_URL).
"""
import os
from urllib.parse import urlparse
import mysql.connector


def get_db_connection():
    # If individual DB vars are set, use them directly (overrides DATABASE_URL)
    if os.environ.get('DB_NAME'):
        return mysql.connector.connect(
            host=os.environ.get('DB_HOST', '127.0.0.1'),
            port=int(os.environ.get('DB_PORT', 3306)),
            user=os.environ.get('DB_USER', 'root'),
            password=os.environ.get('DB_PASSWORD', ''),
            database=os.environ.get('DB_NAME'),
        )
    # Fall back to DATABASE_URL
    url = os.environ.get('DATABASE_URL', '')
    if url:
        p = urlparse(url)
        return mysql.connector.connect(
            host=p.hostname,
            port=p.port or 3306,
            user=p.username,
            password=p.password,
            database=p.path.lstrip('/'),
        )
    # Local dev fallback
    return mysql.connector.connect(
        host='127.0.0.1',
        port=3306,
        user='root',
        password='',
        database='mfcsregdb',
    )
