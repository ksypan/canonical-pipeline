"""Canonical parser entry point; implementation remains in the supplied module."""

from pathlib import Path
import importlib.util
from importlib.machinery import SourceFileLoader
import sys


_path = Path(__file__).with_name("parser_impl.py")
_spec = importlib.util.spec_from_file_location("_parser_impl", _path, loader=SourceFileLoader("_parser_impl", str(_path)))
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load {_path}")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

for _name in dir(_module):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_module, _name)
