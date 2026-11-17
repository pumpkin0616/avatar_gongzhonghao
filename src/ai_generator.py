import json
import re
from typing import List, Dict, Any, Tuple
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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
    ):
        self.title = title
        self.intro = intro
        self.sections = sections
        self.outro = outro
        self.digest = digest or intro[:120]

    def __repr__(self):
        return f"ArticleContent(title={self.title!r}, sections={len(self.sections)})"


class AIGenerator:
    SYSTEM_PROMPT = """你是一名资深的微信公众号文案编辑，擅长撰写图文并茂、吸引读者的公众号文章。
请根据提供的图片文件名，创作一篇高质量的微信公众号文章。

要求：
1. 标题要吸引人，具有传播力，50字以内，不要用书名号，固定前缀为： WeChat ins小众高级个性头像10张 |
2. 开头要有引人入胜的引导语
3. 为每张图片配一段说明文字，描述图片内容并赋予情感或故事性
4. 段落之间要有自然的过渡
5. 结尾要有总结或互动引导
6. 语言风格：温暖、真诚、有画面感，适合公众号读者
7. 全文控制在800-1500字左右
8. 🔴 最重要：你的整个回复必须是【纯 JSON】，不能有任何额外的开场白、解释、Markdown 标记、代码块、
   或"以下是..."之类的文字。直接从 { 开始，到 } 结束。"""

    OUTPUT_SCHEMA = """
必须严格按照以下 JSON 结构返回（只返回 JSON 本身，不包含 ```json 或任何说明文字）：
{
    "title": "文章标题，20字以内",
    "intro": "开头引导段落",
    "sections": [
        {"image_index": 0, "caption": "图1上方小标题（可选，可为空字符串）", "description": "图1的详细说明文字"},
        {"image_index": 1, "caption": "图2小标题", "description": "图2的详细说明文字"},
        {"image_index": N, "caption": "...", "description": "..."}
    ],
    "outro": "结尾段落",
    "digest": "文章摘要，不超过120字"
}
注意：sections 数组的长度必须严格等于图片数量。image_index 必须从 0 开始按顺序递增。
"""

    def __init__(self):
        self.api_key = config.ai.api_key
        self.base_url = config.ai.base_url.rstrip("/")
        self.model = config.ai.model

    def _build_user_prompt(self, image_paths: List[Path]) -> str:
        filenames = [p.name for p in image_paths]
        file_list = "\n".join(f"  [{i}] {name}" for i, name in enumerate(filenames))
        return f"""
以下是本次文章使用的 {len(filenames)} 张图片（按顺序）：
{file_list}

请为这些图片创作一篇完整的微信公众号文章。sections 数组的长度必须等于图片数量 {len(filenames)}，
image_index 从 0 到 {len(filenames) - 1} 依次对应每张图片。

{self.OUTPUT_SCHEMA}
"""

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
            " 生成文章内容..."
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

    def _validate_content(self, data: Dict[str, Any], expected_images: int) -> None:
        required_keys = ["title", "intro", "sections", "outro"]
        for k in required_keys:
            if k not in data:
                raise AIGenerationError(f"生成内容缺少必填字段: {k}")

        sections = data["sections"]
        if not isinstance(sections, list) or len(sections) != expected_images:
            raise AIGenerationError(
                f"sections 数量错误: 期望 {expected_images}, 实际 {len(sections) if isinstance(sections, list) else '非数组'}"
            )

        for i, sec in enumerate(sections):
            if not isinstance(sec, dict) or "image_index" not in sec or "description" not in sec:
                raise AIGenerationError(f"sections[{i}] 格式错误，需要 image_index 和 description 字段")
            if sec["image_index"] != i:
                logger.warning(f"sections[{i}] 的 image_index={sec['image_index']} 与位置不匹配，已自动修正")
                sec["image_index"] = i

    def generate_article(self, image_paths: List[Path]) -> ArticleContent:
        if not image_paths:
            raise AIGenerationError("图片列表为空，无法生成文章")

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(image_paths)},
        ]

        raw_text = self._call_llm(messages)
        logger.debug(f"LLM 原始返回 (前500字符): {raw_text[:500]}")

        data = self._parse_json(raw_text)
        self._validate_content(data, len(image_paths))

        content = ArticleContent(
            title=data["title"].strip(),
            intro=data["intro"].strip(),
            sections=data["sections"],
            outro=data["outro"].strip(),
            digest=data.get("digest", "")[:120].strip(),
        )

        logger.info(f"文章生成成功，标题: {content.title}，共 {len(content.sections)} 个图片段落")
        return content

    def generate_fallback_content(self, image_paths: List[Path]) -> ArticleContent:
        """当AI生成失败时的兜底内容"""
        logger.warning("使用兜底内容生成文章")
        sections = []
        for i, p in enumerate(image_paths):
            sections.append({
                "image_index": i,
                "caption": f"第 {i + 1} 幅",
                "description": f"欣赏这张精美的图片，感受其中的意境与故事。每一个画面都值得我们细细品味，在光影流转间发现生活中的美好。",
            })
        return ArticleContent(
            title="每日美图精选",
            intro="今天为大家带来一组精选图片，希望能给你的一天带来一份美好与宁静。",
            sections=sections,
            outro="感谢你的阅读与陪伴，愿每一张图片都能在你心中留下一抹温暖。我们明天再见！",
            digest="每日精选美图，带你感受生活中的美好瞬间。",
        )
