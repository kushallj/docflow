"""Nested rule engine.

Pure Python, no Django imports, so it can be unit tested and reused anywhere.

The rule tree format mirrors the Flutter rule builder (Fact / Condition / Rule /
RuleGroup with "any" or "all"), serialised as JSON:

    {
      "type": "all",                      # group: "all" (AND) or "any" (OR)
      "not": false,                       # optional: negate the whole group
      "rules": [
        {"fact": "total_amount", "operator": "greaterThan", "value": 50000},
        {"type": "any", "rules": [
            {"fact": "vendor_approved", "operator": "equalTo", "value": false},
            {"fact": "vendor_gstin_valid", "operator": "equalTo", "value": false}
        ]},
        # compare two facts: value can reference another fact
        {"fact": "tax_amount", "operator": "greaterThan", "value": {"fact": "subtotal"}}
      ]
    }

Every evaluation returns a trace tree, so a reviewer (or the Flutter UI) can
see exactly which condition passed or failed and with which actual value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

# --------------------------------------------------------------------------- #
# Fact types and coercion
# --------------------------------------------------------------------------- #

FACT_TYPES = ("string", "number", "bool", "date")

# The Flutter model uses int / double; both map to "number" here.
FACT_TYPE_ALIASES = {"int": "number", "double": "number", "decimal": "number", "boolean": "bool", "str": "string"}


class RuleError(ValueError):
    """Raised for a malformed rule tree."""


def normalise_type(t: str) -> str:
    t = FACT_TYPE_ALIASES.get(t, t)
    if t not in FACT_TYPES:
        raise RuleError(f"Unknown fact type '{t}'")
    return t


def coerce(value: Any, fact_type: str) -> Any:
    """Convert a raw value to the Python type used for comparison. Returns None if empty."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if fact_type == "number":
        if isinstance(value, bool):
            raise RuleError("A boolean is not a number")
        try:
            return Decimal(str(value).replace(",", "").strip())
        except InvalidOperation as exc:
            raise RuleError(f"'{value}' is not a number") from exc
    if fact_type == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "yes", "1", "y"):
            return True
        if s in ("false", "no", "0", "n"):
            return False
        raise RuleError(f"'{value}' is not a boolean")
    if fact_type == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip()[:10])
        except ValueError as exc:
            raise RuleError(f"'{value}' is not an ISO date (YYYY-MM-DD)") from exc
    return str(value).strip()


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Operator:
    name: str
    label: str
    types: tuple[str, ...]          # fact types this operator applies to
    arity: str                      # "none" | "one" | "two" | "list"
    fn: Callable[..., bool]
    needs_value: bool = True


def _fold(v: Any) -> Any:
    return v.casefold() if isinstance(v, str) else v


def _eq(a, b):
    return _fold(a) == _fold(b)


def _contains(a, b):
    return a is not None and _fold(str(b)) in _fold(str(a))


def _matches(a, pattern):
    try:
        return a is not None and re.search(str(pattern), str(a), re.IGNORECASE) is not None
    except re.error as exc:
        raise RuleError(f"Invalid regular expression: {exc}") from exc


def _today() -> date:
    return date.today()


ALL = FACT_TYPES
ORDERED = ("number", "date")

OPERATORS: dict[str, Operator] = {
    op.name: op
    for op in [
        Operator("equalTo", "equals", ALL, "one", _eq),
        Operator("notEqualTo", "does not equal", ALL, "one", lambda a, b: not _eq(a, b)),
        Operator("greaterThan", "greater than", ORDERED, "one", lambda a, b: a > b),
        Operator("greaterThanOrEqual", "greater than or equal to", ORDERED, "one", lambda a, b: a >= b),
        Operator("lessThan", "less than", ORDERED, "one", lambda a, b: a < b),
        Operator("lessThanOrEqual", "less than or equal to", ORDERED, "one", lambda a, b: a <= b),
        Operator("between", "between (inclusive)", ORDERED, "two", lambda a, b: b[0] <= a <= b[1]),
        Operator("in", "is one of", ("string", "number"), "list", lambda a, b: _fold(a) in [_fold(x) for x in b]),
        Operator("notIn", "is not one of", ("string", "number"), "list", lambda a, b: _fold(a) not in [_fold(x) for x in b]),
        Operator("contains", "contains", ("string",), "one", _contains),
        Operator("notContains", "does not contain", ("string",), "one", lambda a, b: not _contains(a, b)),
        Operator("startsWith", "starts with", ("string",), "one", lambda a, b: _fold(str(a)).startswith(_fold(str(b)))),
        Operator("matches", "matches pattern", ("string",), "one", _matches),
        Operator("olderThanDays", "is more than N days ago", ("date",), "one",
                 lambda a, b: a < _today() - timedelta(days=int(b))),
        Operator("withinNextDays", "is within the next N days", ("date",), "one",
                 lambda a, b: _today() <= a <= _today() + timedelta(days=int(b))),
        Operator("isInPast", "is in the past", ("date",), "none", lambda a, _: a < _today(), needs_value=False),
        Operator("exists", "has a value", ALL, "none", lambda a, _: a is not None, needs_value=False),
        Operator("notExists", "is empty", ALL, "none", lambda a, _: a is None, needs_value=False),
    ]
}

