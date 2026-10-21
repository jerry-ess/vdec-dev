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
    "no_rows": {
        "en": "No rows match that question for the period.",
        "vi": "Không có dòng dữ liệu nào phù hợp với câu hỏi này trong kỳ đã chọn.",
        "de": "Für diesen Zeitraum passen keine Zeilen zu dieser Frage."},
    "custom_failed": {
        "en": "I could not turn that into a calculation on your data. Try naming the figure and the period, for example \"median order value in August\".",
        "vi": "Tôi chưa chuyển được câu hỏi này thành phép tính trên dữ liệu của bạn. Hãy nêu rõ chỉ số và kỳ, ví dụ \"trung vị giá trị đơn hàng tháng 8\".",
        "de": "Ich konnte daraus keine Berechnung auf Ihren Daten machen. Nennen Sie bitte die Kennzahl und den Zeitraum, zum Beispiel \"Median des Bestellwerts im August\"."},
    "custom_too_big": {
        "en": "That calculation covers too many rows. Please narrow the period or add a filter.",
        "vi": "Phép tính này bao gồm quá nhiều dòng. Vui lòng thu hẹp kỳ hoặc thêm bộ lọc.",
        "de": "Diese Berechnung umfasst zu viele Zeilen. Bitte verkürzen Sie den Zeitraum oder fügen Sie einen Filter hinzu."},
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
    "custom_query": {
        "en": ["How did revenue do over the last 30 days?"], "vi": ["Doanh thu 30 ngày qua thế nào?"],
        "de": ["Wie hat sich der Umsatz in den letzten 30 Tagen entwickelt?"]},
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


