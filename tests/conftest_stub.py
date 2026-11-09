"""Import the numpy-only modules without Home Assistant installed."""
import importlib, sys, types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "mpc_floor_heating"


def load():
    if "mpc_floor_heating" not in sys.modules:
        pkg = types.ModuleType("mpc_floor_heating")
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules["mpc_floor_heating"] = pkg
    model = importlib.import_module("mpc_floor_heating.model")
    mpc = importlib.import_module("mpc_floor_heating.mpc")
    importlib.import_module("mpc_floor_heating.timeseries")
    return model, mpc
