"""官方 MCP SDK 会话与本机服务注册表；第三方内容只作为工具数据。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import ipaddress
import os
import queue
import re
import threading
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from persistence import read_json, write_json


NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _reference(value):
    if not isinstance(value, str) or not ENV_REF.search(value) or ENV_REF.sub("", value).strip() not in ("", "Bearer"):
        raise ValueError("环境变量和请求头仅接受 ${变量名} 或 Bearer ${变量名}，不保存明文密钥")
    return value


def validate_config(document):
    if not isinstance(document, dict) or not isinstance(document.get("mcpServers"), dict) or len(document["mcpServers"]) > 20:
        raise ValueError("配置应包含 mcpServers 对象，最多20个服务")
    servers = {}
    for name, raw in document["mcpServers"].items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(raw, dict):
            raise ValueError("服务名只接受字母、数字、下划线、连字符")
        kind = raw.get("transport", "streamable_http" if raw.get("url") else "stdio")
        if kind not in {"stdio", "streamable_http", "sse"}:
            raise ValueError("transport 仅支持 stdio、streamable_http、sse")
        timeout = raw.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 5 <= timeout <= 120:
            raise ValueError("MCP超时须在5到120秒之间")
        item = {"transport": kind, "enabled": raw.get("enabled") is True, "timeout": timeout}
        if kind == "stdio":
            command, arguments = raw.get("command"), raw.get("args", [])
            if not isinstance(command, str) or not command.strip() or not isinstance(arguments, list) or len(arguments) > 100 or any(not isinstance(a, str) or len(a) > 8192 for a in arguments):
                raise ValueError("stdio需要有效的command和字符串args数组，不使用shell拼接")
            item.update(command=command, args=arguments)
            if re.search(r"sk-[A-Za-z0-9_-]{12,}", command + " " + " ".join(arguments)):
                raise ValueError("不要在命令参数中保存密钥，请使用环境变量引用")
            values = raw.get("env", {})
            if not isinstance(values, dict) or len(values) > 40:
                raise ValueError("env须为对象，最多40项")
            item["env"] = {key: _reference(value) for key, value in values.items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)}
            if len(item["env"]) != len(values):
                raise ValueError("环境变量名称无效")
        else:
            url = raw.get("url", "")
            if not isinstance(url, str):
                raise ValueError("MCP URL必须是文本")
            parsed = urlsplit(url)
            trusted = raw.get("trusted_local") is True
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise ValueError("MCP地址不能包含账号、查询密钥或片段；认证请使用环境变量请求头")
            if not trusted and parsed.scheme != "https":
                raise ValueError("远程MCP必须使用HTTPS；本地服务需显式设置trusted_local=true")
            if trusted:
                try:
                    local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    local = False
                if not local:
                    raise ValueError("trusted_local只允许localhost或明确的回环IP；远程服务须走公网校验")
            values = raw.get("headers", {})
            if not isinstance(values, dict) or len(values) > 20:
                raise ValueError("headers须为对象")
            if any(not re.fullmatch(r"[A-Za-z0-9-]{1,80}", key) for key in values):
                raise ValueError("请求头名称无效")
            item.update(url=url, trusted_local=trusted, headers={key: _reference(value) for key, value in values.items()})
        servers[name] = item
    return {"mcpServers": servers}


class MCPRegistry:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return validate_config(read_json(self.path, {"mcpServers": {}}))

    def save(self, document):
        value = validate_config(document)
        write_json(self.path, value, backup=True)
        return value


def _resolve(value):
    def replace(match):
        key = match.group(1)
        if not os.environ.get(key):
            raise ValueError(f"尚未设置环境变量：{key}")
        return os.environ[key]
    return ENV_REF.sub(replace, value)


def _bounded(value):
    document = value.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(value, "model_dump") else value
    if len(json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 1_000_000:
        raise RuntimeError("MCP结果超过1MB，调用可能已经执行；不会自动重试")
    return document


class MCPClient:
    def __init__(self, config, cwd):
        self.config = dict(config)
        self.cwd = Path(cwd).resolve()
        self.requests = queue.Queue()
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.error = None
        self.loop = None
        self.main_task = None
        self.thread = threading.Thread(target=self._thread, daemon=True, name="changan-mcp")
        self.thread.start()

    def _thread(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self.error = RuntimeError(f"MCP会话结束：{type(exc).__name__}")
            if not isinstance(exc, asyncio.CancelledError):
                self.error = RuntimeError(f"MCP连接失败：{type(exc).__name__}，请检查依赖、服务地址和环境变量")
        finally:
            self.ready.set()
            self.stopping.set()
            while not self.requests.empty():
                _, _, future = self.requests.get_nowait()
                if not future.done():
                    future.set_exception(self.error or RuntimeError("MCP会话已关闭"))

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamable_http_client
        import httpx

        self.loop, self.main_task = asyncio.get_running_loop(), asyncio.current_task()
        config = self.config
        async with AsyncExitStack() as stack:
            kind = config["transport"]
            if kind == "stdio":
                params = StdioServerParameters(command=config["command"], args=config.get("args", []), env={k: _resolve(v) for k, v in config.get("env", {}).items()}, cwd=str(self.cwd))
                errors = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                streams = await stack.enter_async_context(stdio_client(params, errlog=errors))
            else:
                proxy_url = None
                if not config.get("trusted_local"):
                    from network_safety import resolve_public_url
                    from specialized_tools import PinnedBrowserProxy
                    resolve_public_url(config["url"])
                    proxy = stack.enter_context(PinnedBrowserProxy({urlsplit(config["url"]).hostname.lower()}))
                    proxy_url = proxy.url
                headers = {key: _resolve(value) for key, value in config.get("headers", {}).items()}
                def factory(**kwargs):
                    kwargs.update(proxy=proxy_url, trust_env=False, follow_redirects=False)
                    return httpx.AsyncClient(**kwargs)
                if kind == "sse":
                    streams = await stack.enter_async_context(sse_client(config["url"], headers=headers, timeout=config["timeout"], httpx_client_factory=factory))
                else:
                    client = await stack.enter_async_context(factory(headers=headers, timeout=httpx.Timeout(config["timeout"])))
                    streams = await stack.enter_async_context(streamable_http_client(config["url"], http_client=client))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=config["timeout"])))
            await session.initialize()
            self.ready.set()
            while not self.stopping.is_set():
                try:
                    method, args, future = self.requests.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.04)
                    continue
                if future.cancelled():
                    continue
                try:
                    result = await asyncio.wait_for(getattr(session, method)(**args), config["timeout"])
                    if not future.done():
                        future.set_result(_bounded(result))
                except BaseException as exc:
                    if not future.done():
                        future.set_exception(RuntimeError(f"MCP {method} 失败：{type(exc).__name__}，请核对参数与服务状态"))
                    if isinstance(exc, asyncio.CancelledError):
                        raise

    def request(self, method, args=None, should_stop=None):
        deadline = time.monotonic() + self.config["timeout"] + 5
        while not self.ready.wait(0.05):
            if should_stop and should_stop() or time.monotonic() >= deadline:
                self.close()
                raise RuntimeError("MCP连接已取消或超时")
        if self.error or self.stopping.is_set():
            raise self.error or RuntimeError("MCP会话已关闭")
        future = concurrent.futures.Future()
        self.requests.put((method, args or {}, future))
        while True:
            try:
                return future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                if should_stop and should_stop() or time.monotonic() >= deadline:
                    self.close()
                    raise RuntimeError("MCP请求已取消或超时；副作用状态需人工核实，不会自动重试")

    def close(self, wait=True):
        self.stopping.set()
        if self.loop and self.main_task and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.main_task.cancel)
            except RuntimeError:
                pass
        if wait and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


class MCPTools:
    def __init__(self, registry, cwd, should_stop=None):
        self.registry, self.cwd = registry, Path(cwd)
        self.should_stop = should_stop or (lambda: False)
        self.clients = {}
        self.config = self.registry.load()["mcpServers"]

    def servers(self):
        return {key: value for key, value in self.config.items() if value["enabled"]}

    def client(self, name):
        servers = self.servers()
        if name not in servers:
            raise ValueError("MCP服务不存在或未启用")
        if name not in self.clients or self.clients[name].stopping.is_set():
            self.clients[name] = MCPClient(servers[name], self.cwd)
        return self.clients[name]

    def list(self, server, category="tools"):
        if category not in {"tools", "resources", "prompts"}:
            raise ValueError("MCP分类无效")
        output, cursor = [], None
        for _ in range(10):
            result = self.client(server).request("list_" + category, {"cursor": cursor} if cursor else {}, self.should_stop)
            output.extend(result.get(category, []))
            cursor = result.get("nextCursor")
            if not cursor:
                return {"ok": True, "server": server, category: output, "untrusted": True}
        raise RuntimeError("MCP列表超过10页，请减少服务器暴露的条目")

    def call(self, server, name, arguments):
        from jsonschema import validators
        tools = self.list(server)["tools"]
        tool = next((item for item in tools if item["name"] == name), None)
        if tool is None:
            raise ValueError("服务没有声明这个工具")
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须为JSON对象")
        schema = tool.get("inputSchema", {"type": "object"})
        serialized = json.dumps(schema)
        if re.search(r'"\$(?:ref|dynamicRef)"\s*:\s*"(?!#)', serialized):
            raise ValueError("不加载MCP参数Schema中的外部引用")
        validator = validators.validator_for(schema)
        validator.check_schema(schema)
        validator(schema).validate(arguments)
        value = self.client(server).request("call_tool", {"name": name, "arguments": arguments}, self.should_stop)
        return {"ok": not value.get("isError", False), "server": server, "tool": name, "result": value, "untrusted": True}

    def run(self, name, args, approval):
        if name == "mcp_list":
            return self.list(args["server"], args.get("category", "tools")) if args.get("server") else {"ok": True, "servers": list(self.servers())}
        if name == "mcp_call":
            approval("mcp_call", {"server": args.get("server"), "tool": args.get("tool"), "arguments": args.get("arguments", {}), "notice": "第三方MCP可执行本机程序或外部操作，权限不受项目文件工具限制"})
            return self.call(args["server"], args["tool"], args.get("arguments", {}))
        if name not in {"mcp_read_resource", "mcp_get_prompt"}:
            raise ValueError("未知MCP操作")
        approval(name, {"server": args.get("server"), "uri": args.get("uri"), "prompt": args.get("prompt"), "notice": "第三方资源可能位于项目之外"})
        method, arguments = ("read_resource", {"uri": args.get("uri", "")}) if name == "mcp_read_resource" else ("get_prompt", {"name": args.get("prompt", ""), "arguments": args.get("arguments", {})})
        result = self.client(args["server"]).request(method, arguments, self.should_stop)
        return {"ok": True, "result": result, "untrusted": True}

    def close(self):
        for client in self.clients.values():
            client.close()
