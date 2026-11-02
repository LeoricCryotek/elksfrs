"""Transfer connector ownership before FRS creates or updates any XML records."""
import importlib.util
from pathlib import Path


def migrate(cr, version):
    path = Path(__file__).resolve().parents[2] / 'qbo_migration.py'
    spec = importlib.util.spec_from_file_location('_elksfrs_qbo_migration', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.adopt_legacy_qbo(cr)
