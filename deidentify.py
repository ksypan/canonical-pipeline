"""Canonical import for the first-half deidentification stage."""

from pathlib import Path
import importlib.util
from importlib.machinery import SourceFileLoader
import sys


_path = Path(__file__).with_name("deidentify_impl.py")
_spec = importlib.util.spec_from_file_location("_deidentify_impl", _path, loader=SourceFileLoader("_deidentify_impl", str(_path)))
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load {_path}")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

deidentify_text = _module.deidentify_text
DeidentificationAudit = _module.DeidentificationAudit

__all__ = ["deidentify_text", "DeidentificationAudit"]
