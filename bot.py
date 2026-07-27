"""Telegram bot logic for PocketOption trading signals."""
import asyncio
import logging
from typing import Dict, Any
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)
from loguru import logger

from telegram_bot.config import TELEGRAM_BOT_TOKEN, TIMEFRAMES
from telegram_bot.pocket_client import BotPocketClient
from telegram_bot.analyzer import analyze_candles, evaluate_signal, SignalResult

# Conversation states
CHOOSING_MARKET, CHOOSING_ASSET, CHOOSING_TIMEFRAME, SIGNAL_SENT = range(4)

# In-memory store for active signals keyed by user_id
active_signals: Dict[int, Dict[str, Any]] = {}

# Shared PocketOption client
pocket_client = BotPocketClient()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: show market selection."""
    logger.info(f"Received /start from user {update.effective_user.id if update.effective_user else 'unknown'}")
    text = (
        "🤖 *PocketOption Signal Bot*\n\n"
        "Chọn thị trường bạn muốn giao dịch.\n"
        "Các cặp tiền sẽ được lấy trực tiếp từ API PocketOption."
    )
    keyboard = [
        [InlineKeyboardButton("💱 REAL (thị trường mở cửa)", callback_data="market_real")],
        [InlineKeyboardButton("🌙 OTC (ngoài giờ)", callback_data="market_otc")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return CHOOSING_MARKET


async def _show_asset_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the list of active assets for the selected market."""
    query = update.callback_query
    market = context.user_data.get("market")
    if not market:
        return await start(update, context)

    await query.edit_message_text("⏳ Đang kết nối và lấy danh sách cặp tiền từ API...")

    try:
        await pocket_client.ensure_connected()
        await pocket_client.get_assets()
        if market == "otc":
            symbols = pocket_client.list_otc_assets()
            title = "🌙 *Cặp tiền OTC*"
        else:
            symbols = pocket_client.list_real_assets()
            title = "💱 *Cặp tiền REAL*"

        if not symbols:
            await query.edit_message_text(
                f"Không tìm thấy cặp tiền {market.upper()} nào đang hoạt động. "
                "Thử lại sau hoặc chọn thị trường khác.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Quay lại", callback_data="back_market")]
                ]),
            )
            return CHOOSING_ASSET

        buttons = [
            InlineKeyboardButton(sym, callback_data=f"asset_{sym}")
            for sym in symbols[:100]
        ]
        rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        rows.append([InlineKeyboardButton("🔙 Quay lại", callback_data="back_market")])

        await query.edit_message_text(
            f"{title}\n\n"
            f"Tìm thấy *{len(symbols)}* cặp đang hoạt động.\n"
            f"Chọn một cặp để phân tích:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return CHOOSING_ASSET

    except Exception as e:
        logger.error(f"_show_asset_list error: {e}")
        await query.edit_message_text(
            f"❌ Lỗi: {e}\n\nThử /start lại.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Menu chính", callback_data="back_market")]
            ]),
        )
        return CHOOSING_ASSET


