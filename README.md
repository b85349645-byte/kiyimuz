# Virtual Kiyim Kiydirish — Telegram bot

Flutter ilovadagi bilan bir xil Gradio backendga ulanadigan Telegram bot.

## O'rnatish

```bash
cd telegram_bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Sozlash

1. `.env.example` faylini nusxalab `.env` deb nomlang:
   ```bash
   cp .env.example .env
   ```
2. `.env` faylini oching va `BOT_TOKEN` qatoriga BotFather'dan olgan tokeningizni yozing:
   ```
   BOT_TOKEN=sizning_haqiqiy_tokeningiz
   ```
3. Agar backend manzili (`BASE_URL`) boshqacha bo'lsa, uni ham shu faylda o'zgartiring.

**Muhim:** `.env` faylini hech qachon GitHub'ga yuklamang — u tokeningizni o'z ichiga oladi. Agar Git ishlatsangiz, `.gitignore` fayliga `.env` qatorini qo'shing.

## Ishga tushirish

```bash
python bot.py
```

Bot ishga tushgach, Telegram'da botingizga `/start` buyrug'ini yuboring.

## Qanday ishlaydi

1. Foydalanuvchi `/start` yuboradi.
2. Bot kiyim rasmini so'raydi.
3. Bot odam rasmini so'raydi.
4. Bot ikkala rasmni Gradio backendga yuklaydi, navbatga qo'shiladi va natijani kutadi (bu jarayon xabar orqali yangilanib turadi).
5. Tayyor bo'lgan natija rasm sifatida yuboriladi.

Bekor qilish uchun istalgan vaqtda `/cancel` yuborish mumkin.

## 24/7 ishlashi uchun

`python bot.py` buyrug'i faqat kompyuter/server yoqilgan vaqtda ishlaydi. Doimiy ishlashi uchun quyidagilardan birini tanlang:

- **VPS server** (masalan, DigitalOcean, Timeweb) + `systemd` yoki `screen`/`tmux` orqali fon jarayonida ishga tushirish.
- **Bulutli hosting** (Railway, Render kabi) — repo'ni yuklab, `BOT_TOKEN` va `BASE_URL`ni "Environment Variables" bo'limiga kiritish kifoya.

Agar VPS'da `systemd` orqali doimiy ishlatishni xohlasangiz, ayting — shu uchun ham konfiguratsiya faylini tayyorlab beraman.
