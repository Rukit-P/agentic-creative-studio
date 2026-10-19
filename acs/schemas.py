"""Output contracts for every agent, checked before an output is accepted.

A deliberately small validator (no jsonschema dependency): each field is a type
and an optional constraint. `validate(agent, output)` returns a list of problems;
an empty list means the output is accepted.
"""
from __future__ import annotations

import re

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _str(min_len=1, max_len=600):
    return lambda v: isinstance(v, str) and min_len <= len(v.strip()) <= max_len


def _list(item, min_items=1, max_items=12, exact=None):
    def check(v):
        if not isinstance(v, list) or not all(item(x) for x in v):
            return False
        return len(v) == exact if exact is not None else min_items <= len(v) <= max_items
    return check


def _obj(fields):
    return lambda v: isinstance(v, dict) and not validate_fields(fields, v)


SCHEMAS = {
    "planner": {"objective": _str(), "angle": _str(), "pillars": _list(_str(max_len=120), 2, 6), "deliverables": _list(_str(max_len=80))},
    "copywriter": {"concept": _str(), "headlines": _list(_str(max_len=120), 2, 6), "body": _str(max_len=1200), "cta": _str(max_len=80)},
    "art_director": {"palette": _obj({"name": _str(max_len=40), "colours": _list(lambda c: isinstance(c, str) and bool(HEX.match(c)), exact=4)}),
                     "typography": _obj({"heading": _str(max_len=60), "body": _str(max_len=60)}),
                     "shot_list": _list(_str(max_len=200), 4, 10), "motion": _str(max_len=400)},
    "critic": {"checks": _list(lambda c: isinstance(c, dict) and isinstance(c.get("rule"), str) and isinstance(c.get("passed"), bool), 1, 20),
               "score": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100, "note": _str(max_len=600)},
}


def validate_fields(fields: dict, value: dict) -> list[str]:
    problems = [f"missing or invalid field '{name}'" for name, check in fields.items() if not check(value.get(name))]
    return problems


def validate(agent: str, output) -> list[str]:
    if agent not in SCHEMAS:
        return []                       # packager is assembled in code from already-validated parts
    if not isinstance(output, dict):
        return ["output is not a JSON object"]
    return validate_fields(SCHEMAS[agent], output)