async def market_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle market selection and show asset list."""
    query = update.callback_query
    await query.answer()

    market = query.data.split("_")[-1]  # real or otc
    context.user_data["market"] = market
    return await _show_asset_list(update, context)


async def asset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle asset selection and show timeframe options."""
    query = update.callback_query
    await query.answer()

    if query.data == "back_market":
        return await start(update, context)

    asset = query.data.split("_", 1)[-1]
    context.user_data["asset"] = asset

    keyboard = [
        [InlineKeyboardButton("1 phút", callback_data="tf_1m")],
        [InlineKeyboardButton("3 phút", callback_data="tf_3m")],
        [InlineKeyboardButton("5 phút", callback_data="tf_5m")],
        [InlineKeyboardButton("15 phút", callback_data="tf_15m")],
        [InlineKeyboardButton("🔙 Quay lại", callback_data="back_asset")],
    ]
    await query.edit_message_text(
        f"📊 *{asset}*\n\nChọn khung thời gian tín hiệu:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return CHOOSING_TIMEFRAME


async def timeframe_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Analyze and send signal."""
    query = update.callback_query
    await query.answer()

    if query.data == "back_asset":
        return await _show_asset_list(update, context)

    tf_key = query.data.split("_")[-1]
    timeframe_seconds = TIMEFRAMES[tf_key]
    asset = context.user_data["asset"]

    await query.edit_message_text(
        f"⏳ Đang phân tích *{asset}* khung *{tf_key}*...",
        parse_mode="Markdown",
    )

    try:
        await pocket_client.ensure_connected()
        candles = await pocket_client.get_candles(asset, timeframe_seconds)
        signal = analyze_candles(candles)

        entry_time = datetime.now()
        user_id = update.effective_user.id

        active_signals[user_id] = {
            "asset": asset,
            "timeframe": tf_key,
            "timeframe_seconds": timeframe_seconds,
            "signal": signal,
            "entry_time": entry_time,
            "message_id": query.message.message_id,
            "chat_id": query.message.chat_id,
        }

        direction_emoji = "🟢 CALL" if signal.direction == "CALL" else "🔴 PUT"
        reasons_text = "\n".join(f"• {r}" for r in signal.reasons)

        text = (
            f"📊 *TÍN HIỆU {asset}*\n\n"
            f"🕐 Khung thời gian: *{tf_key}*\n"
            f"💰 Giá tín hiệu: *{signal.price:.6f}*\n"
            f"📈 Hướng: *{direction_emoji}*\n"
            f"🎯 Độ tin cậy: *{int(signal.confidence * 100)}%*\n\n"
            f"*Lý do:*\n{reasons_text}\n\n"
            f"⏱ Kết quả sẽ được cập nhật sau *{tf_key}*."
        )

        keyboard = [
            [InlineKeyboardButton("🔔 Nhận tín hiệu mới", callback_data="new_signal")],
            [InlineKeyboardButton("🏠 Menu chính", callback_data="back_market")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        # Schedule result update
        asyncio.create_task(
            _update_result_after_delay(context.application, user_id, timeframe_seconds)
        )

        return SIGNAL_SENT

    except Exception as e:
        logger.error(f"timeframe_selected error: {e}")
        await query.edit_message_text(f"❌ Lỗi khi phân tích: {e}\n\nThử /start lại.")
        return ConversationHandler.END


async def _update_result_after_delay(application: Application, user_id: int, delay: int) -> None:
    """Wait for the timeframe to elapse, then update the user with the result."""
    await asyncio.sleep(delay)

    signal_data = active_signals.pop(user_id, None)
    if not signal_data:
        return

    asset = signal_data["asset"]
    tf_key = signal_data["timeframe"]
    signal: SignalResult = signal_data["signal"]
    chat_id = signal_data["chat_id"]

    try:
        await pocket_client.ensure_connected()
        exit_price = await pocket_client.get_latest_price(asset, signal_data["timeframe_seconds"])
        result = evaluate_signal(signal, exit_price)

        won = result["won"]
        outcome = "✅ WIN" if won else "❌ LOSS"
        direction_text = "CALL" if signal.direction == "CALL" else "PUT"

        text = (
            f"📊 *KẾT QUẢ {asset}*\n\n"
            f"Hướng: *{direction_text}*\n"
            f"Giá vào: *{signal.price:.6f}*\n"
            f"Giá sau {tf_key}: *{exit_price:.6f}*\n"
            f"Chênh lệch: *{result['diff']:.6f} ({result['diff_pct']:.3f}%)*\n\n"
            f"Kết quả: *{outcome}*"
        )

        await application.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Result update error: {e}")
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Không thể cập nhật kết quả cho {asset}: {e}",
        )


async def new_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Restart from market selection."""
    query = update.callback_query
    await query.answer()
    return await start(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel conversation."""
    if update.message:
        await update.message.reply_text("Đã hủy. Gõ /start để bắt đầu lại.")
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error(f"Update {update} caused error {context.error}")
    import traceback
    logger.error(traceback.format_exc())


def build_application() -> Application:
    """Build and return the Telegram Application."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN chưa được cấu hình.")

    logger.info("Building Telegram application...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    logger.info("Telegram application built successfully")

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_MARKET: [
                CallbackQueryHandler(market_selected, pattern=r"^market_(real|otc)$"),
            ],
            CHOOSING_ASSET: [
                CallbackQueryHandler(asset_selected, pattern=r"^(asset_|back_market)"),
            ],
            CHOOSING_TIMEFRAME: [
                CallbackQueryHandler(timeframe_selected, pattern=r"^(tf_|back_asset)"),
            ],
            SIGNAL_SENT: [
                CallbackQueryHandler(new_signal, pattern=r"^new_signal$"),
                CallbackQueryHandler(start, pattern=r"^back_market$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    return application
