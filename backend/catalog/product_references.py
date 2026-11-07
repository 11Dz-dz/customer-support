"""Positions always refer to a server-verified display order."""
import re


def explicit_position(text):
    from backend.tasks.constraint_updates import number
    positions={int(number(m[1])) for m in re.finditer(
        r'第\s*(\d+|[一二两三四五六七八九十百]+)\s*(?:个|款|件|条|项)',text)}
    return next(iter(positions)) if len(positions)==1 else None
