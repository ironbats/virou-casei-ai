from hashlib import sha256

from sqlalchemy import text


def configuration_lock(db, scope):
    if db.bind.dialect.name == "postgresql":
        identity = int.from_bytes(sha256(scope.encode()).digest()[:8], "big") & ((1 << 63) - 1)
        db.execute(text("SELECT pg_advisory_xact_lock(:identity)"), {"identity": identity})
