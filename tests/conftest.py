import shutil
from pathlib import Path

import pytest

import server.config as config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """单元测试只加载公开默认值，不读取开发者的 dotenv 或连接外部服务。"""
    for key in list(config.os.environ):
        if key.startswith("AGENT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("APP_ENV", "testing")
    shutil.copytree(Path(config.ROOT) / "config", tmp_path / "config")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    config.get_settings.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()
