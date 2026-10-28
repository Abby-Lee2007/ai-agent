"""大模型客户端（OpenAI 兼容接口）。

刻意只用标准库 `urllib` 实现，这样项目不装任何第三方依赖也能跑；
任何兼容 OpenAI Chat Completions 协议的服务都能直接接上：
DeepSeek / 通义千问 / 智谱 GLM / Kimi / OpenAI ...

调用失败时不会抛异常打断对话，而是返回 None，
由上层自动回退到内置规则解析器，保证演示不中断。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config import LLMConfig

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def guess_mime(path: str) -> str:
    return MIME_TYPES.get(Path(path).suffix.lower(), "image/png")


class LLMClient:
    """极简大模型客户端。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else LLMConfig.API_KEY
        self.base_url = (base_url or LLMConfig.BASE_URL).rstrip("/")
        self.model = model or LLMConfig.MODEL
        self.timeout = timeout or LLMConfig.TIMEOUT
        self.last_error: str = ""
        self.calls: int = 0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: List[Dict[str, str]],
        json_mode: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """发起一次对话补全。任何异常都不会抛出，失败返回 None。"""
        if not self.available:
            self.last_error = "未配置 API Key"
            return None

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": LLMConfig.TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or LLMConfig.MAX_TOKENS,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
            self.calls += 1
            data = json.loads(body)
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:200]
            self.last_error = f"HTTP {exc.code}: {detail}"
            return None
        except urllib.error.URLError as exc:
            self.last_error = f"网络错误: {exc.reason}"
            return None
        except (KeyError, IndexError, json.JSONDecodeError, TimeoutError) as exc:
            self.last_error = f"响应解析失败: {exc}"
            return None

    def chat_json(self, messages: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
        """要求模型返回 JSON 并解析成 dict。"""
        raw = self.chat(messages, json_mode=True, temperature=0.0)
        if raw is None:
            return None
        return self.parse_json(raw)

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
    ) -> Optional[Dict[str, Any]]:
        """Ask an OpenAI-compatible model for one validated tool call."""
        if not self.available:
            self.last_error = "未配置 API Key"
            return None
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": min(LLMConfig.MAX_TOKENS, 1024),
            "stream": False,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
            self.calls += 1
            data = json.loads(body)
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                self.last_error = "模型未返回工具调用"
                return None
            function = calls[0].get("function") or {}
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                self.last_error = "工具参数不是 JSON 对象"
                return None
            return {
                "name": str(function.get("name") or ""),
                "arguments": arguments,
            }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:200]
            self.last_error = f"HTTP {exc.code}: {detail}"
            return None
        except urllib.error.URLError as exc:
            self.last_error = f"网络错误: {exc.reason}"
            return None
        except (KeyError, IndexError, json.JSONDecodeError, TimeoutError) as exc:
            self.last_error = f"工具调用响应解析失败: {exc}"
            return None

    def chat_vision(
        self,
        image_path: str,
        prompt: str,
        json_mode: bool = True,
    ) -> Optional[str]:
        """把本地图片以 base64 塞进多模态消息里发给模型。

        需要模型支持视觉输入（如 qwen-vl-max / glm-4v / gpt-4o）。
        失败返回 None，由上层降级到本地 OCR。
        """
        if not self.available:
            self.last_error = "未配置 API Key"
            return None
        try:
            data = Path(image_path).read_bytes()
        except OSError as exc:
            self.last_error = f"读取图片失败：{exc}"
            return None
        if len(data) > 8 * 1024 * 1024:
            self.last_error = "图片超过 8MB，建议先压缩再识别"
            return None
        encoded = base64.b64encode(data).decode("ascii")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{guess_mime(image_path)};base64,{encoded}"}},
        ]
        messages: List[Dict[str, Any]] = [{"role": "user", "content": content}]
        return self.chat(messages, json_mode=json_mode, temperature=0.0, max_tokens=2048)

    def chat_vision_json(self, image_path: str, prompt: str) -> Optional[Dict[str, Any]]:
        """多模态识图并要求返回 JSON。"""
        raw = self.chat_vision(image_path, prompt, json_mode=True)
        if raw is None:
            return None
        return self.parse_json(raw)

    def parse_json(self, raw: str) -> Optional[Dict[str, Any]]:
        """从模型回复里抠出 JSON（兼容 ```json 包裹与前后废话）。"""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            self.last_error = "模型未返回 JSON"
            return None
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            self.last_error = f"JSON 解析失败: {exc}"
            return None

