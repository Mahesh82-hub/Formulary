import os
from pathlib import Path

ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"

if not ROOT_ENV.exists():
    os.environ.setdefault("POSTGRES_DB", "dr_insilico_test")
    os.environ.setdefault("POSTGRES_USER", "dr_insilico_test")
    os.environ.setdefault("POSTGRES_PASSWORD", "test-only-password")

os.environ.setdefault("AUTH_OTP_PEPPER", "test-only-pepper-that-is-at-least-32-characters")
os.environ.setdefault("EMAIL_DELIVERY_MODE", "console")
