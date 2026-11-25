import importlib.util
import os
import sys
import tempfile
from unittest import mock

from django.apps import apps
from django.test import SimpleTestCase

import openIMIS

SCHEMA_PATH = os.path.join(os.path.dirname(openIMIS.__file__), "schema.py")


def _write(root, relative_path, content=""):
    path = os.path.join(root, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(content)


def _load_schema(app_names):
    spec = importlib.util.spec_from_file_location("openIMIS.schema_import_probe", SCHEMA_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch("openIMIS.openimisapps.openimis_apps", return_value=app_names):
        spec.loader.exec_module(module)
    return module


class FakeAppsTestCase(SimpleTestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = root.name
        sys.path.insert(0, self.root)
        self.addCleanup(sys.path.remove, self.root)
        self.addCleanup(self._forget_fake_modules)

    @staticmethod
    def _forget_fake_modules():
        for name in [n for n in sys.modules if n.startswith("fake_app_")]:
            del sys.modules[name]


class SchemaImportTest(FakeAppsTestCase):
    def test_app_without_schema_module_is_skipped(self):
        _write(self.root, "fake_app_plain/__init__.py")
        module = _load_schema(["fake_app_plain"])
        self.assertEqual(module.queries, [])
        self.assertEqual(module.mutations, [])

    def test_schema_importing_a_missing_package_stops_startup(self):
        _write(self.root, "fake_app_broken/__init__.py")
        _write(self.root, "fake_app_broken/schema.py", "import fake_app_missing_dependency\n")
        with self.assertRaises(ModuleNotFoundError) as raised:
            _load_schema(["fake_app_broken"])
        self.assertEqual(raised.exception.name, "fake_app_missing_dependency")


class SignalBindingTest(FakeAppsTestCase):
    def test_binding_failure_is_logged_as_error_with_app_name_and_traceback(self):
        _write(self.root, "fake_app_signals/__init__.py")
        _write(
            self.root,
            "fake_app_signals/signals.py",
            "def bind_service_signals():\n    raise RuntimeError('binding exploded')\n",
        )
        config = apps.get_app_config("signal_binding")
        with self.assertLogs("signal_binding.apps", level="ERROR") as logged:
            config._bind_app_signals("fake_app_signals")
        record = logged.records[0]
        self.assertIn("fake_app_signals", record.getMessage())
        self.assertIn("binding exploded", record.getMessage())
        self.assertIsNotNone(record.exc_info)
