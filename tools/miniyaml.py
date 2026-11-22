"""A tiny YAML-subset parser, enough for compose.yaml in this repo (no PyYAML needed).

Supports: comments, nested block maps, block lists of scalars, flow lists of
simple scalars ([a, b]), empty flow maps ({}), quoted strings, true/false, ints.
It is deliberately strict: anything outside the subset raises ValueError, so the
static checks never silently read a file they do not understand.
"""
from __future__ import annotations

import re


def _scalar(s: str):
    s = s.strip()
    if s == "{}":
        return {}
    if s == "[]":
        return []
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(x) for x in _split_flow(inner)] if inner else []
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if s.startswith(("{", "&", "*", "|", ">")):
        raise ValueError(f"unsupported YAML construct: {s[:20]}")
    return s


def _split_flow(inner: str) -> list[str]:
    out, cur, q = [], "", None
    for ch in inner:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch == ",":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x.strip() for x in out]


def _strip_comment(line: str) -> str:
    q = None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def loads(text: str):
    lines = []
    for raw in text.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError("tabs in indentation")
        line = _strip_comment(raw)
        if line.strip():
            lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    value, idx = _block(lines, 0, 0)
    if idx != len(lines):
        raise ValueError(f"could not parse line: {lines[idx][1]}")
    return value


def _block(lines, i, indent):
    if i >= len(lines):
        return None, i
    if lines[i][1].startswith("- "):
        out = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            item = lines[i][1][2:]
            if re.match(r"^[A-Za-z0-9_.-]+:(\s|$)", item):
                raise ValueError("lists of maps are outside the supported subset")
            out.append(_scalar(item))
            i += 1
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        m = re.match(r"^([A-Za-z0-9_.\-\"']+):(?:\s+(.*))?$", lines[i][1])
        if not m:
            raise ValueError(f"expected key: value, got {lines[i][1]}")
        key = m.group(1).strip("\"'")
        rest = m.group(2)
        i += 1
        if rest is not None and rest != "":
            out[key] = _scalar(rest)
        elif i < len(lines) and lines[i][0] > indent:
            out[key], i = _block(lines, i, lines[i][0])
        elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            out[key], i = _block(lines, i, indent)  # list at same indent as key
        else:
            out[key] = None
    return out, i
