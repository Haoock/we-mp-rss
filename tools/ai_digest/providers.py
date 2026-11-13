"""One model call per article; no model tools, history, or webhook credentials."""
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from common import DigestError, PROMPT


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(url, body, headers=None, timeout=30):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise DigestError("HTTP 接口必须使用 HTTPS")
    request = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                    {"Content-Type": "application/json; charset=utf-8", **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise DigestError(f"HTTP 状态 {response.status}")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise DigestError("HTTP 响应超过大小限制")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise DigestError("HTTP 响应不是 JSON 对象")
            return result
    except urllib.error.HTTPError as error:
        # Never log URLs, response bodies, or credentials, including on redirects.
        raise DigestError(f"HTTP 状态 {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DigestError("网络连接失败或超时") from None
    except (ValueError, UnicodeError):
        raise DigestError("HTTP 响应不是有效 JSON") from None


def validate_summary(text):
    if not isinstance(text, str):
        raise DigestError("模型未返回文本摘要")
    text = text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = sum(line.startswith("- ") for line in lines)
    if not 40 <= len(text) <= 2500 or not 3 <= bullets <= 5 or lines[0].startswith("- "):
        raise DigestError("摘要格式无效：需要一句概括和 3～5 条要点")
    return text


def numeric_usage(value):
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {key: value[key] for key in keys if isinstance(value, dict)
            and type(value.get(key)) in (int, float)}


class Summarizer:
    def __init__(self, config, env):
        self.cfg = config
        self.env = env

    def cli_command(self):
        binary = shutil.which(self.cfg.get("claude_command", "claude"), path=self.env.get("PATH"))
        if not binary:
            raise DigestError("找不到 claude；请配置 model.claude_command 的绝对路径")
        command = [binary, "-p", "--output-format", "json", "--tools", "",
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                   "--disable-slash-commands", "--no-session-persistence", "--max-turns", "1",
                   "--system-prompt", PROMPT]
        if self.cfg.get("name"):
            command.extend(["--model", self.cfg["name"]])
        if self.cfg.get("claude_safe_mode", True):
            command.append("--safe-mode")
        return command

    def check(self):
        if self.cfg["provider"] == "anthropic":
            if not self.cfg.get("name"):
                raise DigestError("API 模式需要填写 model.name")
            if not self.env.get(self.cfg.get("api_key_env", "ANTHROPIC_API_KEY")):
                raise DigestError("API Key 环境变量未设置")
            if urllib.parse.urlsplit(self.cfg["api_url"]).scheme != "https":
                raise DigestError("API 地址必须使用 HTTPS")
        else:
            command = self.cli_command()
            try:
                result = subprocess.run([command[0], "--help"], capture_output=True, text=True,
                                        timeout=15, env=self.child_env())
            except (OSError, subprocess.TimeoutExpired):
                raise DigestError("无法检查 Claude Code CLI") from None
            required = ("--tools", "--strict-mcp-config", "--setting-sources",
                        "--disable-slash-commands", "--no-session-persistence", "--system-prompt")
            if self.cfg.get("claude_safe_mode", True):
                required += ("--safe-mode",)
            if result.returncode or any(flag not in result.stdout for flag in required):
                raise DigestError("Claude Code 缺少所需命令行选项，请更新后重试")

    def child_env(self):
        # Feishu secrets are deliberately not inherited by the model subprocess.
        names = {"HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "TMPDIR", "TZ",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                 "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"}
        env = {key: value for key, value in self.env.items()
               if key in names or key.startswith(("LC_", "ANTHROPIC_", "CLAUDE_"))}
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.cfg["max_output_tokens"])
        return env

    def summarize(self, title, text):
        if len(title) + len(text) > self.cfg["max_input_chars"]:
            raise DigestError("正文超过 max_input_chars；未截断、未调用模型，请人工检查")
        user_content = json.dumps({"title": title, "article": text}, ensure_ascii=False)
        if self.cfg["provider"] == "anthropic":
            self.check()
            result = post_json(self.cfg["api_url"], {
                "model": self.cfg["name"], "max_tokens": self.cfg["max_output_tokens"],
                "system": PROMPT, "messages": [{"role": "user", "content": user_content}],
            }, {"x-api-key": self.env[self.cfg.get("api_key_env", "ANTHROPIC_API_KEY")],
                "anthropic-version": self.cfg.get("api_version", "2023-06-01")},
                timeout=self.cfg["timeout_seconds"])
            if result.get("stop_reason") != "end_turn":
                raise DigestError("API 未正常完成摘要（可能输出截断）")
            content = result.get("content", [])
            if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
                raise DigestError("API 摘要响应格式无效")
            if any(not isinstance(item.get("text"), str) for item in content if item.get("type") == "text"):
                raise DigestError("API 摘要响应格式无效")
            summary = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
        else:
            # A fresh /tmp working directory prevents reading project CLAUDE.md and repo context.
            with tempfile.TemporaryDirectory(prefix="werss-summary-") as workdir:
                try:
                    process = subprocess.Popen(self.cli_command(), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        cwd=workdir, env=self.child_env(), start_new_session=True)
                    try:
                        stdout, _stderr = process.communicate(user_content, timeout=self.cfg["timeout_seconds"])
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                        raise DigestError("Claude Code 总结超时") from None
                    except BaseException:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                        raise
                except OSError:
                    raise DigestError("Claude Code 启动失败") from None
            if process.returncode:
                raise DigestError(f"Claude Code 返回退出码 {process.returncode}；请检查登录、版本及模型配置")
            try:
                result = json.loads(stdout)
            except ValueError:
                raise DigestError("Claude Code 未返回有效 JSON") from None
            if not isinstance(result, dict) or result.get("is_error") or result.get("subtype") != "success":
                raise DigestError("Claude Code 未成功完成摘要")
            summary = result.get("result")
        return validate_summary(summary), numeric_usage(result.get("usage", {}))


def webhook_url(group, env):
    value = env.get(group["webhook_env"], "")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "open.feishu.cn"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9-]+", parsed.path)):
        raise DigestError("飞书 webhook 未配置或格式无效（仅支持 open.feishu.cn 自定义机器人）")
    if group.get("sign_secret_env") and not env.get(group["sign_secret_env"]):
        raise DigestError("飞书签名密钥环境变量未设置")
    return value


def target_hash(group, env):
    return hashlib.sha256(webhook_url(group, env).encode()).hexdigest()


def send_feishu(group, env, text, now):
    url = webhook_url(group, env)
    body = {"msg_type": "text", "content": {"text": text}}
    if group.get("sign_secret_env"):
        timestamp = str(int(now))
        key = f"{timestamp}\n{env[group['sign_secret_env']]}".encode()
        body.update(timestamp=timestamp, sign=base64.b64encode(
            hmac.new(key, b"", hashlib.sha256).digest()).decode())
    result = post_json(url, body)
    if type(result.get("code")) is not int or result["code"] != 0:
        code = result.get("code")
        suffix = str(code) if type(code) is int else "缺少有效 code"
        raise DigestError(f"飞书业务响应失败：{suffix}")
