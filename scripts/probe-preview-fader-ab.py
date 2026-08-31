import sys, time
sys.path.insert(0, "/Users/sp/obs-studio-mode-meters/scripts")
sys.argv = ["x"]
import importlib
probe = importlib.import_module("probe-preview-fader".replace("-", "_")) if False else None
