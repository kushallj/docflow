from datetime import date, timedelta

import pytest

from apps.rules.engine import FactCatalog, FactDef, RuleError, evaluate, operators_for, validate

CATALOG = FactCatalog([
    FactDef("total", "Total", "number"),
    FactDef("subtotal", "Subtotal", "number"),
    FactDef("vendor", "Vendor", "string"),
    FactDef("approved", "Approved", "bool"),
    FactDef("due", "Due date", "date"),
    FactDef("count", "Count", "int"),          # Flutter "int" maps to number
])


def leaf(fact, op, value=None, **kw):
    node = {"fact": fact, "operator": op}
    if value is not None or "value" in kw:
        node["value"] = value
    return node


def run(tree, facts):
    return evaluate(tree, facts, CATALOG)


class TestOperators:
    @pytest.mark.parametrize("op,value,actual,expected", [
        ("equalTo", 100, "100.00", True),
        ("notEqualTo", 100, 101, True),
        ("greaterThan", 50000, "50000.01", True),
        ("greaterThan", 50000, 50000, False),
        ("greaterThanOrEqual", 50000, 50000, True),
        ("lessThan", 10, 9.99, True),
        ("lessThanOrEqual", 10, 10, True),
        ("between", [10, 20], 20, True),
        ("between", [10, 20], 20.01, False),
        ("in", [0, 5, 12, 18, 28], "18.00", True),
        ("notIn", [0, 5], 12, True),
    ])
    def test_number(self, op, value, actual, expected):
        assert run(leaf("total", op, value), {"total": actual}).passed is expected

    def test_string_comparison_ignores_case(self):
        assert run(leaf("vendor", "equalTo", "sharma stationers"), {"vendor": "Sharma Stationers"}).passed
        assert run(leaf("vendor", "contains", "STATION"), {"vendor": "Sharma Stationers"}).passed
        assert run(leaf("vendor", "startsWith", "sharma"), {"vendor": "Sharma Stationers"}).passed
        assert run(leaf("vendor", "matches", r"^sh\w+ st"), {"vendor": "Sharma Stationers"}).passed
        assert run(leaf("vendor", "in", ["acme", "SHARMA STATIONERS"]), {"vendor": "Sharma Stationers"}).passed

    def test_bool_accepts_strings(self):
        assert run(leaf("approved", "equalTo", False), {"approved": "false"}).passed
        assert not run(leaf("approved", "equalTo", True), {"approved": "no"}).passed

    def test_relative_dates(self):
        today = date.today()
        assert run(leaf("due", "isInPast"), {"due": (today - timedelta(days=1)).isoformat()}).passed
        assert run(leaf("due", "withinNextDays", 7), {"due": (today + timedelta(days=7)).isoformat()}).passed
        assert not run(leaf("due", "withinNextDays", 7), {"due": (today + timedelta(days=8)).isoformat()}).passed
        assert run(leaf("due", "olderThanDays", 30), {"due": (today - timedelta(days=31)).isoformat()}).passed

    def test_exists_and_not_exists(self):
        assert run(leaf("vendor", "exists"), {"vendor": "x"}).passed
        assert run(leaf("vendor", "notExists"), {"vendor": "  "}).passed
        assert run(leaf("vendor", "notExists"), {}).passed

    def test_fact_to_fact_comparison(self):
        tree = leaf("total", "greaterThan", {"fact": "subtotal"})
        assert run(tree, {"total": 118, "subtotal": 100}).passed
        assert not run(tree, {"total": 90, "subtotal": 100}).passed
        t = run(tree, {"total": 90})
        assert not t.passed and "subtotal" in t.error

    def test_int_alias_is_number(self):
        assert run(leaf("count", "greaterThan", 2), {"count": 3}).passed


class TestMissingValues:
    def test_missing_fact_fails_closed(self):
        """An unreadable amount must never satisfy an approval condition."""
        t = run(leaf("total", "lessThan", 1000), {})
        assert not t.passed
        assert "no value" in t.error

    def test_negative_operators_pass_on_missing(self):
        assert run(leaf("vendor", "notEqualTo", "acme"), {}).passed

    def test_garbage_value_fails_with_error(self):
        t = run(leaf("total", "greaterThan", 10), {"total": "twelve"})
        assert not t.passed and "bad fact value" in t.error


class TestGroups:
    TREE = {
        "type": "all",
        "rules": [
            leaf("total", "greaterThan", 50000),
            {"type": "any", "rules": [
                leaf("approved", "equalTo", False),
                leaf("vendor", "contains", "unknown"),
            ]},
        ],
    }

    def test_nested_all_any(self):
        assert run(self.TREE, {"total": 60000, "approved": False, "vendor": "x"}).passed
        assert run(self.TREE, {"total": 60000, "approved": True, "vendor": "Unknown Traders"}).passed
        assert not run(self.TREE, {"total": 60000, "approved": True, "vendor": "x"}).passed
        assert not run(self.TREE, {"total": 100, "approved": False, "vendor": "x"}).passed

    def test_not_group(self):
        tree = {"type": "any", "not": True, "rules": [leaf("approved", "equalTo", True)]}
        assert run(tree, {"approved": False}).passed

    def test_trace_explains_every_condition(self):
        t = run(self.TREE, {"total": 60000, "approved": True, "vendor": "x"}).as_dict()
        assert t["kind"] == "group" and t["description"] == "ALL of 2 conditions"
        first, inner = t["children"]
        assert first == {"passed": True, "kind": "condition", "description": "Total greater than 50000",
                         "actual": 60000, "expected": 50000}
        assert [c["passed"] for c in inner["children"]] == [False, False]


class TestValidation:
    def test_valid_tree(self):
        assert validate(TestGroups.TREE, CATALOG) == []

    @pytest.mark.parametrize("tree,needle", [
        ({"type": "all", "rules": []}, "non-empty list"),
        ({"type": "both", "rules": [leaf("total", "exists")]}, "'all' or 'any'"),
        (leaf("nope", "equalTo", 1), "unknown fact 'nope'"),
        (leaf("total", "sortOf", 1), "unknown operator"),
        (leaf("vendor", "greaterThan", 5), "cannot be used with string"),
        (leaf("total", "greaterThan"), "required"),
        (leaf("total", "greaterThan", "abc"), "not a number"),
        (leaf("total", "between", [20, 10]), "low bound is greater"),
        (leaf("total", "in", []), "non-empty list"),
        (leaf("vendor", "matches", "(unclosed"), "regular expression"),
        (leaf("due", "withinNextDays", -1), "negative"),
        (leaf("total", "greaterThan", {"fact": "vendor"}), "expected number"),
    ])
    def test_errors(self, tree, needle):
        errors = validate(tree, CATALOG)
        assert errors and any(needle in e for e in errors), errors

    def test_depth_limit(self):
        tree = leaf("total", "exists")
        for _ in range(10):
            tree = {"type": "all", "rules": [tree]}
        assert any("nested more than" in e for e in validate(tree, CATALOG))

    def test_evaluate_refuses_invalid_tree(self):
        with pytest.raises(RuleError):
            run(leaf("nope", "equalTo", 1), {})


def test_operator_catalog_matches_fact_types():
    names = {o["name"] for o in operators_for("string")}
    assert "contains" in names and "greaterThan" not in names
    assert "between" in {o["name"] for o in operators_for("double")}
