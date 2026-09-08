#!/usr/bin/env python3
"""Word Atlas - 词汇图谱

把「词汇深度分析法」Skill 转成线上作品：用户用知乎账号登录，输入一个英文单词，
由知乎直答按四维框架生成结构化词汇档案，并用知乎搜索补充社区原始来源。

凭证边界（见 references/hackathon-oauth.md）：
  - ZHIHU_ACCESS_SECRET  开放平台调用方鉴权，仅后端使用
  - ZHIHU_OAUTH_APP_KEY  换取 OAuth Token，仅后端使用
  - OAuth access_token   存服务端会话，绝不下发前端
三者都不写入源码、URL、日志与前端响应。
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

OPEN_API = "https://developer.zhihu.com"
OAUTH_API = "https://openapi.zhihu.com"

CHAT_URL = f"{OPEN_API}/v1/chat/completions"
ZHIHU_SEARCH_URL = f"{OPEN_API}/api/v1/content/zhihu_search"
GLOBAL_SEARCH_URL = f"{OPEN_API}/api/v1/content/global_search"
QUOTA_URL = f"{OPEN_API}/api/v1/quota"
USER_CONTENTS_URL = f"{OPEN_API}/api/v1/user/contents"

# 全网搜索 ContentText 用 <em> 标注命中词，前端统一转义，这里先剥掉
EM_TAGS = ("<em>", "</em>")
AUTHORITY = {"1": "低权威", "2": "中权威", "3": "高权威", "4": "超高权威"}

ACCESS_SECRET = os.environ.get("ZHIHU_ACCESS_SECRET", "").strip()
APP_ID = os.environ.get("ZHIHU_OAUTH_APP_ID", "").strip()
APP_KEY = os.environ.get("ZHIHU_OAUTH_APP_KEY", "").strip()
REDIRECT_URI = os.environ.get("ZHIHU_OAUTH_REDIRECT_URI", "").strip()
PORT = int(os.environ.get("PORT", "8787"))
# 默认只监听回环：/api/analyze 无鉴权且会消耗开放平台额度，
# 对外暴露前应置于反向代理或平台网关之后，再显式设置 HOST=0.0.0.0。
HOST = os.environ.get("HOST", "127.0.0.1").strip()

# 会话 Cookie 的 Secure 属性。默认跟随回调地址协议：
# 线上回调是 https 时自动开启，本地 http 调试保持可用。
# 可用 COOKIE_SECURE=1/0 显式覆盖（例如应用跑在反向代理后、自身只监听 http，
# 但对外是 https，这种情况必须手动置 1）。
_COOKIE_SECURE_RAW = os.environ.get("COOKIE_SECURE", "").strip().lower()
if _COOKIE_SECURE_RAW in ("1", "true", "yes", "on"):
    COOKIE_SECURE = True
elif _COOKIE_SECURE_RAW in ("0", "false", "no", "off"):
    COOKIE_SECURE = False
else:
    COOKIE_SECURE = REDIRECT_URI.lower().startswith("https://")


def session_cookie(sid: str = "", clear: bool = False) -> str:
    """统一构造会话 Cookie，避免设置与清除两处属性不一致。"""
    parts = [f"wa_session={sid}", "Path=/"]
    if clear:
        parts.append("Max-Age=0")
    parts += ["HttpOnly", "SameSite=Lax"]
    if COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)

MODELS = {"fast": "zhida-fast-1p5", "deep": "zhida-thinking-1p5"}

# ---------------------------------------------------------------- skill prompt

# 实测：直答会忽略 system 角色的指令（同一条指令放 system 被无视、放 user 轮严格执行），
# 因此框架不走 system，整体拼进 user 轮，并把硬约束放在末尾利用近因效应。
SKILL_FRAMEWORK = """你是一名英语词汇分析专家，严格执行「词汇深度分析法」四维框架。

