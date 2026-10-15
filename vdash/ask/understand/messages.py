"""Fixed wording the understanding layer can answer with by itself (no AI call): English, Vietnamese, German."""
from __future__ import annotations

M = {
    "refuse_scope": {
        "en": "I can't help with that, but I can answer questions about your sales, customers and products.",
        "vi": "Tôi không thể hỗ trợ việc đó, nhưng tôi có thể trả lời các câu hỏi về doanh thu, khách hàng và sản phẩm của bạn.",
        "de": "Dabei kann ich nicht helfen, aber ich kann Fragen zu Ihrem Umsatz, Ihren Kunden und Produkten beantworten."},
    "refuse_role": {
        "en": "Your role does not allow that question. Customer and staff details are available to managers and administrators.",
        "vi": "Vai trò của bạn không được phép xem thông tin này. Chi tiết khách hàng và nhân viên chỉ dành cho quản lý và quản trị viên.",
        "de": "Ihre Rolle erlaubt diese Frage nicht. Kunden- und Mitarbeiterdetails sind Managern und Administratoren vorbehalten."},
    "refuse_data": {
        "en": "I can't share that. I can answer with totals and rankings, not with contact details or other businesses' data.",
        "vi": "Tôi không thể cung cấp thông tin đó. Tôi có thể trả lời bằng số tổng hợp và bảng xếp hạng, không cung cấp thông tin liên hệ hay dữ liệu của doanh nghiệp khác.",
        "de": "Das kann ich nicht weitergeben. Ich kann mit Summen und Rangfolgen antworten, nicht mit Kontaktdaten oder Daten anderer Unternehmen."},
    "refuse_action": {
        "en": "I can't send messages or change anything. I can tell you which customers or products to act on.",
        "vi": "Tôi không thể gửi tin nhắn hay thay đổi dữ liệu. Tôi có thể cho bạn biết nên liên hệ khách hàng hay sản phẩm nào.",
        "de": "Ich kann keine Nachrichten senden oder etwas ändern. Ich kann Ihnen sagen, bei welchen Kunden oder Produkten Handlungsbedarf besteht."},
    "clarify": {
        "en": "Which metric and which period do you mean?",
        "vi": "Bạn muốn xem chỉ số nào và trong khoảng thời gian nào?",
        "de": "Welche Kennzahl und welchen Zeitraum meinen Sie?"},
    "clarify_metric": {
        "en": "Which metric do you mean?",
        "vi": "Bạn muốn xem chỉ số nào?",
        "de": "Welche Kennzahl meinen Sie?"},
    "adhoc": {
        "en": "That needs a custom query that the standard tools do not cover yet. I can show the closest standard figures instead.",
        "vi": "Câu hỏi này cần một truy vấn tùy chỉnh mà các công cụ chuẩn chưa hỗ trợ. Tôi có thể cho bạn xem các số liệu chuẩn gần nhất.",
        "de": "Das erfordert eine individuelle Abfrage, die die Standardwerkzeuge noch nicht abdecken. Ich kann stattdessen die nächstliegenden Standardzahlen zeigen."},
    "unavailable_ai": {
        "en": "I couldn't work out that question right now. Please rephrase it or pick one of the suggested questions.",
        "vi": "Hiện tôi chưa hiểu được câu hỏi này. Bạn hãy diễn đạt lại hoặc chọn một câu hỏi gợi ý.",
        "de": "Ich konnte diese Frage gerade nicht einordnen. Bitte formulieren Sie sie um oder wählen Sie einen Vorschlag."},
}


def msg(key: str, lang: str) -> str:
    d = M[key]
    return d.get(lang) or d["en"]
