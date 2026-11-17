import json
import re
from typing import List, Dict, Any, Iterable, Optional
from pathlib import Path

from requests import RequestException
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import config
from .logger import logger


class AIGenerationError(Exception):
    pass


class ArticleContent:
    def __init__(
        self,
        title: str,
        intro: str,
        sections: List[Dict[str, str]],
        outro: str,
        digest: str = "",
        title_candidates: Optional[List[str]] = None,
    ):
        self.title = title
        self.intro = intro
        self.sections = sections
        self.outro = outro
        self.digest = digest or intro[:120]
        self.title_candidates = title_candidates or [title]

    def __repr__(self):
        return f"ArticleContent(title={self.title!r}, sections={len(self.sections)})"


class AIGenerator:
    SYSTEM_PROMPT = """为微信公众号写 5 条通用的文艺标题短句，每条不超过 20 字。
每条用不同的意象和句式，可以写日常、季节、城市、独处或心情，不描述图片。
语言自然克制，避免「每一张都是故事」「一眼心动的氛围感」「换个头像换个心情」这类套话。
短句只作为标题后半句，不加头像字样、固定前缀、书名号、序号或竖线。
只返回一个 JSON 对象，titles 字段为短句数组。"""

    FALLBACK_TITLES = (
        "晚风替我关了灯",
        "给日常留一扇窗",
        "星期天没有闹钟",
        "慢车经过旧城",
        "把春天折进书页",
        "写给自己的一行小字",
        "街角的灯依次亮起",
        "雨停在九点过后",
        "海那边也有黄昏",
        "明天见，今天就到这里",
        "两站路的闲暇",
        "橘子要留到晴天吃",
    )

    def __init__(self):
        self.api_key = config.ai.api_key
        self.base_url = config.ai.base_url.rstrip("/")
        self.model = config.ai.model

    def _is_deepseek_like(self) -> bool:
        return "deepseek" in self.base_url.lower() or "deepseek" in self.model.lower()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=3, max=15),
        reraise=True,
    )
    def _call_llm(self, messages: List[Dict[str, str]]) -> str:
        import requests

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # DeepSeek V3 (deepseek-chat) 对 response_format=json_object 支持不稳定，
        # 并且 deepseek-reasoner 等模型可能直接报错。
        # 所以默认只在 OpenAI 官方域名上使用严格 JSON 模式。
        # 其余域名/模型 使用强化提示词+强校验即可。
        use_json_mode = not self._is_deepseek_like()

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7 if self._is_deepseek_like() else 0.8,
            "max_tokens": 3500,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self._is_deepseek_like():
            # DeepSeek 支持 frequency_penalty 等参数可能触发兼容性更好
            payload.setdefault("frequency_penalty", 0.3)

        logger.info(
            f"调用 LLM [{self.model}] @ {self.base_url}"
            f" (JSON模式: {'是' if use_json_mode else '否（提示词约束）'}"
            " 生成标题候选..."
        )

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=180)
            # 如果启用了 JSON 模式但失败（如 400/422），且是 DeepSeek，
            # 立即降级为不带 response_format 再请求一次
            if (
                use_json_mode
                and resp.status_code in (400, 422)
                and self._is_deepseek_like()
            ):
                logger.warning(
                    f"启用 response_format 导致 {resp.status_code}，"
                    f"降级重试：移除 response_format 重新请求"
                )
                payload.pop("response_format", None)
                resp = requests.post(url, headers=headers, json=payload, timeout=180)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"LLM 请求异常: {e}")
            raise

        if "error" in data:
            err = data["error"]
            msg = err if isinstance(err, str) else err.get("message", str(err))
            raise AIGenerationError(f"LLM API错误: {msg}")

        try:
            choice = data["choices"][0]
            message = choice.get("message", {})
            # 兼容推理模型可能在 reasoning_content 中有内容
            content = message.get("content") or ""
            # 处理 DeepSeek / Qwen 等可能用 <｜begin_of_thought｜> 等标签
            content = re.sub(r"<\|\S*?>[\s\S]*?<\|\S*?>", "", content).strip()
            if not content:
                raise AIGenerationError("LLM 返回空内容")
        except (KeyError, IndexError, TypeError) as e:
            raise AIGenerationError(f"解析 LLM 返回结构异常: {e} ; data={data!r}") from e

        return content

    def _parse_json(self, text: str) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("直接JSON解析失败，尝试提取代码块中的JSON...")
            match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
            match = re.search(r"(\{[\s\S]*\})", text)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError as e:
                    raise AIGenerationError(f"无法解析JSON内容: {e}\n原始内容: {text[:500]}")
            raise AIGenerationError(f"未找到JSON内容，原始文本: {text[:500]}")

    @staticmethod
    def _build_content(image_paths: List[Path], suffixes: Iterable[str]) -> ArticleContent:
        article_config = config.article
        prefix = article_config.title_prefix.strip().rstrip("|｜").rstrip()
        title_start = f"{prefix} | " if prefix else ""
        candidates = []
        for suffix in suffixes:
            if not isinstance(suffix, str):
                continue
            suffix = suffix.strip()
            if not suffix or len(suffix) > 20 or any(c in suffix for c in "|｜《》\n\r"):
                continue
            title = title_start + suffix
            if len(title) <= 50:
                candidates.append(title)

        if not candidates:
            raise AIGenerationError("没有符合长度和格式要求的标题候选")

        return ArticleContent(
            title=candidates[0],
            intro="",
            sections=[
                {"image_index": i, "caption": "", "description": ""}
                for i in range(len(image_paths))
            ],
            outro="",
            digest=article_config.digest or f"本期精选 {len(image_paths)} 张头像。",
            title_candidates=candidates,
        )

    def generate_article(self, image_paths: List[Path]) -> ArticleContent:
        if not image_paths:
            raise AIGenerationError("图片列表为空，无法生成文章")

        messages = [{"role": "user", "content": self.SYSTEM_PROMPT}]
        try:
            raw_text = self._call_llm(messages)
        except RequestException as exc:
            raise AIGenerationError("AI 标题请求失败") from exc
        logger.debug(f"LLM 原始返回 (前500字符): {raw_text[:500]}")

        data = self._parse_json(raw_text)
        titles = data.get("titles") if isinstance(data, dict) else None
        if not isinstance(titles, list):
            raise AIGenerationError("AI 返回缺少 titles 数组")
        return self._build_content(image_paths, titles)

    def generate_fallback_content(self, image_paths: List[Path]) -> ArticleContent:
        """当AI生成失败时的兜底内容"""
        logger.warning("使用预设文艺短句生成标题")
        return self._build_content(image_paths, self.FALLBACK_TITLES)
