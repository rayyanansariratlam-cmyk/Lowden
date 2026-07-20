import logging
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ---------------- CONFIGURATION ----------------
BOT_TOKEN = "8910166726:AAF_A3DeIjAlVVnq7zzv1AoDKahthYZyMuc"  # अपना टेलीग्राम बोट टोकन यहाँ डालें
OWNER_USERNAME = "@ModDevx"         # अपना टेलीग्राम यूजरनेम यहाँ डालें

# Beniz Payment Gateway Config
API_KEY = "vp_23c371682143ce32a0a85f298dc3d5d5547eb477fad66474"  # आपकी API Key
BASE_URL = "https://paymentsauth.beniz.xyz/api/v1"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Conversation States
WAITING_FOR_AMOUNT = 1

# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# ---------------- HANDLERS ----------------

# 1. /start Command Handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        f"👋 **नमस्ते! बोट में आपका स्वागत है।**\n\n"
        f"👑 **Owner:** {OWNER_USERNAME}\n\n"
        f"नीचे दिए गए बटन पर क्लिक करके पेमेंट करें और अपनी की (Key) तुरंत प्राप्त करें।"
    )
    
    keyboard = [
        [InlineKeyboardButton("💳 Payment Gateway", callback_data="pay_gateway")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text=welcome_text, 
        parse_mode="Markdown", 
        reply_markup=reply_markup
    )

# 2. Payment Gateway Button Click
async def payment_gateway_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text="💰 **कृपया भुगतान की जाने वाली राशि दर्ज करें (Enter Amount):**\n\n"
             "*(न्यूनतम राशि: ₹1)*",
        parse_mode="Markdown"
    )
    return WAITING_FOR_AMOUNT

# 3. Handle Amount & Create Payment Order
async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    
    # Amount validation
    try:
        amount = float(user_text)
        if amount < 1:
            await update.message.reply_text("❌ **त्रुटि:** न्यूनतम राशि ₹1 होनी चाहिए। कृपया पुनः प्रयास करें:")
            return WAITING_FOR_AMOUNT
    except ValueError:
        await update.message.reply_text("❌ **गलत इनपुट:** कृपया केवल नंबर (जैसे 10, 50, 100) दर्ज करें:")
        return WAITING_FOR_AMOUNT

    await update.message.reply_text("⏳ **पेमेंट लिंक और आर्डर बनाया जा रहा है...**")

    # Product Key delivery content (यह आपकी डिजिटल डिलीवरी की होगी)
    delivery_content = "KEY-VIP-8899-X12"

    # API Request to Create Order
    try:
        payload = {
            "amount": amount,
            "delivery_content": delivery_content
        }
        resp = requests.post(f"{BASE_URL}/order.php", json=payload, headers=HEADERS, timeout=10)
        order_data = resp.json()

        order_id = order_data.get("order_id")
        public_link = order_data.get("public_link")

        if not order_id or not public_link:
            await update.message.reply_text("❌ ऑर्डर बनाने में समस्या आई। कृपया एडमिन से संपर्क करें।")
            return ConversationHandler.END

        # Create Payment Link Button
        pay_keyboard = [
            [InlineKeyboardButton("🔗 Pay Now (पेमेंट करें)", url=public_link)]
        ]
        
        msg_text = (
            f"✅ **ऑर्डर सफलतापूर्वक जनरेट हो गया है!**\n\n"
            f"🆔 **Order ID:** `{order_id}`\n"
            f"💵 **Amount:** ₹{amount}\n\n"
            f"👇 नीचे दिए गए लिंक पर क्लिक करके पेमेंट पूरा करें। पेमेंट होते ही ऑटो-वेरिफिकेशन शुरू हो जाएगा।"
        )

        await update.message.reply_text(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(pay_keyboard)
        )

        # Start Async Background Payment Verification
        asyncio.create_task(verify_payment_loop(context, update.effective_chat.id, order_id))

    except Exception as e:
        logging.error(f"Error in order creation: {e}")
        await update.message.reply_text("❌ एपीआई से कनेक्ट करने में विफल। कृपया थोड़ी देर बाद प्रयास करें।")

    return ConversationHandler.END

# 4. Background Verification & Delivery Task
async def verify_payment_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, order_id: str):
    max_checks = 45  # 45 checks * 8 seconds = ~6 minutes timeout
    
    for _ in range(max_checks):
        await asyncio.sleep(8)  # Check every 8 seconds
        
        try:
            r = requests.get(f"{BASE_URL}/verify.php?order_id={order_id}", headers=HEADERS, timeout=10)
            status_data = r.json()

            # If Payment Successful
            if status_data.get("payment"):
                delivery_token = status_data.get("delivery_token")
                
                # Deliver Product
                del_resp = requests.get(f"{BASE_URL}/deliver.php?token={delivery_token}", headers=HEADERS, timeout=10)
                delivery_result = del_resp.json()
                
                delivered_key = delivery_result.get("content", "KEY-VIP-8899-X12")

                success_msg = (
                    f"🎉 **पेमेंट सफल रहा! (Payment Successful)**\n\n"
                    f"🆔 **Order ID:** `{order_id}`\n"
                    f"📦 **आपकी की (Key):** `{delivered_key}`\n\n"
                    f"खरीदारी के लिए धन्यवाद!"
                )
                await context.bot.send_message(chat_id=chat_id, text=success_msg, parse_mode="Markdown")
                return

            # If Order Expired
            elif status_data.get("status") == "EXPIRED":
                fail_msg = f"❌ **पेमेंट समय समाप्त (Order Expired)**\n\nOrder ID: `{order_id}` का समय समाप्त हो गया है।"
                await context.bot.send_message(chat_id=chat_id, text=fail_msg, parse_mode="Markdown")
                return

        except Exception as e:
            logging.error(f"Error checking verification: {e}")

    # Timeout Message after max checks
    await context.bot.send_message(
        chat_id=chat_id, 
        text=f"⚠️ **पेमेंट टाइमआउट:** Order ID `{order_id}` का वेरिफिकेशन टाइमआउट हो गया है।"
    )

# Cancel Handler for conversation
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("प्रक्रिया रद्द कर दी गई है।")
    return ConversationHandler.END

# ---------------- MAIN FUNCTION ----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_gateway_click, pattern="^pay_gateway$")],
        states={
            WAITING_FOR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    print("🤖 Telegram Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
