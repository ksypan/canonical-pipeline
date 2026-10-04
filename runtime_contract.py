"""Canonical ordered template and contract invariants; no medical inference."""
from pathlib import Path
from field_contract import FIELD_CONTRACT
from json_io import read_json

TEMPLATE_PATH = Path(__file__).with_name("participant-output-template-v2.json")


def load_template_keys(template_path=TEMPLATE_PATH, field_contract=FIELD_CONTRACT):
    template = read_json(template_path)
    if not isinstance(template, dict) or len(template) != 50:
        raise ValueError("Official template must contain 50 flat fields.")
    if tuple(template) != tuple(field_contract):
        raise ValueError("Official template and FIELD_CONTRACT keys/order differ.")
    for name, descriptor in field_contract.items():
        if descriptor.get("name") != name:
            raise ValueError("Contract name/key mismatch.")
        if descriptor.get("missing_value") != template[name]:
            raise ValueError("Contract missing_value differs from official template: " + name)
    return tuple(template)


def dependency_conditions(descriptor):
    """All explicitly declared conditions; never infer dependencies from names."""
    conditions = []
    if descriptor.get("applicable_when"):
        conditions.append(descriptor["applicable_when"])
    for rule in descriptor.get("rules", []):
        if rule.get("requires") and rule["requires"] not in conditions:
            conditions.append(rule["requires"])
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"field", "equals"}:
            raise ValueError("Unsupported contract dependency metadata.")
    return conditions
