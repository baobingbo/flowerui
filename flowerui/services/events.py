"""Celery 事件采集线程 + 状态持久化。"""
from __future__ import annotations

import logging
import shelve
import threading
import time
from typing import Optional

from celery.events import EventReceiver

from .state import EventsState, build_state

logger = logging.getLogger(__name__)


class Events(threading.Thread):
    """后台线程消费 Celery 事件流，写入 EventsState。"""

    events_enable_interval = 5.0  # 秒
    # 本进程是否真的广播过 enable_events。作为「我们改过 worker 状态」的凭据，
    # 供退出时判断是否允许还原；从未广播过就绝不能去 disable。
    events_enabled_by_us = False

    def __init__(
        self,
        capp,
        db: str = "flower.state",
        persistent: bool = False,
        enable_events: bool = True,
        state_save_interval: int = 0,
        max_workers: int = 5000,
        max_tasks: int = 100000,
    ):
        super().__init__(daemon=True, name="celery-events")
        self.capp = capp
        self.db = db
        self.persistent = persistent
        self.enable_events = enable_events
        self.state_save_interval = state_save_interval / 1000.0 if state_save_interval else 0

        self.state: Optional[EventsState] = None
        self._stop_flag = threading.Event()
        self._timers: list[threading.Thread] = []
        self.connected = False
        self.last_error: Optional[str] = None

        if self.persistent:
            self._load_state()
        if self.state is None:
            self.state = build_state(max_workers, max_tasks)

    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        try:
            logger.info("从 '%s' 恢复状态...", self.db)
            with shelve.open(self.db) as store:
                if "events" in store:
                    self.state = store["events"]
        except ModuleNotFoundError as exc:
            if "app" in str(exc):
                # 包从 app 改名为 flowerui 后，旧 flower.state 里的 pickle
                # 仍写着 app.* 模块路径，无法反序列化。历史状态只是缓存，
                # 这里重建即可；save_state() 会用新模块名覆盖该文件。
                logger.warning(
                    "检测到旧版（模块名为 app）状态文件 '%s'，已跳过历史状态并重建；"
                    "这是包重命名为 flowerui 后的一次性迁移，可删除该文件消除提示",
                    self.db,
                )
            else:
                logger.warning("恢复状态失败: %s", exc)
        except Exception as exc:
            logger.warning("恢复状态失败: %s", exc)

    def save_state(self) -> None:
        if not self.persistent:
            return
        try:
            with shelve.open(self.db, flag="n") as store:
                store["events"] = self.state
            logger.debug("状态已保存到 '%s'", self.db)
        except Exception as exc:
            logger.warning("保存状态失败: %s", exc)

    # ------------------------------------------------------------------
    def start(self) -> None:  # type: ignore[override]
        super().start()
        if self.enable_events:
            self._spawn_timer(self.events_enable_interval, self._enable_events)
        if self.state_save_interval:
            self._spawn_timer(self.state_save_interval, self.save_state)

    def _spawn_timer(self, interval: float, fn) -> None:
        def loop():
            while not self._stop_flag.wait(interval):
                try:
                    fn()
                except Exception as exc:
                    logger.debug("定时任务异常: %s", exc)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._timers.append(t)

    def _enable_events(self) -> None:
        """周期性开启 worker 事件上报，覆盖监控启动后才上线的 worker。"""
        try:
            self.capp.control.enable_events()
            self.events_enabled_by_us = True
        except Exception as exc:
            logger.debug("enable_events 失败: %s", exc)

    def disable_events(self) -> None:
        """广播 disable_events，还原 worker 的任务事件上报状态。

        这里不自行判断开关；调用方需同时确认本进程确实广播过 enable_events，
        避免把集群原本自带（-E / worker_send_task_events）的事件上报一并关掉。
        """
        try:
            self.capp.control.disable_events()
            self.events_enabled_by_us = False
            logger.info("已广播 disable_events，还原 worker 任务事件上报状态")
        except Exception as exc:
            logger.warning("disable_events 失败（worker 状态可能仍为开启）: %s", exc)

    def stop(self) -> None:
        self._stop_flag.set()
        if self.persistent:
            self.save_state()

    # ------------------------------------------------------------------
    def run(self) -> None:
        try_interval = 1.0
        while not self._stop_flag.is_set():
            try:
                try_interval *= 2
                with self.capp.connection() as conn:
                    recv = EventReceiver(
                        conn, handlers={"*": self.on_event}, app=self.capp
                    )
                    try_interval = 1.0
                    self.connected = True
                    self.last_error = None
                    logger.info("开始采集 Celery 事件...")
                    recv.capture(limit=None, timeout=None, wakeup=True)
            except (KeyboardInterrupt, SystemExit):
                return
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                logger.error(
                    "事件采集失败: %s, %.0f 秒后重试", exc, min(try_interval, 30)
                )
                if self._stop_flag.wait(min(try_interval, 30)):
                    return

    def on_event(self, event) -> None:
        try:
            self.state.event(event)
        except Exception as exc:
            logger.debug("处理事件失败: %s", exc)
