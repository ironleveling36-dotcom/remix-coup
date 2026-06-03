"""
main.py — Bot entry point.

Features:
 • Async polling with aiogram v3
 • Auto-restart on crash (supervisor loop)
 • Error logging with traceback to admin
 • Database init on startup
 • All routers registered here
"""

import asyncio
import logging
import sys
import traceback
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, ADMIN_IDS
from database.db import init_db

# ─── Routers ─────────────────────────────────────────────────────────────────
from handlers.admin import router as admin_router
from handlers.user import router as user_router
from handlers.payment import router as payment_router
from handlers.wallet import router as wallet_router
from handlers.referral import router as referral_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────── Error Notifier ──────────────────────────────────

async def notify_admin_error(bot: Bot, error: Exception, context: str = ""):
    tb = traceback.format_exc()
    text = (
        f"🚨 *Bot Error* — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        f"Context: {context}\n"
        f"Error: `{type(error).__name__}: {error}`\n\n"
        f"```\n{tb[-2000:]}\n```"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="Markdown")
        except Exception:
            pass


# ─────────────────────────── Wallet topup approve hook ───────────────────────
# Patch admin router's approve handler to handle wallet top-ups

from aiogram import Router, F
from aiogram.types import CallbackQuery
from handlers.wallet import handle_wallet_topup_approval, WALLET_TOPUP_CATEGORY

_patch_router = Router()

@_patch_router.callback_query(F.data.startswith("approve_"))
async def _approve_with_wallet_hook(cq: CallbackQuery, bot: Bot):
    """
    Intercepts approve callbacks.
    If it's a wallet top-up order → credit wallet.
    Otherwise → let admin.py's handler run (this router is registered first).
    """
    order_id = int(cq.data.split("_")[1])
    from database.db import get_order
    order = await get_order(order_id)
    if order and order.get("category_name") == WALLET_TOPUP_CATEGORY:
        if order["status"] != "pending":
            return await cq.answer(f"Already {order['status']}.", show_alert=True)
        await handle_wallet_topup_approval(bot, order, cq.from_user.id)
        await cq.message.edit_text(
            cq.message.text + f"\n\n✅ *Wallet credited* — approved by @{cq.from_user.username or cq.from_user.id}",
            parse_mode="Markdown",
        )
        await cq.answer("✅ Wallet credited!", show_alert=True)
    # Fall through to admin router for normal orders


# ─────────────────────────── App Setup ───────────────────────────────────────

def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Register wallet hook BEFORE admin router (order matters for approve_ callback)
    dp.include_router(_patch_router)
    dp.include_router(admin_router)
    dp.include_router(user_router)
    dp.include_router(payment_router)
    dp.include_router(wallet_router)
    dp.include_router(referral_router)

    return dp


async def on_startup(bot: Bot):
    await init_db()
    me = await bot.get_me()
    logger.info("Bot @%s started successfully.", me.username)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"✅ *Bot started* — @{me.username}\n"
                f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def on_shutdown(bot: Bot):
    logger.info("Bot shutting down.")


# ─────────────────────────── Self-Healing Loop ───────────────────────────────

async def run_bot():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = create_dispatcher()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


MAX_CRASHES = 10
CRASH_WAIT_SECONDS = 5

async def supervisor():
    """
    Self-healing supervisor loop.
    Restarts the bot on crash. After MAX_CRASHES in a row, gives up.
    Notifies admins on each crash with traceback.
    """
    crashes = 0
    while crashes < MAX_CRASHES:
        try:
            logger.info("Starting bot (attempt %d)…", crashes + 1)
            await run_bot()
            crashes = 0  # reset on clean exit
        except Exception as e:
            crashes += 1
            tb = traceback.format_exc()
            logger.error("Bot crashed (crash #%d):\n%s", crashes, tb)

            # Try to notify admins
            try:
                bot_temp = Bot(token=BOT_TOKEN)
                text = (
                    f"💥 *Bot crashed* (#{crashes}/{MAX_CRASHES})\n\n"
                    f"`{type(e).__name__}: {e}`\n\n"
                    f"```\n{tb[-1500:]}\n```\n\n"
                    f"🔄 Restarting in {CRASH_WAIT_SECONDS}s…"
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await bot_temp.send_message(admin_id, text, parse_mode="Markdown")
                    except Exception:
                        pass
                await bot_temp.session.close()
            except Exception:
                pass

            if crashes < MAX_CRASHES:
                wait = CRASH_WAIT_SECONDS * crashes  # exponential-ish back-off
                logger.info("Waiting %ds before restart…", wait)
                await asyncio.sleep(wait)

    logger.critical("Bot exceeded max crash limit (%d). Exiting.", MAX_CRASHES)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(supervisor())
