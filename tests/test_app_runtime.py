"""Windows 便携版应用路径解析的隔离回归。"""

import tempfile
import unittest
from pathlib import Path

from app_runtime import resolve_app_paths


class AppPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_source_mode_keeps_data_next_to_source(self):
        script = self.root / "Agent" / "desktop_app.py"
        app, resources, data = resolve_app_paths(script, self.root / "python.exe", False, environ={})
        self.assertEqual(app, script.parent)
        self.assertEqual(resources, app)
        self.assertEqual(data, app / "data")

    def test_packaged_app_reuses_existing_agent_project_data(self):
        project = self.root / "Agent"
        app = project / "apps" / "ChangAn"
        app.mkdir(parents=True)
        (project / "desktop_app.py").touch()
        (project / "data").mkdir()
        (project / "data" / "workspace.json").write_text("{}", encoding="utf-8")
        executable = app / "ChangAn.exe"
        executable.touch()
        resources = app / "_internal"
        resolved = resolve_app_paths(app / "desktop_app.py", executable, True, resources, {})
        self.assertEqual(resolved, (app, resources, project / "data"))

    def test_fresh_install_uses_per_user_data_and_explicit_override_wins(self):
        app = self.root / "Programs" / "ChangAn"
        app.mkdir(parents=True)
        executable = app / "ChangAn.exe"
        local_appdata = self.root / "LocalAppData"
        resolved = resolve_app_paths(app / "desktop_app.py", executable, True, app / "_internal",
                                     {"LOCALAPPDATA": str(local_appdata)})
        self.assertEqual(resolved[2], local_appdata / "ChangAn" / "data")
        override = self.root / "ChosenData"
        resolved = resolve_app_paths(app / "desktop_app.py", executable, True, app / "_internal",
                                     {"LOCALAPPDATA": str(local_appdata), "CHANGAN_DATA_DIR": str(override)})
        self.assertEqual(resolved[2], override)

    def test_empty_data_override_falls_back_to_normal_app_location(self):
        app = self.root / "Programs" / "ChangAn"
        app.mkdir(parents=True)
        local_appdata = self.root / "LocalAppData"
        resolved = resolve_app_paths(app / "desktop_app.py", app / "ChangAn.exe", True, None,
                                     {"LOCALAPPDATA": str(local_appdata), "CHANGAN_DATA_DIR": "  "})
        self.assertEqual(resolved[2], local_appdata / "ChangAn" / "data")


if __name__ == "__main__":
    unittest.main()
