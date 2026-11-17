import json
import traceback
from pathlib import Path
from dataclasses import dataclass, field
from typing import Iterable, Optional, List
from datetime import datetime

from .config import BASE_DIR, config
from .logger import logger
from .image_manager import ImageTransaction, ImageManagerError
from .ai_generator import AIGenerator, ArticleContent, AIGenerationError
from .html_generator import HTMLGenerator
from .title_history import TitleHistory
from .wechat_client import WeChatClient, WeChatAPIError, WeChatDraftResult


@dataclass
class PublishState:
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    phase: str = "init"
    selected_images: List[str] = field(default_factory=list)
    pending_dir: Optional[str] = None
    article_title: Optional[str] = None
    uploaded_image_count: int = 0
    draft_media_id: Optional[str] = None
    committed_images: List[str] = field(default_factory=list)
    success: bool = False
    error: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self):
        return self.__dict__.copy()

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class PublishResult:
    def __init__(self, success: bool, state: PublishState, message: str = ""):
        self.success = success
        self.state = state
        self.message = message

    def __str__(self):
        status = "成功" if self.success else "失败"
        title = self.state.article_title or "(无标题)"
        return f"发布{status}: {title} | {self.message}"


class Publisher:
    """
    核心发布编排器。
    保证以下原子性语义：
    - 只有当「草稿创建成功」这一步返回成功后，才会 commit 图片事务（移动到 used/）
    - 在那之前的任何异常，都会触发 rollback，将图片恢复到 images/
    - 每次运行前会先扫描残留 pending 目录进行自动恢复
    """

    def __init__(
        self,
        wechat_client: Optional[WeChatClient] = None,
        ai_generator: Optional[AIGenerator] = None,
        html_generator: Optional[HTMLGenerator] = None,
    ):
        self.wechat = wechat_client or WeChatClient()
        self.ai = ai_generator or AIGenerator()
        self.html = html_generator or HTMLGenerator()
        self.state_dir = config.article.temp_dir / "states"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.titles = TitleHistory(self.state_dir)

    def _new_state(self) -> PublishState:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        state = PublishState()
        state.save(self.state_dir / f"state_{ts}.json")
        return state

    def _count_images(self) -> int:
        """当前 images/ 中剩余的可用图片数量"""
        return ImageTransaction.count_images(config.article.images_dir)

    def _persist_state(self, state: PublishState):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        state.save(self.state_dir / f"state_{ts}.json")

    def _generate_article(
        self,
        image_paths: List[Path],
        *,
        reserve_title: bool,
        use_fallback_on_ai_failure: bool = True,
        excluded_titles: Iterable[str] = (),
    ) -> ArticleContent:
        try:
            for _ in range(2):
                article = self.ai.generate_article(image_paths)
                title = self.titles.choose(
                    article.title_candidates, reserve=reserve_title, excluded_titles=excluded_titles
                )
                if title:
                    article.title = title
                    return article
                logger.info("标题候选与历史标题过于相似，重新选取")
            raise AIGenerationError("AI 未生成与历史标题有区分度的候选")
        except AIGenerationError as exc:
            if not use_fallback_on_ai_failure:
                raise
            logger.warning(f"AI 标题生成失败，尝试预设短句: {exc}")

        article = self.ai.generate_fallback_content(image_paths)
        title = self.titles.choose(
            article.title_candidates, reserve=reserve_title, excluded_titles=excluded_titles
        )
        if not title:
            raise AIGenerationError("没有可用的预设标题，请重试 AI 生成")
        article.title = title
        return article

    def run_once(self, use_fallback_on_ai_failure: bool = True) -> PublishResult:
        logger.info("=" * 60)
        logger.info("开始新一轮发布流程")
        logger.info("=" * 60)

        state = self._new_state()

        image_tx = ImageTransaction()

        try:
            recovered = image_tx.recover_stale_pending()
            if recovered:
                logger.info(f"自动恢复了 {recovered} 个残留事务")

            state.phase = "select_images"
            selected = image_tx.select_images(shuffle=True)
            state.selected_images = [str(p) for p in selected]
            self._persist_state(state)
            logger.info(f"[阶段1/6] 选取图片: {len(selected)} 张")

            state.phase = "begin_transaction"
            image_tx.begin()
            state.pending_dir = str(image_tx._pending_dir) if image_tx._pending_dir else None
            pending_images = image_tx.get_pending_images()
            self._persist_state(state)
            logger.info(f"[阶段2/6] 事务已开始，图片已移入 pending 目录")

            state.phase = "ai_generate"
            article = self._generate_article(
                pending_images,
                reserve_title=True,
                use_fallback_on_ai_failure=use_fallback_on_ai_failure,
            )
            state.article_title = article.title
            self._persist_state(state)
            logger.info(f"[阶段3/6] AI 生成文章成功: {article.title}")

            state.phase = "upload_images"
            wechat_img_urls: List[str] = []
            for idx, img_path in enumerate(pending_images):
                try:
                    url = self.wechat.upload_content_image(img_path)
                    wechat_img_urls.append(url)
                    state.uploaded_image_count = idx + 1
                    self._persist_state(state)
                    logger.info(f"  上传进度: {idx + 1}/{len(pending_images)}")
                except WeChatAPIError as e:
                    logger.error(f"上传图片 {img_path.name} 失败: {e}")
                    raise
            logger.info(f"[阶段4/6] {len(wechat_img_urls)} 张图片已上传至微信")

            state.phase = "generate_html"
            html_content = self.html.generate(article, wechat_img_urls)
            preview_path = (
                config.article.temp_dir
                / f"preview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            )
            self.html.save_preview(html_content, preview_path)
            logger.info(f"[阶段5/6] 文章HTML已生成，预览: {preview_path}")

            state.phase = "create_draft"
            thumb_media_id = None
            try:
                thumb_media_id = self.wechat.upload_thumb_material(pending_images[0])
                logger.info(f"  缩略图素材上传成功: {thumb_media_id}")
            except Exception as e:
                logger.warning(f"缩略图素材上传失败（草稿仍可创建，但无封面）: {e}")

            author = config.article.author or ""
            digest = article.digest or config.article.digest or ""

            draft_result: WeChatDraftResult = self.wechat.add_draft(
                title=article.title,
                content=html_content,
                thumb_media_id=thumb_media_id,
                author=author,
                digest=digest,
            )
            state.draft_media_id = draft_result.media_id
            self._persist_state(state)
            logger.info(f"[阶段6/6] 微信草稿创建成功！media_id: {draft_result.media_id}")

            state.phase = "commit"
            final_paths = image_tx.commit()
            state.committed_images = [str(p) for p in final_paths]
            state.success = True
            state.finished_at = datetime.now().isoformat()
            self._persist_state(state)

            logger.info("=" * 60)
            logger.info("✅ 发布流程全部完成！")
            logger.info(f"  文章标题: {article.title}")
            logger.info(f"  草稿 media_id: {draft_result.media_id}")
            logger.info(f"  使用图片: {len(final_paths)} 张已移入 used/")
            logger.info(f"  本地预览: {preview_path}")
            logger.info("=" * 60)

            return PublishResult(
                success=True,
                state=state,
                message=f"草稿创建成功，media_id={draft_result.media_id}",
            )

        except Exception as e:
            tb = traceback.format_exc()
            state.error = f"{type(e).__name__}: {e}\n{tb}"
            state.finished_at = datetime.now().isoformat()
            self._persist_state(state)

            logger.error("=" * 60)
            logger.error(f"❌ 发布流程在 [{state.phase}] 阶段失败: {e}")
            logger.error(tb)
            logger.error("=" * 60)

            try:
                logger.warning("开始事务回滚，将图片恢复到 images/ ...")
                image_tx.rollback()
                logger.warning("事务回滚完成，图片已恢复")
            except ImageManagerError as rollback_err:
                logger.critical(
                    f"⚠️ 事务回滚失败！请立即手动检查 temp/ 下的 pending 目录 "
                    f"并将图片移回 images/。错误: {rollback_err}"
                )
            except Exception as rollback_err2:
                logger.critical(
                    f"⚠️ 事务回滚异常！请手动检查 pending 目录: {rollback_err2}"
                )

            return PublishResult(
                success=False,
                state=state,
                message=f"在 {state.phase} 阶段失败: {e}",
            )

    def run_batch(self, use_fallback_on_ai_failure: bool = True) -> PublishResult:
        """
        批量发布：
        只要 images/ 中剩余图片数量超过 images_per_article（默认 10），
        就持续生成文章（每篇消耗 images_per_article 张图片，成功后移入 used/），
        剩余图片不超过阈值时停止。
        """
        logger.info("=" * 60)
        logger.info("开始批量发布流程（图片超过阈值就继续生成，不足则停止）")
        logger.info("=" * 60)

        threshold = config.article.images_per_article
        results: List[PublishResult] = []
        total_articles = 0

        while True:
            available = self._count_images()
            if available < threshold:
                logger.info(
                    f"images/ 中剩余 {available} 张图片，"
                    f"未超过阈值 {threshold} 张，停止批量生成"
                )
                break

            logger.info(
                f"images/ 剩余 {available} 张图片 > 阈值 {threshold} 张，"
                f"开始生成第 {total_articles + 1} 篇文章"
            )
            result = self.run_once(
                use_fallback_on_ai_failure=use_fallback_on_ai_failure
            )
            results.append(result)
            if result.success:
                total_articles += 1
                logger.info(f"✅ 第 {total_articles} 篇发布成功")
            else:
                logger.error(
                    "本篇发布失败，停止后续批量生成"
                    "（失败时图片已回滚到 images/，可重新运行 batch）"
                )
                break

        remaining = self._count_images()
        total_images_used = sum(
            len(r.state.committed_images) for r in results if r.success
        )

        if not results:
            message = (
                f"images/ 图片数量（{remaining} 张）未超过阈值（{threshold} 张），"
                "本轮未生成文章"
            )
        else:
            message = (
                f"共生成 {total_articles} 篇文章，使用 {total_images_used} 张图片，"
                f"剩余 {remaining} 张图片"
            )

        state = self._new_state()
        state.phase = "batch_done"
        state.article_title = f"批量发布 {total_articles} 篇"
        state.success = all(r.success for r in results)
        state.finished_at = datetime.now().isoformat()
        self._persist_state(state)

        logger.info("=" * 60)
        if not results:
            logger.info("🗂 批量流程结束：图片数量不足，未生成文章")
        else:
            logger.info(
                f"📦 批量流程结束：成功 {total_articles} 篇，"
                f"使用 {total_images_used} 张图片，剩余 {remaining} 张"
            )
        logger.info("=" * 60)

        return PublishResult(
            success=state.success,
            state=state,
            message=message,
        )

    def dry_run_batch(self) -> PublishResult:
        """
        批量 DRY-RUN：
        不调用微信API、不移动任何图片，按阈值把 images/ 中的图片分批，
        为每个批次生成 HTML 预览，用于验证内容和排版。
        """
        logger.info("=" * 60)
        logger.info("批量 DRY-RUN（不调用微信API，不移动图片，仅生成预览）")
        logger.info("=" * 60)

        threshold = config.article.images_per_article
        all_images = ImageTransaction.list_all_images(config.article.images_dir)
        available = len(all_images)

        if available <= threshold:
            logger.info(
                f"images/ 中剩余 {available} 张图片，"
                f"未超过阈值 {threshold} 张，无需生成"
            )
            state = self._new_state()
            state.phase = "batch_dry_done"
            state.success = True
            state.finished_at = datetime.now().isoformat()
            self._persist_state(state)
            return PublishResult(
                success=True,
                state=state,
                message=f"图片数量（{available} 张）未超过阈值（{threshold} 张），无需生成",
            )

        preview_paths: List[Path] = []
        preview_titles: set[str] = set()
        article_no = 0
        remaining = available
        offset = 0

        while remaining > threshold:
            chunk = all_images[offset : offset + threshold]
            article_no += 1
            article = self._generate_article(
                chunk, reserve_title=False, excluded_titles=preview_titles
            )
            preview_titles.add(article.title)

            demo_urls = self._load_demo_wechat_image_urls(need=len(chunk))
            html_content = self.html.generate(article, demo_urls)
            preview_path = (
                config.article.temp_dir
                / f"dry_preview_batch{article_no}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            )
            self.html.save_preview(html_content, preview_path)
            preview_paths.append(preview_path)
            logger.info(f"✅ 第 {article_no} 篇预览已生成: {preview_path}")

            offset += threshold
            remaining -= threshold

        state = self._new_state()
        state.phase = "batch_dry_done"
        state.article_title = f"批量预览 {article_no} 篇"
        state.success = True
        state.finished_at = datetime.now().isoformat()
        self._persist_state(state)

        logger.info("=" * 60)
        logger.info(f"📦 批量 DRY-RUN 结束：生成 {article_no} 篇预览，剩余 {remaining} 张图片")
        logger.info("=" * 60)

        return PublishResult(
            success=True,
            state=state,
            message=(
                f"批量 DRY-RUN 成功，生成 {article_no} 篇预览，剩余 {remaining} 张图片，"
                f"预览文件: {', '.join(str(p) for p in preview_paths)}"
            ),
        )

    def dry_run(self, generate_preview: bool = True) -> PublishResult:
        """
        测试模式：不调用微信API，仅完成图片选取 -> AI生成 -> HTML生成，
        不会移动图片到 used/（也不会移入 pending），用于验证流程和调试提示词。

        注意：DRY-RUN 为了让预览能真正显示图片，会使用 test.html 中提取出来的
        若干真实可访问图 URL；真正发布时会替换成实际上传返回的 URL。
        """
        logger.info("=" * 60)
        logger.info("DRY-RUN 模式（不调用微信API，不移动图片）")
        logger.info("=" * 60)

        state = self._new_state()
        image_tx = ImageTransaction()

        try:
            state.phase = "select_images"
            selected = image_tx.select_images(shuffle=True)
            state.selected_images = [str(p) for p in selected]
            self._persist_state(state)
            logger.info(f"[阶段1/3 DRY] 选取图片: {len(selected)} 张")

            state.phase = "ai_generate"
            article = self._generate_article(selected, reserve_title=False)
            state.article_title = article.title
            self._persist_state(state)
            logger.info(f"[阶段2/3 DRY] AI 生成文章: {article.title}")

            state.phase = "generate_html"
            demo_urls = self._load_demo_wechat_image_urls(need=len(selected))
            html_content = self.html.generate(article, demo_urls)
            if generate_preview:
                preview_path = (
                    config.article.temp_dir
                    / f"dry_preview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
                )
                self.html.save_preview(html_content, preview_path)
                logger.info(f"[阶段3/3 DRY] 预览HTML: {preview_path}")
            state.success = True
            state.finished_at = datetime.now().isoformat()
            self._persist_state(state)
            logger.info("✅ DRY-RUN 成功，未对图片和微信做任何修改")
            return PublishResult(success=True, state=state, message="DRY-RUN 成功")

        except Exception as e:
            tb = traceback.format_exc()
            state.error = f"{type(e).__name__}: {e}\n{tb}"
            state.finished_at = datetime.now().isoformat()
            self._persist_state(state)
            logger.error(f"❌ DRY-RUN 失败: {e}\n{tb}")
            return PublishResult(success=False, state=state, message=str(e))

    @staticmethod
    def _load_demo_wechat_image_urls(need: int) -> List[str]:
        """
        DRY-RUN 用：尝试从项目根 test.html 里提取真实可显示的 mmbiz 图片 URL；
        如果 test.html 不存在或不够，再兜底用一组经典可访问的 mmbiz 图。
        """
        import re as _re
        urls: List[str] = []
        test_html = BASE_DIR / "test.html"
        if test_html.exists():
            try:
                text = test_html.read_text(encoding="utf-8")
                found = _re.findall(r'https://mmbiz\.qlogo\.cn/[^"\'\s)]+', text)
                seen = set()
                for u in found:
                    if u not in seen:
                        seen.add(u)
                        urls.append(u)
                    if len(urls) >= need:
                        break
            except Exception as e:
                logger.warning(f"读取 test.html 失败，使用兜底预览图: {e}")
        # test.html 不存在 / 数量不够 -> 兜底（用 test.html 里常见的几类路径做占位，注意这些也是来自 test.html 里能显示的）
        fallback_base = [
            "https://mmbiz.qlogo.cn/sz_mmbiz_png/9yPxDmqsZB3aRvMMkBWiaazpUfx790m5j2VjCzwweKbo9RF5rnoOHcXSxIumK37RUVTCOUuYdickS7b6XrLGnz1oicJia91HHuslvOKrKfMXroQ/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/mmbiz_png/9yPxDmqsZB3X1lukGl8rNpzCcatk9xibp75JnpfocTkCHicGUBEFGicEW1lsicjSriaY528RicNRnKz7luZrtSzu3WCTDrSScZrxkhI6ic1GGs6mAY/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/sz_mmbiz_png/9yPxDmqsZB2sdGJkH7ZhAHe6xpF5BdNQ3iaXFg9YJ69Cr7AxkYvkxvCDh0cojic2yo1p8DkmmUWHQZw2GorR6SHfcfdR6zS408m68NGtWamCs/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/mmbiz_png/9yPxDmqsZB2Tiaxmc2NAiac131GouClFIicoRBG1Oic7cXxZ0RO4icVb7KvGYDNbX3gcSRtx83YUVmIzKu2cjvBL4rKLv1R5w4ibmgpREMJpZvfZU/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/sz_mmbiz_png/9yPxDmqsZB1fbe1PTIf7giaIlpwSqpGq3ia4Z7r2P8ChDHDK0UWNUbBFNg7LEhbj3MV7Jwmbib7DgliaQ67lBkEKZkRZKtV7ncBIQhs2zCdiaPUE/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/mmbiz_png/9yPxDmqsZB2FIL4BwTFaaVuxPxDv3NW1Rzxu2etfasoEQlfsib5baOSzjHvoWaMHrquQhe2s7y8zFChlX6xBTiawA5bZZtDTIickHre7E7ibYibY/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/mmbiz_png/9yPxDmqsZB39ArAMia7sEnqBib68I33rtdib6CTbcr45JbQUB5tX7mT3m53SGDich5NHicoKqtc6ZGGgMzwe8y4Hfh40xyt8xwULL6icd5YwofRN8/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/mmbiz_png/9yPxDmqsZB0Fve2T61SnSUQXEs8EcATFXcXvVo1XiazSEO0Hw0ysPZSKkPQFjoQPWPTRqngcyU94waTAuGLSXaBPWclcm0O4HicicLDyVYRGZI/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/sz_mmbiz_png/9yPxDmqsZB1EugYibczxibBicOdSkfVr08age6uxj5GRK3mMEv5k3xg7uWzsOIbibSA5wNCmQW22tzU3Bb7ic84qMs9tUeHbkaIWvGjJtKZN54a4/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/sz_mmbiz_png/9yPxDmqsZB1fgicGWTRbOkib8FOQ0qQP62DZleu7OTC6QJ2LstRVyuM2Yw1H2zQ8c7FAeUplOkl92CXoaB3ULd0QBaKavUhgVic8icfQmgFJKxg/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/mmbiz_png/9yPxDmqsZB2FIL4BwTFaaVuxPxDv3NW1Rzxu2etfasoEQlfsib5baOSzjHvoWaMHrquQhe2s7y8zFChlX6xBTiawA5bZZtDTIickHre7E7ibYibY/0?wx_fmt=png&from=appmsg",
            "https://mmbiz.qlogo.cn/mmbiz_gif/9yPxDmqsZB3zjDicy2h7iczLNlyfdEicBDskt0a0uwHE7zRibCicklj2Rqyp7eWkiaL0hs0VPhWicw4cTHjG9dFcTicx5FicTyzgxSGupleCHncVNfiaU/0?wx_fmt=gif&from=appmsg",
        ]
        i = 0
        while len(urls) < need:
            urls.append(fallback_base[i % len(fallback_base)])
            i += 1
        return urls[:need]

