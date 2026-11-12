"""启动时一次性解析连接 / 全部配置（单一事实来源）。

设计目标（用户需求）
--------------------
1. **只在启动时解析一次**：不再把配置来源散落在 ``config.py`` 导入期、
   ``cli.py`` 覆盖装配、``runtime_config.load()`` 回放三处各自为政；本模块是
   进程启动时唯一决定「每个配置项最终取值」的地方。运行期不再重新解析连接配置。
2. **显式 if/elif/elif/else 级联**，优先级从高到低：

       用户 celery app.conf  >  FLOWER_* 环境变量  >  CLI 参数
       >  配置文件(flowerui.runtime.toml)  >  代码默认值

   （注：``flowerui-settings.toml`` 静态默认值在 ``config.py`` 导入期已被合入
   作为底，运行期若没有任何更高来源覆盖，最终落到它 / 代码默认值。）
3. **每个判断都输出「应用成功 / 失败」**：对每个配置项、每个来源打印
   ``✓ 采用 <值>`` 或 ``× 未采用``，并标明最终采用的来源，方便排障
   （例如一眼看出 broker 究竟来自 app.conf 还是环境变量还是配置文件）。

本模块只在进程启动（``main.lifespan`` 与 ``cli`` 的 dry-run）时被导入，
其内部依赖 ``config`` / ``runtime_config`` 等运行期模块；``cli`` 在 Celery
构建 ``--help`` 时也会导入它，因此这里**不引入任何会拖垮 CLI 的重依赖**
（仅标准库 + 项目内轻量模块）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import toml

from . import paths
from .core.config import settings
from .services import runtime_config

logger = logging.getLogger("flowerui.config")

# 与 cli.py 约定的环境变量（CLI 把解析到的 app.conf / CLI 覆盖写进这两个变量，
# 供 uvicorn --reload / --workers 子进程继承后回放）。
APP_CONF_ENV = "FLOWERUI_APP_CONF"

# 脱敏正则：仅用于日志展示，避免把 broker 密码打进终端。
_URL_PASSWORD_RE = re.compile(r"(://[^:@/?#]*:)([^@]+)(@)")


def _masked(value: Any) -> str:
    text = str(value)
    if "://" in text and "@" in text:
        return _URL_PASSWORD_RE.sub(r"\1******\3", text)
    return text


def _read_env_raw() -> Dict[str, str]:
    """原始 FLOWER_* 环境变量（键名转小写），用于级联判定。"""
    return {
        k[len("FLOWER_"):].lower(): v
        for k, v in os.environ.items()
        if k.startswith("FLOWER_")
    }


def _read_file_source() -> Dict[str, Any]:
    """读取「配置文件」来源的原始键值：仅 ``flowerui.runtime.toml``。

    这是用户通过 Web 界面或手工编辑后落盘的「运行时配置文件」，也是级联里
    唯一的「配置文件」来源。静态默认值文件 ``flowerui-settings.toml`` 已在
    ``core.config`` 导入期被合入 ``Settings`` 作为基线（级联的「默认值」分支
    读的就是这个基线），在此不再重复读取，否则会把它当成"来自配置文件"反复报告。

    注意：runtime.toml 里「与基线相等」的值并不算真正的文件覆盖（见 ``resolve``
    里的基线比较过滤），避免把"没改过的默认值"误报成"来自配置文件"。
    """
    data: Dict[str, Any] = {}
    path = paths.runtime_file()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                parsed = toml.load(f) or {}
            for k, v in parsed.items():
                if isinstance(k, str) and k.strip():
                    data[k.strip()] = v
        except Exception as exc:  # 解析失败不阻断启动，交由后续默认兜底
            logger.warning("读取运行时配置文件失败 %s: %s", path, exc)
    return data


def _baseline_value(key: str) -> Any:
    """取字段的导入期基线值（settings_toml 合入 + 代码默认值）。

    级联的「默认值」分支返回的就是它；用于判断 runtime.toml 里的值是否真的
    偏离了基线——等于基线的不算"来自配置文件"，回落到默认值。
    """
    if key in runtime_config._UI_DEFAULTS:
        return runtime_config._ui_state.get(key, runtime_config._UI_DEFAULTS[key])
    return getattr(settings, key, None)


def _file_overrides_baseline(key: str, raw: Any) -> bool:
    """runtime.toml 中的值是否真的偏离基线（否则不应算"来自配置文件"）。"""
    field = runtime_config.FIELD_MAP.get(key)
    if field is None:
        return True  # 未知字段：当作生效，交由 _apply_base 处理
    try:
        coerced = runtime_config._coerce(field, raw)
    except Exception:
        return True  # 无法判定就当作生效，交由 _apply_base 处理
    baseline = _baseline_value(key)
    try:
        return coerced != baseline
    except Exception:
        return True


def _usable(value: Any) -> bool:
    """该来源提供的值是否「有效」（非空 / 非空容器）。"""
    if value is None:
        return False
    if isinstance(value, (dict, list)) and not value:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _apply_base(key: str, value: Any) -> bool:
    """把最终取值落到 settings（并触发副作用钩子），不记录为 Web 编辑。

    与 ``runtime_config.update`` 的区别：不写 ``_overrides``、不落盘——
    这些值是启动期由更高优先级来源决定的「底配置」，Web 编辑（PUT /api/config）
    仍走 ``update`` 并独立记录，二者互不污染；``reset`` 也只回退 Web 编辑。
    """
    field = runtime_config.FIELD_MAP.get(key)
    if field is None:
        return False
    try:
        val = runtime_config._coerce(field, value)
        runtime_config._validate_range(field, val)
    except Exception as exc:  # 单个值非法不应拖垮整体启动
        logger.warning("配置项 %s 取值非法（%s），已忽略并回退默认", key, exc)
        return False
    if key in runtime_config._UI_DEFAULTS:
        runtime_config._ui_state[key] = val
    else:
        setattr(settings, key, val)
    for fn in runtime_config._hooks.get(key, []):
        try:
            fn(val)
        except Exception:  # noqa: BLE001 - 钩子失败不影响主流程
            logger.debug("配置钩子执行失败: %s", key)
    return True


def _fmt_line(key: str, verdicts: Dict[str, Any], win_src: str) -> str:
    """生成单条配置项的来源判定行：每个来源 ✓/×，并标明最终采用源。"""
    parts = []
    for src in ("app.conf", "环境变量", "CLI 参数", "配置文件"):
        v = verdicts.get(src)
        if v is None:
            parts.append("%s=×" % src)
        else:
            parts.append("%s=✓%s" % (src, _masked(v)))
    return ("[配置解析] %-26s %s  → 采用[%s]"
            % (key, " | ".join(parts), win_src))


def resolve(
    app_conf: Optional[Dict[str, Any]] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
    *,
    log: bool = True,
) -> Tuple[Dict[str, str], List[str]]:
    """启动时一次性解析全部配置项（if/elif/elif/else 级联）。

    优先级（高 → 低）：
        用户 celery app.conf  >  FLOWER_* 环境变量  >  CLI 参数
        >  flowerui.runtime.toml  >  代码默认值

    参数
    ----
    app_conf:       来自用户 celery app 的显式配置（由 ``cli`` 解析后传入；
                    子进程从 ``FLOWERUI_APP_CONF`` 读回）。
    cli_overrides:  命令行 --option / --set / 位置参数 覆盖（已剔除被环境变量
                    覆盖的键）；子进程从 ``FLOWERUI_CLI_OVERRIDES`` 读回。
    log:            是否用 logger.info 输出每条判定。

    返回
    ----
    ``(summary, lines)``：summary 为 ``{key: 最终来源}``，lines 为每条判定文本。
    """
    if app_conf is None:
        raw = os.environ.get(APP_CONF_ENV)
        if raw:
            try:
                app_conf = json.loads(raw)
            except (TypeError, ValueError):
                app_conf = {}
    if cli_overrides is None:
        raw = os.environ.get(runtime_config.CLI_OVERRIDES_ENV)
        if raw:
            try:
                cli_overrides = json.loads(raw)
            except (TypeError, ValueError):
                cli_overrides = {}

    app_conf = dict(app_conf or {})
    cli_overrides = dict(cli_overrides or {})
    env_raw = _read_env_raw()
    runtime_raw = _read_file_source()

    summary: Dict[str, str] = {}
    lines: List[str] = []

    for f in runtime_config.FIELDS:
        key = f.key
        winner: Any = None
        win_src: Optional[str] = None
        verdicts: Dict[str, Any] = {}

        # 1) 用户 celery app.conf（最高优先级）
        if key in app_conf and _usable(app_conf[key]):
            winner, win_src = app_conf[key], "app.conf"
        # 2) FLOWER_* 环境变量
        elif key in env_raw and _usable(env_raw[key]):
            winner, win_src = env_raw[key], "环境变量"
        # 3) CLI 参数（置于环境变量之后）
        elif key in cli_overrides and _usable(cli_overrides[key]):
            winner, win_src = cli_overrides[key], "CLI 参数"
        # 4) 配置文件（flowerui.runtime.toml，用户运行时编辑层）
        #    仅当该值真的偏离基线时才算"来自配置文件"，否则回落默认值分支，
        #    避免把"没改过的默认值"误报成"来自配置文件"。
        elif (
            key in runtime_raw
            and _usable(runtime_raw[key])
            and _file_overrides_baseline(key, runtime_raw[key])
        ):
            winner, win_src = runtime_raw[key], "配置文件"
        # 5) 代码默认值（含 flowerui-settings.toml 静态默认已在导入期合入）
        else:
            winner, win_src = getattr(settings, key, None), "默认值"

        # 记录各来源判定（成功 / 失败）
        for src in ("app.conf", "环境变量", "CLI 参数", "配置文件"):
            if win_src == src:
                verdicts[src] = winner
            else:
                verdicts[src] = None

        # 应用最终值（除默认值分支外都显式落盘到 settings）
        if win_src != "默认值":
            _apply_base(key, winner)
        summary[key] = win_src
        lines.append(_fmt_line(key, verdicts, win_src))

    if log:
        logger.info("===== 启动配置解析（仅此一次，级联来源判定）=====")
        for ln in lines:
            logger.info(ln)
        logger.info("==============================================")
    return summary, lines


# 进程级一次性解析缓存。继任者重启进程会先自行解析拿到 host/port，
# 随后 uvicorn 加载 ``flowerui.main`` 时 lifespan 又调一次；若让它重算，
# 就会把「上一次刚应用的值」当成基线，把 runtime.toml 里真正生效的配置
# 误报成「默认值」。配置本来就只该在启动时解析一次，这里做进程级幂等。
_PROCESS_RESOLVE_CACHE: Optional[Tuple[Dict[str, str], List[str]]] = None


def resolve_from_cli_env(
    *, log: bool = True, force: bool = False
) -> Tuple[Dict[str, str], List[str]]:
    """仅依赖环境变量（FLOWERUI_APP_CONF / FLOWERUI_CLI_OVERRIDES）解析。

    供 ``main.lifespan`` 在任意进程（含 reload / workers 子进程）里调用，
    无需传入 app / ctx。同一个进程内只解析一次并返回缓存结果；
    ``force=True`` 可显式重算（一般不需要）。
    """
    global _PROCESS_RESOLVE_CACHE
    if _PROCESS_RESOLVE_CACHE is not None and not force:
        return _PROCESS_RESOLVE_CACHE
    result = resolve(app_conf=None, cli_overrides=None, log=log)
    _PROCESS_RESOLVE_CACHE = result
    return result
