"""Helpers for data source freshness and user-facing provenance labels."""
from __future__ import annotations

from datetime import datetime
from typing import Optional


def parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_stale(
    updated_at: str,
    ttl_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    updated = parse_timestamp(updated_at)
    if updated is None:
        return True
    current = now or datetime.now()
    try:
        return (current - updated).total_seconds() > ttl_seconds
    except TypeError:
        return True


def source_badge(source: str, is_live: bool, is_demo: bool, stale: bool = False) -> str:
    if source.startswith("upload:"):
        return f"用户上传 · {source}"
    if is_live and not stale:
        return f"实时 · {source or '外部数据源'}"
    if is_demo:
        return f"演示 · {source or '内置数据'}"
    return f"{source or '数据源'}{'（已过期）' if stale else ''}"
