"""
Virtual Kiyim Kiydirish - Telegram bot

Foydalanuvchi kiyim rasmi va odam rasmini yuboradi,
bot Gradio (Hugging Face Space) backendiga so'rov yuborib,
natija rasmini qaytarib beradi.

Qo'shimcha imkoniyatlar:
- Kutish vaqtida jonli animatsiya (status yangilanib turadi)
- Kanalga majburiy obuna tekshiruvi (ixtiyoriy, sozlanadi)
- Natija rasmiga avtomatik pechat (watermark) bosish
"""

import asyncio
import io
import json
import logging
import os
import time
import uuid

import aiohttp
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "https://weshopai-weshopai-virtual-try-on.hf.space")

# ---------------------------------------------------------------------------
# Proxylar: har bir foydalanuvchi so'rovlari alohida IP orqali yuborilishi uchun
# ---------------------------------------------------------------------------
#
# .env faylida PROXIES o'zgaruvchisiga vergul bilan ajratilgan proxy
# manzillarini kiriting, masalan:
#   PROXIES=http://user1:pass1@1.2.3.4:8000,http://user2:pass2@5.6.7.8:8000,socks5://user3:pass3@9.9.9.9:1080
#
# Har bir Telegram foydalanuvchisi (user_id) ro'yxatdagi proxylardan biriga
# doimiy ("sticky") tarzda biriktiriladi — ya'ni bitta foydalanuvchi doim bitta
# proxy orqali so'rov yuboradi, turli foydalanuvchilar esa (agar proxylar soni
# yetarli bo'lsa) turli IP'lardan foydalanadi.
#
# Diqqat: bu yerda haqiqiy proxy serverlar kerak (masalan pullik
# residential/datacenter proxy provayderidan). Proxysiz (PROXIES bo'sh
# bo'lsa) bot avvalgidek — barcha foydalanuvchilar uchun bitta umumiy
# IP (serverning o'zi) orqali ishlaydi.
PROXIES = [p.strip() for p in os.getenv("PROXIES", "").replace("\n", ",").split(",") if p.strip()]


def get_proxy_for_user(user_id: int) -> str | None:
    """Foydalanuvchi ID'siga qarab ro'yxatdan doimiy proxy tanlaydi."""
    if not PROXIES:
        return None
    return PROXIES[user_id % len(PROXIES)]


def _mask_proxy(proxy: str) -> str:
    """Login/parolni log'larda ko'rsatmaslik uchun proxy manzilini yashiradi."""
    if "@" in proxy:
        scheme_and_creds, host_part = proxy.rsplit("@", 1)
        scheme = scheme_and_creds.split("://", 1)[0]
        return f"{scheme}://***@{host_part}"
    return proxy

# Kanalga obuna tekshiruvi (ixtiyoriy). @ belgisisiz kiriting, masalan: mychannel
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").lstrip("@").strip()
SUBSCRIPTION_REQUIRED = bool(CHANNEL_USERNAME)

# Natija rasmiga bosiladigan matn
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT") or (
    f"@{CHANNEL_USERNAME}" if CHANNEL_USERNAME else "Virtual Try-On"
)

# Gradio backendning ichki tuzilishiga bog'liq qiymatlar.
# Agar Space qayta deploy qilinsa, bular o'zgarishi mumkin.
FN_INDEX = 2
TRIGGER_ID = 18

# Natijani kutish uchun maksimal vaqt (soniyalarda)
RESULT_TIMEOUT = 180

# Status animatsiyasini yangilash oralig'i (soniya)
ANIMATION_INTERVAL = 2.5

# "Qayta urinish" tugmasi bilan nechta marta urinishga ruxsat berish
# (backend band bo'lganda foydalanuvchiga imkoniyat berish uchun)
MAX_RETRY_ATTEMPTS = 3

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suhbat holatlari
WAITING_CLOTH, WAITING_PERSON = range(2)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [["/cancel"]], resize_keyboard=True, one_time_keyboard=False
)


# ---------------------------------------------------------------------------
# Kanalga obuna tekshiruvi
# ---------------------------------------------------------------------------

async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not SUBSCRIPTION_REQUIRED:
        return True
    try:
        member = await context.bot.get_chat_member(
            chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id
        )
        return member.status in ("member", "administrator", "creator")
    except Exception:
        logger.warning("Obunani tekshirishda xatolik (bot admin emasmi?)")
        return False


