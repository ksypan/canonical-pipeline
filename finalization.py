"""Pure finalization: per-field defaults, then declared dependencies."""
from copy import deepcopy
from field_contract import FIELD_CONTRACT
from pipeline_errors import FinalizationError
from runtime_contract import TEMPLATE_PATH, load_template_keys, dependency_conditions


def finalize_values(values, template_path=TEMPLATE_PATH, field_contract=FIELD_CONTRACT):
    keys = load_template_keys(template_path, field_contract)
    if not isinstance(values, dict) or set(values) != set(keys):
        raise FinalizationError("Finalization requires exactly the official fields.")
    result = {
        name: deepcopy(field_contract[name]["missing_value"] if values[name] is None else values[name])
        for name in keys
    }
    for name in keys:
        for condition in dependency_conditions(field_contract[name]):
            if condition["field"] not in result:
                raise FinalizationError("Dependency references an unknown field.")
            if result[condition["field"]] != condition["equals"]:
                result[name] = deepcopy(field_contract[name]["missing_value"])
    return result
