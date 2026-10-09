"""Display helpers for the Streamlit UI."""

from __future__ import annotations

import re

_UNESCAPED_DOLLAR = re.compile(r"(?<!\\)\$")


def md_safe(text: str) -> str:
    """Escape $ so Streamlit markdown shows "$14.40 and $7.08" as money, not LaTeX."""
    return _UNESCAPED_DOLLAR.sub(r"\\$", text)
