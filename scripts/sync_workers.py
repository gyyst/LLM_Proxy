"""Python Workers 依赖安装。

以独立 pylock.toml 隔离 Linux 服务依赖和 Pyodide 依赖。Cloudflare 项目
统一由 cf 管理；workers-py 仅负责按锁文件构建与安装 Python 包。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def compatibility_settings(root: Path) -> dict[str, object]:
    """从唯一 Cloudflare 配置读取 Python 运行时参数。"""
    source = (root / "cloudflare.config.ts").read_text(encoding="utf-8")
    date_match = re.search(r'compatibilityDate:\s*"(\d{4}-\d{2}-\d{2})"', source)
    flags_match = re.search(r"compatibilityFlags:\s*\[([^]]*)\]", source)
    if date_match is None or flags_match is None:
        raise RuntimeError("Cloudflare config must declare compatibilityDate and compatibilityFlags")
    flags = re.findall(r'"([a-z0-9_]+)"', flags_match.group(1))
    return {"compatibility_date": date_match.group(1), "compatibility_flags": flags}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    from pywrangler import utils
    from pywrangler.resolve import InstallPlan
    from pywrangler.sync import _install_requirements_to_vendor, create_pyodide_venv, create_workers_venv

    settings = compatibility_settings(root)
    utils._parse_wrangler_config = lambda: settings
    utils.get_python_version.cache_clear()
    create_workers_venv()
    create_pyodide_venv()
    lockfile = root / "pylock.toml"
    if not lockfile.exists():
        raise RuntimeError("Run uv run python scripts/lock_workers.py to update the Workers dependency lock")
    error = _install_requirements_to_vendor(InstallPlan(lockfile))
    if error:
        raise RuntimeError(f"Workers package installation failed: {error}")
    subprocess.run(["npm", "--prefix", "frontend", "run", "build"], cwd=root, check=True)
    legacy_assets = root / "workers/static/app"
    if legacy_assets.exists():
        shutil.rmtree(legacy_assets)
    asset_root = root / "workers/static/static/app"
    if asset_root.exists():
        shutil.rmtree(asset_root)
    asset_root.mkdir(parents=True, exist_ok=True)
    for source in (root / "src/presentation/static/app").iterdir():
        target = asset_root / source.name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    for name in ("src", "hooks"):
        target = root / "workers" / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(root / name, target)


if __name__ == "__main__":
    main()
