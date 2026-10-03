"""AI assistants: Daily Media Dose and Caption / Content Generator."""

import random
import asyncio
import logging
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
from datetime import timedelta
import google.generativeai as genai
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from telegram.constants import ChatAction
from telegram.ext import ContextTypes, ConversationHandler

import config
from constants import CaptionState
from controllers.common import is_supervisor
from views.formatting import escape_html

try:
    from groq import Groq
    HAS_GROQ = True
except ImportError:
    HAS_GROQ = False

logger = logging.getLogger("bot.ai")

if config.GEMINI_API_KEY and config.GEMINI_API_KEY != "ضـع_مفتـاح_الـAPI_هنا":
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Error configuring Gemini API: {e}")


async def get_ai_response(prompt: str) -> str:
    """محاولة توليد محتوى باستخدام Gemini أولاً، ثم Groq كبديل تلقائي."""
    if config.GEMINI_API_KEY and config.GEMINI_API_KEY != "ضـع_مفتـاح_الـAPI_هنا":
        try:
            models_to_try = ['gemini-flash-latest', 'gemini-1.5-flash-8b', 'gemini-1.5-pro']
            for model_name in models_to_try:
                try:
                    safety_settings = [
                        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                    ]
                    model = genai.GenerativeModel(model_name, safety_settings=safety_settings)
                    response = await asyncio.to_thread(model.generate_content, prompt)
                    if response and response.text:
                        return response.text.strip()
                except Exception as inner_e:
                    logger.warning(f"Model {model_name} failed: {inner_e}")
                    continue
        except Exception as e:
            logger.error(f"All Gemini models failed: {e}")

    # Fallback to Groq
    if HAS_GROQ and config.GROQ_API_KEY and config.GROQ_API_KEY != "ضـع_مفتـاح_Groq_هنـا":
        try:
            client = Groq(api_key=config.GROQ_API_KEY)
            groq_models = ["qwen/qwen3.8-27b", "allam-2-7b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
            def call_groq():
                for m in groq_models:
                    try:
                        completion = client.chat.completions.create(
                            model=m,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        if completion and completion.choices:
                            return completion.choices[0].message.content
                    except Exception as ge:
                        logger.warning(f"Groq model {m} failed: {ge}")
                        continue
                return None
            response_text = await asyncio.to_thread(call_groq)
            if response_text:
                return response_text.strip()
        except Exception as e:
            logger.error(f"Groq error: {e}")
    elif not HAS_GROQ and config.GROQ_API_KEY and config.GROQ_API_KEY != "ضـع_مفتـاح_Groq_هنـا":
        logger.warning("Groq library is not installed. Fallback skipped.")

    raise Exception("عذراً، جميع خدمات الذكاء الاصطناعي (Gemini & Groq) غير متاحة حالياً بسبب ضغط الطلبات أو انتهاء الحصة.")


async def get_ai_response_by_model(prompt: str, model_key: str) -> str:
    if model_key == "gemini":
        models_to_try = ['gemini-flash-latest', 'gemini-1.5-flash-latest', 'gemini-1.5-flash-8b']
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        for model_name in models_to_try:
            try:
                model = genai.GenerativeModel(model_name, safety_settings=safety_settings)
                response = await asyncio.to_thread(model.generate_content, prompt)
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                logger.warning(f"Gemini model {model_name} failed: {e}")
                continue
        raise Exception("جميع نماذج Gemini غير متاحة حالياً")
    elif model_key == "groq":
        if not HAS_GROQ:
            raise Exception("مكتبة Groq غير مثبتة")
        if not config.GROQ_API_KEY or config.GROQ_API_KEY == "ضـع_مفتـاح_Groq_هنـا":
            raise Exception("مفتاح Groq غير مهيأ")
        client = Groq(api_key=config.GROQ_API_KEY)
        groq_models = ["qwen/qwen3.8-27b", "allam-2-7b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        def call_groq():
            for m in groq_models:
                try:
                    completion = client.chat.completions.create(
                        model=m,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    if completion and completion.choices:
                        return completion.choices[0].message.content
                except Exception as ge:
                    logger.warning(f"Groq model {m} failed: {ge}")
                    continue
            return None
        result = await asyncio.to_thread(call_groq)
        if result:
            return result.strip()
        raise Exception("جميع نماذج Groq غير متاحة حالياً")
    raise Exception(f"نموذج غير معروف: {model_key}")


# ─── Daily Media Dose ─────────────────────────────────────────────────────────

async def send_daily_media_dose(context: ContextTypes.DEFAULT_TYPE, prev_dose: str = ""):
    """إرسال جرعة إعلامية يومية. prev_dose: نص الجرعة السابقة لتجنب التكرار."""
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "ضـع_مفتـاح_الـAPI_هنا":
        logger.warning("Gemini API key is not configured. Daily dose skipped.")
        return False, "مفتاح API الخاص بـ Gemini غير مهيأ."

    categories = ["نصيحة تقنية أو احترافية", "خطأ إعلامي شائع وكيفية تجنبه", "فكرة إبداعية ومبتكرة للتغطيات", "تجربة شخصية أو موقف طريف", "اتجاه جديد في الإعلام"]
    weights = [35, 20, 20, 15, 10]
    chosen_category = random.choices(categories, weights=weights, k=1)[0]
    topics = [
        "التصوير الفوتوغرافي", "تصوير الفيديو والمونتاج", "إجراء المقابلات",
        "هندسة الصوت الميدانية", "صياغة العناوين والكابشن", "توزيع الإضاءة",
        "إدارة الوقت الميداني", "التعامل مع الضيوف", "تحرير الفيديو",
        "التصوير الجوي بالدرون", "البث المباشر", "كتابة السيناريو",
        "تصوير المؤتمرات والفعاليات الكبرى", "التعامل مع الكاميرات المختلفة",
        "أخلاقيات العمل الإعلامي", "تصوير المنتجات", "الإخراج التلفزيوني",
        "المونتاج السينمائي", "تصحيح الألوان", "التعليق الصوتي",
        "الإنفوجرافيك والتصميم", "صناعة المحتوى الرقمي", "تحرير الصور",
        "إدارة حسابات التواصل الاجتماعي", "التحليل الإعلامي",
        "البودكاست وإعداده", "التغطيات الرياضية", "الترجمة الإعلامية",
        "الأمان الميداني للفرق الإعلامية", "إعداد التقارير الإخبارية",
    ]
    random_topic = random.choice(topics)

    avoid_block = (
        f"\nمهم: الجرعة السابقة كانت: '{prev_dose[:120]}...'\nاحرص أن تكون مختلفة تماماً بفكرة وأسلوب مختلف."
        if prev_dose else ""
    )

    try:
        prompt = (
            f"اكتب رسالة قصيرة لفريق إعلامي في سوريا حول '{chosen_category}' في مجال {random_topic}."
            f" اللغة سورية طبيعية، الأسلوب مباشر وعملي بدون مبالغة أو تزويق. الجمل قصيرة ومختصرة."
            f" 2-3 أسطر بدون مقدمة طويلة، الفكرة قابلة للتطبيق ضمن الإمكانيات المتاحة في سوريا (ضعف التجهيزات، تقطع الكهرباء والنت، الحصار والعقوبات)."
            f" لا تذكر شغلات مثالية أو مكلفة أو مستحيلة التطبيق محلياً. لا تستخدم ألقاب أو مديح زائد."
            f" يمكن استخدام إيموجي واحد أو اثنين."
            f"{avoid_block}"
        )

        ai_text = await get_ai_response(prompt)

        emojis = {
            "نصيحة تقنية أو احترافية": "💡",
            "خطأ إعلامي شائع وكيفية تجنبه": "⚠️",
            "فكرة إبداعية ومبتكرة للتغطيات": "🌟",
            "تجربة شخصية أو موقف طريف": "🎙️",
            "اتجاه جديد في الإعلام": "📈"
        }
        header = f"{emojis[chosen_category]} <b>{random_topic}</b>"
        msg = f"{header}\n\n{ai_text}"

        context.bot_data["last_dose_text"] = ai_text
        target_chat_id = getattr(config, "DOSE_CHAT_ID", config.ADMIN_CHAT_ID)
        kwargs = {
            "chat_id": target_chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "reply_markup": InlineKeyboardMarkup([[InlineKeyboardButton("🔄 جرعة ثانية", callback_data="dose_refresh")]])
        }
        topic_id = getattr(config, "DOSE_TOPIC_ID", config.ADMIN_TOPIC_ID)
        if topic_id:
            kwargs["message_thread_id"] = topic_id

        await context.bot.send_message(**kwargs)
        logger.info(f"Daily dose ({chosen_category}) sent.")
        return True, None
    except Exception as e:
        logger.error(f"Error generating daily dose: {e}")
        return False, str(e)


async def test_dose_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_supervisor(update.effective_user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return
    status_msg = await update.message.reply_text("⏳ جاري إنشاء الرسالة من الذكاء الاصطناعي... يرجى الانتظار.")
    success, error = await send_daily_media_dose(context)
    if success:
        await status_msg.edit_text("✅ تم توليد الرسالة وإرسالها إلى كروب الإدارة بنجاح.")
    else:
        await status_msg.edit_text(f"❌ حدث خطأ أثناء التوليد:\n{error}")


async def handle_dose_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ جاري توليد جرعة جديدة...")
    prev_text = context.bot_data.get("last_dose_text", "")
    try:
        await query.edit_message_reply_markup(None)
    except Exception:
        pass
    success, error = await send_daily_media_dose(context, prev_dose=prev_text)
    if not success:
        await query.message.reply_text(f"❌ تعذّر توليد جرعة جديدة:\n{error}")


# ─── Caption Assistant ────────────────────────────────────────────────────────

CAPTION_TASKS = {
    "reels":    ("🎬", "كابشن ريلز/شورتس",    "اكتب كابشن احترافي لفيديو ريلز على إنستغرام وتيك توك حول الموضوع التالي. الأسلوب جذاب وسريع ومثير للفضول، ابدأ بجملة صادمة أو سؤال قوي، أضف 5-7 هاشتاغات مناسبة في النهاية:\n\n"),
    "news":     ("📰", "خبر صحفي رسمي",        "اكتب خبراً صحفياً رسمياً بأسلوب الصحافة المحترفة (الهرم المقلوب) حول الموضوع التالي. ابدأ بسطر ملخص قوي، ثم التفاصيل، ثم السياق. اللغة عربية فصحى رصينة:\n\n"),
    "three":    ("📋", "3 صيغ مختلفة",         "اكتب 3 صيغ مختلفة تماماً لكابشن حول الموضوع التالي:\nالصيغة 1 - رسمية ومؤسسية\nالصيغة 2 - تفاعلية وشعبية\nالصيغة 3 - قصيرة وصاعقة (أقل من 15 كلمة)\n\nالموضوع:\n"),
    "hashtags": ("🏷️", "هاشتاغات ذكية",         "ولّد 15-20 هاشتاغاً عربياً وإنجليزياً متنوعاً ومناسباً للمنصات لهذا الموضوع. قسّمها: هاشتاغات عامة، متخصصة، ومحلية:\n\n"),
    "proofread":("✅", "تدقيق وتحسين",          "دقق النص التالي إملائياً ونحوياً وأسلوبياً. أعطني:\n1. النص المصحح كاملاً\n2. قائمة بالأخطاء التي وجدتها\n3. 2-3 مقترحات لتحسين الأسلوب\n\nالنص:\n"),
    "platform": ("📱", "صيغة منصة محددة",       "سأعطيك نصاً أو فكرة، أعد صياغتها بما يناسب إنستغرام وتويتر/X وتيليغرام بشكل منفصل مع مراعاة حد الأحرف وطبيعة كل منصة:\n\n"),
    "headline": ("📣", "عنوان جذاب",            "اقترح 5 عناوين جذابة ومختلفة في الأسلوب لهذا الموضوع أو الخبر. كل عنوان لا يتجاوز 10 كلمات:\n\n"),
}

CAPTION_TONES = {
    "formal":   ("🏛️", "رسمي ومؤسساتي", "النبرة المطلوبة: رسمية، مؤسسية، فصحى رصينة، ومهنية عالية تناسب البيانات والفعاليات الرسمية."),
    "exciting": ("🔥", "حماسي وتشويقي", "النبرة المطلوبة: حماسية، تشويقية، سريعة، تجذب الانتباه وتناسب منصات التواصل وريلز وتيك توك."),
    "news":     ("📰", "صحفي ومحايد",  "النبرة المطلوبة: إخبارية وموضوعية ودقيقة وفق أسلوب الهرم المقلوب ووكالات الأنباء المعتمدة."),
    "casual":   ("☕", "بسيط وتفاعلي", "النبرة المطلوبة: تفاعلية، قريبة وودية، سهلة وممتعة بدون تكلف تناسب قنوات ومجموعات تيليغرام."),
}

def _task_keyboard():
    buttons = []
    items = list(CAPTION_TASKS.items())
    for i in range(0, len(items), 2):
        row = []
        for key, (emoji, label, _) in items[i:i+2]:
            row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"captask_{key}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ إلغاء", callback_data="captask_cancel")])
    return InlineKeyboardMarkup(buttons)

def _tone_keyboard():
    buttons = [
        [
            InlineKeyboardButton("🏛️ رسمي ومؤسساتي", callback_data="captone_formal"),
            InlineKeyboardButton("🔥 حماسي وتشويقي", callback_data="captone_exciting"),
        ],
        [
            InlineKeyboardButton("📰 صحفي ومحايد",  callback_data="captone_news"),
            InlineKeyboardButton("☕ بسيط وتفاعلي", callback_data="captone_casual"),
        ],
        [InlineKeyboardButton("🔙 رجوع لاختيار المهمة", callback_data="captone_back")]
    ]
    return InlineKeyboardMarkup(buttons)

def _model_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✨ Gemini (أقوى)", callback_data="capmodel_gemini"),
        InlineKeyboardButton("⚡ Groq (أسرع)",  callback_data="capmodel_groq"),
    ]])

