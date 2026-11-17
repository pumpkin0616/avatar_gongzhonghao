from typing import List, Tuple, Optional
from pathlib import Path
from html import escape as html_escape

from .ai_generator import ArticleContent
from .logger import logger
from .wechat_client import WeChatClient


class HTMLGenerator:
    """
    按 test.html 模板生成微信公众号文章 HTML：
    - 顶部粉色下划线的分类标题条（使用文章标题）
    - 每两张图一组，两列展示，外框带左上/右下虚线装饰
    - 每两组之间穿插 ✦ ─ • ─ ✦ 风格装饰分隔线
    - 图片下方不生成说明文字
    - 结尾 CTA：拿图吱一声~点个赞也行❤️
    """

    ROOT_START = '<p><br></p>\n<section style="font-size: 16px; color: rgb(62, 62, 62);">'
    ROOT_END = '\n<p style="white-space: normal;"><br></p>\n</section>'

    ACCENT_COLOR = "rgb(255, 189, 204)"
    ACCENT_BG = "rgb(254, 216, 234)"
    DOTTED_BORDER_COLOR = "rgb(193, 193, 193)"

    @staticmethod
    def _spacer_svg() -> str:
        return (
            '<svg viewBox="0 0 1 1" '
            'style="float:left;line-height:0;width:0;vertical-align:top;"></svg>'
        )

    @classmethod
    def _render_top_title_bar(cls, title: str) -> str:
        """顶部粉色下划线的标题条（对应 test.html 里的「女生头像/ 搞怪/ 可爱 头像」）"""
        return f"""
<section style="margin-top: 10px; margin-bottom: 10px; text-align: center; position: static;">
  <section style="display: inline-block; vertical-align: top;">
    <section style="margin-bottom: -6px; line-height: 1em; padding-left: 2px; padding-right: 2px;">
      <p>{html_escape(title)}</p>
    </section>
    <section style="width: 100%; height: 10px; background-color: {cls.ACCENT_BG};">{cls._spacer_svg()}</section>
  </section>
</section>"""

    @classmethod
    def _render_single_image(cls, img_url: str, alt: str = "", img_index: int = 0, ext: str = "png") -> str:
        """
        生成 test.html 同款单图标签：
        <img class="raw-image rich_pages wxw-img"
             src="..."
             style="vertical-align: middle; width: 100%;"
             data-ratio="1" data-w="1080" data-s="300,640"
             data-type="png" data-imgqrcoded="1">
        """
        safe_url = WeChatClient.normalize_wechat_image_url(img_url)
        safe_alt = html_escape(alt or f"图片{img_index + 1}")
        safe_ext = (ext or "png").lower().lstrip(".")
        return (
            f'<img class="raw-image rich_pages wxw-img" '
            f'src="{safe_url}" '
            f'style="vertical-align: middle; width: 100%;" '
            f'data-ratio="1" data-w="1080" data-s="300,640" '
            f'data-type="{safe_ext}" '
            f'alt="{safe_alt}" />'
        )

    @classmethod
    def _render_two_image_block(cls, left_url: str, right_url: str,
                                left_ext: str = "png", right_ext: str = "png",
                                left_alt: str = "", right_alt: str = "",
                                block_index: int = 0) -> str:
        """两列图卡片：虚线框（左上+右下装饰）+ 左右两张图各 50%"""
        left_img = cls._render_single_image(left_url, left_alt, block_index * 2, left_ext)
        right_img = cls._render_single_image(right_url, right_alt, block_index * 2 + 1, right_ext)
        dotted = cls.DOTTED_BORDER_COLOR
        return f"""
<section style="text-align: center; position: static;">
  <section style="display: inline-block; vertical-align: top; width: 100%;">
    <section style="width: 2.5em; height: 2.5em; margin-bottom: -2.5em; border-left: 2px dotted {dotted}; border-top: 2px dotted {dotted};">{cls._spacer_svg()}</section>
    <section style="padding: 8px;">
      <section style="width: 100%; background-color: rgba(255, 255, 255, 0);">
        <section style="justify-content: center; display: flex; flex-flow: row; position: static;">
          <section style="display: inline-block; vertical-align: top; width: 50%; padding: 0px 5px 0px 0px; align-self: flex-start; flex: 0 0 auto;">
            <section style="margin: 0px 0%; line-height: 0; position: static;">
              <section style="vertical-align: middle; display: inline-block; line-height: 0;">{left_img}</section>
            </section>
          </section>
          <section style="display: inline-block; vertical-align: top; width: 50%; padding: 0px 0px 0px 5px; align-self: flex-start; flex: 0 0 auto;">
            <section style="margin: 0px 0%; line-height: 0; position: static;">
              <section style="vertical-align: middle; display: inline-block; line-height: 0;">{right_img}</section>
            </section>
          </section>
        </section>
      </section>
    </section>
    <section style="width: 2.5em; height: 2.5em; float: right; margin-top: -2.5em; border-right: 2px dotted {dotted}; border-bottom: 2px dotted {dotted};">{cls._spacer_svg()}</section>
  </section>
</section>"""

    @classmethod
    def _render_decoration_divider(cls) -> str:
        """✦ ─── • ─── ✦ 装饰分隔线"""
        accent = cls.ACCENT_COLOR
        return f"""
<section style="text-align: left; justify-content: flex-start; display: flex; flex-flow: row; position: static;">
  <section style="display: inline-block; vertical-align: middle; width: auto; align-self: center; flex: 0 0 auto; min-width: 5%; height: auto;">
    <section style="text-align: right; transform: translate3d(-6px, 0px, 0px); margin: -2px 0px 0px; position: static;">
      <section style="color: {accent}; text-align: center; font-size: 15px;"><p>✦</p></section>
    </section>
  </section>
  <section style="display: inline-block; vertical-align: middle; width: auto; align-self: center; flex: 100 100 0%; height: auto;">
    <section style="position: static;"><section style="background-color: {accent}; height: 1px;">{cls._spacer_svg()}</section></section>
  </section>
  <section style="display: inline-block; vertical-align: middle; width: auto; align-self: center; flex: 0 0 auto; min-width: 5%; height: auto;">
    <section style="text-align: center; color: {accent}; font-size: 18px;"><p>•</p></section>
  </section>
  <section style="display: inline-block; vertical-align: middle; width: auto; align-self: center; flex: 100 100 0%; height: auto;">
    <section style="position: static;"><section style="background-color: {accent}; height: 1px;">{cls._spacer_svg()}</section></section>
  </section>
  <section style="display: inline-block; vertical-align: middle; width: auto; align-self: center; flex: 0 0 auto; min-width: 5%; height: auto;">
    <section style="text-align: right; transform: translate3d(6px, 0px, 0px); margin: -2px 0px 0px; position: static;">
      <section style="color: {accent}; text-align: center; font-size: 15px;"><p>✦</p></section>
    </section>
  </section>
</section>"""

    @classmethod
    def _render_cta(cls, text: str = "拿图吱一声~点个赞也行❤️") -> str:
        """底部 CTA 互动区"""
        return f"""
<section style="line-height: 0; text-align: center; margin: 10px 0px 3px; position: static;">
  <section style="text-align: center; margin: 0px 0px 10px; position: static;">
    <section style="text-align: justify; font-size: 14px;">
      <p style="text-align: center; white-space: normal;"><strong>{html_escape(text)}</strong></p>
    </section>
  </section>
</section>"""

    @staticmethod
    def _guess_ext(url: str, fallback: str = "png") -> str:
        """从 wx_fmt=xxx 查询参数或后缀里猜图片扩展名"""
        if not url:
            return fallback
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            fmt = qs.get("wx_fmt", [None])[0]
            if fmt:
                return fmt.lower()
            ext = Path(parsed.path).suffix.lower().lstrip(".")
            if ext in ("jpg", "jpeg", "png", "gif", "bmp", "webp"):
                return "jpg" if ext == "jpeg" else ext
        except Exception:
            pass
        return fallback

    def generate(
        self,
        content: ArticleContent,
        image_wechat_urls: List[str],
    ) -> str:
        """
        按 test.html 模板输出完整的文章正文 HTML。
        - 如果图片数是奇数，最后一张单独占 100% 宽度（用「左列=空占位 + 右列=图」的方式兜底，
          或者干脆生成一个单图 block，这里为了简单直接用两列逻辑；最后一张若是奇数，另作处理）
        """
        n_img = len(image_wechat_urls)
        n_sec = len(content.sections)
        if n_img != n_sec:
            logger.warning(
                f"图片URL数量({n_img})与sections数量({n_sec})不匹配，按较小数生成"
            )
        total = min(n_img, n_sec)

        parts: List[str] = []
        parts.append(self.ROOT_START)

        parts.append(self._render_top_title_bar(content.title))

        i = 0
        block_index = 0
        while i < total:
            left_idx = i
            right_idx = i + 1 if i + 1 < total else i

            left_url = image_wechat_urls[left_idx]
            right_url = image_wechat_urls[right_idx]
            left_ext = self._guess_ext(left_url)
            right_ext = self._guess_ext(right_url)

            if right_idx == left_idx:
                # 最后只剩一张：生成一个特殊的「单图卡片」，两边padding让它居中占一列宽度
                single_block = self._render_single_image_block(
                    left_url, left_ext, "", block_index
                )
                parts.append(single_block)
            else:
                parts.append(
                    self._render_two_image_block(
                        left_url, right_url,
                        left_ext=left_ext, right_ext=right_ext,
                        left_alt="",
                        right_alt="",
                        block_index=block_index,
                    )
                )

            # 如果不是最后一组，加上装饰分隔线
            if i + 2 < total:
                parts.append(self._render_decoration_divider())

            i += 2
            block_index += 1

        # 结尾
        parts.append(self._render_decoration_divider())
        parts.append(self._render_cta())

        parts.append(self.ROOT_END)

        html = "\n".join(parts)
        size_kb = len(html.encode("utf-8")) / 1024
        logger.info(f"文章HTML生成成功，大小: {size_kb:.1f}KB")
        if size_kb > 2000:
            logger.warning(f"HTML内容较大 ({size_kb:.1f}KB)，微信公众号建议不超过2MB")
        return html

    @classmethod
    def _render_single_image_block(cls, img_url: str, ext: str, alt: str, block_index: int) -> str:
        """奇数尾图：单列居中，同样带虚线边框装饰"""
        single_img = cls._render_single_image(img_url, alt, block_index * 2, ext)
        dotted = cls.DOTTED_BORDER_COLOR
        return f"""
<section style="text-align: center; position: static;">
  <section style="display: inline-block; vertical-align: top; width: 100%;">
    <section style="width: 2.5em; height: 2.5em; margin-bottom: -2.5em; border-left: 2px dotted {dotted}; border-top: 2px dotted {dotted};">{cls._spacer_svg()}</section>
    <section style="padding: 8px;">
      <section style="width: 100%; background-color: rgba(255, 255, 255, 0);">
        <section style="justify-content: center; display: flex; flex-flow: row; position: static;">
          <section style="display: inline-block; vertical-align: top; width: 100%; padding: 0px; align-self: flex-start; flex: 0 0 auto;">
            <section style="margin: 0px 0%; line-height: 0; position: static;">
              <section style="vertical-align: middle; display: inline-block; line-height: 0;">{single_img}</section>
            </section>
          </section>
        </section>
      </section>
    </section>
    <section style="width: 2.5em; height: 2.5em; float: right; margin-top: -2.5em; border-right: 2px dotted {dotted}; border-bottom: 2px dotted {dotted};">{cls._spacer_svg()}</section>
  </section>
</section>"""

    @classmethod
    def _render_description(cls, text: str) -> str:
        """单图说明段落（居中灰色）"""
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        if not paragraphs:
            return ""
        style = (
            "text-align: center;"
            "line-height: 1.7em;"
            "font-size: 14px;"
            "color: #6a6a6a;"
            "margin: 10px 10px 18px 10px;"
        )
        body = "".join(
            f'<p style="margin:0 0 8px 0; white-space: normal;">{html_escape(p)}</p>'
            for p in paragraphs
        )
        return f'<section style="{style}">{body}</section>'

    def save_preview(self, html: str, output_path: Path) -> Path:
        """保存带 shell 的本地预览 HTML"""
        full_html = f"""<!DOCTYPE html>
<html class="ueditor" style="padding-left: 95px; padding-right: 95px;" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="UTF-8">
<style type="text/css">
  body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }}
</style>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>文章预览</title>
</head>
<body class="view rich_media_content" style="overflow-y: hidden; cursor: text;">
{html}
</body>
</html>"""
        output_path.write_text(full_html, encoding="utf-8")
        logger.info(f"预览HTML已保存: {output_path}")
        return output_path
