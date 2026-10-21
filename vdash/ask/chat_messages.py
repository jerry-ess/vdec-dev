"""Fixed wording of the chat (limits, quota, errors) in English, Vietnamese and German - no AI, no numbers invented."""
from __future__ import annotations

M = {
    "rate_minute": {
        "en": "You are asking quite fast - please wait a moment and try again.",
        "vi": "Bạn đang hỏi khá nhanh - vui lòng đợi một chút rồi thử lại.",
        "de": "Sie fragen sehr schnell - bitte warten Sie einen Moment und versuchen Sie es erneut."},
    "rate_day": {
        "en": "Your business has reached today's limit of questions. It resets tomorrow.",
        "vi": "Doanh nghiệp của bạn đã đạt giới hạn số câu hỏi trong ngày. Giới hạn sẽ được đặt lại vào ngày mai.",
        "de": "Ihr Unternehmen hat das heutige Fragenlimit erreicht. Es wird morgen zurückgesetzt."},
    "busy": {
        "en": "Your previous question is still being answered - please wait for it.",
        "vi": "Câu hỏi trước của bạn vẫn đang được trả lời - vui lòng đợi.",
        "de": "Ihre vorherige Frage wird noch beantwortet - bitte warten Sie."},
    "too_long": {
        "en": "Please keep the question under {n} characters.",
        "vi": "Vui lòng giữ câu hỏi dưới {n} ký tự.",
        "de": "Bitte halten Sie die Frage unter {n} Zeichen."},
    "empty": {
        "en": "Please type a question.",
        "vi": "Vui lòng nhập câu hỏi.",
        "de": "Bitte geben Sie eine Frage ein."},
    "long_chat": {
        "en": "This conversation is long - please start a new one.",
        "vi": "Cuộc trò chuyện này đã dài - vui lòng bắt đầu cuộc trò chuyện mới.",
        "de": "Diese Unterhaltung ist lang - bitte beginnen Sie eine neue."},
    "quota": {
        "en": "Your plan's AI questions for this month are used up, so this answer was written without AI and may understand less. It resets on {date}.",
        "vi": "Số câu hỏi AI của gói trong tháng này đã hết, nên câu trả lời này được viết không dùng AI và có thể hiểu kém hơn. Sẽ đặt lại vào {date}.",
        "de": "Die KI-Fragen Ihres Tarifs für diesen Monat sind aufgebraucht, daher wurde diese Antwort ohne KI verfasst und versteht eventuell weniger. Zurücksetzung am {date}."},
    "no_credits": {
        "en": "Your AI credits are used up, so this answer was written without AI and may understand less. Top up credits to use AI again.",
        "vi": "Tín dụng AI đã hết, nên câu trả lời này được viết không dùng AI và có thể hiểu kém hơn. Hãy nạp thêm tín dụng để dùng lại AI.",
        "de": "Ihre KI-Guthaben sind aufgebraucht, daher wurde diese Antwort ohne KI verfasst und versteht eventuell weniger. Laden Sie Guthaben auf, um KI wieder zu nutzen."},
    "ai_off": {
        "en": "AI is not switched on for this business, so this answer was written by rules and may understand less.",
        "vi": "AI chưa được bật cho doanh nghiệp này, nên câu trả lời được viết theo quy tắc và có thể hiểu kém hơn.",
        "de": "KI ist für dieses Unternehmen nicht aktiviert, daher wurde die Antwort regelbasiert verfasst und versteht eventuell weniger."},
    "error": {
        "en": "Something went wrong while answering. Please try again.",
        "vi": "Đã xảy ra lỗi khi trả lời. Vui lòng thử lại.",
        "de": "Beim Beantworten ist etwas schiefgelaufen. Bitte versuchen Sie es erneut."},
    "not_found": {
        "en": "Conversation not found.",
        "vi": "Không tìm thấy cuộc trò chuyện.",
        "de": "Unterhaltung nicht gefunden."},
    "disabled": {
        "en": "Talk to Data is not switched on for this business.",
        "vi": "Tính năng Hỏi dữ liệu chưa được bật cho doanh nghiệp này.",
        "de": "Mit Daten sprechen ist für dieses Unternehmen nicht aktiviert."},
}


def cmsg(key: str, lang: str, **kw) -> str:
    d = M[key]
    out = d.get(lang) or d["en"]
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out
