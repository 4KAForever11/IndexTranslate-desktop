"""Core (non-UI) layer: config, API client, prompt building, storage."""

from .config import (
    APP_NAME,
    APP_PROJECT,
    APP_SLUG,
    VERSION,
    data_dir,
    resource_path,
    settings,
)

__all__ = [
    "APP_NAME",
    "APP_PROJECT",
    "APP_SLUG",
    "VERSION",
    "data_dir",
    "resource_path",
    "settings",
]
