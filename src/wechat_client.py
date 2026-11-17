import json
import time
import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import config
from .logger import logger


class WeChatAPIError(Exception):
    def __init__(self, errcode: int, errmsg: str):
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"微信API错误 [{errcode}]: {errmsg}")


@dataclass
class WeChatImageMaterial:
    media_id: str
    url: str


@dataclass
class WeChatDraftResult:
    media_id: str
    article_url: Optional[str] = None


class WeChatClient:
    BASE_URL = "https://api.weixin.qq.com"
    TOKEN_FILE = ".wechat_token.json"

    @staticmethod
    def normalize_wechat_image_url(url: str) -> str:
        """
        规范化微信正文图 URL（修复显示400问题的关键：绝不能改动域名！）
        只做一件事：在 query 里追加/覆盖 from=appmsg

        原因：
          - 微信上传接口返回的 URL 里，域名（mmbiz.qpic.cn / mmbiz.qlogo.cn）和 路径
            （/mmbiz_png/xxx、/sz_mmbiz_png/xxx 等）是严格绑定 CDN 路由的，
            强改域名会让服务端无法匹配到图片资源，直接 400 / 404。
          - from=appmsg 是公众号编辑器后台会自动加上的参数，表示该图用于图文消息。
            但上传后 API 默认不会给，所以要手动补（幂等：已有时覆盖成 appmsg）。
        """
        if not url or "://" not in url:
            return url
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        if not parsed.scheme or not parsed.netloc:
            return url

        query_list = parse_qsl(parsed.query, keep_blank_values=True)
        has_from = any(k == "from" for k, _ in query_list)
        if not has_from:
            query_list.append(("from", "appmsg"))
        else:
            query_list = [
                (k, "appmsg") if k == "from" else (k, v)
                for k, v in query_list
            ]
        new_query = urlencode(query_list)

        # 只改 query，其他一律不动（域名、端口、路径、fragment 全保留原）
        new_parsed = parsed._replace(query=new_query)
        return urlunparse(new_parsed)



    def __init__(self):
        self.app_id = config.wechat.app_id
        self.app_secret = config.wechat.app_secret
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._token_cache_path = config.article.temp_dir / self.TOKEN_FILE
        self._load_cached_token()

    def _load_cached_token(self):
        try:
            if self._token_cache_path.exists():
                data = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
                if data.get("expires_at", 0) > time.time() + 60:
                    self._access_token = data["access_token"]
                    self._token_expires_at = data["expires_at"]
                    logger.debug("从缓存加载 access_token 成功")
        except Exception as e:
            logger.warning(f"加载缓存 token 失败: {e}")

    def _save_cached_token(self):
        try:
            data = {
                "access_token": self._access_token,
                "expires_at": self._token_expires_at,
            }
            self._token_cache_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"保存缓存 token 失败: {e}")

    def _check_response(self, response: requests.Response) -> Dict[str, Any]:
        response.raise_for_status()
        data = response.json()
        if "errcode" in data and data["errcode"] != 0:
            errcode = data["errcode"]
            errmsg = data.get("errmsg", "未知错误")
            if errcode == 40001 or errcode == 42001:
                self._access_token = None
                self._token_expires_at = 0
                if self._token_cache_path.exists():
                    self._token_cache_path.unlink()
                raise WeChatAPIError(errcode, f"{errmsg} (token已失效，将自动刷新)")
            raise WeChatAPIError(errcode, errmsg)
        return data

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, WeChatAPIError)),
        reraise=True,
    )
    def get_access_token(self, force_refresh: bool = False) -> str:
        now = time.time()
        if (
            not force_refresh
            and self._access_token
            and self._token_expires_at > now + 60
        ):
            return self._access_token

        logger.info("请求新的 access_token ...")
        params = {
            "grant_type": "client_credential",
            "appid": self.app_id,
            "secret": self.app_secret,
        }
        resp = requests.get(f"{self.BASE_URL}/cgi-bin/token", params=params, timeout=30)
        data = self._check_response(resp)
        self._access_token = data["access_token"]
        self._token_expires_at = now + int(data.get("expires_in", 7200))
        self._save_cached_token()
        logger.info("access_token 获取成功")
        return self._access_token

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def upload_content_image(self, image_path: Path) -> str:
        """
        上传图文消息内的图片，获取可在正文中使用的 URL
        文档：https://developers.weixin.qq.com/doc/offiaccount/Asset_Management/Adding_Permanent_Assets.html
        接口：/cgi-bin/media/uploadimg
        """
        if not image_path.exists():
            raise FileNotFoundError(f"图片不存在: {image_path}")

        if image_path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(f"图片 {image_path.name} 超过10MB限制")

        ext = image_path.suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".bmp"):
            raise ValueError(f"不支持的图片格式: {ext}")

        access_token = self.get_access_token()
        url = f"{self.BASE_URL}/cgi-bin/media/uploadimg?access_token={access_token}"

        logger.info(f"上传正文图片: {image_path.name}")
        with open(image_path, "rb") as f:
            files = {"media": (image_path.name, f, "image/jpeg")}
            resp = requests.post(url, files=files, timeout=60)

        try:
            data = self._check_response(resp)
        except WeChatAPIError as e:
            if e.errcode == 40001 or e.errcode == 42001:
                access_token = self.get_access_token(force_refresh=True)
                url = f"{self.BASE_URL}/cgi-bin/media/uploadimg?access_token={access_token}"
                with open(image_path, "rb") as f:
                    files = {"media": (image_path.name, f, "image/jpeg")}
                    resp = requests.post(url, files=files, timeout=60)
                data = self._check_response(resp)
            else:
                raise

        img_url = data.get("url")
        if not img_url:
            raise WeChatAPIError(-1, f"上传图片返回空URL: {data}")
        img_url = self.normalize_wechat_image_url(img_url)
        logger.info(f"图片上传成功，URL: {img_url[:80]}...")
        return img_url

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def add_draft(
        self,
        title: str,
        content: str,
        thumb_media_id: Optional[str] = None,
        author: str = "",
        digest: str = "",
        content_source_url: str = "",
        need_open_comment: int = 0,
        only_fans_can_comment: int = 0,
    ) -> WeChatDraftResult:
        """
        创建草稿
        文档：https://developers.weixin.qq.com/doc/offiaccount/Draft_Box/Add_draft.html
        接口：/cgi-bin/draft/add
        """
        access_token = self.get_access_token()
        url = f"{self.BASE_URL}/cgi-bin/draft/add?access_token={access_token}"

        article = {
            "title": title,
            "content": content,
            "author": author,
            "digest": digest,
            "content_source_url": content_source_url,
            "need_open_comment": need_open_comment,
            "only_fans_can_comment": only_fans_can_comment,
        }
        if thumb_media_id:
            article["thumb_media_id"] = thumb_media_id

        payload = {"articles": [article]}
        headers = {"Content-Type": "application/json; charset=utf-8"}

        logger.info(f"创建草稿，标题: {title}")
        logger.debug(f"草稿内容 (前500字符): {content[:500]}...")

        resp = requests.post(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, timeout=60,
        )

        try:
            data = self._check_response(resp)
        except WeChatAPIError as e:
            if e.errcode == 40001 or e.errcode == 42001:
                access_token = self.get_access_token(force_refresh=True)
                url = f"{self.BASE_URL}/cgi-bin/draft/add?access_token={access_token}"
                resp = requests.post(
                    url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers, timeout=60,
                )
                data = self._check_response(resp)
            else:
                raise

        media_id = data.get("media_id")
        if not media_id:
            raise WeChatAPIError(-1, f"创建草稿未返回 media_id: {data}")

        result = WeChatDraftResult(media_id=media_id)
        logger.info(f"草稿创建成功，media_id: {media_id}")
        return result

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def upload_thumb_material(self, image_path: Path) -> str:
        """
        上传永久缩略图素材，返回 media_id 用于草稿的 thumb_media_id
        接口：/cgi-bin/material/add_material?type=thumb
        """
        if not image_path.exists():
            raise FileNotFoundError(f"缩略图不存在: {image_path}")

        size = image_path.stat().st_size
        if size > 64 * 1024:
            logger.warning(f"缩略图 {image_path.name} 较大 ({size/1024:.1f}KB)，建议不超过64KB")

        access_token = self.get_access_token()
        url = (
            f"{self.BASE_URL}/cgi-bin/material/add_material"
            f"?access_token={access_token}&type=thumb"
        )

        logger.info(f"上传缩略图素材: {image_path.name}")
        with open(image_path, "rb") as f:
            files = {"media": (image_path.name, f, "image/jpeg")}
            resp = requests.post(url, files=files, timeout=60)

        try:
            data = self._check_response(resp)
        except WeChatAPIError as e:
            if e.errcode == 40001 or e.errcode == 42001:
                access_token = self.get_access_token(force_refresh=True)
                url = (
                    f"{self.BASE_URL}/cgi-bin/material/add_material"
                    f"?access_token={access_token}&type=thumb"
                )
                with open(image_path, "rb") as f:
                    files = {"media": (image_path.name, f, "image/jpeg")}
                    resp = requests.post(url, files=files, timeout=60)
                data = self._check_response(resp)
            else:
                raise

        media_id = data.get("media_id")
        if not media_id:
            raise WeChatAPIError(-1, f"上传缩略图未返回 media_id: {data}")
        logger.info(f"缩略图上传成功，media_id: {media_id}")
        return media_id

    def get_draft_count(self) -> int:
        access_token = self.get_access_token()
        url = f"{self.BASE_URL}/cgi-bin/draft/count?access_token={access_token}"
        resp = requests.get(url, timeout=30)
        data = self._check_response(resp)
        return data.get("total_count", 0)
