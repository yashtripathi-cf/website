# Monkeypatch shared.lib.gcp to skip metadata server check
import importlib
import sys

# Pre-create the module so when shared.lib.gcp is imported, it uses our version
class FakeGcp:
    def in_google_network(self=None):
        return False

# This will be applied via sitecustomize
import types
_mod = types.ModuleType('shared.lib.gcp')
_mod.in_google_network = lambda: False
sys.modules['shared.lib.gcp'] = _mod
