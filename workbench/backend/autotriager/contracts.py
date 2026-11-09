"""Validate boundaries against the frozen v1 schema."""
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts/runtime.schema.json").read_text(encoding="utf-8-sig"))

def validate(kind, value):
    Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": "#/$defs/" + kind}, format_checker=FormatChecker()).validate(value)
    return value

def example(kind):
    rows = json.loads((ROOT / "contracts/examples.json").read_text(encoding="utf-8-sig"))["objects"]
    return next(x["value"] for x in rows if x["schema"] == kind)
