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
from telegram_bot.pocket_client import BotPocketClient, get_category_label
from telegram_bot.analyzer import analyze_candles, evaluate_signal, SignalResult

# Conversation states
CHOOSING_MARKET, CHOOSING_ASSET, CHOOSING_TIMEFRAME, SIGNAL_SENT = range(4)

# In-memory store for active signals keyed by user_id
active_signals: Dict[int, Dict[str, Any]] = {}

# Shared PocketOption client
pocket_client = BotPocketClient()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: show market category selection."""
    logger.info(f"Received /start from user {update.effective_user.id if update.effective_user else 'unknown'}")

    # Clear navigation state so returning to the menu starts fresh
    context.user_data.pop("category", None)
    context.user_data.pop("page", None)

    text = (
        "🤖 *PocketOption Signal Bot*\n\n"
        "Chọn nhóm thị trường bạn muốn giao dịch.\n"
        "Các cặp tiền sẽ được lấy trực tiếp từ API PocketOption."
    )

    try:
        await pocket_client.ensure_connected()
        await pocket_client.get_assets()
        categories = pocket_client.list_categories()
    except Exception as e:
        logger.warning(f"Could not load categories for menu: {e}")
        categories = []

    if not categories:
        # Fallback to the two classic categories if API is unavailable
        categories = ["real", "otc"]
        labels = {"real": "💱 REAL", "otc": "🌙 OTC"}
        keyboard = [[InlineKeyboardButton(labels.get(c, c.upper()), callback_data=f"cat_{c}")] for c in categories]
    else:
        # Build buttons in a 2-column grid
        buttons = [InlineKeyboardButton(get_category_label(c), callback_data=f"cat_{c}") for c in categories]
        keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return CHOOSING_MARKET


ASSETS_PER_PAGE = 15  # 3 columns × 5 rows


async def _show_asset_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the paginated list of active assets for the selected category."""
    query = update.callback_query
    category = context.user_data.get("category")
    page = context.user_data.get("page", 0)
    if not category:
        return await start(update, context)

    await query.edit_message_text("⏳ Đang kết nối và lấy danh sách cặp tiền từ API...")

    try:
        await pocket_client.ensure_connected()
        await pocket_client.get_assets()

        if category in ("real", "otc"):
            # Backward-compatible legacy market filter
            symbols = pocket_client.list_otc_assets() if category == "otc" else pocket_client.list_real_assets()
            title = "🌙 *Cặp tiền OTC*" if category == "otc" else "💱 *Cặp tiền REAL*"
        else:
            symbols = pocket_client.list_assets_by_category(category)
            title = f"{get_category_label(category)} *Cặp tiền*"

        if not symbols:
            await query.edit_message_text(
                "Không tìm thấy cặp tiền nào đang hoạt động trong nhóm này. "
                "Thử lại sau hoặc chọn nhóm khác.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Quay lại", callback_data="back_market")]
                ]),
            )
            return CHOOSING_ASSET

        total_pages = max(1, (len(symbols) + ASSETS_PER_PAGE - 1) // ASSETS_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        context.user_data["page"] = page

        start_idx = page * ASSETS_PER_PAGE
        end_idx = start_idx + ASSETS_PER_PAGE
        page_symbols = symbols[start_idx:end_idx]

        buttons = [
            InlineKeyboardButton(sym, callback_data=f"asset_{sym}")
            for sym in page_symbols
        ]
        rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]

        # Pagination controls
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Trang trước", callback_data=f"page_{category}_{page - 1}"))
        nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="page_info"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Trang sau ➡️", callback_data=f"page_{category}_{page + 1}"))
        if nav_buttons:
            rows.append(nav_buttons)

        rows.append([InlineKeyboardButton("🔙 Quay lại", callback_data="back_market")])

        await query.edit_message_text(
            f"{title}\n\n"
            f"Tìm thấy *{len(symbols)}* cặp. Trang *{page + 1}/{total_pages}*.\n"
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


async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle category selection and show the first page of assets."""
    query = update.callback_query
    await query.answer()

    category = query.data.split("_", 1)[-1]
    context.user_data["category"] = category
    context.user_data["page"] = 0
    return await _show_asset_list(update, context)


async def page_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle pagination navigation."""
    query = update.callback_query
    await query.answer()

    if query.data == "page_info":
        return CHOOSING_ASSET

    _, category, page_str = query.data.split("_", 2)
    context.user_data["category"] = category
    context.user_data["page"] = int(page_str)
    return await _show_asset_list(update, context)


async def asset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle asset selection and show timeframe options."""
    query = update.callback_query
    await query.answer()

    if query.data == "back_market":
        return await start(update, context)

    if query.data == "back_asset":
        return await _show_asset_list(update, context)

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
            "user_id": user_id,
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

        # Schedule result update. Pass a snapshot of the signal data so the
        # update task is independent from any future signals the user requests.
        asyncio.create_task(
            _update_result_after_delay(
                context.application,
                dict(active_signals[user_id]),
            )
        )

        return SIGNAL_SENT

    except Exception as e:
        logger.error(f"timeframe_selected error: {e}")
        await query.edit_message_text(f"❌ Lỗi khi phân tích: {e}\n\nThử /start lại.")
        return ConversationHandler.END


async def _update_result_after_delay(application: Application, signal_data: Dict[str, Any]) -> None:
    """Wait for the timeframe to elapse, then update the user with the result.

    signal_data is a snapshot of the signal that was just sent; it is owned by
    this task and will not be affected by later signals from the same user.
    """
    # Wait for the candle to close, then add a small buffer so the API has
    # the new candle available before we request it.
    await asyncio.sleep(signal_data["timeframe_seconds"] + 5)

    user_id = signal_data.get("user_id")
    if user_id:
        active_signals.pop(user_id, None)

    asset = signal_data["asset"]
    tf_key = signal_data["timeframe"]
    signal: SignalResult = signal_data["signal"]
    chat_id = signal_data["chat_id"]

    try:
        await pocket_client.ensure_connected()

        # Retry a few times if the exit price hasn't changed yet, in case the
        # API returns stale data right after the candle closes.
        exit_price = None
        for attempt in range(3):
            exit_price = await pocket_client.get_latest_price(asset, signal_data["timeframe_seconds"])
            if exit_price != signal.price:
                break
            logger.warning(f"Result price for {asset} unchanged (attempt {attempt + 1}/3), retrying...")
            await asyncio.sleep(3)

        result = evaluate_signal(signal, exit_price)

        won = result["won"]
        if result["diff"] == 0:
            outcome = "🔄 HÒA"
        elif won:
            outcome = "✅ WIN"
        else:
            outcome = "❌ LOSS"
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
                CallbackQueryHandler(category_selected, pattern=r"^cat_"),
            ],
            CHOOSING_ASSET: [
                CallbackQueryHandler(asset_selected, pattern=r"^(asset_|back_market)"),
                CallbackQueryHandler(page_selected, pattern=r"^page_"),
                CallbackQueryHandler(category_selected, pattern=r"^cat_"),
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