def _action_keyboard(has_variants=False):
    rows = [[
        InlineKeyboardButton("📝 تعديل",       callback_data="caption_edit"),
        InlineKeyboardButton("🔄 مهمة أخرى",   callback_data="caption_restart"),
        InlineKeyboardButton("✅ إنهاء",        callback_data="caption_done"),
    ]]
    return InlineKeyboardMarkup(rows)

async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "✍️ <b>المساعد الإعلامي للكتابة والمحتوى</b>\n\n"
        "أرسل لي النص أو الفكرة أو الموضوع الذي تريد العمل عليه:",
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True)
    )
    return CaptionState.TYPING_PROMPT

async def caption_receive_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("⚠️ النص فارغ، أرسله مجدداً.")
        return CaptionState.TYPING_PROMPT

    context.user_data['caption_prompt'] = text
    await update.message.reply_text(
        "🎯 <b>اختر المهمة المطلوبة:</b>",
        parse_mode="HTML",
        reply_markup=_task_keyboard()
    )
    return CaptionState.CHOOSING_TASK

async def caption_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "captask_cancel":
        await query.edit_message_text("✅ تم إلغاء الجلسة.")
        context.user_data.clear()
        return ConversationHandler.END

    task_key = query.data.replace("captask_", "")
    if task_key not in CAPTION_TASKS:
        return CaptionState.CHOOSING_TASK

    emoji, label, _ = CAPTION_TASKS[task_key]
    context.user_data['caption_task'] = task_key

    await query.edit_message_text(
        f"{emoji} <b>{label}</b>\n\n🎭 <b>اختر نبرة الصوت المناسبة للمحتوى:</b>",
        parse_mode="HTML",
        reply_markup=_tone_keyboard()
    )
    return CaptionState.CHOOSING_TONE

