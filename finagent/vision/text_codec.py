"""Text encoding helpers for bank exports."""
from __future__ import annotations


def decode_text_bytes(data: bytes) -> str:
    """Decode common Chinese bank exports without corrupting text/CSV files."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
