"""Canonical import for the declarative field contract."""

from pathlib import Path
import importlib.util
from importlib.machinery import SourceFileLoader
import sys


_path = Path(__file__).with_name("field_contract_impl.py")
_spec = importlib.util.spec_from_file_location("_field_contract_impl", _path, loader=SourceFileLoader("_field_contract_impl", str(_path)))
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load {_path}")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

CONTRACT_VERSION = _module.CONTRACT_VERSION
NOT_SPECIFIED = _module.NOT_SPECIFIED
DATE_FORMAT = _module.DATE_FORMAT
DATE_STRPTIME = _module.DATE_STRPTIME
GLOBAL_RULES = _module.GLOBAL_RULES
FIELD_CONTRACT = _module.FIELD_CONTRACT
FIELD_NAMES = _module.FIELD_NAMES
FIELDS_BY_EXTRACTOR = _module.FIELDS_BY_EXTRACTOR
DEFERRED_FIELDS = _module.DEFERRED_FIELDS
BINARY_CODE_FIELDS = _module.BINARY_CODE_FIELDS
get_contract = _module.get_contract

__all__ = ["CONTRACT_VERSION", "NOT_SPECIFIED", "DATE_FORMAT", "DATE_STRPTIME", "GLOBAL_RULES", "FIELD_CONTRACT", "FIELD_NAMES", "FIELDS_BY_EXTRACTOR", "DEFERRED_FIELDS", "BINARY_CODE_FIELDS", "get_contract"]
