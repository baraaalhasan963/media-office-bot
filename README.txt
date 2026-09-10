# بوت الإعلام الجامعي (Telegram)

بوت تلغرام لإدارة تغطيات وأغراض مكتب الإعلام في الجامعة.
مبني على **python-telegram-bot v21** (polling) ببنية MVC:

```
main.py                    -> handlers + نقطة دخول (python main.py)
config/__init__.py         -> الإعدادات (من env.txt)
models/database.py         -> طبقة البيانات (aiosqlite + pooling + WAL)
views/keyboards.py         -> الأزرار
views/formatting.py        -> تنسيق النصوص والتواريخ
views/messages.py          -> بناء رسائل التفاصيل ولوحة الاستعارات
controllers/router.py      -> تسجيل الـ handlers + main()
api_client/client.py       -> غلاف رسائل Telegram (اختياري، غير مربوط)
```

## الإعداد

1. من بوابة BotFather أنشئ بوتاً وانسخ التوكن.
2. انسخ `env.txt.example` إلى `env.txt` واملأ القيم:
   - `BOT_TOKEN` — توكن البوت
   - `ADMIN_CHAT_ID` — معرّف كروب الإدارة
   - `ADMIN_TOPIC_ID` — معرّف توبيك التغطيات
   - `BORROW_TOPIC_ID` — معرّف توبيك الاستعارات
   - `ADMIN_USERS_IDS` — معرّفات المشرفين (بالفواصل)
   - `DB_PATH` — مسار قاعدة البيانات (افتراضياً bot_database.db)
   - `GEMINI_API_KEY` / `GROQ_API_KEY` — مفاتيح الجرعة الإعلامية اليومية
3. `pip install -r requirements.txt`
4. `python main.py`

> ⚠️ **لا ترفع `env.txt` الحقيقي لأي ريبو عام** — يحتوي أسراراً (توكن البوت ومفاتيح API).
> هو مستثنى في `.gitignore`.