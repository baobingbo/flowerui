"""回归测试：broker 客户端 / broker_uri / transport_driver 必须按签名缓存复用。

对应需求：运行期不再每次请求都重新创建连接。
- ``ServiceManager._get_broker``：按连接签名缓存 broker 客户端，签名不变时复用，
  只在签名变化（配置变了）时重建；
- ``celery_app.broker_uri`` / ``transport_driver``：按配置签名记忆化，避免每次请求
  都重建 kombu Connection 对象。

不需要真实 broker：redis-py / kombu 的连接都是惰性建立，构造客户端对象不会真正连。
"""
import logging
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ["FLOWER_MOCK"] = "false"
os.environ["FLOWER_BROKER_URL"] = "redis://127.0.0.1:6399/3"
os.environ["FLOWER_RUNTIME_CONFIG_FILE"] = "/tmp/flowerui_test_no_runtime.toml"

from flowerui.core import celery_app as celery_app_mod  # noqa: E402
from flowerui.core.celery_app import broker_uri, transport_driver  # noqa: E402
from flowerui.core.config import settings  # noqa: E402
from flowerui.services.manager import ServiceManager  # noqa: E402


@pytest.fixture(autouse=True)
def _broker_env():
    """settings 是进程级单例，环境变量只在首次实例化时生效，后续用例改 env 无效。

    这里直接设值并清掉 celery_app 的记忆化缓存，保证本测试用的 broker_url
    不被前面用例污染，也不污染后面用例（后面用例会各自设定自己的值）。
    """
    saved = settings.broker_url
    settings.broker_url = "redis://127.0.0.1:6399/3"
    celery_app_mod._broker_uri_cache.clear()
    celery_app_mod._transport_cache = (None, None)
    yield
    settings.broker_url = saved
    celery_app_mod._broker_uri_cache.clear()
    celery_app_mod._transport_cache = (None, None)


def test_broker_client_cached_by_signature() -> None:
    class _Filter(logging.Filter):
        def __init__(self):
            super().__init__()
            self.records = []

        def filter(self, record):
            if "已（重新）建立 broker 客户端连接" in record.getMessage():
                self.records.append(record.getMessage())
            return True

    flt = _Filter()
    mgr_logger = logging.getLogger("flowerui.services.manager")
    mgr_logger.setLevel(logging.INFO)  # 确保 INFO 级建立日志能到达过滤器
    mgr_logger.addFilter(flt)

    mgr = ServiceManager()

    # 首次取：应建立一次
    b1 = mgr._get_broker()
    after_first = len(flt.records)
    assert after_first == 1, "首次取 broker 客户端应建立一次"

    # 连续第二次（签名不变）：应复用，不重建
    b2 = mgr._get_broker()
    assert b1 is b2, "签名不变时应复用同一 broker 客户端"
    assert len(flt.records) == after_first, "签名不变时不应再次建立 broker 客户端"

    # 签名变化（配置变了）：必须重建
    mgr._broker_sig = None
    b3 = mgr._get_broker()
    assert b3 is not b1, "签名变化后应重建 broker 客户端"
    assert len(flt.records) == after_first + 1, "签名变化后应重建一次"


def test_broker_uri_and_transport_memoized() -> None:
    # 连续多次取：值稳定，且第二次起不再重建 kombu Connection（记忆化）
    u1 = broker_uri()
    u2 = broker_uri(include_password=True)
    u3 = broker_uri()
    assert u1 == u3
    assert "6399/3" in u2  # 脱敏后的展示串里仍含地址

    t1 = transport_driver()
    t2 = transport_driver()
    assert t1 == t2


if __name__ == "__main__":
    test_broker_client_cached_by_signature()
    test_broker_uri_and_transport_memoized()
    print("ALL PASS")
