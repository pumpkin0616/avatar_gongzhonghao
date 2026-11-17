import shutil
import json
import random
from stat import S_ISREG
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from .config import MAX_CONTENT_IMAGE_BYTES, config
from .logger import logger


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


class ImageManagerError(Exception):
    pass


class ImageTransaction:
    """
    图片事务管理器。
    工作流程：
    1. select_images() -> 选取待使用的图片
    2. begin() -> 将图片从 images/ 移动到 temp/pending_xxx/ (标记为占用，防止重复消费)
    3. commit() -> 草稿创建成功，从 temp/pending_xxx/ 移动到 used/
    4. rollback() -> 任一步骤失败，从 temp/pending_xxx/ 移回 images/

    这样可以保证：
    - 即使程序中途崩溃，重启后也能通过检查 pending 目录发现未完成的事务并自动回滚
    - 绝对不会在草稿创建成功前丢失图片或把图片标记为已消费
    """

    def __init__(self):
        self.images_dir: Path = config.article.images_dir
        self.used_dir: Path = config.article.used_images_dir
        self.temp_dir: Path = config.article.temp_dir
        self.images_per_article: int = config.article.images_per_article

        self._selected: List[Path] = []
        self._pending_dir: Optional[Path] = None
        self._moved_to_pending: List[tuple[Path, Path]] = []  # (src, dst_in_pending)

    @staticmethod
    def list_all_images(directory: Path) -> List[Path]:
        """按文件名列出格式受支持且大小不超过正文上传限制的图片。"""
        if not directory.exists():
            return []

        images = []
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            try:
                file_info = path.stat()
            except FileNotFoundError:
                continue
            if not S_ISREG(file_info.st_mode):
                continue
            if file_info.st_size > MAX_CONTENT_IMAGE_BYTES:
                logger.debug("跳过超过 10 MB 的图片: %s", path.name)
                continue
            images.append(path)
        return images

    @staticmethod
    def count_images(directory: Path) -> int:
        """统计目录中的图片数量（供批量生成时判断是否超过阈值）"""
        return len(ImageTransaction.list_all_images(directory))

    def _list_all_images(self, directory: Path) -> List[Path]:
        return ImageTransaction.list_all_images(directory)

    def select_images(self, shuffle: bool = True) -> List[Path]:
        all_images = self._list_all_images(self.images_dir)
        if len(all_images) < self.images_per_article:
            raise ImageManagerError(
                f"图片不足！images/ 中只有 {len(all_images)} 张可用图片，"
                f"需要至少 {self.images_per_article} 张"
            )

        if shuffle:
            shuffled = all_images.copy()
            random.shuffle(shuffled)
            selected = shuffled[: self.images_per_article]
        else:
            selected = all_images[: self.images_per_article]

        self._selected = selected
        logger.info(
            f"已选取 {len(selected)} 张图片（总共可用 {len(all_images)} 张）："
            + ", ".join(p.name for p in selected[:5])
            + (" ..." if len(selected) > 5 else "")
        )
        return selected

    @property
    def selected_images(self) -> List[Path]:
        if not self._selected:
            raise ImageManagerError("尚未选取图片，请先调用 select_images()")
        return list(self._selected)

    def begin(self) -> Path:
        """
        开始事务：将选中的图片从 images/ 移动到 temp/pending_xxx/
        返回 pending 目录路径
        """
        if not self._selected:
            raise ImageManagerError("没有可执行事务的图片，请先调用 select_images()")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pending_name = f"pending_{ts}_{random.randint(1000,9999)}"
        pending_dir = self.temp_dir / pending_name
        pending_dir.mkdir(parents=True, exist_ok=False)

        manifest = []
        moved: List[tuple[Path, Path]] = []

        try:
            for src in self._selected:
                dst = pending_dir / src.name
                if dst.exists():
                    dst = pending_dir / f"{src.stem}_{random.randint(100,999)}{src.suffix}"
                shutil.move(str(src), str(dst))
                moved.append((src, dst))
                manifest.append({
                    "original_path": str(src),
                    "pending_path": str(dst),
                    "filename": src.name,
                })
                logger.debug(f"事务移动: {src.name} -> pending/")

            (pending_dir / "_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"事务 begin 过程中出错，立即回滚已移动的图片: {e}")
            for src, dst in moved:
                try:
                    if dst.exists():
                        shutil.move(str(dst), str(src))
                except Exception as move_err:
                    logger.critical(f"紧急回滚失败！请手动处理: {dst} -> {src}: {move_err}")
            try:
                shutil.rmtree(pending_dir, ignore_errors=True)
            except Exception:
                pass
            raise ImageManagerError(f"事务 begin 失败: {e}") from e

        self._pending_dir = pending_dir
        self._moved_to_pending = moved
        logger.info(f"事务已开始，pending 目录: {pending_dir.name}")
        return pending_dir

    def get_pending_images(self) -> List[Path]:
        """获取当前在 pending 目录中的图片路径（供上传等操作使用）"""
        if not self._pending_dir:
            raise ImageManagerError("事务尚未开始")
        return [dst for _, dst in self._moved_to_pending]

    def commit(self) -> List[Path]:
        """
        提交事务：将 pending 目录中的图片移动到 used/
        只有在草稿创建成功后才调用此方法
        返回最终在 used/ 中的图片路径
        """
        if not self._pending_dir or not self._moved_to_pending:
            raise ImageManagerError("没有可提交的事务")

        ts = datetime.now().strftime("%Y%m%d")
        used_subdir = self.used_dir / ts
        used_subdir.mkdir(parents=True, exist_ok=True)

        final_paths: List[Path] = []

        try:
            for src, dst_in_pending in self._moved_to_pending:
                final_dst = used_subdir / dst_in_pending.name
                if final_dst.exists():
                    suffix = dst_in_pending.suffix
                    stem = dst_in_pending.stem
                    i = 1
                    while True:
                        candidate = used_subdir / f"{stem}_{i}{suffix}"
                        if not candidate.exists():
                            final_dst = candidate
                            break
                        i += 1
                shutil.move(str(dst_in_pending), str(final_dst))
                final_paths.append(final_dst)
                logger.debug(f"事务提交: {dst_in_pending.name} -> used/{ts}/")
        except Exception as e:
            logger.error(
                f"事务 commit 过程中出错！部分图片可能已移动到 used/。"
                f"失败后不会执行回滚（因为草稿已成功创建，图片应视为已消费）。错误: {e}"
            )
            raise ImageManagerError(f"事务 commit 部分失败: {e}") from e

        try:
            shutil.rmtree(self._pending_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"清理 pending 目录失败（不影响结果）: {e}")

        logger.info(
            f"事务提交成功！{len(final_paths)} 张图片已移动到 used/{ts}/："
            + ", ".join(p.name for p in final_paths[:5])
            + (" ..." if len(final_paths) > 5 else "")
        )
        self._pending_dir = None
        self._moved_to_pending = []
        self._selected = []
        return final_paths

    def rollback(self) -> None:
        """
        回滚事务：将 pending 目录中的图片全部移回 images/
        草稿创建失败或流程中断时调用
        """
        if not self._pending_dir or not self._moved_to_pending:
            logger.warning("没有可回滚的事务")
            return

        rollback_errors = []
        for src, dst in self._moved_to_pending:
            try:
                if dst.exists():
                    final_src = src
                    i = 1
                    while final_src.exists():
                        final_src = src.parent / f"{src.stem}_recovered{i}{src.suffix}"
                        i += 1
                    shutil.move(str(dst), str(final_src))
                    logger.info(f"事务回滚: {dst.name} -> images/")
                else:
                    logger.warning(f"回滚时发现 pending 中图片不存在: {dst}")
            except Exception as e:
                rollback_errors.append((dst, src, e))
                logger.critical(f"回滚失败！请手动处理: {dst} -> {src}: {e}")

        try:
            shutil.rmtree(self._pending_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"清理 pending 目录失败: {e}")

        if rollback_errors:
            raise ImageManagerError(
                f"事务回滚有 {len(rollback_errors)} 个失败，"
                f"请手动检查 pending 目录并恢复图片"
            )

        logger.warning("事务已回滚，所有图片已恢复到 images/")
        self._pending_dir = None
        self._moved_to_pending = []
        self._selected = []

    def recover_stale_pending(self) -> int:
        """
        扫描 temp/ 中残留的 pending_xxx 目录（如程序崩溃导致），
        并根据 manifest.json 将图片移回 images/
        返回恢复的 pending 目录数量
        """
        recovered = 0
        for entry in self.temp_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith("pending_"):
                continue

            manifest_file = entry / "_manifest.json"
            if not manifest_file.exists():
                logger.warning(
                    f"发现残留 pending 目录但缺少 manifest: {entry}，跳过"
                )
                continue

            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"解析 manifest 失败 {entry}: {e}，跳过")
                continue

            logger.warning(f"发现残留未完成事务: {entry.name}，包含 {len(manifest)} 张图片，开始恢复...")
            success_count = 0
            for item in manifest:
                pending_path = Path(item["pending_path"])
                original_path = Path(item["original_path"])
                try:
                    if pending_path.exists():
                        dst = original_path
                        i = 1
                        while dst.exists():
                            dst = original_path.parent / f"{original_path.stem}_recovered{i}{original_path.suffix}"
                            i += 1
                        shutil.move(str(pending_path), str(dst))
                        success_count += 1
                        logger.info(f"  恢复: {pending_path.name} -> images/")
                    else:
                        logger.warning(f"  pending 中图片不存在，跳过: {pending_path}")
                except Exception as e:
                    logger.critical(f"  恢复失败，需手动处理: {pending_path} -> {original_path}: {e}")

            if success_count == len(manifest):
                shutil.rmtree(entry, ignore_errors=True)
                logger.warning(f"事务 {entry.name} 恢复完成，已清理")
            recovered += 1

        if recovered:
            logger.info(f"扫描完成，共恢复 {recovered} 个残留事务")
        return recovered
