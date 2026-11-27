from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def inr(value):
    """Indian digit grouping: 1234567.5 -> 12,34,567.50"""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


@register.filter
def pct(value):
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


@register.filter
def get(d, key):
    return d.get(key) if isinstance(d, dict) else None


@register.filter
def listify(value):
    return [value] if isinstance(value, dict) else (value or [])


@register.filter
def pretty_json(value):
    import json

    return json.dumps(value, indent=2, ensure_ascii=False)