# Operators that are evaluated even when the fact has no value.
NULL_SAFE = {"exists", "notExists", "notEqualTo", "notIn", "notContains"}
# Day-count operators take an integer, not a value of the fact's own type.
DAY_COUNT_OPS = {"olderThanDays", "withinNextDays"}


def operators_for(fact_type: str) -> list[dict]:
    """Operator list for a fact type, used to fill the Flutter dropdowns."""
    fact_type = normalise_type(fact_type)
    return [
        {"name": op.name, "label": op.label, "arity": op.arity, "needs_value": op.needs_value}
        for op in OPERATORS.values()
        if fact_type in op.types
    ]


# --------------------------------------------------------------------------- #
# Fact catalog
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FactDef:
    key: str
    label: str
    type: str
    description: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "description": self.description,
            "operators": operators_for(self.type),
        }


class FactCatalog:
    def __init__(self, facts: Iterable[FactDef]):
        self._facts = {f.key: FactDef(f.key, f.label, normalise_type(f.type), f.description) for f in facts}

    def __contains__(self, key: str) -> bool:
        return key in self._facts

    def __getitem__(self, key: str) -> FactDef:
        return self._facts[key]

    def __iter__(self):
        return iter(self._facts.values())

    def as_list(self) -> list[dict]:
        return [f.as_dict() for f in self._facts.values()]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

MAX_DEPTH = 8


def _is_group(node: Mapping) -> bool:
    return "rules" in node


def _is_fact_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value.keys()) == {"fact"}


def validate(tree: Any, catalog: FactCatalog) -> list[str]:
    """Return a list of human-readable problems. Empty list means the tree is valid."""
    errors: list[str] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            errors.append(f"{path}: groups are nested more than {MAX_DEPTH} levels deep")
            return
        if not isinstance(node, Mapping):
            errors.append(f"{path}: must be an object")
            return
        if _is_group(node):
            if node.get("type") not in ("all", "any"):
                errors.append(f"{path}.type: must be 'all' or 'any'")
            rules = node.get("rules")
            if not isinstance(rules, list) or not rules:
                errors.append(f"{path}.rules: must be a non-empty list")
                return
            for i, child in enumerate(rules):
                walk(child, f"{path}.rules[{i}]", depth + 1)
            return
        _validate_leaf(node, path, catalog, errors)

    walk(tree, "root", 0)
    return errors


def _validate_leaf(node: Mapping, path: str, catalog: FactCatalog, errors: list[str]) -> None:
    fact_key, op_name = node.get("fact"), node.get("operator")
    if fact_key not in catalog:
        errors.append(f"{path}.fact: unknown fact '{fact_key}'")
        return
    fact = catalog[fact_key]
    op = OPERATORS.get(op_name)
    if op is None:
        errors.append(f"{path}.operator: unknown operator '{op_name}'")
        return
    if fact.type not in op.types:
        errors.append(f"{path}.operator: '{op_name}' cannot be used with {fact.type} fact '{fact_key}'")
        return
    if not op.needs_value:
        return
    if "value" not in node:
        errors.append(f"{path}.value: required for operator '{op_name}'")
        return
    value = node["value"]
    if _is_fact_ref(value):
        ref = value["fact"]
        if ref not in catalog:
            errors.append(f"{path}.value: unknown fact '{ref}'")
        elif catalog[ref].type != fact.type:
            errors.append(f"{path}.value: fact '{ref}' is {catalog[ref].type}, expected {fact.type}")
        return
    try:
        _coerce_operand(value, fact.type, op)
    except RuleError as exc:
        errors.append(f"{path}.value: {exc}")


