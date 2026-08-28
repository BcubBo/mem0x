"""security/utils — 共享工具函数（standalone 版）。

路径优先级：环境变量 > ~/.mem0x/ > 项目目录
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("mem0x.security.utils")

# ── 路径配置 ──────────────────────────────────────────────────
HOME_DIR = Path.home()
MEM0X_HOME = Path(os.environ.get("MEM0X_HOME", str(HOME_DIR / ".mem0x")))
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _find_config() -> str:
    """按优先级查找 config.json。"""
    # 1. 环境变量
    env_path = os.environ.get("MEM0X_CONFIG")
    if env_path and os.path.exists(env_path):
        return env_path

    # 2. ~/.mem0x/config.json
    home_config = MEM0X_HOME / "config.json"
    if home_config.exists():
        return str(home_config)

    # 3. 项目目录/config.json
    project_config = PROJECT_DIR / "config.json"
    if project_config.exists():
        return str(project_config)

    raise FileNotFoundError("找不到 config.json")


# 配置文件路径
CONFIG_PATH = _find_config()


def get_user_id() -> str:
    """获取当前 user_id（环境变量 > config.json > 'default'）。"""
    env_id = os.environ.get("MEM0X_USER_ID")
    if env_id:
        return env_id
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return cfg.get("user_id", "default")
    except Exception as e:
        logger.debug("get_user_id config read failed: %s", e, exc_info=True)
        return "default"


def get_config() -> dict:
    """读取完整配置。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_data_dir() -> str:
    """获取数据目录。

    优先级：
    1. 环境变量 MEM0X_DATA_DIR
    2. config.json 中的 data_dir
    3. ~/.mem0x/data/
    """
    # 1. 环境变量
    env_dir = os.environ.get("MEM0X_DATA_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir

    # 2. config.json
    try:
        cfg = get_config()
        data_dir = cfg.get("data_dir")
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            return data_dir
    except Exception as e:
        logger.debug("get_data_dir config read failed: %s", e, exc_info=True)

    # 3. ~/.mem0x/data/
    default_dir = MEM0X_HOME / "data"
    os.makedirs(default_dir, exist_ok=True)
    return str(default_dir)
