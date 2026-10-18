import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
SQLITE_DATABASE_URL = f"sqlite:///{(PROJECT_ROOT / 'signallog.db').as_posix()}"


@dataclass
class Settings:
    app_name: str = "SignalLog"
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    cors_origin: str = os.getenv("CORS_ORIGIN", "http://localhost:3000")


settings = Settings()
