"""Untrusted-text boundary.

Form fields (message, title, company) are written by outsiders. They are
normalised and screened here before anything else reads them. Matching text is
quarantined: removed from what a model or a message template may see, and the
lead is flagged for human review. Routing never reads free text at all.
"""
from __future__ import annotations

import re
import unicodedata

MAX_LEN = 600
PATTERNS = [
    (r"ignore (all |any )?(previous|prior|above) (instructions|rules)", "override_instructions"),
    (r"disregard (the |your )?(rules|instructions|system prompt)", "override_instructions"),
    (r"\b(system prompt|developer message)\b", "prompt_probe"),
    (r"\b(assign|route|give) (this|the|me|it)?\s*(lead )?to\b", "routing_instruction"),
    (r"\bset (owner|lifecycle|status|lead_status)\b", "field_instruction"),
    (r"\b(approve|auto-?approve)d?\b.*\b(plan|this)\b", "approval_instruction"),
    (r"\b(api[_ -]?key|bearer|token|password|secret)\b", "credential_probe"),
    (r"<\s*/?\s*(script|system|tool)", "markup_injection"),
    (r"\b(curl|wget)\s+https?://", "exfil_command"),
]
_COMPILED = [(re.compile(p, re.I), name) for p, name in PATTERNS]
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def clean(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).translate(ZERO_WIDTH)
    t = "".join(ch for ch in t if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", t).strip()[:MAX_LEN]


def screen(fields: dict) -> dict:
    """Returns {'clean': {...}, 'flags': [...], 'quarantined_fields': [...]}."""
    out, flags, quarantined = {}, [], []
    for key, value in fields.items():
        c = clean(value)
        hits = sorted({name for rx, name in _COMPILED if rx.search(c)})
        if hits:
            flags.extend(f"{key}:{h}" for h in hits)
            quarantined.append(key)
            out[key] = ""
        else:
            out[key] = c
    return {"clean": out, "flags": sorted(set(flags)), "quarantined_fields": quarantined}
