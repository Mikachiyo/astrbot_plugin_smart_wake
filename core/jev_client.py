"""Jev API 客户端：双通道、key 轮询 + 冷却、429 指数退避（最多重试 2 次）。

TypeSafe AI "System One" 单步决策模型，不兼容 OpenAI chat 格式。
- 官方直连：POST {base_url}/v1/systemone
- OpenRouter：POST {base_url}/api/alpha/decisions（注意没有 /v1）
"""

import asyncio
import time

import httpx
from astrbot.api import logger

# OpenRouter 通道上模型 ID 固定映射为 typesafe/jev-1.13
OPENROUTER_MODEL = "typesafe/jev-1.13"


class JevError(Exception):
    """Jev 请求失败（网络/鉴权/限流等），上层按"判定不通过"处理。"""


class JevClient:
    def __init__(self, config):
        self._config = config
        self._rr_index = 0
        self._key_fail_until: dict[str, float] = {}  # key -> 冷却截止

    # ----- 通道与模型 -----
    def _channel(self) -> str:
        return (self._config.get("jev_channel") or "official").strip()

    def _url(self) -> str:
        base = (self._config.get("jev_base_url") or "").strip().rstrip("/")
        if not base:
            base = "https://openrouter.ai" if self._channel() == "openrouter" else "https://api.typesafe.ai"
        return f"{base}/api/alpha/decisions" if self._channel() == "openrouter" else f"{base}/v1/systemone"

    def _model(self) -> str:
        model = (self._config.get("jev_model") or "jev-1.13.0").strip()
        if self._channel() == "openrouter" and model.startswith("jev"):
            return OPENROUTER_MODEL
        return model

    # ----- key 轮询 -----
    def _available_keys(self) -> list[str]:
        keys = [k.strip() for k in (self._config.get("jev_api_keys") or []) if isinstance(k, str) and k.strip()]
        if not keys:
            return []
        now = time.time()
        usable = [k for k in keys if self._key_fail_until.get(k, 0) <= now]
        if not usable:
            usable = keys  # 全部冷却中也照试
        start = self._rr_index % len(usable)
        self._rr_index += 1
        return usable[start:] + usable[:start]

    def _mark_failed(self, key: str) -> None:
        cooldown = float(self._config.get("key_cooldown_seconds", 300) or 300)
        self._key_fail_until[key] = time.time() + cooldown

    def _mark_ok(self, key: str) -> None:
        self._key_fail_until.pop(key, None)

    # ----- 主调用 -----
    async def decide(self, state: dict, questions: dict) -> dict:
        """调用一次 Jev。返回完整响应 dict；任何失败抛 JevError（上层兜底）。"""
        keys = self._available_keys()
        if not keys:
            raise JevError("未配置 Jev API Key")
        body = {"model": self._model(), "state": state, "questions": questions}
        timeout = float(self._config.get("jev_timeout", 30) or 30)
        url = self._url()
        last_err = None
        for key in keys:
            try:
                result = await self._post(url, key, body, timeout)
                self._mark_ok(key)
                return result
            except Exception as e:
                last_err = e
                self._mark_failed(key)
                logger.warning(f"[smart_wake] Jev key(...{key[-4:]}) 失败: {e}")
        raise JevError(f"所有 Jev key 均失败，最后错误: {last_err}")

    async def _post(self, url: str, key: str, body: dict, timeout: float) -> dict:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for attempt in range(3):  # 首次 + 最多 2 次重试（仅 429）
                resp = await client.post(url, headers=headers, json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    if "answers" not in data:
                        raise JevError(f"响应缺少 answers 字段: {str(data)[:200]}")
                    return data
                err_text = resp.text[:200]
                if resp.status_code == 429 and attempt < 2:
                    retry_after = resp.headers.get("retry-after")
                    try:
                        wait = float(retry_after) if retry_after else 2 ** attempt
                    except ValueError:
                        wait = 2 ** attempt
                    await asyncio.sleep(min(wait, 30))
                    continue
                raise JevError(f"HTTP {resp.status_code}: {err_text}")
        raise JevError("429 重试耗尽")

    async def test_connection(self) -> tuple[bool, str]:
        """dashboard 测试连接：发一条最小请求。"""
        state = {"bot_name": "测试", "recent_messages": ["A: 你好"]}
        questions = {"ping": {"type": "noul", "instructions": "这句话是问候吗？",
                              "criteria": {"true": "是问候", "false": "不是问候"}}}
        try:
            data = await self.decide(state, questions)
        except Exception as e:
            return False, str(e)
        usage = data.get("usage") or {}
        return True, f"通道={self._channel()}，模型={data.get('model') or self._model()}，" \
                     f"tokens={usage.get('input_tokens', '?')}+{usage.get('output_tokens', '?')}"