# ---------------------------------------------------------------------------------------------------- structured answers
# Fixed wording of the answer's verdict, headings and notes (the figures always come from the tools).
BR = {
    "tone_good": {"en": "Good news", "vi": "Tin tốt", "de": "Gute Nachricht"},
    "tone_bad": {"en": "Needs attention", "vi": "Cần chú ý", "de": "Braucht Aufmerksamkeit"},
    "tone_mixed": {"en": "Mixed picture", "vi": "Trái chiều", "de": "Gemischtes Bild"},
    "tone_flat": {"en": "Stable", "vi": "Ổn định", "de": "Stabil"},
    "say_worse": {"en": "Weaker than before", "vi": "Yếu hơn trước đó", "de": "Schwächer als zuvor"},
    "say_better": {"en": "Better than before", "vi": "Tốt hơn trước đó", "de": "Besser als zuvor"},
    "say_same": {"en": "About the same as before", "vi": "Gần như không đổi so với trước đó",
                 "de": "Etwa so wie zuvor"},
    "cmp_up": {"en": "{kpi}: {a} in {pa}, up {chg} from {b} in {pb}.",
               "vi": "{kpi}: {a} trong {pa}, tăng {chg} so với {b} trong {pb}.",
               "de": "{kpi}: {a} im Zeitraum {pa}, ein Plus von {chg} gegenüber {b} im Zeitraum {pb}."},
    "cmp_down": {"en": "{kpi}: {a} in {pa}, down {chg} from {b} in {pb}.",
                 "vi": "{kpi}: {a} trong {pa}, giảm {chg} so với {b} trong {pb}.",
                 "de": "{kpi}: {a} im Zeitraum {pa}, ein Minus von {chg} gegenüber {b} im Zeitraum {pb}."},
    "cmp_flat": {"en": "{kpi}: {a} in {pa}, about the same as {b} in {pb} (change {chg}).",
                 "vi": "{kpi}: {a} trong {pa}, gần như không đổi so với {b} trong {pb} (thay đổi {chg}).",
                 "de": "{kpi}: {a} im Zeitraum {pa}, etwa gleich wie {b} im Zeitraum {pb} (Veränderung {chg})."},
    "say_mixed": {"en": "Mixed: some things improved, some got worse", "vi": "Trái chiều: có mặt tốt lên, có mặt xấu đi",
                  "de": "Gemischt: manches hat sich verbessert, manches verschlechtert"},
    "as_of": {"en": "as of {date}", "vi": "tính đến {date}", "de": "Stand {date}"},
    "h_worse": {"en": "Got worse", "vi": "Xấu đi", "de": "Verschlechtert"},
    "h_better": {"en": "Improved", "vi": "Tốt lên", "de": "Verbessert"},
    "h_fell": {"en": "Fell", "vi": "Giảm", "de": "Gesunken"},
    "h_rose": {"en": "Rose", "vi": "Tăng", "de": "Gestiegen"},
    "h_what_changed": {"en": "What moved revenue (effect in money; change of the factor in brackets)",
                       "vi": "Điều gì làm doanh thu thay đổi (tác động bằng tiền; mức thay đổi của yếu tố trong ngoặc)",
                       "de": "Was den Umsatz bewegt hat (Wirkung in Geld; Änderung des Faktors in Klammern)"},
    "p_orders": {"en": "Number of orders", "vi": "Số đơn hàng", "de": "Anzahl Bestellungen"},
    "p_basket": {"en": "Average order value", "vi": "Giá trị đơn trung bình", "de": "Durchschnittlicher Bestellwert"},
    "p_items": {"en": "Items per order", "vi": "Số món mỗi đơn", "de": "Artikel pro Bestellung"},
    "p_price": {"en": "Price per item (incl. discounts and product mix)", "vi": "Giá mỗi món (gồm giảm giá và cơ cấu sản phẩm)",
                "de": "Preis pro Artikel (inkl. Rabatten und Sortimentsmix)"},
    "h_priority": {"en": "Priorities", "vi": "Ưu tiên", "de": "Prioritäten"},
    "expl_baseline": {"en": "The change compares {now} with {before}.", "vi": "Thay đổi này so sánh {now} với {before}.",
                      "de": "Die Veränderung vergleicht {now} mit {before}."},
    "lbl_highest": {"en": "Highest", "vi": "Cao nhất", "de": "Höchster Wert"},
    "lbl_lowest": {"en": "Lowest", "vi": "Thấp nhất", "de": "Niedrigster Wert"},
    "lbl_average": {"en": "Average", "vi": "Trung bình", "de": "Durchschnitt"},
    "lbl_latest": {"en": "Latest", "vi": "Mới nhất", "de": "Zuletzt"},
    "lbl_half": {"en": "Second half vs first half", "vi": "Nửa sau so với nửa đầu", "de": "Zweite Hälfte vs. erste Hälfte"},
    "lbl_peak": {"en": "Highest vs average", "vi": "Cao nhất so với trung bình", "de": "Höchster Wert im Vergleich zum Durchschnitt"},
    "wn_none": {"en": "Nothing needs your attention right now.", "vi": "Hiện chưa có điều gì cần bạn chú ý.",
                "de": "Derzeit braucht nichts Ihre Aufmerksamkeit."},
    "wn_one": {"en": "One thing needs your attention.", "vi": "Có một điều cần bạn chú ý.",
               "de": "Ein Punkt braucht Ihre Aufmerksamkeit."},
    "wn_many": {"en": "{n} things need your attention.", "vi": "Có {n} điều cần bạn chú ý.",
                "de": "{n} Punkte brauchen Ihre Aufmerksamkeit."},
    "act_head": {"en": "Here are your priorities, most urgent first.", "vi": "Đây là các ưu tiên của bạn, việc gấp nhất ở trên cùng.",
                 "de": "Das sind Ihre Prioritäten, das Dringendste zuerst."},
    "act_none_for": {"en": "No suggested action is tied to this figure; these are the main priorities instead.",
                     "vi": "Không có đề xuất nào gắn trực tiếp với chỉ số này; dưới đây là các ưu tiên chính.",
                     "de": "Zu dieser Kennzahl gibt es keine eigene Empfehlung; hier stattdessen die wichtigsten Prioritäten."},
    "only_one": {"en": "“{title}” has only one entry, so there is nothing to compare it with.",
                 "vi": "“{title}” chỉ có một mục nên không có gì để so sánh.",
                 "de": "„{title}“ hat nur einen Eintrag, daher gibt es nichts zu vergleichen."},
    "future_value_note": {"en": "This is what today's customers are expected to spend over their whole future, not a forecast for next month.",
                          "vi": "Đây là mức chi tiêu dự kiến của khách hiện tại trong toàn bộ thời gian tới, không phải dự báo cho tháng sau.",
                          "de": "Das ist die erwartete künftige Gesamtausgabe der heutigen Kunden, keine Prognose für den nächsten Monat."},
    "no_change_note": {"en": "No earlier period to compare with is available for this figure.",
                       "vi": "Chưa có kỳ trước để so sánh cho chỉ số này.",
                       "de": "Für diese Kennzahl liegt kein Vorzeitraum zum Vergleich vor."},
    "movers_partial": {"en": "Only items that are in the top list in both periods are compared; items that entered or left the list are not shown.",
                       "vi": "Chỉ so sánh các mục có mặt trong danh sách đứng đầu ở cả hai kỳ; mục mới vào hoặc rời khỏi danh sách không được hiển thị.",
                       "de": "Verglichen werden nur Einträge, die in beiden Zeiträumen in der Top-Liste stehen; neu hinzugekommene oder weggefallene Einträge werden nicht gezeigt."},
    "movers_none_rose": {"en": "None of the compared items rose.", "vi": "Không có mục nào trong số được so sánh tăng.",
                         "de": "Keiner der verglichenen Einträge ist gestiegen."},
    "movers_none_fell": {"en": "None of the compared items fell.", "vi": "Không có mục nào trong số được so sánh giảm.",
                         "de": "Keiner der verglichenen Einträge ist gesunken."},
    "fc_top": {"en": "Expected to sell the most: {names}.", "vi": "Dự kiến bán nhiều nhất: {names}.", "de": "Voraussichtlich am meisten verkauft: {names}."},
    "cs_head": {"en": "{what}: {n} customers. First on the list: {names}.", "vi": "{what}: {n} khách hàng. Đứng đầu danh sách: {names}.",
                "de": "{what}: {n} Kunden. Zuerst in der Liste: {names}."},
    "lbl_change": {"en": "Change", "vi": "Thay đổi", "de": "Veränderung"},
    "more_rows": {"en": "The table shows the full list.", "vi": "Bảng hiển thị danh sách đầy đủ.", "de": "Die Tabelle zeigt die vollständige Liste."},
}


def br(key: str, lang: str, **kw) -> str:
    d = BR[key]
    s = d.get(lang) or d["en"]
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s
