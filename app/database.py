import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import SQLITE_DATABASE_URL

SQLITE_BUSY_TIMEOUT_MS = 5_000


@event.listens_for(Engine, "connect")
def configure_sqlite_connection(dbapi_connection: object, _connection_record: object) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return

    cursor = dbapi_connection.cursor()
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


engine = create_engine(
    SQLITE_DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_size=5,
    max_overflow=5,
    pool_timeout=5,
)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
