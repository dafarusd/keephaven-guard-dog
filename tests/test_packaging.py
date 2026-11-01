"""The installed package is what runs on people's machines: every subpackage and data file must be in it."""
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless((ROOT / "pyproject.toml").exists() and (ROOT / "guard_dog").is_dir(),
                     "checks the source tree; the .deb build runs the tests against the installed copy")
class Packaging(unittest.TestCase):
    def test_every_subpackage_and_data_file_is_shipped(self):
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
        on_disk = {".".join(p.parent.relative_to(ROOT).parts) for p in (ROOT / "guard_dog").rglob("__init__.py")}
        self.assertEqual(set(cfg["packages"]), on_disk)
        data = cfg["package-data"]["guard_dog"]
        for f in (ROOT / "guard_dog" / "data").iterdir():
            self.assertTrue(any(f.match(pat) for pat in data), f.name)
