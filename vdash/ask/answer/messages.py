"""Fixed wording of the answer writer (no AI): empty and refused results, and next-question suggestions per tool.
English, Vietnamese, German. Numbers are never typed here: the tools format them."""
from __future__ import annotations

M = {
    "no_data": {
        "en": "There is no data for that period.",
        "vi": "Không có dữ liệu cho khoảng thời gian này.",
        "de": "Für diesen Zeitraum liegen keine Daten vor."},
    "empty_now": {          # a "right now" list (snapshot) that is empty: a good or neutral outcome, not missing data
        "en": "Nothing matches that right now.",
        "vi": "Hiện chưa có mục nào phù hợp.",
        "de": "Dazu gibt es derzeit keine Treffer."},
    "empty_segments": {
        "en": "No customer is in that group right now (for example, no customer is above the high-risk line).",
        "vi": "Hiện chưa có khách hàng nào thuộc nhóm này (ví dụ: chưa có khách hàng nào vượt ngưỡng rủi ro cao).",
        "de": "Derzeit gehört kein Kunde zu dieser Gruppe (zum Beispiel liegt kein Kunde über der Hochrisiko-Grenze)."},
    "forbidden": {
        "en": "Your role does not allow that question. Customer and staff details are available to managers and administrators.",
        "vi": "Vai trò của bạn không được phép xem thông tin này. Chi tiết khách hàng và nhân viên chỉ dành cho quản lý và quản trị viên.",
        "de": "Ihre Rolle erlaubt diese Frage nicht. Kunden- und Mitarbeiterdetails sind Managern und Administratoren vorbehalten."},
    "not_available": {
        "en": "This business has no data for that yet{missing}.",
        "vi": "Doanh nghiệp của bạn chưa có dữ liệu cho phần này{missing}.",
        "de": "Für diesen Bereich liegen noch keine Daten vor{missing}."},
    "no_forecast": {
        "en": "There is no forecast for this business yet.",
        "vi": "Chưa có dự báo nào cho doanh nghiệp của bạn.",
        "de": "Für dieses Unternehmen gibt es noch keine Prognose."},
    "failed": {
        "en": "I couldn't read the data right now. Please try again in a moment.",
        "vi": "Hiện tôi chưa đọc được dữ liệu. Vui lòng thử lại sau ít phút.",
        "de": "Die Daten konnten gerade nicht gelesen werden. Bitte versuchen Sie es gleich noch einmal."},
    "part_failed": {
        "en": "One part of your question could not be answered right now.",
        "vi": "Một phần của câu hỏi hiện chưa thể trả lời.",
        "de": "Ein Teil Ihrer Frage konnte gerade nicht beantwortet werden."},
    "nothing": {
        "en": "I found nothing to report for that question.",
        "vi": "Tôi không tìm thấy gì để báo cáo cho câu hỏi này.",
        "de": "Zu dieser Frage habe ich nichts zu berichten."},
    "missing_in": {
        "en": " (missing: {areas})", "vi": " (còn thiếu: {areas})", "de": " (es fehlt: {areas})"},
}

# next questions the owner can ask, per tool that produced the answer (each is understood by the planner in that language)
FOLLOW = {
    "kpi_value": {
        "en": ["Why did it change?", "Show the trend over time"],
        "vi": ["Vì sao nó thay đổi?", "Cho tôi xem xu hướng theo thời gian"],
        "de": ["Warum hat es sich verändert?", "Zeig mir den Verlauf über die Zeit"]},
    "compare_periods": {
        "en": ["Why did it change?", "Which products moved the most?"],
        "vi": ["Vì sao nó thay đổi?", "Sản phẩm nào thay đổi nhiều nhất?"],
        "de": ["Warum hat es sich verändert?", "Welche Produkte haben sich am stärksten verändert?"]},
    "kpi_breakdown": {
        "en": ["How did this change compared with the previous period?", "Show the trend over time"],
        "vi": ["Điều này thay đổi thế nào so với kỳ trước?", "Cho tôi xem xu hướng theo thời gian"],
        "de": ["Wie hat sich das gegenüber dem Vorzeitraum verändert?", "Zeig mir den Verlauf über die Zeit"]},
    "kpi_trend": {
        "en": ["Why did revenue change?", "What were the best and worst days?"],
        "vi": ["Vì sao doanh thu thay đổi?", "Những ngày tốt nhất và tệ nhất là ngày nào?"],
        "de": ["Warum hat sich der Umsatz verändert?", "Was waren die besten und schlechtesten Tage?"]},
    "explain_change": {
        "en": ["Which products moved the most?", "What should I do about it?"],
        "vi": ["Sản phẩm nào thay đổi nhiều nhất?", "Tôi nên làm gì về việc này?"],
        "de": ["Welche Produkte haben sich am stärksten verändert?", "Was sollte ich dagegen tun?"]},
    "top_movers": {
        "en": ["Why did revenue change?", "What should I do about it?"],
        "vi": ["Vì sao doanh thu thay đổi?", "Tôi nên làm gì về việc này?"],
        "de": ["Warum hat sich der Umsatz verändert?", "Was sollte ich dagegen tun?"]},
    "warnings": {
        "en": ["What should I do this week?", "How did we do this period?"],
        "vi": ["Tuần này tôi nên làm gì?", "Kỳ này kinh doanh thế nào?"],
        "de": ["Was sollte ich diese Woche tun?", "Wie haben wir in diesem Zeitraum abgeschnitten?"]},
    "findings": {
        "en": ["What should I do this week?", "What should I worry about?"],
        "vi": ["Tuần này tôi nên làm gì?", "Tôi nên lo lắng về điều gì?"],
        "de": ["Was sollte ich diese Woche tun?", "Worüber sollte ich mir Sorgen machen?"]},
    "recommended_actions": {
        "en": ["What should I worry about?", "Why did revenue change?"],
        "vi": ["Tôi nên lo lắng về điều gì?", "Vì sao doanh thu thay đổi?"],
        "de": ["Worüber sollte ich mir Sorgen machen?", "Warum hat sich der Umsatz verändert?"]},
    "forecast": {
        "en": ["How did revenue do over the last 30 days?", "What should I do this week?"],
        "vi": ["Doanh thu 30 ngày qua thế nào?", "Tuần này tôi nên làm gì?"],
        "de": ["Wie hat sich der Umsatz in den letzten 30 Tagen entwickelt?", "Was sollte ich diese Woche tun?"]},
    "drill": {
        "en": ["Show the trend over time"], "vi": ["Cho tôi xem xu hướng theo thời gian"],
        "de": ["Zeig mir den Verlauf über die Zeit"]},
    "customer_segments": {
        "en": ["How much revenue is at risk?", "Which customers spend the most?"],
        "vi": ["Bao nhiêu doanh thu đang có nguy cơ mất?", "Khách hàng nào chi tiêu nhiều nhất?"],
        "de": ["Wie viel Umsatz ist gefährdet?", "Welche Kunden geben am meisten aus?"]},
}


def msg(key: str, lang: str, **kw) -> str:
    d = M[key]
    s = d.get(lang) or d["en"]
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def follow_ups(tool: str, lang: str) -> list[str]:
    d = FOLLOW.get(tool) or {}
    return list(d.get(lang) or d.get("en") or [])
