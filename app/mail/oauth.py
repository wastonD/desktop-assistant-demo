# -*- coding: utf-8 -*-
"""OAuth2（XOAUTH2）令牌获取与刷新。只用标准库。

- microsoft：设备码流程（Outlook/Hotmail/Office365）。需要在 config/mail.json 的
  oauth.microsoft.client_id 填一个 Azure 应用注册的客户端 ID（"公共客户端流"开启，
  API 权限 IMAP.AccessAsUser.All + SMTP.Send + offline_access）。
- google：本机回环 + PKCE 流程（Gmail 的 https://mail.google.com/ 作用域不支持设备码）。
  需要 Google Cloud "桌面应用" 类型的 client_id + client_secret（桌面应用的 secret 不算机密）。

refresh_token 存 Windows 凭据管理器（credentials.token_key），access_token 只在内存。
"""
import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import credentials

MS_SCOPES = ("https://outlook.office.com/IMAP.AccessAsUser.All "
             "https://outlook.office.com/SMTP.Send offline_access")
GOOGLE_SCOPES = "https://mail.google.com/"
_access_cache: dict[str, tuple[str, float]] = {}  # account_id → (access_token, 过期时间戳)


class OAuthError(Exception):
    pass


class NeedsLogin(OAuthError):
    """没有可用 refresh_token，需要用户交互登录一次。"""


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise OAuthError(f"HTTP {e.code}") from e


def xoauth2_string(user: str, access_token: str) -> str:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


# ---------------- Microsoft ----------------

def _ms_base(cfg: dict) -> str:
    tenant = cfg.get("tenant") or "common"
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def ms_device_login(account_id: str, cfg: dict, prompt_cb) -> None:
    """设备码登录。prompt_cb(text) 用来把"打开网址输入代码"提示给用户（气泡/对话面板）。阻塞直到完成或超时。"""
    client_id = cfg.get("client_id")
    if not client_id:
        raise OAuthError("未配置 oauth.microsoft.client_id")
    base = _ms_base(cfg)
    dc = _post(base + "/devicecode", {"client_id": client_id, "scope": MS_SCOPES})
    if "device_code" not in dc:
        raise OAuthError(dc.get("error_description") or str(dc))
    prompt_cb(dc.get("message") or f"打开 {dc['verification_uri']} 输入代码 {dc['user_code']}")
    interval = int(dc.get("interval", 5))
    deadline = time.time() + int(dc.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        tok = _post(base + "/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id, "device_code": dc["device_code"]})
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            raise OAuthError(tok.get("error_description") or err)
        _store_tokens(account_id, tok)
        return
    raise OAuthError("设备码已过期，请重试")


def _ms_refresh(account_id: str, cfg: dict, refresh_token: str) -> dict:
    return _post(_ms_base(cfg) + "/token", {
        "grant_type": "refresh_token", "client_id": cfg.get("client_id", ""),
        "refresh_token": refresh_token, "scope": MS_SCOPES})


# ---------------- Google ----------------

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def google_loopback_login(account_id: str, cfg: dict, prompt_cb, timeout: int = 300) -> None:
    """打开浏览器授权，本机 127.0.0.1 随机端口接回调（PKCE）。"""
    client_id, secret = cfg.get("client_id"), cfg.get("client_secret", "")
    if not client_id:
        raise OAuthError("未配置 oauth.google.client_id")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("授权完成，可以关闭此页面。".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = GOOGLE_AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    prompt_cb("已在浏览器打开 Google 授权页，登录后回到这里。")
    webbrowser.open(url)
    server.timeout = 1
    deadline = time.time() + timeout
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise OAuthError(result.get("error") or "授权超时")
    tok = _post(GOOGLE_TOKEN, {
        "grant_type": "authorization_code", "code": result["code"], "client_id": client_id,
        "client_secret": secret, "redirect_uri": redirect, "code_verifier": verifier})
    if "error" in tok:
        raise OAuthError(tok.get("error_description") or tok["error"])
    _store_tokens(account_id, tok)


def _google_refresh(account_id: str, cfg: dict, refresh_token: str) -> dict:
    return _post(GOOGLE_TOKEN, {
        "grant_type": "refresh_token", "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""), "refresh_token": refresh_token})


# ---------------- 通用 ----------------

def _store_tokens(account_id: str, tok: dict) -> None:
    if tok.get("refresh_token"):
        credentials.set_secret(credentials.token_key(account_id), tok["refresh_token"])
    if tok.get("access_token"):
        _access_cache[account_id] = (tok["access_token"],
                                     time.time() + int(tok.get("expires_in", 3600)) - 120)


def access_token(account_id: str, kind: str, cfg: dict) -> str:
    """拿可用 access_token：内存缓存 → refresh_token 刷新 → 抛 NeedsLogin。"""
    cached = _access_cache.get(account_id)
    if cached and cached[1] > time.time():
        return cached[0]
    refresh = credentials.get_secret(credentials.token_key(account_id))
    if not refresh:
        raise NeedsLogin(account_id)
    refresher = {"microsoft": _ms_refresh, "google": _google_refresh}.get(kind)
    if not refresher:
        raise OAuthError(f"未知 OAuth 类型：{kind}")
    tok = refresher(account_id, cfg, refresh)
    if "access_token" not in tok:
        if tok.get("error") in ("invalid_grant", "interaction_required"):
            credentials.delete_secret(credentials.token_key(account_id))
            raise NeedsLogin(account_id)
        raise OAuthError(tok.get("error_description") or str(tok))
    _store_tokens(account_id, tok)  # 微软会轮换 refresh_token，要存新的
    return tok["access_token"]


def interactive_login(account_id: str, kind: str, cfg: dict, prompt_cb) -> None:
    """在后台线程里调用（会阻塞数分钟等用户完成浏览器登录）。"""
    if kind == "microsoft":
        ms_device_login(account_id, cfg, prompt_cb)
    elif kind == "google":
        google_loopback_login(account_id, cfg, prompt_cb)
    else:
        raise OAuthError(f"未知 OAuth 类型：{kind}")


def login_in_background(account_id: str, kind: str, cfg: dict, prompt_cb, done_cb) -> threading.Thread:
    def run():
        try:
            interactive_login(account_id, kind, cfg, prompt_cb)
            done_cb(None)
        except Exception as e:  # 回调里给用户看
            done_cb(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
