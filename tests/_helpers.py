"""共享测试假件（headless 复用，零第三方依赖，仅 stdlib unittest）。

``FakeWebView`` 抽取自 test_header_title_bridge.py 与 test_history_close_protocol.py
各自内联的同类副本（统一属性名 ``calls``，旧 ``js_calls`` 均已迁移至此）。
"""


class FakeWebView:
    """记录 ``run_javascript`` 调用的假 WebView（统一属性名 ``calls``）。"""

    def __init__(self):
        self.calls = []

    def run_javascript(self, js, *args):
        self.calls.append(js)
