# -*- coding: utf-8 -*-
"""可插拔 LLM 接口。config/llm.json 里切换 provider，改配置即可换模型/换云端，代码零改动。

- local（默认）：llama.cpp 子进程**按需拉起**，空闲 idle_stop_min 分钟后自动退出，0 常驻。
- 任意 OpenAI 兼容 API（type: "openai"）：填 base_url + model + api_key_env 即可。
人设在 config/persona.txt，用户可随意改。
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "llm.json"
PERSONA_PATH = ROOT / "config" / "persona.txt"

DEFAULT_CONFIG = {
    "provider": "local",
    "idle_stop_min": 5,
    "max_tokens": 600,
    "temperature": 0.7,
    "providers": {
        "local": {"type": "local_llama",
                  "model": "models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
                  "server_dir": "llama_cpp", "port": 8765, "ctx": 8192,
                  "ngl": 0, "threads": -1},
        "bailian": {"type": "openai",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "model": "qwen-plus", "api_key_env": "DASHSCOPE_API_KEY"},
    },
}
DEFAULT_PERSONA = (
    "你是坐在用户 Windows 任务栏上的桌面宠物助理「小银」，一只银发猫娘。"
    "性格高冷傲娇：说话简短、偶尔毒舌、不爱撒娇，但实际上很关心用户、办事可靠。"
    "始终用中文，回复尽量简洁（一般不超过三四句），不用 markdown 排版，少用感叹号。"
    "你可以帮用户：管理日程提醒、查看和总结邮件与资讯、起草邮件（你只能起草，发出必须由用户输入 /发送 编号 确认，不要声称已经发出）。"
)

CREATE_NO_WINDOW = 0x08000000


class LLMError(Exception):
    pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k == "providers":
                    for name, spec in v.items():
                        cfg["providers"].setdefault(name, {}).update(spec)
                else:
                    cfg[k] = v
        except (json.JSONDecodeError, OSError):
            pass
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return cfg


def persona() -> str:
    if PERSONA_PATH.exists():
        text = PERSONA_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    else:
        PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
        PERSONA_PATH.write_text(DEFAULT_PERSONA, encoding="utf-8")
    return DEFAULT_PERSONA


def _api_key(spec):
    env = spec.get("api_key_env")
    if env:
        if os.environ.get(env):
            return os.environ[env]
        for f in (ROOT / ".env",):
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith(env + "="):
                        val = line.split("=", 1)[1].strip().strip('"')
                        if val:
                            return val
    return spec.get("api_key")


def _chat_http(base_url, api_key, model, messages, cfg, tools=None, timeout=300):
    """返回 message 字典（可能含 content 或 tool_calls）。"""
    body = {"model": model, "messages": messages,
            "max_tokens": cfg.get("max_tokens", 600),
            "temperature": cfg.get("temperature", 0.7)}
    if tools:
        body["tools"] = tools
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    import urllib.error
    import urllib.request  # 按需加载（会带进 ssl，约 5MB）
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMError(f"连接失败：{e}") from None
    try:
        return resp["choices"][0]["message"]
    except (KeyError, IndexError):
        raise LLMError("响应格式异常：" + json.dumps(resp, ensure_ascii=False)[:300]) from None


class LocalLlama:
    """按需拉起的 llama.cpp 服务。"""

    def __init__(self, spec):
        self.spec = spec
        self.proc = None
        self.last_used = 0.0
        self.lock = threading.Lock()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.spec.get('port', 8765)}/v1"

    def _healthy(self):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.spec.get('port', 8765)}/health", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False

    def ensure_running(self, status_cb=None):
        with self.lock:
            if self._healthy():
                return
            if status_cb:
                status_cb("正在唤醒本地模型…")
            exe = ROOT / self.spec.get("server_dir", "llama_cpp") / "llama-server.exe"
            model = Path(self.spec["model"])
            if not model.is_absolute():
                model = ROOT / model
            if not model.exists():
                raise LLMError(f"模型文件不存在：{model}")
            args = [str(exe), "-m", str(model), "--host", "127.0.0.1",
                    "--port", str(self.spec.get("port", 8765)),
                    "-c", str(self.spec.get("ctx", 8192)),
                    "-ngl", str(self.spec.get("ngl", 0)),
                    "-t", str(self.spec.get("threads", -1)), "--no-webui"]
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         creationflags=CREATE_NO_WINDOW)
            deadline = time.time() + 180
            while time.time() < deadline:
                if self._healthy():
                    return
                if self.proc.poll() is not None:
                    raise LLMError(f"llama-server 启动失败（退出码 {self.proc.returncode}）")
                time.sleep(0.5)
            self.stop()
            raise LLMError("llama-server 启动超时")

    def chat(self, messages, cfg, tools=None, status_cb=None):
        self.ensure_running(status_cb)
        try:
            reply = _chat_http(self.base_url, None, "local", messages, cfg, tools=tools)
        finally:
            self.last_used = time.time()
        return reply

    def maybe_stop_idle(self, idle_min):
        with self.lock:
            if self.proc and self.proc.poll() is None and self.last_used \
                    and time.time() - self.last_used > idle_min * 60:
                self.stop()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


_local: LocalLlama | None = None
_local_lock = threading.Lock()


def _get_local(spec) -> LocalLlama:
    global _local
    with _local_lock:
        if _local is None:
            _local = LocalLlama(spec)
        return _local


def _context_note() -> str:
    from datetime import datetime
    from . import store
    now = datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    lines = [f"当前时间：{now:%Y-%m-%d %H:%M}，星期{weekday}。"]
    events = store.today_events()
    if events:
        items = "；".join(f"{e['at'][11:16]} {e['title']}{'（已完成）' if e['done'] else ''}"
                          for e in events)
        lines.append(f"用户今日日程：{items}。")
    else:
        lines.append("用户今天没有日程安排。")
    from . import status
    st = status.read()
    extra = []
    if isinstance(st.get("unread_mail"), int):
        extra.append(f"未读邮件 {st['unread_mail']} 封")
    if st.get("news_ready"):
        extra.append(f"未读资讯 {st['news_ready']} 条")
    if extra:  # 只给数字，详情需要时模型自己调 list_mail / list_news
        lines.append("、".join(extra) + "（详情可调用工具查看）。")
    return "\n".join(lines)


MAX_TOOL_ROUNDS = 4


def generate(messages, status_cb=None, use_tools=True) -> str:
    """messages 不含 system；人设和实时上下文自动加在最前。status_cb(str) 用于界面状态提示。

    use_tools=True 时走工具循环：模型可调用 tools.py 里的操作（记日程等），
    执行结果回喂给模型，直到它给出普通答复。
    """
    from . import tools as tool_registry
    cfg = load_config()
    name = cfg.get("provider", "local")
    spec = cfg["providers"].get(name)
    if not spec:
        raise LLMError(f"未知 provider：{name}")
    full = [{"role": "system", "content": persona() + "\n\n" + _context_note()}] + messages
    schemas = tool_registry.SCHEMAS if use_tools else None

    def call(msgs):
        if status_cb:
            status_cb("思考中…")
        if spec.get("type") == "local_llama":
            return _get_local(spec).chat(msgs, cfg, tools=schemas, status_cb=status_cb)
        return _chat_http(spec["base_url"], _api_key(spec), spec["model"], msgs, cfg,
                          tools=schemas)

    for _ in range(MAX_TOOL_ROUNDS):
        msg = call(full)
        calls = msg.get("tool_calls")
        if not calls:
            return (msg.get("content") or "").strip()
        full.append(msg)
        if status_cb:
            status_cb("执行操作中…")
        for tc in calls:
            full.append(tool_registry.execute_call(tc))
    return "操作轮数太多，我先停下了。用 /日程 之类的指令可以直接操作。"


def availability() -> tuple[bool, str]:
    """当前 provider 是否可用。不可用时返回原因（用于界面提示，功能分界的一部分）。"""
    cfg = load_config()
    name = cfg.get("provider", "local")
    spec = cfg["providers"].get(name)
    if not spec:
        return False, f"配置里没有 provider「{name}」"
    if spec.get("type") == "local_llama":
        model = Path(spec["model"])
        if not model.is_absolute():
            model = ROOT / model
        if not model.exists():
            return False, f"本地模型未下载（{model.name}）"
        exe = ROOT / spec.get("server_dir", "llama_cpp") / "llama-server.exe"
        if not exe.exists():
            return False, "缺少 llama_cpp/llama-server.exe"
        return True, "ok"
    if not _api_key(spec):
        return False, f"缺少 API Key（{spec.get('api_key_env', 'api_key')}）"
    return True, "ok"


def maybe_stop_idle():
    cfg = load_config()
    if _local is not None:
        _local.maybe_stop_idle(cfg.get("idle_stop_min", 5))


def local_running() -> bool:
    return _local is not None and _local.proc is not None and _local.proc.poll() is None


def shutdown():
    if _local is not None:
        _local.stop()
