import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jobradar.config import load_settings

ROOT = Path(__file__).resolve().parents[2]


class ConfigurationTests(unittest.TestCase):
    def test_documented_limits_and_portable_paths(self):
        settings = load_settings(ROOT / "config/v0.1.json")
        self.assertEqual(settings.database_path, ROOT / "data/jobradar-v0.1.sqlite3")
        self.assertEqual(settings.prompts_dir, ROOT / "prompts")
        self.assertEqual(settings.values["search"], {"max_source_vacancies":100,"max_new_analyses":30,"deadline_seconds":600})
        self.assertEqual(settings.values["llm"]["timeout_seconds"], 60)
        self.assertEqual(settings.values["source"]["page_timeout_seconds"], 30)

    def test_invalid_limits_fail_before_database_creation(self):
        original = json.loads((ROOT / "config/v0.1.json").read_text())
        cases = [("dialogue","max_tool_calls",21),("retries","max_safe_retries",3),("search","max_new_analyses",-1),("llm","timeout_seconds",True)]
        for section, key, value in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                config = json.loads(json.dumps(original))
                config[section][key] = value
                path = Path(temporary) / "config.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_settings(path)

    def test_init_cli_works_outside_project_and_without_api_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = json.loads((ROOT / "config/v0.1.json").read_text())
            config["database"]["path"] = "fresh.sqlite3"
            path = root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run([sys.executable,"-m","jobradar","init-db","--config",str(path)], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "fresh.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
