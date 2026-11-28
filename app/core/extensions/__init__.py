"""轻核心侧的扩展管理（不依赖任何重型库）。"""
from __future__ import annotations

from .manifest import ExtensionManifest, ManifestError, is_newer, parse_version, version_tuple
from .manager import (
    ExtensionError,
    ExtensionManager,
    InstalledExtension,
    download_extension,
    sha256_file,
)
from .sidecar_client import SidecarClient, SidecarError, SidecarTimeout

__all__ = [
    "ExtensionError",
    "ExtensionManager",
    "ExtensionManifest",
    "InstalledExtension",
    "ManifestError",
    "SidecarClient",
    "SidecarError",
    "SidecarTimeout",
    "download_extension",
    "is_newer",
    "parse_version",
    "sha256_file",
    "version_tuple",
]
