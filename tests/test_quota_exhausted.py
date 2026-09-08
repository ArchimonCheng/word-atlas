#!/usr/bin/env python3
"""验证公共 secret 直答额度耗尽时，analyze 返回 429 quota_exhausted 引导自填。"""
import importlib.util
import pathlib
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

APP_PATH = pathlib.Path(__file__).resolve().parent.parent / "app.py"
spec = importlib.util.spec_from_file_location("wa", APP_PATH)
wa = importlib.util.module_from_spec(spec)
sys.modules["wa"] = wa
spec.loader.exec_module(wa)

MOCK_PORT, APP_PORT = 8921, 8922
CALLS = {"quota": 0, "search": 0}


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/v1/quota":
            CALLS["quota"] += 1
            # 直答额度耗尽（RemainingQuota=0）
            return self._j(200, {"Data": [
                {"APIID": "zhida_openai", "RemainingQuota": 0, "TotalQuota": 100},
                {"APIID": "global_search", "RemainingQuota": 5000, "TotalQuota": 5000},
            ]})
        if "zhihu_search" in p.path:
            CALLS["search"] += 1
            return self._j(200, {"Data": {"Items": [], "EmptyReason": ""}})
        self._j(404, {"error": "not_found"})


results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  | {detail}" if detail else ""))


wa.ACCESS_SECRET = "env_secret"
wa.QUOTA_URL = f"http://127.0.0.1:{MOCK_PORT}/api/v1/quota"
wa.ZHIHU_SEARCH_URL = f"http://127.0.0.1:{MOCK_PORT}/api/v1/content/zhihu_search"

mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockUpstream)
threading.Thread(target=mock.serve_forever, daemon=True).start()
app = ThreadingHTTPServer(("127.0.0.1", APP_PORT), wa.Handler)
threading.Thread(target=app.serve_forever, daemon=True).start()
time.sleep(0.5)
APP = f"http://127.0.0.1:{APP_PORT}"

# 1. 公共 secret（无 cookie）直答耗尽 → 429 quota_exhausted
wa.CACHE._data.clear()
r = requests.post(f"{APP}/api/analyze", json={"word": "test", "model": "fast"}, timeout=10)
j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
check("耗尽返回 429", r.status_code == 429, f"status={r.status_code}")
check("错误码 quota_exhausted", j.get("error") == "quota_exhausted", j.get("error"))
check("带引导 message", "请使用用户 Access Secret" in (j.get("message") or ""))
check("未发起直答/搜索（提前拦截）", CALLS["search"] == 0,
      f"search 调用 {CALLS['search']} 次")

# 2. 用户自填 secret 后，不触发公共额度检测（走用户额度，不被 429 拦截）
s = requests.Session()
s.post(f"{APP}/api/secret", json={"secret": "user_own_secret"}, timeout=10)
wa.CACHE._data.clear()
# 用户 secret 的 quota 也返回耗尽，但检测只针对公共 secret，所以应继续走到搜索
r = s.post(f"{APP}/api/analyze", json={"word": "test", "model": "fast"}, timeout=10)
# 走到搜索后，会继续发直答 POST —— mock 未处理 CHAT，这里只需确认没有被 429 拦截即可
check("自填 secret 不被公共额度检测拦截",
      not (r.status_code == 429 and r.headers.get("Content-Type", "").startswith("application/json")),
      f"status={r.status_code} content-type={r.headers.get('Content-Type')}")

mock.shutdown()
app.shutdown()

print("\n" + "=" * 60)
p = sum(1 for _, ok in results if ok)
print(f"{p}/{len(results)} passed")
bad = [n for n, ok in results if not ok]
if bad:
    print("FAILED:", bad)
sys.exit(0 if not bad else 1)