def _coerce_operand(value: Any, fact_type: str, op: Operator) -> Any:
    if op.name in DAY_COUNT_OPS:
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise RuleError("must be a whole number of days") from exc
        if n < 0:
            raise RuleError("must not be negative")
        return n
    if op.arity == "two":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise RuleError("must be a list of two values [low, high]")
        low, high = coerce(value[0], fact_type), coerce(value[1], fact_type)
        if low is None or high is None:
            raise RuleError("both bounds are required")
        if low > high:
            raise RuleError("low bound is greater than high bound")
        return (low, high)
    if op.arity == "list":
        if not isinstance(value, (list, tuple)) or not value:
            raise RuleError("must be a non-empty list")
        return [coerce(v, fact_type) for v in value]
    if op.name == "matches":
        try:
            re.compile(str(value))
        except re.error as exc:
            raise RuleError(f"invalid regular expression: {exc}") from exc
    coerced = coerce(value, fact_type)
    if coerced is None:
        raise RuleError("must not be empty")
    return coerced


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass
class Trace:
    passed: bool
    kind: str                       # "group" | "condition"
    description: str
    actual: Any = None
    expected: Any = None
    error: str | None = None
    children: list["Trace"] = field(default_factory=list)

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"passed": self.passed, "kind": self.kind, "description": self.description}
        if self.kind == "condition":
            d["actual"] = _jsonable(self.actual)
            d["expected"] = _jsonable(self.expected)
        if self.error:
            d["error"] = self.error
        if self.children:
            d["children"] = [c.as_dict() for c in self.children]
        return d


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def evaluate(tree: Mapping, facts: Mapping[str, Any], catalog: FactCatalog) -> Trace:
    """Evaluate a validated rule tree against a dict of fact values.

    A condition on a missing fact evaluates to False (except null-safe operators
    such as notExists), so a document with unreadable fields never silently
    passes an approval rule.
    """
    errors = validate(tree, catalog)
    if errors:
        raise RuleError("; ".join(errors))
    return _eval(tree, facts, catalog)


def _eval(node: Mapping, facts: Mapping[str, Any], catalog: FactCatalog) -> Trace:
    if _is_group(node):
        children = [_eval(c, facts, catalog) for c in node["rules"]]
        results = [c.passed for c in children]
        passed = all(results) if node["type"] == "all" else any(results)
        negate = bool(node.get("not"))
        if negate:
            passed = not passed
        word = "ALL" if node["type"] == "all" else "ANY"
        desc = f"{'NOT ' if negate else ''}{word} of {len(children)} conditions"
        return Trace(passed=passed, kind="group", description=desc, children=children)
    return _eval_leaf(node, facts, catalog)


def _eval_leaf(node: Mapping, facts: Mapping[str, Any], catalog: FactCatalog) -> Trace:
    fact = catalog[node["fact"]]
    op = OPERATORS[node["operator"]]
    raw_expected = node.get("value")
    desc = f"{fact.label} {op.label}"

    try:
        actual = coerce(facts.get(fact.key), fact.type)
    except RuleError as exc:
        return Trace(False, "condition", desc, facts.get(fact.key), raw_expected, error=f"bad fact value: {exc}")

    expected: Any = None
    if op.needs_value:
        if _is_fact_ref(raw_expected):
            ref = catalog[raw_expected["fact"]]
            desc += f" {ref.label}"
            try:
                expected = coerce(facts.get(ref.key), ref.type)
            except RuleError as exc:
                return Trace(False, "condition", desc, actual, None, error=f"bad fact value: {exc}")
            if expected is None:
                return Trace(False, "condition", desc, actual, None, error=f"'{ref.key}' has no value")
        else:
            expected = _coerce_operand(raw_expected, fact.type, op)
            desc += f" {_jsonable(expected)}"

    if actual is None and op.name not in NULL_SAFE:
        return Trace(False, "condition", desc, None, expected, error=f"'{fact.key}' has no value")

    passed = bool(op.fn(actual, expected))
    return Trace(passed, "condition", desc, actual, expected)
