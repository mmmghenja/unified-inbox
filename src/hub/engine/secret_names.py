"""Имена файлов-секретов (вынесено из гарда дежурной `hub-wake`, Ф57-2).

Встроенный список: такие имена не отдаются сессиям как скачанные файлы (`hub_download`).
"""

import fnmatch

SECRET_NAME_PATTERNS = (
    ".env*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_*",
    ".netrc",
    "*.token",
    "*credential*",
    "*secret*",
    ".claude",
    ".git",
)


def is_secret_name(name: str) -> bool:
    """Ф22-1 C2/П8: имя похоже на секрет по встроенному списку `SECRET_NAME_PATTERNS`."""
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in SECRET_NAME_PATTERNS)
