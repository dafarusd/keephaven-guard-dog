"""One file per site. Each defines SCRIPT, an OptOut. SCRIPTS maps broker id -> script."""
import importlib
import pkgutil

SCRIPTS = {}
for _m in pkgutil.iter_modules(__path__):
    _s = importlib.import_module(f"{__name__}.{_m.name}").SCRIPT
    SCRIPTS[_s.site] = _s