async def caption_tone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "captone_back":
        await query.edit_message_text(
            "🎯 <b>اختر المهمة المطلوبة:</b>",
            parse_mode="HTML",
            reply_markup=_task_keyboard()
        )
        return CaptionState.CHOOSING_TASK

    tone_key = query.data.replace("captone_", "")
    if tone_key not in CAPTION_TONES:
        tone_key = "formal"

    context.user_data['caption_tone'] = tone_key
    tone_emoji, tone_label, _ = CAPTION_TONES[tone_key]
    task_key = context.user_data.get('caption_task', 'reels')
    task_emoji, task_label, _ = CAPTION_TASKS.get(task_key, ("🎨", "مهمة", ""))

    await query.edit_message_text(
        f"{task_emoji} <b>{task_label}</b> | {tone_emoji} <b>{tone_label}</b>\n\n"
        "🧠 <b>اختر نموذج الذكاء الاصطناعي:</b>",
        parse_mode="HTML",
        reply_markup=_model_keyboard()
    )
    return CaptionState.CHOOSING_MODEL

async def caption_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    model_key = query.data.replace("capmodel_", "")
    model_labels = {"gemini": "Gemini ✨", "groq": "Groq ⚡"}
    label = model_labels.get(model_key, model_key)

    context.user_data['caption_model'] = model_key
    task_key = context.user_data.get('caption_task', 'reels')
    tone_key = context.user_data.get('caption_tone', 'formal')
    user_text = context.user_data.get('caption_prompt', '')

    task_emoji, task_label, task_instruction = CAPTION_TASKS.get(task_key, ("🎬", "محتوى", ""))
    tone_emoji, tone_label, tone_instruction = CAPTION_TONES.get(tone_key, ("🏛️", "رسمي", ""))

    full_prompt = (
        f"{task_instruction}\n"
        f"{tone_instruction}\n\n"
        f"الموضوع أو المسودة:\n{user_text}\n\n"
        "ملاحظة إلزامية: أخرج النتيجة النهائية المكتوبة فقط بشكل مباشر بدون أي مقدمات أو تحيات أو هوامش خارج النص."
    )

    await query.edit_message_text(f"⏳ جاري الصياغة بواسطة <b>{label}</b>...", parse_mode="HTML")

    # Typing indicator
    try:
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    try:
        ai_text = await get_ai_response_by_model(full_prompt, model_key)
        context.user_data['last_caption'] = ai_text

        # Clean one-tap copy block
        escaped_ai_text = escape_html(ai_text)
        reply = (
            f"{task_emoji} <b>{task_label}</b> | {tone_emoji} <b>{tone_label}</b> ({label})\n"
            f"{'─' * 25}\n\n"
            f"<code>{escaped_ai_text}</code>\n\n"
            f"📋 <i>اضغط على النص بالأعلى لنسخه فوراً بنقرة واحدة.</i>"
        )
        await query.edit_message_text(reply, parse_mode="HTML", reply_markup=_action_keyboard())
        return CaptionState.EDITING
    except Exception as e:
        logger.error(f"Caption generation error ({model_key}): {e}")
        await query.edit_message_text(
            f"❌ حدث خطأ:\n<code>{str(e)}</code>\n\nاختر نموذجاً آخر:",
            parse_mode="HTML",
            reply_markup=_model_keyboard()
        )
        return CaptionState.CHOOSING_MODEL

