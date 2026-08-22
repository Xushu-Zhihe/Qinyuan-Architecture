import hashlib
import unicodedata
from datetime import datetime, timezone
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    return " ".join(normalized.strip().split())


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest()


def raw_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
