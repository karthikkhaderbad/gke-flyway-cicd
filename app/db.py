import os

import psycopg


def _conninfo() -> str:
    return (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '5432')} "
        f"dbname={os.getenv('DB_NAME', 'app')} "
        f"user={os.getenv('DB_USER', 'app')} "
        f"password={os.getenv('DB_PASSWORD', '')} "
        "connect_timeout=3"
    )


def db_version() -> str:
    """Connect to the database and return the server version string.

    Raises if the database is unreachable, which is what the readiness and
    health endpoints rely on.
    """
    with psycopg.connect(_conninfo()) as conn:
        row = conn.execute("SELECT version()").fetchone()
    return row[0]
