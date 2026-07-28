"""Telegram bot logic for PocketOption trading signals."""
import asyncio
import logging
from typing import Dict, Any
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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
active_signal_tasks: Dict[int, asyncio.Task] = {}

# Shared PocketOption client
pocket_client = BotPocketClient()

START_IMAGE = "https://i.ibb.co/gMz28z9K/uploaded-image.jpg"
ASSET_IMAGE = "https://i.ibb.co/3m4mJbqW/uploaded-image.jpg"
TIMEFRAME_IMAGE = "https://i.ibb.co/8gspvnSH/uploaded-image.jpg"
CALL_IMAGE = "https://i.ibb.co/0RqnRvmn/uploaded-image.jpg"
PUT_IMAGE = "https://i.ibb.co/hxNhCwc5/uploaded-image.jpg"
WAIT_IMAGE = "https://i.ibb.co/B52N912P/uploaded-image.jpg"
TIMEFRAME_LABELS = {
    "s15": "S15",
    "s30": "S30",
    "m1": "M1",
    "m3": "M3",
    "m5": "M5",
}


async def _show_photo_message(
    query, image_url: str, caption: str, reply_markup: InlineKeyboardMarkup
) -> None:
    """Replace a callback message with a photo while preserving its buttons."""
    media = InputMediaPhoto(
        media=image_url,
        caption=caption,
        parse_mode="Markdown",
    )
    try:
        await query.edit_message_media(media=media, reply_markup=reply_markup)
    except Exception:
        # A text message cannot be converted to media in place. Replace it so
        # the first transition after /start also gets the requested image.
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_photo(
            photo=image_url,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )


async def _show_caption_message(query, text: str) -> None:
    """Update the caption while the current callback message is a photo."""
    try:
        await query.edit_message_caption(caption=text, parse_mode="Markdown")
    except Exception:
        await query.edit_message_text(text, parse_mode="Markdown")


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
        # Never block /start on the broker connection. The menu remains
        # usable while assets are refreshed in the background/next interaction.
        await asyncio.wait_for(pocket_client.ensure_connected(), timeout=4.0)
        await asyncio.wait_for(pocket_client.get_assets(max_wait=2.0), timeout=4.0)
        categories = pocket_client.list_categories()
    except Exception as e:
        logger.warning(f"Could not load categories for menu (using fallback): {e}")
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
        await update.message.reply_photo(
            photo=START_IMAGE,
            caption=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    elif update.callback_query:
        query = update.callback_query
        if context.user_data.pop("send_new_menu", False):
            try:
                await query.message.delete()
            except Exception as e:
                logger.debug(f"Could not delete previous signal message: {e}")
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=START_IMAGE,
                caption=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        else:
            await _show_photo_message(query, START_IMAGE, text, reply_markup)
    return CHOOSING_MARKET


ASSETS_PER_PAGE = 15  # 3 columns × 5 rows


async def _show_asset_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the paginated list of active assets for the selected category."""
    query = update.callback_query
    category = context.user_data.get("category")
    page = context.user_data.get("page", 0)
    if not category:
        return await start(update, context)

    try:
        if category in ("real", "otc"):
            # Backward-compatible legacy market filter
            symbols = pocket_client.list_otc_assets() if category == "otc" else pocket_client.list_real_assets()
            title = "🌙 *Cặp tiền OTC*" if category == "otc" else "💱 *Cặp tiền REAL*"
        else:
            symbols = pocket_client.list_assets_by_category(category)
            title = f"{get_category_label(category)} *Cặp tiền*"

        if not symbols:
            await _show_photo_message(
                query,
                ASSET_IMAGE,
                "Không tìm thấy cặp tiền nào đang hoạt động trong nhóm này. "
                "Thử lại sau hoặc chọn nhóm khác.",
                InlineKeyboardMarkup([
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

        asset_caption = (
            f"{title}\n\n"
            f"Tìm thấy *{len(symbols)}* cặp. Trang *{page + 1}/{total_pages}*.\n"
            "Chọn một cặp để phân tích:"
        )
        if context.user_data.pop("send_new_asset_menu", False):
            # A new signal keeps the previous market category (OTC/Forex) but
            # must be delivered as a fresh message, not an edit of the signal.
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=ASSET_IMAGE,
                caption=asset_caption,
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode="Markdown",
            )
        else:
            await _show_photo_message(
                query,
                ASSET_IMAGE,
                asset_caption,
                InlineKeyboardMarkup(rows),
            )
        return CHOOSING_ASSET

    except Exception as e:
        logger.error(f"_show_asset_list error: {e}")
        await _show_photo_message(
            query,
            ASSET_IMAGE,
            f"❌ Lỗi: {e}\n\nThử /start lại.",
            InlineKeyboardMarkup([
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
    # Telegram can deliver callbacks from an older menu message after the
    # broker status has changed. Reject stale buttons immediately instead of
    # allowing a closed pair to reach the timeframe screen.
    if not pocket_client.is_asset_tradable(asset):
        await query.answer("Cặp này hiện đã đóng hoặc tạm dừng.", show_alert=True)
        return await _show_asset_list(update, context)

    context.user_data["asset"] = asset

    keyboard = [
        [
            InlineKeyboardButton("S15", callback_data="tf_s15"),
            InlineKeyboardButton("S30", callback_data="tf_s30"),
        ],
        [
            InlineKeyboardButton("M1", callback_data="tf_m1"),
            InlineKeyboardButton("M3", callback_data="tf_m3"),
            InlineKeyboardButton("M5", callback_data="tf_m5"),
        ],
        [InlineKeyboardButton("Quay Lại", callback_data="back_asset")],
    ]
    await _show_photo_message(
        query,
        TIMEFRAME_IMAGE,
        f"📊 *{asset}*\n\nChọn khung thời gian tín hiệu:",
        InlineKeyboardMarkup(keyboard),
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
    timeframe_label = TIMEFRAME_LABELS[tf_key]
    asset = context.user_data["asset"]

    await _show_caption_message(
        query,
        f"⏳ Đang phân tích *{asset}* khung *{timeframe_label}*...",
    )

    try:
        await pocket_client.ensure_connected()
        if not pocket_client.is_asset_tradable(asset):
            await _show_caption_message(
                query,
                f"⏸ *{asset}* hiện không mở giao dịch hoặc đã tạm dừng.\n\n"
                "Vui lòng quay lại và chọn cặp đang hoạt động.",
            )
            return await _show_asset_list(update, context)
        candles = await pocket_client.get_candles(asset, timeframe_seconds)
        signal = analyze_candles(candles)

        # Use the live tick price as the entry price so consecutive signals
        # do not share the same stale candle close. Wait briefly for the
        # stream to deliver a tick after subscribing via changeSymbol.
        current_price = await pocket_client.get_current_price_with_timeout(asset, timeout=2.0)
        entry_tick = pocket_client.get_current_tick(asset)
        if current_price is None or entry_tick is None:
            raise RuntimeError(
                f"Không có giá live hợp lệ cho {asset}; "
                "không thể tạo tín hiệu chính xác."
            )
        signal.price = current_price
        logger.info(f"Using live price for {asset}: {current_price}")

        entry_time = datetime.now()
        user_id = update.effective_user.id

        active_signals[user_id] = {
            "user_id": user_id,
            "asset": asset,
            "timeframe": timeframe_label,
            "timeframe_seconds": timeframe_seconds,
            "signal": signal,
            "entry_tick": entry_tick,
            "entry_time": entry_time,
            "message_id": query.message.message_id,
            "chat_id": query.message.chat_id,
        }

        if signal.direction == "WAIT":
            direction_emoji = "⏸ WAIT"
        else:
            direction_emoji = "🟢 CALL" if signal.direction == "CALL" else "🔴 PUT"

        text = (
            f"📊 *TÍN HIỆU {asset}*\n\n"
            f"🕐 Khung thời gian: *{timeframe_label}*\n"
            f"💰 Giá tín hiệu: *{signal.price:.6f}*\n"
            f"📈 Hướng: *{direction_emoji}*\n"
            f"🎯 Độ tin cậy: *{int(signal.confidence * 100)}%*\n\n"
            +
            (
                f"⏱ Kết quả sẽ được cập nhật sau *{timeframe_label}*."
                if signal.direction != "WAIT"
                else "⏸ Chưa đủ điều kiện xác nhận — bot không khuyến nghị vào lệnh."
            )
        )
        validation = signal.indicators.get("walk_forward", {})
        if validation.get("samples", 0) >= 12:
            text += (
                f"\n🧪 Kiểm định lịch sử: *{validation['accuracy'] * 100:.0f}%* "
                f"({validation['samples']} mẫu)"
            )

        keyboard = [
            [InlineKeyboardButton("🔔 Nhận tín hiệu mới", callback_data="new_signal")],
            [InlineKeyboardButton("🏠 Menu chính", callback_data="back_market")],
        ]
        # WAIT has no directional trade image and must not look like a PUT.
        signal_image = (
            CALL_IMAGE if signal.direction == "CALL"
            else PUT_IMAGE if signal.direction == "PUT"
            else WAIT_IMAGE
        )
        await _show_photo_message(
            query,
            signal_image,
            text,
            InlineKeyboardMarkup(keyboard),
        )

        if signal.direction != "WAIT":
            # Schedule result update. Pass a snapshot of the signal data so the
            # update task is independent from any future signals the user requests.
            task = asyncio.create_task(
                _update_result_after_delay(
                    context.application,
                    dict(active_signals[user_id]),
                )
            )
            active_signal_tasks[user_id] = task

        return SIGNAL_SENT

    except Exception as e:
        logger.error(f"timeframe_selected error: {e}")
        await _show_caption_message(
            query,
            f"❌ Không thể lấy dữ liệu phân tích: {e}\n\nVui lòng thử lại.",
        )
        return ConversationHandler.END


async def _update_result_after_delay(application: Application, signal_data: Dict[str, Any]) -> None:
    """Wait for the timeframe to elapse, then update the user with the result.

    signal_data is a snapshot of the signal that was just sent; it is owned by
    this task and will not be affected by later signals from the same user.
    """
    await asyncio.sleep(signal_data["timeframe_seconds"])

    user_id = signal_data.get("user_id")
    if user_id:
        active_signals.pop(user_id, None)
        active_signal_tasks.pop(user_id, None)

    asset = signal_data["asset"]
    tf_key = signal_data["timeframe"]
    signal: SignalResult = signal_data["signal"]
    chat_id = signal_data["chat_id"]

    if signal.direction == "WAIT":
        logger.info(f"Skipping result update for WAIT signal: {asset}")
        return

    try:
        await pocket_client.ensure_connected()

        # Prefer the live tick price for the exit; fall back to fetching the
        # latest candle close if the stream hasn't provided a price yet.
        entry_tick = signal_data.get("entry_tick")
        entry_tick_timestamp = entry_tick[1] if entry_tick else None
        exit_price = await pocket_client.get_current_price_with_timeout(
            asset,
            timeout=3.0,
            min_timestamp=entry_tick_timestamp,
        )
        if exit_price is None:
            logger.warning(
                f"Result for {asset}: no fresh tick after entry; "
                "will not classify the signal"
            )
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Không đủ dữ liệu giá mới cho {asset} sau {tf_key}. "
                    "Bot không thể xác định WIN/LOSS chính xác."
                ),
            )
            return
        logger.info(
            f"Result for {asset}: using fresh live price {exit_price} "
            f"(entry_tick={entry_tick_timestamp})"
        )

        logger.info(
            f"Evaluating {asset}: direction={signal.direction}, "
            f"entry={signal.price}, exit={exit_price}"
        )
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
    """Delete the old signal and reopen the same OTC/Forex asset list."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id if update.effective_user else None
    if user_id:
        task = active_signal_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        active_signals.pop(user_id, None)
    category = context.user_data.get("category")
    if not category:
        # Older signal messages may not have category state; only those fall
        # back to the market selector.
        context.user_data["send_new_menu"] = True
        return await start(update, context)

    context.user_data["page"] = 0
    context.user_data["send_new_asset_menu"] = True
    try:
        await query.message.delete()
    except Exception as e:
        logger.debug(f"Could not delete previous signal message: {e}")
    return await _show_asset_list(update, context)


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
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    application.add_handler(conv_handler)
    # Handle /start even if a user is not currently in the conversation.
    application.add_handler(CommandHandler("start", start))
    application.add_error_handler(error_handler)
    return application
