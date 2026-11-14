"""确保 config.example.toml 与配置模型不漂移。"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    from maibot_sdk import PluginConfigBase  # noqa: F401
except ImportError:  # pragma: no cover - 没有安装 SDK 时跳过
    raise unittest.SkipTest("maibot-plugin-sdk 未安装") from None

_spec = importlib.util.spec_from_file_location(
    "matrix_adapter", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
assert _spec is not None and _spec.loader is not None
_package = importlib.util.module_from_spec(_spec)
sys.modules["matrix_adapter"] = _package
_spec.loader.exec_module(_package)

from matrix_adapter.settings import AdapterSettings  # noqa: E402

# 模板里允许使用占位值，但仍需保留同名字段。
PLACEHOLDERS = {"matrix.user_id", "matrix.homeserver"}


def flatten(data: dict, prefix: str = "") -> dict:
    result = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            result.update(flatten(value, f"{path}."))
        else:
            result[path] = value
    return result


class ConfigTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        with (ROOT / "config.example.toml").open("rb") as stream:
            self.example = flatten(tomllib.load(stream))
        self.defaults = flatten(AdapterSettings().model_dump())

    def test_keys_match_model(self) -> None:
        self.assertEqual(set(self.example), set(self.defaults))

    def test_values_match_defaults(self) -> None:
        for key, value in self.defaults.items():
            if key in PLACEHOLDERS:
                continue
            self.assertEqual(self.example[key], value, key)

    def test_required_plugin_version(self) -> None:
        self.assertTrue(self.example["plugin.config_version"])
        self.assertIn("plugin.enabled", self.example)


if __name__ == "__main__":
    unittest.main()
