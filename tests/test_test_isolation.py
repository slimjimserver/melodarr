"""Fail-closed regression coverage for test filesystem isolation."""

if __package__:
    from ._test_environment import TEST_ROOT
else:  # Support unittest discovery with tests/ as the top-level directory.
    from _test_environment import TEST_ROOT

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import application, config as backend_config


class TestFilesystemIsolationTests(unittest.TestCase):
    def test_every_mutable_backend_path_is_inside_shared_test_root(self):
        paths = {
            "database": backend_config.DATABASE,
            "cache database": backend_config.CACHE_DATABASE,
            "settings": backend_config.SETTINGS_FILE,
            "artwork": backend_config.ARTWORK_CACHE_DIRECTORY,
            "session secret": backend_config.SECRET_KEY_FILE,
        }
        for label, path in paths.items():
            with self.subTest(path=label):
                self.assertNotEqual(os.path.realpath(path), TEST_ROOT)
                self.assertEqual(
                    os.path.commonpath((TEST_ROOT, os.path.realpath(path))),
                    TEST_ROOT,
                )

    def test_factory_rejects_production_paths_before_any_initializer_runs(self):
        production = "/app/data"
        with (
            patch.multiple(
                backend_config,
                DATABASE=f"{production}/melodarr.db",
                CACHE_DATABASE=f"{production}/cache/metadata.db",
                SETTINGS_FILE=f"{production}/settings.json",
                ARTWORK_CACHE_DIRECTORY=f"{production}/cache/artwork",
                SECRET_KEY_FILE=f"{production}/session-secret.key",
            ),
            patch.object(application, "load_session_secret") as load_secret,
            patch.object(application, "init_cache_db") as init_cache,
            patch.object(application, "migrate_legacy_cache") as migrate_cache,
            patch.object(application, "init_db") as init_database,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "outside MELODARR_TEST_ROOT",
            ):
                application.create_app({"TESTING": True, "SECRET_KEY": "test"})

        load_secret.assert_not_called()
        init_cache.assert_not_called()
        migrate_cache.assert_not_called()
        init_database.assert_not_called()

    def test_every_test_module_bootstraps_before_importing_backend(self):
        tests_directory = Path(__file__).parent
        for path in sorted(tests_directory.glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            bootstrap_at = source.find("_test_environment import")
            backend_at = source.find("from backend")
            with self.subTest(module=path.name):
                self.assertGreaterEqual(bootstrap_at, 0)
                if backend_at >= 0:
                    self.assertLess(bootstrap_at, backend_at)


if __name__ == "__main__":
    unittest.main()
