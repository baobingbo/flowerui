"""识别当前进程的部署形态（容器 / 编排器托管 / 裸机）。

「重启系统」必须据此选择策略：容器里服务通常就是主进程（PID 1），**让它退出
等于结束容器**，此时 fork 出来的继任者子进程会被一并杀掉。这类环境只能让
当前进程优雅退出，交由外部重启策略（Docker restart policy / k8s
restartPolicy / systemd / supervisord）重新拉起服务。

判定优先级：显式 ``FLOWERUI_IN_CONTAINER`` > Kubernetes / OCI 环境变量 >
``/.dockerenv`` > cgroup > PID 1。任一命中即认为处于容器中。

所有检测函数都做成可被 mock 的独立小函数，便于跨平台（含 Windows）回归测试。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

# 显式覆盖：容器里检测手段被裁剪、或宿主机误判时手动指定。
CONTAINER_ENV = "FLOWERUI_IN_CONTAINER"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

_CGROUP_HINTS = (
    "docker", "kubepods", "containerd", "cri-o", "crio",
    "lxc", "podman", "libpod", "/ecs/",
)


def _explicit_override() -> Optional[bool]:
    raw = os.environ.get(CONTAINER_ENV)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def _has_kubernetes_env() -> bool:
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def _has_oci_env() -> bool:
    # podman / systemd-nspawn / 部分 OCI 运行时会注入 container=...
    return bool(os.environ.get("container"))


def _dockerenv_exists() -> bool:
    # Windows 上 "/.dockerenv" 会被解析成当前盘符根目录下的文件，故限定 POSIX。
    if os.name != "posix":
        return False
    try:
        return Path("/.dockerenv").exists()
    except OSError:
        return False


def _cgroup_text() -> str:
    """读自身与 PID 1 的 cgroup；容器镜像里通常含 docker / kubepods 等字样。"""
    text = []
    for path in ("/proc/self/cgroup", "/proc/1/cgroup"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text.append(fh.read())
        except OSError:
            continue
    return "\n".join(text).lower()


def _cgroup_hint() -> str:
    text = _cgroup_text()
    for hint in _CGROUP_HINTS:
        if hint in text:
            return hint
    return ""


def _is_pid1() -> bool:
    """容器内以 CMD 直接启动服务时，它就是 PID 1。Windows 无此语义。"""
    return os.name != "nt" and os.getpid() == 1


def detect() -> Dict[str, object]:
    """返回判定结论与依据，便于日志排障。

    ``source`` 为 ``FLOWERUI_IN_CONTAINER`` 时表示用户显式指定，其余为自动探测。
    """
    explicit = _explicit_override()
    if explicit is not None:
        return {
            "container": explicit,
            "source": CONTAINER_ENV,
            "reasons": ["%s=%s" % (CONTAINER_ENV, os.environ.get(CONTAINER_ENV))],
        }

    reasons: List[str] = []
    if _has_kubernetes_env():
        reasons.append("KUBERNETES_SERVICE_HOST")
    if _has_oci_env():
        reasons.append("container 环境变量")
    if _dockerenv_exists():
        reasons.append("/.dockerenv")
    hint = _cgroup_hint()
    if hint:
        reasons.append("cgroup:" + hint)
    if _is_pid1():
        reasons.append("pid=1")

    return {"container": bool(reasons), "source": "auto", "reasons": reasons}


def is_container() -> bool:
    return bool(detect()["container"])