维度一 溯源与构词
1 定血统：区分日耳曼本族词（短促、日常）与拉丁／希腊借词（较长、学术、带可辨识词缀）。本族词通常不可拆。
2 拆部件（仅借词）：切分前缀＋词根＋后缀并给词根核心义。例 conglomerate = con-(共同)+glomer-(拉丁 glomus 线团/球)+-ate。
3 查外来语：无法按拉丁希腊词根拆解的（caribou、cenote），追溯来源语并给直译画面。
4 家族扩展：串联同根词，词源链条不直观的补一句说明。

维度二 发音与听感
5 标音与重音：给 IPA，英美并列，单独点明重音位置。
6 音节按读音切分而非拼写切分，否则重音标记会对不上。
7 易错点只标真实陷阱，三类：拼写与读音严重脱节（colonel）；重音或后缀区分词性（ˈrecord/reˈcord）；词典收录多读且都可接受（algae 美式 /ˈældʒiː/、英式也接受 /ˈælɡiː/，并列标注不判错）。
禁止使用中文谐音注音，需要辅助记忆时用同韵英文词类比。

维度三 频率与语境
8 词频用量级带表述，不给未经核实的精确排名：前 3000 高频核心／3000-10000 常见／10000-30000 偏专业书面／30000 以后低频。CEFR 给 A1-C2 或「未收录」。
9 语义映射：本义→引申义→当代常用义。
10 易混辨析只列真会混的（形近＋义近＋同语境竞争至少满足两项）。desolate/dissolute/disconsolate 是真陷阱；desolate 与 isolate 词源无关只共享尾缀，属反例不要列。

维度四 语法行为与产出
11 词性与屈折：可数性、及物性、不规则变化、借词复数（alga→algae、criterion→criteria）。
12 搭配给高频组合而非孤立释义（make a decision、consist of）。
13 语域与色彩：学术／新闻／口语／正式，中性／褒／贬。
14 例句 1-2 条，每条同时体现一个搭配和一种语域。

事实与不确定性（硬约束）
- 词源、词频、CEFR 是最容易被编造的信息。查不到就写「待核查」并说明缺口，不要猜一个填进去。
- 凭语感推断的标注「推断」，与查证结果区分。
- 词典分歧并列呈现，不单选一个当唯一答案。
- 宁可给一份有明确空缺的档案，也不要一份看起来完整但局部失真的档案。

输出用 Markdown，严格按以下骨架，不要加额外前言或总结：

# <word> /IPA/

**一句话定位**：<血统 + 词频带 + 最常用义项>

## 一、溯源与构词
- 血统：
- 拆解：
- 直译画面：
- 同根词族：

## 二、发音与听感
- IPA：US /…/　UK /…/
- 音节（按读音切分）：
- 易错点：

## 三、频率与语境
- 词频：
- CEFR：
- 语义路径：
- 易混辨析：

