"""为 Workers 生成独立 Pyodide 依赖锁。"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from sync_workers import compatibility_settings


def _temp_root(root: Path) -> Path:
    path = root / "tmp"
    path.mkdir(exist_ok=True)
    return path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    from pywrangler import utils

    utils._parse_wrangler_config = lambda: compatibility_settings(root)
    utils.get_python_version.cache_clear()
    dependencies = [
        "flask>=3.1.3",
        "pyyaml>=6.0.3",
        "ruamel-yaml>=0.18.6",
        "tomlkit>=0.13.3",
        "workers-runtime-sdk>=1.9.6",
    ]
    with tempfile.TemporaryDirectory(dir=_temp_root(root)) as directory:
        requirements = Path(directory) / "workers-requirements.txt"
        requirements.write_text("\n".join(dependencies), encoding="utf-8")
        subprocess.run(
            [
                "uv",
                "pip",
                "compile",
                str(requirements),
                "--python",
                utils.get_uv_pyodide_interp_name(),
                "--extra-index-url",
                utils.get_pyodide_index(),
                "--index-strategy",
                "unsafe-best-match",
                "--no-build",
                "--no-header",
                "-o",
                str(root / "pylock.toml"),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
