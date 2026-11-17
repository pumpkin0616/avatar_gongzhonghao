import argparse
import sys
import signal
import time

from .config import config
from .logger import logger
from .publisher import Publisher


def run_once(args):
    publisher = Publisher()
    if args.dry_run:
        result = publisher.dry_run(generate_preview=True)
    else:
        result = publisher.run_once(
            use_fallback_on_ai_failure=not args.no_ai_fallback
        )
    return 0 if result.success else 1


def run_batch(args):
    publisher = Publisher()
    if args.dry_run:
        result = publisher.dry_run_batch()
    else:
        result = publisher.run_batch(
            use_fallback_on_ai_failure=not args.no_ai_fallback
        )
    return 0 if result.success else 1


def run_scheduled(args):
    import schedule

    publisher = Publisher()
    schedule_time = config.schedule.schedule_time

    def job():
        logger.info("⏰ 定时任务触发，开始执行发布流程...")
        try:
            result = publisher.run_once(use_fallback_on_ai_failure=True)
            logger.info(f"⏰ 定时任务执行结果: {result}")
        except Exception as e:
            logger.exception(f"⏰ 定时任务出现未捕获异常: {e}")

    schedule.every().day.at(schedule_time).do(job)
    logger.info(f"📅 定时任务已设置，每天 {schedule_time} 执行一次发布")
    logger.info(f"    按 Ctrl+C 退出")

    logger.info("🔍 为防止遗漏，启动后先立即执行一次检查（可选：--run-now）")
    if args.run_now:
        logger.info("🚀 --run-now 指定，立即执行一次")
        job()

    def _handle_sigint(signum, frame):
        logger.info("收到退出信号，关闭调度器")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            logger.info("用户中断，退出")
            break
        except Exception as e:
            logger.exception(f"调度循环出错，30秒后重试: {e}")
            time.sleep(30)

    return 0


def recover(args):
    from .image_manager import ImageTransaction

    tx = ImageTransaction()
    count = tx.recover_stale_pending()
    logger.info(f"恢复操作完成，处理了 {count} 个残留 pending 目录")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="微信公众号文章自动发布系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 执行一次发布流程（完整流程）
  python -m src.main run

  # 仅本地测试：不调用微信API，不移动图片，仅验证AI和HTML
  python -m src.main run --dry-run

  # 启动每天定时任务（默认 09:00，可在 .env 中修改 SCHEDULE_TIME）
  python -m src.main schedule

  # 启动定时任务并先立即执行一次
  python -m src.main schedule --run-now

  # 批量发布：images/ 图片超过阈值(默认10)就继续生成，不足则停止。
  # 例：images/ 有 25 张 -> 生成 2 篇（用 20 张），剩余 5 张停止
  python -m src.main batch

  # 批量发布前先本地预览各批次内容（不调用微信API，不移动图片）
  python -m src.main batch --dry-run

  # 手动恢复残留 pending 目录（程序崩溃后手动使用）
  python -m src.main recover
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行一次发布流程")
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="仅本地测试：不调用微信API，不移动图片，仅验证AI和HTML生成",
    )
    p_run.add_argument(
        "--no-ai-fallback",
        action="store_true",
        help="AI生成失败时不使用兜底内容（默认失败时使用兜底保证流程继续）",
    )
    p_run.set_defaults(func=run_once)

    p_batch = sub.add_parser(
        "batch",
        help="批量生成多篇文章：images/ 图片超过阈值（默认10）就继续生成，不足则停止",
    )
    p_batch.add_argument(
        "--dry-run",
        action="store_true",
        help="仅本地测试：不调用微信API，不移动图片，逐个批次生成预览",
    )
    p_batch.add_argument(
        "--no-ai-fallback",
        action="store_true",
        help="AI生成失败时不使用兜底内容（默认失败时使用兜底保证流程继续）",
    )
    p_batch.set_defaults(func=run_batch)

    p_sched = sub.add_parser("schedule", help="启动每日定时调度")
    p_sched.add_argument(
        "--run-now",
        action="store_true",
        help="启动后立即执行一次，然后按计划时间继续",
    )
    p_sched.set_defaults(func=run_scheduled)

    p_rec = sub.add_parser("recover", help="手动恢复崩溃残留的 pending 图片")
    p_rec.set_defaults(func=recover)

    args = parser.parse_args()

    try:
        rc = args.func(args)
    except EnvironmentError as e:
        logger.error(f"配置错误: {e}")
        logger.error("请先复制 .env.example 为 .env 并填写凭据")
        rc = 2
    except Exception as e:
        logger.exception(f"执行出错: {e}")
        rc = 1

    sys.exit(rc)


if __name__ == "__main__":
    main()
