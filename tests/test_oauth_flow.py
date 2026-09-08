#!/usr/bin/env python3
"""本地 OAuth 全流程测试：用 mock 上游验证 app.py 的 OAuth 代码路径。

验证范围（仅代码路径，不代表与知乎线上联调通过）：
  1. /auth/login 是否正确构造授权跳转
  2. 回调用 authorization_code 与 code 两种参数名都能接收
  3. token 交换的表单字段是否符合文档（grant_type 固定、字段名为 code）
  4. 会话建立、Cookie 属性、X-OAuth-Token 是否正确带到用户数据接口
  5. 无授权码、交换失败、token 失效等失败路径
"""
import importlib.util
import os
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

MOCK_PORT, APP_PORT = 8901, 8902
RECEIVED = {"token_form": None, "user_headers": None}
VALID_CODE = "mock_auth_code_abc123"
ISSUED_TOKEN = "mock_oauth_token_xyz789"


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
        if p.path == "/api/v1/user/contents":
            RECEIVED["user_headers"] = dict(self.headers)
            if self.headers.get("X-OAuth-Token") != ISSUED_TOKEN:
                return self._j(401, {"Code": 40100, "Message": "invalid oauth token"})
            return self._j(200, {"Code": 0, "Data": {"Items": [
                {"ContentType": "answer", "Url": "https://example.test/a/1",
                 "Title": "mock answer", "Summary": "s", "CreatedAt": 1,
                 "LikeCount": 0, "CommentCount": 0, "FavoriteCount": 0}],
                "Paging": {"IsEnd": True}}})
        self._j(404, {"error": "not_found"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/access_token":
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode()
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            RECEIVED["token_form"] = form
            if form.get("code") != VALID_CODE:
                return self._j(200, {"code": 40001, "message": "invalid code"})
            return self._j(200, {"code": 20000, "access_token": ISSUED_TOKEN,
                                 "expires_in": 3600})
        self._j(404, {"error": "not_found"})


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()


results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  | {detail}" if detail else ""))


mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockUpstream)
start(mock)

# 把 app 的上游指向 mock，并注入测试用 OAuth 配置
base = f"http://127.0.0.1:{MOCK_PORT}"
wa.OAUTH_API = base
wa.USER_CONTENTS_URL = f"{base}/api/v1/user/contents"
wa.APP_ID = "mock_app_id"
wa.APP_KEY = "mock_app_key"
wa.REDIRECT_URI = f"http://127.0.0.1:{APP_PORT}/auth/callback"
wa.ACCESS_SECRET = "mock_access_secret"

app = ThreadingHTTPServer(("127.0.0.1", APP_PORT), wa.Handler)
start(app)
time.sleep(0.6)
APP = f"http://127.0.0.1:{APP_PORT}"

# 1 授权跳转
r = requests.get(f"{APP}/auth/login", allow_redirects=False, timeout=10)
loc = r.headers.get("Location", "")
q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
check("login 返回 302", r.status_code == 302, f"status={r.status_code}")
check("跳转到 authorize 端点", "/authorize" in loc)
check("携带 app_id", q.get("app_id") == ["mock_app_id"])
check("response_type=code", q.get("response_type") == ["code"])
check("redirect_uri 与登记值一致", q.get("redirect_uri") == [wa.REDIRECT_URI])
check("发送了 state", bool(q.get("state", [""])[0]))

# 2 回调缺授权码
r = requests.get(f"{APP}/auth/callback", allow_redirects=False, timeout=10)
check("无授权码返回 400 且不换 token", r.status_code == 400 and RECEIVED["token_form"] is None,
      f"status={r.status_code}")

# 3 authorization_code 主参数
s = requests.Session()
r = s.get(f"{APP}/auth/callback?authorization_code={VALID_CODE}",
          allow_redirects=False, timeout=10)
check("回调 302 回首页", r.status_code == 302 and r.headers.get("Location") == "/")
form = RECEIVED["token_form"] or {}
check("token 表单 grant_type 固定值", form.get("grant_type") == "authorization_code", str(form.get("grant_type")))
check("token 表单字段名为 code（非 authorization_code）",
      form.get("code") == VALID_CODE and "authorization_code" not in form)
check("token 表单含 app_id/app_key", form.get("app_id") == "mock_app_id" and form.get("app_key") == "mock_app_key")
check("token 表单 redirect_uri 一致", form.get("redirect_uri") == wa.REDIRECT_URI)

sc = r.headers.get("Set-Cookie", "")
check("Cookie 设置 HttpOnly", "HttpOnly" in sc, sc.split(";")[0] if sc else "none")
check("Cookie 设置 SameSite", "SameSite" in sc)
check("Cookie 限定 Path", "Path=/" in sc)
check("Cookie 不含 OAuth token 明文", ISSUED_TOKEN not in sc)
# 本测试的回调是 http，按「默认跟随回调协议」不应带 Secure，否则浏览器会丢弃 cookie
check("http 回调下不带 Secure", "Secure" not in sc, sc)

# 4 已登录态
cfg = s.get(f"{APP}/api/config", timeout=10).json()
check("config 反映已登录", cfg.get("logged_in") is True and cfg.get("oauth_enabled") is True)

r = s.get(f"{APP}/api/me/contents", timeout=10)
check("已登录可读本人创作", r.status_code == 200, f"status={r.status_code}")
uh = RECEIVED["user_headers"] or {}
check("带 X-OAuth-Token", uh.get("X-OAuth-Token") == ISSUED_TOKEN)
check("带 Access Secret Bearer", uh.get("Authorization") == "Bearer mock_access_secret")
check("带 X-Request-Timestamp", (uh.get("X-Request-Timestamp") or "").isdigit())
check("App Key 未出现在任何请求头", not any("mock_app_key" in str(v) for v in uh.values()))
body = r.text
check("响应不含 OAuth token", ISSUED_TOKEN not in body)

# 5 兼容 code 参数名
s2 = requests.Session()
r = s2.get(f"{APP}/auth/callback?code={VALID_CODE}", allow_redirects=False, timeout=10)
check("兼容 code 参数名", r.status_code == 302)

# 6 交换失败
s3 = requests.Session()
r = s3.get(f"{APP}/auth/callback?authorization_code=WRONG", allow_redirects=False, timeout=10)
check("无 access_token 时报错不建会话", r.status_code == 502, f"status={r.status_code}")
check("失败后仍为未登录", s3.get(f"{APP}/api/config", timeout=10).json().get("logged_in") is False)

# 7 登出
r = s.post(f"{APP}/auth/logout", timeout=10)
check("登出成功", r.status_code == 200)
check("登出后未登录", s.get(f"{APP}/api/config", timeout=10).json().get("logged_in") is False)
check("登出后读创作 401", s.get(f"{APP}/api/me/contents", timeout=10).status_code == 401)

# 8 token 失效降级
sid = wa.new_session("stale_token_not_accepted", 3600)
s4 = requests.Session()
s4.cookies.set("wa_session", sid)
r = s4.get(f"{APP}/api/me/contents", timeout=10)
check("上游 401 时返回 401 且不回退本人账号", r.status_code == 401,
      f"status={r.status_code} body={r.text[:80]}")
check("失效后会话已清除", s4.get(f"{APP}/api/config", timeout=10).json().get("logged_in") is False)

# 9 会话过期
sid = wa.new_session("expiring", -1)
check("过期会话不可用", wa.get_session(sid) is None)

# 10 health 暴露 cookie_secure，便于部署后确认
hj = requests.get(f"{APP}/api/health", timeout=10).json()
check("health 暴露 cookie_secure", "cookie_secure" in hj,
      f"cookie_secure={hj.get('cookie_secure')}")
check("health 不含凭证明文",
      "mock_app_key" not in requests.get(f"{APP}/api/health", timeout=10).text)

# 11 COOKIE_SECURE 判定矩阵
# COOKIE_SECURE 在模块加载时确定，因此按不同环境变量各载入一份独立模块实例。
# 模块顶层只做变量赋值，不会启动服务，不影响上面正在运行的实例。
def load_with(env):
    saved = {k: os.environ.get(k) for k in
             ("COOKIE_SECURE", "ZHIHU_OAUTH_REDIRECT_URI")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(env)
        s = importlib.util.spec_from_file_location("wa_probe", APP_PATH)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
        return m
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


HTTPS_CB = {"ZHIHU_OAUTH_REDIRECT_URI": "https://demo.example/auth/callback"}
HTTP_CB = {"ZHIHU_OAUTH_REDIRECT_URI": "http://127.0.0.1:8902/auth/callback"}
matrix = [
    ("https 回调默认开启", HTTPS_CB, True),
    ("http 回调默认关闭", HTTP_CB, False),
    ("无回调默认关闭", {}, False),
    ("显式 1 覆盖 http", {**HTTP_CB, "COOKIE_SECURE": "1"}, True),
    ("显式 true", {"COOKIE_SECURE": "true"}, True),
    ("显式 0 覆盖 https", {**HTTPS_CB, "COOKIE_SECURE": "0"}, False),
    ("大写 HTTPS 回调", {"ZHIHU_OAUTH_REDIRECT_URI": "HTTPS://Demo/cb"}, True),
    ("无效值回落协议推断", {**HTTPS_CB, "COOKIE_SECURE": "maybe"}, True),
]
for name, env, want in matrix:
    m = load_with(env)
    set_c, clr_c = m.session_cookie("probe"), m.session_cookie(clear=True)
    ok = (m.COOKIE_SECURE == want
          and ("Secure" in set_c) == want
          and ("Secure" in clr_c) == want)   # 设置与清除必须一致
    check(f"COOKIE_SECURE：{name}", ok, f"cookie={set_c}")

m = load_with(HTTPS_CB)
check("https 下 Cookie 四个属性齐备",
      all(a in m.session_cookie("x")
          for a in ("Path=/", "HttpOnly", "SameSite=Lax", "Secure")),
      m.session_cookie("x"))

print("\n" + "=" * 60)
p = sum(1 for _, ok, _ in results if ok)
print(f"{p}/{len(results)} passed")
bad = [n for n, ok, _ in results if not ok]
if bad:
    print("FAILED:", bad)
sys.exit(0 if not bad else 1)