## 四、语法行为与产出
- 词性与屈折：
- 高频搭配：
- 语域与色彩：
- 例句：
"""


FINAL_CONSTRAINTS = """
再次强调，以下要求必须全部满足：
1. 四个一级小标题必须原样出现且顺序不变：「## 一、溯源与构词」「## 二、发音与听感」「## 三、频率与语境」「## 四、语法行为与产出」。不要自创「核心释义」「学习要点」「记忆提示」之类的小标题。
2. 维度三必须包含「词频：」和「CEFR：」两行。无法确认就写「待核查」，禁止省略这两行。
3. 词源、词频、CEFR 三项凡未经查证，必须显式写「待核查」或「推断」。
4. 禁止中文谐音注音。
5. 后缀 -ate 类词若名动读音不同，写明是后缀元音变化（/ət/ 与 /eɪt/），不要错写成「重音移到末尾」。
6. 直接以「# 」开头输出档案，不要任何前言、结语或额外说明。
"""


def build_user_prompt(word: str, sources: list[dict]) -> str:
    parts = [SKILL_FRAMEWORK, f"\n现在分析这个英文单词：{word}"]
    if sources:
        parts.append(
            "\n以下是知乎站内检索到的相关讨论摘要，仅在与该词的中文释义、"
            "使用场景或学习难点相关时参考；与词汇无关就忽略，"
            "不要为了用上它们而编造联系，也不要把它们的错误结论当作依据："
        )
        for i, s in enumerate(sources[:5], 1):
            title = (s.get("Title") or "").strip()
            text = (s.get("ContentText") or "").strip()[:200]
            if title or text:
                parts.append(f"{i}. {title}｜{text}")
    parts.append(FINAL_CONSTRAINTS)
    return "\n".join(parts)


# --------------------------------------------------------------------- helpers

# 这些 query 参数是凭证或会话标识，不得进入日志。
# authorization code 虽然一次性且短时效，仍属凭证：日志会被打包、截图或贴进 issue。
SENSITIVE_QS_KEYS = {
    "authorization_code", "code", "access_token", "token", "id_token",
    "app_key", "secret", "access_secret", "state", "session",
}


def redact_query(text: str) -> str:
    """遮蔽请求行 query 中的敏感参数值，保留键名便于排查。"""
    if "?" not in text:
        return text
    head, sep, rest = text.partition("?")
    # 请求行形如 'GET /path?query HTTP/1.1'，query 到第一个空格为止
    query, space, tail = rest.partition(" ")
    parts = []
    for pair in query.split("&"):
        key, eq, val = pair.partition("=")
        if eq and val and key.lower() in SENSITIVE_QS_KEYS:
            parts.append(f"{key}=<redacted:{len(val)}>")
        else:
            parts.append(pair)
    return head + sep + "&".join(parts) + space + tail


def fingerprint(value: str) -> str:
    """凭证诊断只暴露长度与 SHA-256 短前缀，不暴露明文。"""
    if not value:
        return "absent"
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"len={len(value)} sha256:{digest}"


def api_headers(oauth_token: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {ACCESS_SECRET}",
        "X-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json",
    }
    if oauth_token:
        headers["X-OAuth-Token"] = oauth_token
    return headers


class TTLCache:
    """应用层缓存 + 请求去重，避免重复消耗开放接口额度。"""

    def __init__(self, ttl: float = 900.0, maxsize: int = 512):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, key: str):
        with self._guard:
            hit = self._data.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > self.ttl:
            with self._guard:
                self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value) -> None:
        with self._guard:
            if len(self._data) >= self.maxsize:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.time(), value)

    def get_or_call(self, key: str, producer):
        cached = self.get(key)
        if cached is not None:
            return cached, True
        with self._lock_for(key):
            cached = self.get(key)
            if cached is not None:
                return cached, True
            value = producer()
            self.set(key, value)
            return value, False


CACHE = TTLCache()
SESSIONS: dict[str, dict] = {}
SESSION_LOCK = threading.Lock()
RATE: dict[str, list[float]] = {}
RATE_LOCK = threading.Lock()


def rate_limited(ip: str, limit: int = 20, window: float = 60.0) -> bool:
    now = time.time()
    with RATE_LOCK:
        hits = [t for t in RATE.get(ip, []) if now - t < window]
        hits.append(now)
        RATE[ip] = hits
        return len(hits) > limit


def new_session(token: str, expires_in: int | None) -> str:
    sid = secrets.token_urlsafe(24)
    with SESSION_LOCK:
        SESSIONS[sid] = {
            "oauth_token": token,
            "created": time.time(),
            "expires_at": time.time() + expires_in if expires_in else None,
        }
    return sid


def get_session(sid: str | None) -> dict | None:
    if not sid:
        return None
    with SESSION_LOCK:
        sess = SESSIONS.get(sid)
        if not sess:
            return None
        exp = sess.get("expires_at")
        if exp and time.time() > exp:
            SESSIONS.pop(sid, None)
            return None
        return dict(sess)


def drop_session(sid: str | None) -> None:
    if sid:
        with SESSION_LOCK:
            SESSIONS.pop(sid, None)


def oauth_ready() -> bool:
    return bool(APP_ID and APP_KEY and REDIRECT_URI)


# -------------------------------------------------------------- upstream calls

def fetch_sources(word: str, gloss: str = "") -> dict:
    """知乎站内搜索，为档案提供社区原始来源。

    检索词用「词汇本身」或「词汇 + 中文释义」，不再附加「词根 用法」之类的
    元描述词——那会把结果带向词根汇总长文，而不是这个词自身的讨论。
    """
    query = f"{word} {gloss}".strip() if gloss else f"{word} 英语"
    params = {"Query": query, "Count": 5}
    resp = requests.get(ZHIHU_SEARCH_URL, params=params,
                        headers=api_headers(), timeout=20)
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code, "items": []}
    body = resp.json()
    data = body.get("Data") or {}
    items = data.get("Items") or []
    return {
        "ok": True,
        "items": items,
        "query": query,
        "empty_reason": data.get("EmptyReason") or "",
    }


def fetch_global_sources(word: str, gloss: str = "") -> dict:
    """全网搜索，为档案提供知乎之外的权威来源。

    与站内搜索分开调用：站内给社区经验，全网给词典、外刊、机构来源。
    两类来源分开呈现，不混成一个黑盒。
    """
    query = f"{word} {gloss}".strip() if gloss else f"{word} 英语 词义"
    # 全网搜索 Count 上限 20（站内是 10）
    params = {"Query": query, "Count": 8}
    resp = requests.get(GLOBAL_SEARCH_URL, params=params,
                        headers=api_headers(), timeout=20)
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code, "items": []}
    data = (resp.json() or {}).get("Data") or {}
    return {
        "ok": True,
        "items": data.get("Items") or [],
        "query": query,
        "has_more": bool(data.get("HasMore")),
    }


def strip_em(text: str) -> str:
    """去掉全网搜索摘要里的 <em> 高亮标签，保留纯文本。"""
    out = text or ""
    for tag in EM_TAGS:
        out = out.replace(tag, "")
    return out


def fetch_quota() -> dict:
    resp = requests.get(QUOTA_URL, headers=api_headers(), timeout=15)
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code}
    return {"ok": True, "data": resp.json().get("Data")}


def fetch_user_contents(oauth_token: str, limit: int = 5) -> dict:
    params = {"ContentType": "all", "Limit": limit,
              "SortField": "ts", "SortOrder": "desc"}
    resp = requests.get(USER_CONTENTS_URL, params=params,
                        headers=api_headers(oauth_token), timeout=20)
    if resp.status_code in (401, 403):
        return {"ok": False, "reason": "oauth_invalid", "status": resp.status_code}
    if resp.status_code != 200:
        return {"ok": False, "reason": "upstream", "status": resp.status_code}
    return {"ok": True, "data": resp.json().get("Data")}


def exchange_token(code: str) -> dict:
    form = {
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }
    resp = requests.post(f"{OAUTH_API}/access_token", data=form, timeout=20)
    try:
        body = resp.json()
    except ValueError:
        return {"ok": False, "reason": "bad_response", "status": resp.status_code}
    # 以是否存在 access_token 判断成功，不单凭业务 code 判失败
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    if not token:
        return {"ok": False, "reason": "no_token", "status": resp.status_code}
    expires = body.get("expires_in") or (body.get("data") or {}).get("expires_in")
    return {"ok": True, "token": token,
            "expires_in": int(expires) if str(expires or "").isdigit() else None}


# -------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "WordAtlas/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 只记录方法与状态，不记录凭证
        # 请求行含 query，/auth/callback 的 authorization_code 会随之落盘，先遮蔽。
        # flush：输出重定向到文件时 stdout 为块缓冲，否则日志迟迟不落盘
        print(f"[{self.log_date_time_string()}] {redact_query(fmt % args)}", flush=True)

    # ---- plumbing
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict, extra: dict | None = None):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", extra)

    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _sid(self) -> str | None:
        return self._cookies().get("wa_session")

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    # ---- routing
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/":
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/config":
            return self._api_config()
        if path == "/api/health":
            return self._api_health()
        if path == "/api/quota":
            return self._api_quota()
        if path == "/api/sources":
            return self._api_sources(query)
        if path == "/api/global":
            return self._api_global(query)
        if path == "/api/me/contents":
            return self._api_me()
        if path == "/auth/login":
            return self._auth_login()
        if path == "/auth/callback":
            return self._auth_callback(query)
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/analyze":
            return self._api_analyze()
        if parsed.path == "/auth/logout":
            drop_session(self._sid())
            return self._json(200, {"ok": True}, {
                "Set-Cookie": session_cookie(clear=True)})
        return self._json(404, {"error": "not_found"})

    # ---- static
    def _serve_static(self, rel: str):
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._json(404, {"error": "not_found"})
        # 图片类型必须显式声明：响应带 X-Content-Type-Options: nosniff，
        # 若回落成 application/octet-stream，浏览器会拒绝渲染。
        types = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".gif": "image/gif", ".png": "image/png", ".webp": "image/webp",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".ico": "image/x-icon"}
        extra = None
        # 素材内容不变，给长缓存；HTML 不缓存，保证改动立即可见。
        if target.suffix in (".gif", ".png", ".webp", ".jpg", ".jpeg", ".ico"):
            extra = {"Cache-Control": "public, max-age=604800"}
        self._send(200, target.read_bytes(),
                   types.get(target.suffix, "application/octet-stream"), extra)

    # ---- api
    def _api_config(self):
        sess = get_session(self._sid())
        self._json(200, {
            "access_secret_configured": bool(ACCESS_SECRET),
            "oauth_enabled": oauth_ready(),
            "logged_in": bool(sess),
            "models": list(MODELS.keys()),
        })

    def _api_health(self):
        """诊断只暴露来源、是否配置、长度与哈希短前缀。"""
        self._json(200, {
            "ok": bool(ACCESS_SECRET),
            "credentials": {
                "access_secret": {"source": "env:ZHIHU_ACCESS_SECRET",
                                  "configured": bool(ACCESS_SECRET),
                                  "fingerprint": fingerprint(ACCESS_SECRET)},
                "oauth_app_key": {"source": "env:ZHIHU_OAUTH_APP_KEY",
                                  "configured": bool(APP_KEY),
                                  "fingerprint": fingerprint(APP_KEY)},
                "oauth_app_id": {"configured": bool(APP_ID)},
                "redirect_uri_configured": bool(REDIRECT_URI),
            },
            "cache": {"entries": len(CACHE._data), "ttl_seconds": CACHE.ttl},
            "sessions": len(SESSIONS),
            # 部署到 HTTPS 后应确认此项为 true，否则会话 cookie 可能经明文信道发送
            "cookie_secure": COOKIE_SECURE,
        })

    def _api_quota(self):
        if not ACCESS_SECRET:
            return self._json(503, {"error": "access_secret_missing"})
        result, cached = CACHE.get_or_call("quota", fetch_quota)
        if not result.get("ok"):
            return self._json(502, {"error": "quota_unavailable",
                                    "status": result.get("status")})
        self._json(200, {"data": result["data"], "cached": cached})

    def _api_sources(self, query: dict):
        word = (query.get("word", [""])[0] or "").strip()
        gloss = (query.get("gloss", [""])[0] or "").strip()[:32]
        if not word:
            return self._json(400, {"error": "word_required"})
        if not ACCESS_SECRET:
            return self._json(503, {"error": "access_secret_missing"})
        if rate_limited(self._client_ip()):
            return self._json(429, {"error": "rate_limited"})
        key = f"src:{word.lower()}:{gloss}"
        result, cached = CACHE.get_or_call(
            key, lambda: fetch_sources(word, gloss))
        if not result.get("ok"):
            return self._json(502, {"error": "search_unavailable",
                                    "status": result.get("status")})
        items = [{"title": i.get("Title"), "url": i.get("Url"),
                  "author": i.get("AuthorName"),
                  "excerpt": (i.get("ContentText") or "")[:180]}
                 for i in result["items"]]
        self._json(200, {"items": items, "cached": cached,
                         "query": result.get("query", ""),
                         "empty_reason": result.get("empty_reason", "")})

    def _api_global(self, query: dict):
        word = (query.get("word", [""])[0] or "").strip()
        gloss = (query.get("gloss", [""])[0] or "").strip()[:32]
        if not word:
            return self._json(400, {"error": "word_required"})
        if not ACCESS_SECRET:
            return self._json(503, {"error": "access_secret_missing"})
        if rate_limited(self._client_ip()):
            return self._json(429, {"error": "rate_limited"})
        key = f"glb:{word.lower()}:{gloss}"
        result, cached = CACHE.get_or_call(
            key, lambda: fetch_global_sources(word, gloss))
        if not result.get("ok"):
            return self._json(502, {"error": "search_unavailable",
                                    "status": result.get("status")})
        items = []
        for i in result["items"]:
            url = i.get("Url") or ""
            host = urllib.parse.urlparse(url).netloc
            lvl = str(i.get("AuthorityLevel") or "")
            items.append({
                "title": strip_em(i.get("Title") or ""),
                "url": url,
                "host": host,
                "author": i.get("AuthorName") or "",
                "excerpt": strip_em(i.get("ContentText") or "")[:180],
                "authority": AUTHORITY.get(lvl, ""),
                "authority_level": lvl,
            })
        self._json(200, {"items": items, "cached": cached,
                         "query": result.get("query", ""),
                         "has_more": result.get("has_more", False)})

    def _api_me(self):
        sess = get_session(self._sid())
        if not sess:
            return self._json(401, {"error": "login_required"})
        result = fetch_user_contents(sess["oauth_token"])
        if not result.get("ok"):
            if result.get("reason") == "oauth_invalid":
                drop_session(self._sid())
                return self._json(401, {"error": "oauth_expired"})
            return self._json(502, {"error": "upstream_error",
                                    "status": result.get("status")})
        data = result.get("data") or {}
        items = [{"title": i.get("Title"), "url": i.get("Url"),
                  "type": i.get("ContentType")}
                 for i in (data.get("Items") or [])]
        self._json(200, {"items": items})

    def _api_analyze(self):
        """把 Skill 交给知乎直答执行，SSE 流式回传档案。"""
        if not ACCESS_SECRET:
            return self._json(503, {"error": "access_secret_missing"})
        if rate_limited(self._client_ip(), limit=8):
            return self._json(429, {"error": "rate_limited"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad_json"})

        word = (payload.get("word") or "").strip()
        if not word or len(word) > 64:
            return self._json(400, {"error": "word_invalid"})
        model = MODELS.get(payload.get("model") or "deep", MODELS["deep"])
        gloss = (payload.get("gloss") or "").strip()[:32]

        # 缓存键与 /api/sources 保持一致，避免同一词被查两次白耗额度
        src, _ = CACHE.get_or_call(f"src:{word.lower()}:{gloss}",
                                   lambda: fetch_sources(word, gloss))
        sources = src.get("items", []) if src.get("ok") else []

        body = {
            "model": model,
            "stream": True,
            # 不使用 system 角色：实测直答会忽略其中的指令。
            "messages": [
                {"role": "user", "content": build_user_prompt(word, sources)},
            ],
        }

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(event: str, data: dict):
            chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(chunk.encode())
            self.wfile.flush()

        try:
            emit("meta", {"word": word, "model": model,
                          "source_count": len(sources)})
            with requests.post(CHAT_URL, json=body, headers=api_headers(),
                               stream=True, timeout=180) as resp:
                if resp.status_code != 200:
                    emit("error", {"message": f"直答接口返回 {resp.status_code}",
                                   "status": resp.status_code})
                    emit("done", {"ok": False})
                    return
                # 上游 text/event-stream 未声明 charset，requests 会按 ISO-8859-1
                # 解码而产生乱码；这里按 UTF-8 增量解码，并自行缓冲不完整行。
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                pending = ""
                finished = False
                for chunk in resp.iter_content(chunk_size=512):
                    if not chunk:
                        continue
                    pending += decoder.decode(chunk)
                    while "\n" in pending:
                        raw, pending = pending.split("\n", 1)
                        raw = raw.rstrip("\r")
                        if raw == "" or raw.startswith(":"):
                            continue  # 空行与 : keep-alive 心跳
                        if not raw.startswith("data:"):
                            continue
                        data = raw[5:].strip()
                        if data == "[DONE]":
                            finished = True
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("error"):
                            emit("error", {"message": obj["error"].get("message", "上游错误")})
                            continue
                        for ch in obj.get("choices") or []:
                            delta = ch.get("delta") or {}
                            if delta.get("reasoning_content"):
                                emit("reasoning", {"text": delta["reasoning_content"]})
                            if delta.get("content"):
                                emit("content", {"text": delta["content"]})
                            if ch.get("finish_reason") == "error":
                                emit("error", {"message": "上游在流式中途报错"})
                    if finished:
                        break
            emit("done", {"ok": True})
        except requests.Timeout:
            emit("error", {"message": "直答接口超时，未生成完整档案"})
            emit("done", {"ok": False})
        except requests.RequestException as exc:
            emit("error", {"message": f"网络错误：{type(exc).__name__}"})
            emit("done", {"ok": False})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- oauth
    def _auth_login(self):
        if not oauth_ready():
            return self._json(503, {"error": "oauth_not_configured"})
        # 仍然发送随机 state，但按 references/oauth.md 实测回调不返回 state，
        # 因此这里无法据此做 CSRF 校验——不要把它当作已生效的防护。
        state = secrets.token_urlsafe(16)
        url = (f"{OAUTH_API}/authorize?"
               + urllib.parse.urlencode({"redirect_uri": REDIRECT_URI,
                                         "app_id": APP_ID,
                                         "response_type": "code",
                                         "state": state}))
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _auth_callback(self, query: dict):
        # 主参数 authorization_code，兼容 code
        code = (query.get("authorization_code", [""])[0]
                or query.get("code", [""])[0]).strip()
        if not code:
            return self._send(400, b"<h1>authorization_code missing</h1>"
                                   b"<p>authorization_code missing, flow stopped.</p>"
                                   b'<p><a href="/">Back</a></p>',
                              "text/html; charset=utf-8")
        result = exchange_token(code)
        if not result.get("ok"):
            return self._send(502, ("<h1>Token exchange failed</h1>"
                                    f"<p>reason: {result.get('reason')}</p>"
                                    '<p><a href="/">Back</a></p>').encode(),
                              "text/html; charset=utf-8")
        sid = new_session(result["token"], result.get("expires_in"))
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", session_cookie(sid))
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    # 统一行缓冲，保证重定向到文件时启动与访问日志能及时落盘
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    if not ACCESS_SECRET:
        print("[warn] ZHIHU_ACCESS_SECRET 未设置，直答与搜索会返回 503")
    if not oauth_ready():
        print("[warn] OAuth 未配置（需要 APP_ID / APP_KEY / REDIRECT_URI），登录入口不可用")
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"[warn] 正在监听 {HOST}：/api/analyze 无鉴权且会消耗开放平台额度，"
              "请确认前面有反向代理或平台网关做访问控制")
    print(f"[ok] Word Atlas 监听 http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
