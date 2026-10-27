import nonebot
from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import logger

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载插件目录（app/plugins 下的每个 .py 都是插件）
nonebot.load_plugins("app/plugins")

from app.db import init_db  # noqa: E402
from app.services import crypto  # noqa: E402
from app.services.tasks import start_scheduler  # noqa: E402


@driver.on_startup
async def _startup():
    try:
        init_db()
    except Exception as e:
        # 数据库起不来则一切业务都无法工作，快速失败并给出明确报错，避免带病运行
        logger.error(f"数据库初始化失败，机器人无法启动：{e}")
        raise
    try:
        # 旧明文凭据（佳明密码 / COROS token）一次性升级为加密存储，读侧已兼容明文。
        # 迁移失败（如密钥文件损坏）不阻断启动：老明文仍可按兼容路径读取。
        n = crypto.migrate_encrypt()
        if n:
            logger.info(f"已将 {n} 个明文凭据迁移为加密存储")
    except Exception as e:
        logger.warning(f"明文凭据迁移失败（不影响启动，按明文兼容读取）: {e}")
    # 把日志同时写到文件，便于排查问题（data 目录已由 init_db 创建）
    logger.add("data/bot.log", rotation="1 MB", retention=7, encoding="utf-8", level="DEBUG")
    if not get_driver().config.superusers:
        # 没有管理员则运维告警私聊、管理员命令、礼物通知全部失效，启动时明确提醒
        logger.warning("未配置 SUPERUSERS：管理员命令与运维告警私聊不可用，请检查 .env")
    logger.info("数据库初始化完成")
    start_scheduler()
    logger.info("机器人启动完成")


if __name__ == "__main__":
    nonebot.run()
