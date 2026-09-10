from enum import IntEnum, auto

class State(IntEnum):
    MENU = auto()
    CHOOSING_DEPT = auto()
    EVENT_NAME = auto()
    EVENT_TYPE = auto()
    OBJECTIVE = auto()
    DATE = auto()
    TIME = auto()
    END_TIME = auto()
    LOCATION = auto()
    COVERAGE_TYPE = auto()
    IMPORTANCE = auto()
    EXTERNAL_MEDIA = auto()
    NOTES = auto()
    CONFIRMATION = auto()
    BORROW_ITEM = auto()
    BORROW_QUANTITY = auto()
    BORROW_BORROWER = auto()
    BORROW_REASON = auto()
    BORROW_PHONE = auto()
    BORROW_RETURN_DATE = auto()
    BORROW_RETURN_TIME = auto()
    BORROW_CONFIRMATION = auto()
    BORROW_RESPONSIBILITY = auto()

class CaptionState(IntEnum):
    TYPING_PROMPT  = auto()   # المستخدم يكتب النص / البرومبت
    CHOOSING_TASK  = auto()   # يختار نوع المهمة (كابشن، تدقيق، هاشتاغ...)
    CHOOSING_MODEL = auto()   # يختار نموذج الذكاء
    EDITING        = auto()   # يطلب تعديل

DEPARTMENTS = [
    "فعاليات", 
    "تدريب", 
    "تنظيم", 
    "أكاديمي", 
    "متابعة", 
    "إعلام", 
    "مدينة جامعية"
]

EVENT_TYPES = [
    "محاضرة",
    "ندوة",
    "ورشة عمل",
    "تدريب",
    "اجتماع",
    "فعالية",
    "حفل",
    "حملة",

]

COVERAGE_OPTIONS = [
    "صور منوعة",
    "ڤيديوهات منوعة",
    "توثيق فيديو كامل",
    "مقابلات وأسئلة",
]

IMPORTANCE_LEVELS = [
    "مهم جداً - غير قابل للتأجيل", 
    "أهمية متوسطة - له أولوية بحال التعارض", 
    "أهمية ضعيفة - يمكن تجاهله"
]

BORROW_ITEMS = [
    "📷 كاميرا سوني a7III",
    "🎥 اوزمو بوكيت 3",
    "🎤 مايك dji",
    "💡 اضاءة محمولة",
    "🔦 اضاءة ثابتة",
    "🗼 ترايبود",
]

BORROW_ITEM_STOCK = {
    "📷 كاميرا سوني a7III": 1,
    "🎥 اوزمو بوكيت 3": 1,
    "🎤 مايك dji": 2,
    "💡 اضاءة محمولة": 1,
    "🔦 اضاءة ثابتة": 1,
    "🗼 ترايبود": 2,
}

