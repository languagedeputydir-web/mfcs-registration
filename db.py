"""
db.py — thin wrapper around mysql-connector-python.

Railway provides a DATABASE_URL like:
    mysql://user:pass@host:port/dbname

We parse it and return a plain connection.
"""
import os
from urllib.parse import urlparse
import mysql.connector


def get_db_connection():
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
    # Fallback to individual env vars (handy for local dev)
    return mysql.connector.connect(
        host=os.environ.get('DB_HOST', '127.0.0.1'),
        port=int(os.environ.get('DB_PORT', 3306)),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=os.environ.get('DB_NAME', 'legacyregdb2'),
    )
