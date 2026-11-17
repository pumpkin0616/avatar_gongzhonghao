import os
import logging
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
MAX_CONTENT_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass
class WeChatConfig:
    app_id: str
    app_secret: str


@dataclass
class AIConfig:
    api_key: str
    base_url: str
    model: str


@dataclass
class ArticleConfig:
    images_dir: Path
    used_images_dir: Path
    temp_dir: Path
    images_per_article: int
    title_prefix: str
    author: str
    digest: str


@dataclass
class ScheduleConfig:
    schedule_time: str


class Config:
    def __init__(self):
        self._validated = False

    def _get_env(self, key: str, default: str = None, required: bool = False) -> str:
        value = os.getenv(key, default)
        if required and (value is None or value == ""):
            raise EnvironmentError(f"环境变量 {key} 未配置")
        return value

    def ensure_credentials(self):
        """延迟验证凭据，只在真正需要访问微信或AI接口时才调用。"""
        if self._validated:
            return
        wechat_app_id = self._get_env("WECHAT_APP_ID", required=True)
        wechat_app_secret = self._get_env("WECHAT_APP_SECRET", required=True)
        ai_api_key = self._get_env("AI_API_KEY", required=True)
        if (
            wechat_app_id == "your_app_id_here"
            or wechat_app_secret == "your_app_secret_here"
            or ai_api_key == "your_api_key_here"
        ):
            raise EnvironmentError("请先在 .env 文件中配置真实的凭据（WECHAT_APP_ID / WECHAT_APP_SECRET / AI_API_KEY）")
        self._validated = True

    @property
    def wechat(self) -> WeChatConfig:
        self.ensure_credentials()
        return WeChatConfig(
            app_id=self._get_env("WECHAT_APP_ID", required=True),
            app_secret=self._get_env("WECHAT_APP_SECRET", required=True),
        )

    @property
    def ai(self) -> AIConfig:
        self.ensure_credentials()
        return AIConfig(
            api_key=self._get_env("AI_API_KEY", required=True),
            base_url=self._get_env("AI_BASE_URL", "https://api.openai.com/v1"),
            model=self._get_env("AI_MODEL", "gpt-4o-mini"),
        )

    @property
    def article(self) -> ArticleConfig:
        images_dir = Path(self._get_env("IMAGES_DIR", "./images"))
        used_images_dir = Path(self._get_env("USED_IMAGES_DIR", "./used"))
        temp_dir = Path(self._get_env("TEMP_DIR", "./temp"))

        if not images_dir.is_absolute():
            images_dir = BASE_DIR / images_dir
        if not used_images_dir.is_absolute():
            used_images_dir = BASE_DIR / used_images_dir
        if not temp_dir.is_absolute():
            temp_dir = BASE_DIR / temp_dir

        images_dir.mkdir(parents=True, exist_ok=True)
        used_images_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        return ArticleConfig(
            images_dir=images_dir,
            used_images_dir=used_images_dir,
            temp_dir=temp_dir,
            images_per_article=int(self._get_env("IMAGES_PER_ARTICLE", "10")),
            title_prefix=self._get_env(
                "ARTICLE_TITLE_PREFIX",
                "WeChat ins小众高级个性头像10张",
            ),
            author=self._get_env("ARTICLE_AUTHOR", ""),
            digest=self._get_env("ARTICLE_DIGEST", ""),
        )

    @property
    def schedule(self) -> ScheduleConfig:
        return ScheduleConfig(
            schedule_time=self._get_env("SCHEDULE_TIME", "09:00"),
        )

    @property
    def log_level(self) -> int:
        level_str = self._get_env("LOG_LEVEL", "INFO").upper()
        return getattr(logging, level_str, logging.INFO)


config = Config()
