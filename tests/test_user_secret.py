#!/usr/bin/env python3
"""用户自填 Access Secret 功能测试。

验证「按请求 secret 优先于环境变量」的核心逻辑，用 mock 上游精确捕获
Authorization header，避免依赖知乎对无效 secret 的真实响应（其 quota 接口
对无效 secret 返回 200 + 空 Data 而非 401，不够确定）。
"""
import importlib.util
import pathlib
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

APP_PATH = pathlib.Path(__file__).resolve().parent.parent / "app.py"
spec = importlib.util.spec_from_file_location("wa", APP_PATH)
wa = importlib.util.module_from_spec(spec)
sys.modules["wa"] = wa
spec.loader.exec_module(wa)

MOCK_PORT, APP_PORT = 8911, 8912
RECEIVED = {"auth": None}


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
            RECEIVED["auth"] = self.headers.get("Authorization")
            return self._j(200, {"Data": [{"APIID": "x", "RemainingQuota": 1}]})
        self._j(404, {"error": "not_found"})


results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  | {detail}" if detail else ""))


# ---- 单元测试（不依赖服务）----
check("api_headers 显式 secret", wa.api_headers(secret="USER_X")["Authorization"] == "Bearer USER_X")
check("api_headers 回退环境变量", wa.api_headers()["Authorization"] == f"Bearer {wa.ACCESS_SECRET}")
check("api_headers 空 secret 回退", wa.api_headers(secret="")["Authorization"] == f"Bearer {wa.ACCESS_SECRET}")
check("secret_scope 不同 secret 隔离", wa.secret_scope("A") != wa.secret_scope("B"))
check("secret_scope 空 secret", wa.secret_scope("") == "none")
check("secret_scope 稳定", wa.secret_scope("SAME") == wa.secret_scope("SAME"))

# ---- 集成测试 ----
wa.ACCESS_SECRET = "env_secret_value"
wa.QUOTA_URL = f"http://127.0.0.1:{MOCK_PORT}/api/v1/quota"

mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockUpstream)
threading.Thread(target=mock.serve_forever, daemon=True).start()
app = ThreadingHTTPServer(("127.0.0.1", APP_PORT), wa.Handler)
threading.Thread(target=app.serve_forever, daemon=True).start()
import time
time.sleep(0.5)
APP = f"http://127.0.0.1:{APP_PORT}"

# 1. 无 cookie，quota 用环境变量
wa.CACHE._data.clear()
r = requests.get(f"{APP}/api/quota", timeout=10)
check("无 cookie 时 quota 用环境变量", RECEIVED["auth"] == "Bearer env_secret_value",
      RECEIVED["auth"])

# 2. 填用户 secret 后，quota 用用户 secret
s = requests.Session()
r = s.post(f"{APP}/api/secret", json={"secret": "user_secret_value"}, timeout=10)
check("存 secret 返回 ok", r.status_code == 200 and r.json().get("ok"))
check("存 secret 不返回明文", "user_secret_value" not in r.text)
check("存 secret 返回指纹", "fingerprint" in r.json())

wa.CACHE._data.clear()  # 清缓存，确保真实调用 mock
r = s.get(f"{APP}/api/quota", timeout=10)
check("填 secret 后 quota 用用户 secret", RECEIVED["auth"] == "Bearer user_secret_value",
      RECEIVED["auth"])

# 3. config 反映 user_secret_configured
cfg = s.get(f"{APP}/api/config", timeout=10).json()
check("config 反映用户 secret", cfg.get("user_secret_configured") is True)

# 4. 清除后回环境变量
r = s.post(f"{APP}/api/secret", json={"secret": ""}, timeout=10)
check("清除 secret", r.status_code == 200 and r.json().get("cleared") is True)
cfg = s.get(f"{APP}/api/config", timeout=10).json()
check("清除后 config 回 false", cfg.get("user_secret_configured") is False)
wa.CACHE._data.clear()
r = s.get(f"{APP}/api/quota", timeout=10)
check("清除后 quota 回环境变量", RECEIVED["auth"] == "Bearer env_secret_value",
      RECEIVED["auth"])

# 5. 缓存隔离：不同 secret 的 quota 缓存 key 不同
key_env = f"quota:{wa.secret_scope('env_secret_value')}"
key_usr = f"quota:{wa.secret_scope('user_secret_value')}"
check("缓存 key 按 secret 隔离", key_env != key_usr, f"{key_env} vs {key_usr}")

# 6. 过长 secret 被拒
r = s.post(f"{APP}/api/secret", json={"secret": "x" * 200}, timeout=10)
check("过长 secret 返回 400", r.status_code == 400)

mock.shutdown()
app.shutdown()

print("\n" + "=" * 60)
p = sum(1 for _, ok in results if ok)
print(f"{p}/{len(results)} passed")
bad = [n for n, ok in results if not ok]
if bad:
    print("FAILED:", bad)
sys.exit(0 if not bad else 1)