def subscription_gate_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Kanalga qo'shilish",
                    url=f"https://t.me/{CHANNEL_USERNAME}",
                )
            ],
            [InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")],
        ]
    )


async def send_subscription_gate(update: Update) -> None:
    text = (
        "Botdan foydalanish uchun avval kanalimizga obuna bo'ling, "
        "so'ng \"✅ Tekshirish\" tugmasini bosing."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=subscription_gate_markup())
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=subscription_gate_markup()
        )


# ---------------------------------------------------------------------------
# Gradio bilan ishlash funksiyalari (Flutter ilovadagi api_service.dart bilan bir xil oqim)
# ---------------------------------------------------------------------------

async def upload_bytes(
    session: aiohttp.ClientSession,
    image_bytes: bytes,
    filename: str,
    proxy: str | None = None,
) -> dict:
    form = aiohttp.FormData()
    form.add_field("files", image_bytes, filename=filename, content_type="image/jpeg")

    async with session.post(
        f"{BASE_URL}/gradio_api/upload", data=form, proxy=proxy
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Yuklashda xatolik: {resp.status}")
        decoded = await resp.json()
        path = decoded[0]

    return {
        "path": path,
        "url": f"{BASE_URL}/gradio_api/file={path}",
        "orig_name": filename,
        "meta": {"_type": "gradio.FileData"},
    }


async def join_queue(
    session: aiohttp.ClientSession,
    cloth_data: dict,
    person_data: dict,
    session_hash: str,
    proxy: str | None = None,
) -> None:
    payload = {
        "data": [cloth_data, person_data, None],
        "event_data": None,
        "fn_index": FN_INDEX,
        "trigger_id": TRIGGER_ID,
        "session_hash": session_hash,
    }
    async with session.post(
        f"{BASE_URL}/gradio_api/queue/join",
        json=payload,
        headers={"Content-Type": "application/json"},
        proxy=proxy,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Navbatga qo'shilishda xatolik: {resp.status}")


async def wait_for_result(
    session: aiohttp.ClientSession, session_hash: str, proxy: str | None = None
) -> str:
    url = f"{BASE_URL}/gradio_api/queue/data?session_hash={session_hash}"
    start_time = time.monotonic()

    async with session.get(
        url, headers={"Accept": "text/event-stream"}, proxy=proxy
    ) as resp:
        async for raw_line in resp.content:
            if time.monotonic() - start_time > RESULT_TIMEOUT:
                raise TimeoutError("Natija kutish vaqti tugadi")

            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data: "):
                continue

            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            msg = data.get("msg")

            if msg == "process_completed":
                output = data.get("output") or {}

                if not output.get("success", True):
                    error_msg = output.get("error") or "Server xatoligi"
                    logger.error("Backend 'success=False' qaytardi: %s", output)
                    raise RuntimeError(f"Generatsiya xatoligi: {error_msg}")

                result_data = output.get("data")
                if not result_data or not isinstance(result_data, list):
                    logger.error("Kutilmagan 'data' formati: %s", output)
                    raise RuntimeError("Natija formatini o'qib bo'lmadi")

                first_item = result_data[0]
                if first_item is None:
                    logger.error(
                        "Backend natija o'rniga null qaytardi. To'liq output: %s",
                        output,
                    )
                    raise RuntimeError(
                        "Model natija bera olmadi. Rasmlarni almashtirib "
                        "qayta urinib ko'ring"
                    )

                if isinstance(first_item, dict) and "url" in first_item:
                    return first_item["url"]
                if isinstance(first_item, str):
                    return first_item

                logger.error(
                    "Natija elementi kutilmagan tipda (%s): %s",
                    type(first_item).__name__,
                    first_item,
                )
                raise RuntimeError("Natija formatini o'qib bo'lmadi")

            if msg in ("process_error", "unexpected_error"):
                raise RuntimeError("Server xatolik qaytardi")

    raise RuntimeError("Natija olinmadi (ulanish yopildi)")


def _build_connector(proxy: str | None):
    """SOCKS proxylar uchun maxsus connector qaytaradi (aiohttp o'zi
    SOCKS'ni tushunmaydi, HTTP(S) proxylarni esa `proxy=` argumenti
    orqali to'g'ridan-to'g'ri qo'llab-quvvatlaydi)."""
    if proxy and proxy.startswith(("socks4://", "socks5://")):
        from aiohttp_socks import ProxyConnector

        return ProxyConnector.from_url(proxy)
    return None


async def generate_try_on(
    cloth_bytes: bytes,
    person_bytes: bytes,
    proxy: str | None = None,
    status_callback=None,
) -> str:
    # uuid4 ishlatiladi, chunki millisekund vaqt tamg'asi (time.time())
    # bir nechta foydalanuvchi bir vaqtda so'rov yuborganda bir xil
    # qiymat berib qo'yishi mumkin edi — bu esa Gradio navbatida turli
    # foydalanuvchilarning so'rovlari aralashib ketishiga olib kelardi.
    session_hash = uuid.uuid4().hex

    is_socks = bool(proxy) and proxy.startswith(("socks4://", "socks5://"))
    # SOCKS proxy connector orqali o'rnatiladi, HTTP(S) proxy esa har bir
    # so'rovda `proxy=` argumenti orqali beriladi.
    http_proxy = proxy if (proxy and not is_socks) else None

    timeout = aiohttp.ClientTimeout(total=RESULT_TIMEOUT + 30)
    connector = _build_connector(proxy)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        if status_callback:
            status_callback("Rasmlar yuklanmoqda")

        cloth_data = await upload_bytes(session, cloth_bytes, "cloth.jpg", proxy=http_proxy)
        person_data = await upload_bytes(session, person_bytes, "person.jpg", proxy=http_proxy)

        if status_callback:
            status_callback("Navbatda kutilmoqda")

        await join_queue(session, cloth_data, person_data, session_hash, proxy=http_proxy)

        if status_callback:
            status_callback("Natija tayyorlanmoqda")

        result_url = await wait_for_result(session, session_hash, proxy=http_proxy)

    return result_url


async def download_bytes(url: str, proxy: str | None = None) -> bytes:
    is_socks = bool(proxy) and proxy.startswith(("socks4://", "socks5://"))
    http_proxy = proxy if (proxy and not is_socks) else None
    connector = _build_connector(proxy)

    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url, proxy=http_proxy) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Natija rasmini yuklab bo'lmadi: {resp.status}")
            return await resp.read()


# ---------------------------------------------------------------------------
# Watermark (pechat) qo'shish
# ---------------------------------------------------------------------------

def _get_font(size: int) -> ImageFont.ImageFont:
    for font_name in ("DejaVuSans-Bold.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(font_name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def add_watermark(image_bytes: bytes, text: str) -> bytes:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    font_size = max(16, image.width // 28)
    font = _get_font(font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    margin = max(10, int(image.width * 0.025))
    x = image.width - text_w - margin
    y = image.height - text_h - margin

    draw.rectangle(
        [x - 12, y - 8, x + text_w + 12, y + text_h + 12],
        fill=(0, 0, 0, 120),
    )
    draw.text((x, y - bbox[1]), text, font=font, fill=(255, 255, 255, 235))

    result = Image.alpha_composite(image, layer).convert("RGB")
    output = io.BytesIO()
    result.save(output, format="JPEG", quality=92)
    return output.getvalue()


# ---------------------------------------------------------------------------
# Kutish animatsiyasi
# ---------------------------------------------------------------------------

class StatusAnimator:
    """Status xabarini fon rejimida jonlantirib turadi."""

    _FRAMES = ["⏳", "⌛"]
    _DOTS = ["", ".", "..", "..."]

    def __init__(self, bot, chat_id: int, message_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._text = "Boshlanmoqda"
        self._task = None

    def set_text(self, text: str) -> None:
        self._text = text

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        tick = 0
        try:
            while True:
                frame = self._FRAMES[tick % len(self._FRAMES)]
                dots = self._DOTS[tick % len(self._DOTS)]
                await self._safe_edit(f"{frame} {self._text}{dots}")
                try:
                    await self._bot.send_chat_action(
                        chat_id=self._chat_id, action=ChatAction.UPLOAD_PHOTO
                    )
                except Exception:
                    pass
                tick += 1
                await asyncio.sleep(ANIMATION_INTERVAL)
        except asyncio.CancelledError:
            raise

    async def _safe_edit(self, text: str) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id, message_id=self._message_id, text=text
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Telegram bot handlerlari
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user_id = update.effective_user.id

    if not await is_subscribed(context, user_id):
        await send_subscription_gate(update)
        return ConversationHandler.END

    await update.message.reply_text(
        "Assalomu alaykum! 👋\n\n"
        "Bu bot orqali kiyimni virtual tarzda kiyib ko'rishingiz mumkin.\n\n"
        "1️⃣ Avval *kiyim* rasmini yuboring.\n"
        "2️⃣ Keyin *o'zingizning* rasmingizni yuboring.\n\n"
        "Bekor qilish uchun /cancel buyrug'ini yuboring.",
        parse_mode="Markdown",
        reply_markup=CANCEL_KEYBOARD,
    )
    return WAITING_CLOTH


async def check_subscription_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user_id = query.from_user.id

    if await is_subscribed(context, user_id):
        await query.answer("Rahmat!")
        await query.edit_message_text(
            "✅ Obuna tasdiqlandi!\n\nEndi *kiyim* rasmini yuboring.",
            parse_mode="Markdown",
        )
        return WAITING_CLOTH

    await query.answer("Hali kanalga qo'shilmagansiz 🙁", show_alert=True)
    return ConversationHandler.END


async def receive_cloth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1]
    file = await photo.get_file()
    cloth_bytes = bytes(await file.download_as_bytearray())

    context.user_data["cloth_bytes"] = cloth_bytes

    await update.message.reply_text(
        "✅ Kiyim rasmi qabul qilindi.\n\nEndi o'zingizning rasmingizni yuboring."
    )
    return WAITING_PERSON


async def receive_person(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1]
    file = await photo.get_file()
    person_bytes = bytes(await file.download_as_bytearray())

    cloth_bytes = context.user_data.get("cloth_bytes")
    if cloth_bytes is None:
        await update.message.reply_text(
            "Xatolik: avval kiyim rasmi yuborilmagan. /start buyrug'i bilan qayta boshlang."
        )
        return ConversationHandler.END

    context.user_data["person_bytes"] = person_bytes
    context.user_data["retry_count"] = 0

    # Klaviaturani olib tashlash uchun alohida, vaqtinchalik xabar yuboriladi.
    # (Muhim: ReplyKeyboardRemove bilan yuborilgan xabarni Telegram keyinchalik
    # tahrirlashga ruxsat bermaydi, shuning uchun status xabari undan ajratilgan.)
    await update.message.reply_text(
        "Qabul qilindi.", reply_markup=ReplyKeyboardRemove()
    )

    await run_generation(context, update.effective_chat.id, update.effective_user.id)
    return ConversationHandler.END


async def run_generation(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int
) -> None:
    """context.user_data'da saqlangan cloth_bytes/person_bytes asosida
    generatsiyani bajaradi. Bir nechta joydan (birinchi urinish va
    "Qayta urinish" tugmasi) chaqiriladi."""

    cloth_bytes = context.user_data.get("cloth_bytes")
    person_bytes = context.user_data.get("person_bytes")

    if cloth_bytes is None or person_bytes is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Xatolik: rasmlar topilmadi. /start buyrug'i bilan qayta boshlang.",
        )
        return

    proxy = get_proxy_for_user(user_id)
    if proxy:
        logger.info(
            "Foydalanuvchi %s uchun proxy ishlatilmoqda: %s",
            user_id,
            _mask_proxy(proxy),
        )

    status_message = await context.bot.send_message(chat_id=chat_id, text="⏳ Boshlanmoqda...")

    animator = StatusAnimator(
        bot=context.bot, chat_id=chat_id, message_id=status_message.message_id
    )
    animator.start()

    async def safe_edit(text: str) -> None:
        try:
            await status_message.edit_text(text)
        except Exception:
            logger.warning("Status xabarini tahrirlab bo'lmadi, yangi xabar yuborilmoqda")
            await context.bot.send_message(chat_id=chat_id, text=text)

    try:
        result_url = await generate_try_on(
            cloth_bytes=cloth_bytes,
            person_bytes=person_bytes,
            proxy=proxy,
            status_callback=animator.set_text,
        )

        await animator.stop()
        await safe_edit("✅ Tayyor! Natija yuborilmoqda...")

        try:
            raw_bytes = await download_bytes(result_url, proxy=proxy)
            final_bytes = add_watermark(raw_bytes, WATERMARK_TEXT)
            photo_to_send = io.BytesIO(final_bytes)
            photo_to_send.name = "result.jpg"
        except Exception:
            logger.exception("Watermark qo'shishda xatolik, original rasm yuboriladi")
            photo_to_send = result_url

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_to_send,
            caption="Natijangiz tayyor! Yana urinib ko'rish uchun /start buyrug'ini yuboring.",
        )
        context.user_data.clear()

    except TimeoutError:
        await animator.stop()
        await safe_edit(
            "⚠️ Server javob berishga ancha vaqt ketmoqda "
            "(odatda backend bir vaqtda ko'p foydalanuvchi tomonidan band bo'lganda)."
        )
        await offer_retry(context, chat_id)
    except Exception as exc:
        await animator.stop()
        logger.exception("Try-on generatsiyasida xatolik")
        await safe_edit(f"❌ Xatolik yuz berdi: {exc}")
        await offer_retry(context, chat_id)


async def offer_retry(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Xatodan keyin foydalanuvchiga xuddi shu rasmlar bilan qayta urinish
    imkoniyatini beradi. Ko'pincha bu backend bandligi tufayli sodir bo'ladi,
    shuning uchun qayta urinish tez-tez yordam beradi."""

    attempts = context.user_data.get("retry_count", 0)

    if attempts >= MAX_RETRY_ATTEMPTS:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Bir necha marta urinildi, lekin natija olinmadi.\n\n"
                "Bu odatda backend serveri bir vaqtning o'zida juda ko'p "
                "foydalanuvchi tomonidan ishlatilganda yuz beradi.\n\n"
                "Iltimos, 5-10 daqiqadan so'ng /start bilan qaytadan urinib "
                "ko'ring, yoki boshqa rasmlar bilan sinab ko'ring."
            ),
        )
        context.user_data.clear()
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Qayta urinish", callback_data="retry_tryon")]]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Bu ko'pincha backend bir vaqtda ko'p foydalanuvchi tomonidan "
            "ishlatilganda yuz beradi. Xuddi shu rasmlar bilan qayta urinib "
            "ko'rishingiz mumkin 👇"
        ),
        reply_markup=keyboard,
    )


async def retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not context.user_data.get("cloth_bytes") or not context.user_data.get("person_bytes"):
        await query.edit_message_text(
            "Rasmlar topilmadi. /start buyrug'i bilan qayta boshlang."
        )
        return

    context.user_data["retry_count"] = context.user_data.get("retry_count", 0) + 1

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await run_generation(context, update.effective_chat.id, update.effective_user.id)


async def wrong_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Iltimos, rasm (photo) yuboring.")
    return context.chat_data.get("_current_state", WAITING_CLOTH)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Bekor qilindi. Qayta boshlash uchun /start buyrug'ini yuboring.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN topilmadi. .env faylida BOT_TOKEN=... qiymatini kiriting."
        )

    # concurrent_updates=True: standart holatda python-telegram-bot har bir
    # xabarni ketma-ket (bittadan) qayta ishlaydi. Bizning holatda esa bitta
    # generatsiya 180 soniyagacha davom etishi mumkin — shu vaqt ichida
    # boshqa barcha foydalanuvchilarning xabarlari navbatda kutib turadi va
    # bot ularga "javob bermayotgandek" ko'rinadi. Shuning uchun bir nechta
    # foydalanuvchini parallel qayta ishlashga ruxsat beramiz.
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(64)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(check_subscription_callback, pattern="^check_sub$"),
        ],
        states={
            WAITING_CLOTH: [
                MessageHandler(filters.PHOTO, receive_cloth),
                MessageHandler(~filters.COMMAND, wrong_content),
            ],
            WAITING_PERSON: [
                MessageHandler(filters.PHOTO, receive_person),
                MessageHandler(~filters.COMMAND, wrong_content),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(
        CallbackQueryHandler(retry_callback, pattern="^retry_tryon$")
    )

    logger.info("Bot ishga tushdi...")
    if SUBSCRIPTION_REQUIRED:
        logger.info("Kanalga obuna tekshiruvi yoqilgan: @%s", CHANNEL_USERNAME)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()