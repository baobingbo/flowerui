#!/usr/bin/env python3
"""
FlowerUI 构建脚本 - 打包检查 + wheel/sdist 构建 + 可选 PyPI / TestPyPI 上传。

所有输出使用纯 ASCII（避免 Windows GBK 控制台编码崩溃）。
所有路径锚定到项目根目录（脚本所在目录），不依赖调用方 cwd。

用法:
    python build.py                  # 检查 + 构建 + 交互选择上传平台
    python build.py --no-upload      # 只构建，跳过上传
    python build.py --repo pypi      # 直接上传到官方 PyPI
    python build.py --repo testpypi  # 直接上传到 TestPyPI（强烈建议先走这步验证管线）

凭据与平台:
    读取项目根或用户主目录的 .pypirc（[distutils] index-servers 列出平台名）。
    .pypirc 含真实 token，已被 .gitignore 忽略，且被 MANIFEST.in 排除，
    因此既不会进 git 仓库，也不会进上传到 PyPI 的分发包 —— 切勿手动提交它！
    也可用环境变量注入：
        TWINE_USERNAME / TWINE_PASSWORD / TWINE_REPOSITORY_URL
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
# FlowerUI 为扁平布局：包 flowerui/ 位于仓库根（非 src/ 子目录）
PACKAGE_DIR = PROJECT_ROOT / "flowerui"
PACKAGE_NAME = "flowerui"
DIST_DIR = PROJECT_ROOT / "dist"


def check_project() -> bool:
    """检查打包所需的关键文件是否存在。"""
    print("=" * 60)
    print("Project checks")
    print("=" * 60)

    ok = True
    for label, path in (
        ("pyproject.toml", PROJECT_ROOT / "pyproject.toml"),
        ("README.md", PROJECT_ROOT / "README.md"),
        (f"{PACKAGE_NAME} package", PACKAGE_DIR),
        (f"{PACKAGE_NAME}/__init__.py", PACKAGE_DIR / "__init__.py"),
    ):
        if path.exists():
            print(f"[OK]   {label}: {path}")
        else:
            print(f"[MISS] {label}: {path}")
            ok = False

    py_files = sorted(PACKAGE_DIR.rglob("*.py")) if PACKAGE_DIR.exists() else []
    print(f"[INFO] {len(py_files)} .py files under {PACKAGE_NAME}/")
    for f in py_files[:10]:
        print(f"       - {f.relative_to(PROJECT_ROOT)}")
    if len(py_files) > 10:
        print(f"       ... and {len(py_files) - 10} more")

    c_files = list(PACKAGE_DIR.rglob("*.c")) if PACKAGE_DIR.exists() else []
    if c_files:
        print(f"[WARN] unexpected C sources: {[str(f) for f in c_files]}")
    else:
        print("[INFO] no C sources (pure Python package)")
    return ok


def build_package() -> bool:
    """清理旧产物并构建 wheel + sdist，再校验内容与敏感文件泄漏。"""
    print()
    print("=" * 60)
    print("Building sdist + wheel")
    print("=" * 60)

    for target in (PROJECT_ROOT / "build", DIST_DIR):
        if target.exists():
            shutil.rmtree(target)
            print(f"[OK]   removed {target.name}/")

    for egg_info in glob.glob(str(PROJECT_ROOT / "*.egg-info")):
        shutil.rmtree(egg_info)
        print(f"[OK]   removed {Path(egg_info).name}/")

    # 优先 uv build（本项目用 uv 管理）；失败回退 python -m build
    result = subprocess.run(
        ["uv", "build"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("[INFO] uv build unavailable/failed, falling back to python -m build")
        result = subprocess.run(
            [sys.executable, "-m", "build"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        print("[FAIL] build failed:")
        print(result.stderr)
        return False

    print("[OK]   build succeeded")
    for f in sorted(DIST_DIR.iterdir()) if DIST_DIR.exists() else []:
        print(f"       - {f.name} ({f.stat().st_size} bytes)")

    # 校验 wheel：必须包含包源码，不能混入构建垃圾
    wheels = sorted(DIST_DIR.glob("*.whl")) if DIST_DIR.exists() else []
    if not wheels:
        print("[FAIL] no wheel produced")
        return False
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
        has_pkg = any(n.startswith(f"{PACKAGE_NAME}/") and n.endswith(".py") for n in names)
        junk = [n for n in names if n.endswith((".pyc", ".c")) or "/build/" in n]
        if not has_pkg:
            print(f"[FAIL] {wheel.name}: package sources missing")
            return False
        if junk:
            print(f"[FAIL] {wheel.name}: junk files inside: {junk}")
            return False
        print(f"[OK]   {wheel.name}: {len(names)} entries, package sources verified")

    # 校验 sdist：绝不能泄漏密钥/运行时文件
    sdists = sorted(DIST_DIR.glob("*.tar.gz")) if DIST_DIR.exists() else []
    for sd in sdists:
        with tarfile.open(sd) as tf:
            snames = tf.getnames()
        leak = [
            n
            for n in snames
            if any(k in n for k in (".pypirc", "runtime.toml", "flower.state", ".env", "__pycache__", ".git"))
        ]
        if leak:
            print(f"[FAIL] {sd.name}: sensitive files leaked into sdist: {leak}")
            return False
        print(f"[OK]   {sd.name}: no sensitive files ({len(snames)} entries)")
    return True


def load_pypirc_repos() -> dict:
    """解析 .pypirc（项目根优先，其次主目录），返回 {平台名: 上传URL}（保持配置顺序）。"""
    import configparser

    for pypirc in (PROJECT_ROOT / ".pypirc", Path.home() / ".pypirc"):
        if not pypirc.exists():
            continue
        cp = configparser.ConfigParser()
        try:
            cp.read(pypirc, encoding="utf-8")
        except configparser.Error as e:
            print(f"[WARN] cannot parse {pypirc}: {e}")
            return {}
        names = []
        if cp.has_section("distutils") and cp.has_option("distutils", "index-servers"):
            names = [n.strip() for n in cp.get("distutils", "index-servers").split() if n.strip()]
        repos = {n: (cp.get(n, "repository", fallback="").strip() or "(no repository URL)") for n in names}
        if repos:
            return repos
    return {}


def describe_upload_target(repo: str | None) -> str:
    """探测并描述 twine 将上传到的平台/仓库。"""
    if repo:
        url = load_pypirc_repos().get(repo)
        return f"repository '{repo}'" + (f" -> {url}" if url else " (not found in .pypirc)")
    if os.environ.get("TWINE_PASSWORD"):
        return "via TWINE_PASSWORD env (official PyPI by default)"
    for pypirc in (PROJECT_ROOT / ".pypirc", Path.home() / ".pypirc"):
        if pypirc.exists():
            return f".pypirc configured: {pypirc}"
    return "official PyPI (pypi.org) - no credentials found"


def _pypirc_repo_config(repo: str | None) -> dict | None:
    """从 .pypirc（项目根优先，其次主目录）读取指定仓库的完整配置。"""
    import configparser

    if not repo:
        return None
    for pypirc in (PROJECT_ROOT / ".pypirc", Path.home() / ".pypirc"):
        if not pypirc.exists():
            continue
        cp = configparser.ConfigParser()
        try:
            cp.read(pypirc, encoding="utf-8")
        except configparser.Error:
            continue
        if cp.has_section(repo):
            return {
                "pypirc_path": str(pypirc),
                "repository": cp.get(repo, "repository", fallback="").strip(),
                "username": cp.get(repo, "username", fallback="").strip(),
                "password": cp.get(repo, "password", fallback="").strip(),
            }
    return None


def _twine_cmd() -> list:
    """选择 twine 调用方式：优先 uvx twine（免安装），其次 twine，再次 python -m twine。"""
    if shutil.which("uv"):
        return ["uvx", "twine"]
    if shutil.which("twine"):
        return ["twine"]
    return [sys.executable, "-m", "twine"]


def upload_to_pypi(repo: str | None = None) -> None:
    """将 dist 中真实存在的分发文件上传到 twine 仓库（需 .pypirc 或环境变量凭据）。"""
    print()
    print("Uploading...")
    print(f"[INFO] target: {describe_upload_target(repo)}")
    files = sorted(glob.glob(str(DIST_DIR / "*.whl"))) + sorted(glob.glob(str(DIST_DIR / "*.tar.gz")))
    if not files:
        print("[FAIL] nothing to upload (dist/ is empty)")
        return
    cfg = _pypirc_repo_config(repo)
    if cfg and cfg["password"].startswith("<") and cfg["password"].endswith(">"):
        print(f"[FAIL] .pypirc repository '{repo}' password is still a placeholder (<...>)")
        print("       create an API token and fill it into .pypirc, then retry:")
        print("       pypi      -> https://pypi.org/manage/account/token/")
        print("       pypi_test -> https://test.pypi.org/manage/account/token/")
        return
    print(f"[INFO] uploading: {[Path(f).name for f in files]}")
    # --disable-progress-bar: rich 进度条在 Windows GBK 控制台会 UnicodeEncodeError
    cmd = _twine_cmd() + ["upload", "--non-interactive", "--disable-progress-bar"]
    env = os.environ.copy()
    if cfg:
        # 通过环境变量注入凭据，避免依赖 twine 读取 ~/.pypirc 的路径限制
        env["TWINE_USERNAME"] = cfg["username"]
        env["TWINE_PASSWORD"] = cfg["password"]
        if cfg.get("repository"):
            env["TWINE_REPOSITORY_URL"] = cfg["repository"]
    cmd += files
    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False, env=env)
    except FileNotFoundError:
        print("[FAIL] twine not found; install it with: pip install twine (or use uvx)")
        return
    if result.returncode == 0:
        print("[OK]   upload succeeded")
        if result.stdout.strip():
            print(result.stdout)
    else:
        print("[FAIL] upload failed (twine output):")
        for stream in (result.stdout, result.stderr):
            if stream.strip():
                print(stream.rstrip())
        blob = result.stdout + result.stderr
        if "timed out" in blob or "NameResolutionError" in result.stderr:
            print("       hint: network unreachable - check proxy/VPN, then retry")
        if "403" in blob or "401" in blob:
            print("       hint: authentication rejected by the server.")
            print("       - pypi 与 testpypi 的 token 不通用，需在各自平台单独创建:")
            print("         pypi      -> https://pypi.org/manage/account/token/")
            print("         testpypi -> https://test.pypi.org/manage/account/token/")
        if "409" in blob or "already exists" in blob:
            print("       hint: 该版本号已存在且不可覆盖；请先在 pyproject.toml 升版本号再传。")


def _read_line(timeout: int) -> str | None:
    """读取一行输入；超时或 EOF 返回 None。"""
    user_input: dict = {"value": None}

    def _read_input() -> None:
        try:
            user_input["value"] = input().strip()
        except EOFError:
            user_input["value"] = None

    t = threading.Thread(target=_read_input, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None
    return user_input["value"]


def prompt_upload_to_pypi(repo: str | None = None, timeout: int = 30) -> None:
    """构建完成后选择上传平台（已给 --repo 直接上传；否则交互选择）。"""
    repos = load_pypirc_repos()
    if repo:
        if repos and repo not in repos:
            print(f"\n[FAIL] --repo '{repo}' not in .pypirc (available: {', '.join(repos)})")
            return
        upload_to_pypi(repo)
        return
    print()
    if repos:
        print("Build complete. Choose upload platform:")
        for i, (name, url) in enumerate(repos.items(), 1):
            print(f"  {i}) {name:<10} -> {url}")
    else:
        print("Build complete. No .pypirc found; default target is official PyPI (needs token).")
    print(f"  Enter a number/name to upload, [n] to skip (default, auto-cancel after {timeout}s)")
    answer = _read_line(timeout)
    if answer is None:
        print("No input, upload skipped.")
        return
    a = answer.strip().lower()
    if not a or a in ("n", "q", "skip"):
        print("Upload skipped.")
        return
    target: str | None = None
    if a.isdigit():
        idx = int(a)
        if 1 <= idx <= len(repos):
            target = list(repos)[idx - 1]
    elif a in repos:
        target = a
    if target is None:
        print(f"[FAIL] invalid choice '{answer}', upload skipped.")
        return
    upload_to_pypi(target)


def main(argv: list) -> int:
    repo = None
    if "--repo" in argv:
        i = argv.index("--repo")
        if i + 1 >= len(argv):
            print("[FAIL] --repo requires a repository name (from .pypirc)")
            return 1
        repo = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if not check_project():
        print()
        print("Project checks failed; aborting.")
        return 1
    if not build_package():
        return 1
    if "--no-upload" not in argv:
        prompt_upload_to_pypi(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
