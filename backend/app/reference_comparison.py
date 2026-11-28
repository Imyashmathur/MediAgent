"""Compare explicitly transcribed numeric results with simple stated limits.

This does not infer a reference interval or interpret the clinical significance.
"""
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
SINGLE = re.compile(rf"^\s*([<>]=?|[≤≥])?\s*({NUMBER})\s*$")
RANGE = re.compile(rf"^\s*({NUMBER})\s*(?:-|–|—|\bto\b)\s*({NUMBER})\s*$", re.I)


@dataclass(frozen=True)
class Interval:
    lower: Decimal | None
    lower_inclusive: bool
    upper: Decimal | None
    upper_inclusive: bool


def parse_interval(value: str | None, *, reference: bool) -> Interval | None:
    if not isinstance(value, str) or len(value) > 100:
        return None
    match = RANGE.fullmatch(value) if reference else None
    try:
        if match:
            low, high = Decimal(match.group(1)), Decimal(match.group(2))
            return Interval(low, True, high, True) if low <= high else None
        match = SINGLE.fullmatch(value)
        if not match:
            return None
        op, number = match.groups()
        number = Decimal(number)
        if op in ("<", "≤", "<="):
            return Interval(None, False, number, op != "<")
        if op in (">", "≥", ">="):
            return Interval(number, op != ">", None, False)
        return None if reference else Interval(number, True, number, True)
    except InvalidOperation:
        return None


def compare(value: str | None, reference: str | None) -> str:
    result, limits = parse_interval(value, reference=False), parse_interval(reference, reference=True)
    if not result or not limits:
        return "unable_to_determine"
    if limits.lower is not None and result.upper is not None and (result.upper < limits.lower or (result.upper == limits.lower and not (result.upper_inclusive and limits.lower_inclusive))):
        return "below"
    if limits.upper is not None and result.lower is not None and (result.lower > limits.upper or (result.lower == limits.upper and not (result.lower_inclusive and limits.upper_inclusive))):
        return "above"
    lower_inside = limits.lower is None or (result.lower is not None and (result.lower > limits.lower or (result.lower == limits.lower and (limits.lower_inclusive or not result.lower_inclusive))))
    upper_inside = limits.upper is None or (result.upper is not None and (result.upper < limits.upper or (result.upper == limits.upper and (limits.upper_inclusive or not result.upper_inclusive))))
    return "within" if lower_inside and upper_inside else "unable_to_determine"


def assessments(data: dict) -> list[str]:
    statuses = []
    for row in data.get("laboratory_tests", []):
        value, reference = row.get("observed_value"), row.get("reference_range")
        if not isinstance(value, dict) or not isinstance(reference, dict) or not row.get("test_name"):
            statuses.append("unable_to_determine")
            continue
        # Source fields must come from the same page/segment and extraction layer.
        value_evidence, range_evidence = value.get("evidence"), reference.get("evidence")
        if value.get("origin") == "source" and reference.get("origin") == "source" and (not value_evidence or not range_evidence or (value_evidence.get("page"), value_evidence.get("location_type", "page"), value_evidence.get("layer", "primary")) != (range_evidence.get("page"), range_evidence.get("location_type", "page"), range_evidence.get("layer", "primary"))):
            statuses.append("unable_to_determine")
            continue
        raw_value = value.get("value")
        embedded_flag = None
        if isinstance(raw_value, str):
            tagged = re.fullmatch(rf"\s*([<>]=?|[≤≥])?\s*({NUMBER})\s+([HL])\*?\s*", raw_value, re.I)
            if tagged:
                raw_value = (tagged.group(1) or "") + tagged.group(2)
                embedded_flag = tagged.group(3).upper()
        status = compare(raw_value, reference.get("value"))
        flag = row.get("abnormality_flag")
        reported_flags = [embedded_flag] if embedded_flag else []
        if isinstance(flag, dict) and flag.get("value"):
            reported_flags.append(flag["value"].strip().upper().rstrip("*"))
        for reported in reported_flags:
            if reported not in ("H", "HIGH", "L", "LOW", "N", "NORMAL") or (reported in ("H", "HIGH") and status != "above") or (reported in ("L", "LOW") and status != "below") or (reported in ("N", "NORMAL") and status != "within"):
                status = "unable_to_determine"
        statuses.append(status)
    return statuses