async def caption_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "caption_done":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("✅ تم اعتماد المحتوى وإنهاء الجلسة.")
        context.user_data.clear()
        return ConversationHandler.END
    elif query.data == "caption_restart":
        context.user_data.pop('last_caption', None)
        context.user_data.pop('caption_task', None)
        context.user_data.pop('caption_tone', None)
        await query.edit_message_text(
            "🎯 <b>اختر المهمة التالية:</b>",
            parse_mode="HTML",
            reply_markup=_task_keyboard()
        )
        return CaptionState.CHOOSING_TASK
    elif query.data == "caption_edit":
        await query.message.reply_text(
            "✏️ أرسل طلب التعديل (مثال: اجعله أقصر، أضف حماس أكثر، غير الأسلوب...):",
            reply_markup=ForceReply(selective=True)
        )
        context.user_data['awaiting_caption_edit'] = True
        return CaptionState.EDITING

async def caption_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_caption_edit'):
        return CaptionState.EDITING

    user_edit = update.message.text
    context.user_data['awaiting_caption_edit'] = False

    original_text  = context.user_data.get('caption_prompt', '')
    old_caption    = context.user_data.get('last_caption', '')
    model_key      = context.user_data.get('caption_model', 'gemini')
    task_key       = context.user_data.get('caption_task', 'reels')
    tone_key       = context.user_data.get('caption_tone', 'formal')
    task_emoji, task_label, _ = CAPTION_TASKS.get(task_key, ("🎨", "محتوى", ""))
    tone_emoji, tone_label, tone_instruction = CAPTION_TONES.get(tone_key, ("🎭", "نبرة", ""))
    model_labels   = {"gemini": "Gemini ✨", "groq": "Groq ⚡"}
    label = model_labels.get(model_key, model_key)

    edit_prompt = (
        f"النص الأصلي: {original_text}\n"
        f"الكابشن الحالي:\n{old_caption}\n\n"
        f"{tone_instruction}\n"
        f"المطلوب تعديله: {user_edit}\n"
        f"أعد كتابة الكابشن بناءً على التعديل المطلوب فقط بدون مقدمات:"
    )

    # Typing indicator
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    status_msg = await update.message.reply_text(f"⏳ جاري تطبيق التعديل عبر <b>{label}</b>...", parse_mode="HTML")
    try:
        new_caption = await get_ai_response_by_model(edit_prompt, model_key)
        context.user_data['last_caption'] = new_caption

        escaped_new = escape_html(new_caption)
        reply = (
            f"{task_emoji} <b>{task_label} (معدّل)</b> | {tone_emoji} <b>{tone_label}</b> ({label})\n"
            f"{'─' * 25}\n\n"
            f"<code>{escaped_new}</code>\n\n"
            f"📋 <i>اضغط على النص بالأعلى لنسخه فوراً.</i>"
        )
        await status_msg.edit_text(reply, parse_mode="HTML", reply_markup=_action_keyboard())
    except Exception as e:
        logger.error(f"Caption edit error ({model_key}): {e}")
        await status_msg.edit_text(f"❌ حدث خطأ أثناء التعديل:\n<code>{escape_html(str(e))}</code>", parse_mode="HTML")
    return CaptionState.EDITING

async def caption_timeout(update: object, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(
            chat_id=context._chat_id,
            text="⏰ انتهت مدة جلسة الكابشن (30 دقيقة) وتم إغلاقها تلقائياً."
        )
    except Exception:
        pass
    context.user_data.clear()
    return ConversationHandler.END
