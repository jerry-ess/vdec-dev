"""Router: the questions that never need the AI. Rules first, because safety must not depend on a model's mood.

    route(text, pctx, state, best_kpi_score) -> Route(kind, reason, message)
    kind: "pass" (go on to the planner) | "refuse" | "clarify"

* refuse   prompt injection, secrets, raw SQL, other businesses' data, contact-detail dumps, actions (send/delete),
           and plainly off-topic requests (weather, jokes, shares).
* clarify  a question that names no metric and no intent and has no earlier turn to lean on ("Show me the numbers",
           "And the month before?" as the first message).
* role     is checked later, on the plan (a KPI or tool above the user's role), so the router never guesses at it.
Languages: English, Vietnamese (accents optional), German. Template-level; nothing names a tenant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .context import PlanningContext
from .messages import msg
from .kpi_match import MIN_KPI_SCORE  # noqa: F401
from .periods import fold


ROUTER_MIN = 0.5      # any real overlap with a metric word; choosing *which* KPI is the planner's job


@dataclass
class Route:
    kind: str                    # pass | refuse | clarify | adhoc
    reason: str = ""
    message: str = ""


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts))


INJECTION = _rx(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|your\s+|the\s+|previous\s+|prior\s+|above\s+)*(instruction|rule|prompt|guideline|polic)",
    r"system\s*prompt|developer\s*(message|prompt)|reveal your (prompt|instructions)|jailbreak|\bdan mode\b",
    r"\bapi[\s_-]*(key|schlussel|token)\b|\bsecret key\b|\bpassword|\bpasswort|\bmat khau\b|\bcredentials?\b|\baccess token\b",
    r"bo qua (cac |moi |tat ca )?(huong dan|chi dan|quy tac|lenh)",
    r"ignorier\w*\s+(alle\s+|deine\s+|die\s+|vorherigen?\s+)*(anweisung|regel|instruktion|vorgabe)",
    r"(zeig|gib|nenn)\w*\s+(mir\s+)?(den\s+)?(system\s*prompt|systemprompt)",
)
SQL = _rx(
    r"\b(drop|truncate|alter|create)\s+(table|database|schema|index|view)\b",
    r"\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b",
    r"\bselect\b[^.?!]{0,80}\bfrom\b",
    r"\b(run|execute|exec)\s+(this\s+|the\s+|my\s+)?(sql|query|statement|script|command)\b",
    r"\b(chay|thuc thi)\s+(cau\s+)?(lenh\s+)?sql\b",
    r"\bsql\b[^.?!]{0,30}\b(ausfuhren|ausfuehren)\b|\bfuhr\w*\s+(diese[ns]?\s+)?sql\b",
)
CROSS_TENANT = _rx(
    r"(other|another|different)\s+(tenants?|businesses|companies|customers'? (data|accounts?))",
    r"(on|in|across)\s+(the\s+)?(whole\s+)?(system|platform)\b.*\b(other|another|different)\b",
    r"\b(other|another)\s+(store|shop|business|company)s?\s+(on|in)\s+the\s+(system|platform)",
    r"(cua hang|doanh nghiep|cong ty|khach hang)\s+khac\s+tren\s+(he thong|nen tang)",
    r"\b(tenant|khach hang cua vdec)\s+khac\b",
    r"(andere[nrs]?|fremde[nrs]?)\s+(mandanten?|unternehmen|firmen|kunden des systems|geschafte)\b.*\b(system|plattform)",
    r"\b(grocery[\s-]*store[\s-]*\d|coffee[\s-]*shop[\s-]*\w+|phung[\s-]*kitchen|vn[\s-]*grocery[\s-]*chain)\b",
)
ACTION = _rx(
    r"\b(send|sends|sending|mail|forward|text|sms|broadcast|blast)\s+(an?\s+|the\s+|out\s+)?(e-?mails?|mails?|sms|messages?|newsletters?|offers?)\b",
    r"\b(e-?mail|message|sms|text)\s+(all|every|each)\b|\b(offer|offering)\s+\d+\s*%\s*off\b",
    r"\b(delete|remove|update|change|edit|create|cancel|refund|approve|reprice)\s+(the\s+|all\s+|every\s+|my\s+)?(orders?|customers?|products?|prices?|records?|data|accounts?|users?)\b",
    r"\bgui\s+(e-?mail|mail|sms|tin nhan|thong bao|khuyen mai)\b|\bgui\b.{0,30}\b(tat ca|toan bo)\s+khach",
    r"\b(xoa|sua|cap nhat|doi gia|hoan tien)\s+(don hang|khach hang|san pham|gia|du lieu)\b",
    r"(e-?mail|mail|sms|nachricht)\w*\s+(an\s+)?(alle|jeden)\b|\b(sende|schicke|versende)\w*\b.{0,40}\b(e-?mail|mail|sms|nachricht|angebot|rabatt)",
    r"\b(losche|loesche|andere|aktualisiere)\s+(die\s+|alle\s+)?(bestellung|kunden|produkte|preise|daten)",
)
PII_DUMP = _rx(
    r"(phone|telephone|mobile|e-?mail|address|home address|birthday|date of birth|id card|passport|bank account)\s*(numbers?|addresses?|details|s)?\b[^.?!]{0,60}\b(customers?|clients?|buyers?|staff|employees?|members?)",
    r"\b(customers?|clients?|buyers?|staff|employees?|members?)\b[^.?!]{0,60}\b(phone|telephone|mobile|e-?mail|address|birthday|date of birth|id card|passport|bank account)",
    r"\bfull\s+names?\b|\breal\s+names?\b|\bcontact\s+(details|info|information)\b",
    r"(so dien thoai|dien thoai|email|dia chi|ngay sinh|cmnd|cccd|tai khoan ngan hang)[^.?!]{0,60}(khach|nhan vien|thanh vien)",
    r"(khach|nhan vien|thanh vien)[^.?!]{0,60}(so dien thoai|dien thoai|email|dia chi|ngay sinh|cmnd|cccd)",
    r"\bho (va )?ten day du\b|\bten that\b|\bthong tin lien he\b",
    r"(telefonnummer|handynummer|telefon|e-?mail-?adresse|adresse|geburtstag|ausweis)\w*[^.?!]{0,60}(kunden|mitarbeiter|mitglieder)",
    r"(kunden|mitarbeiter|mitglieder)[^.?!]{0,60}(telefonnummer|handynummer|telefon|e-?mail-?adresse|adresse|geburtstag)",
    r"\b(vollstandige[nr]?|vollen|echten?)\s+namen\b|\bkontaktdaten\b",
)
OFF_TOPIC = _rx(
    r"\b(weather|forecast for (tomorrow|today)|horoscope|recipe|lyrics|poem|song|joke|riddle|movie|football|soccer|bitcoin|crypto|lottery)\b",
    r"\b(stocks?|shares?|etf|forex)\b.{0,30}\b(buy|invest|should)\b|\bwhich\s+(stocks?|shares?)\b|\bshould i (buy|invest)\b",
    r"\bhow are you\b|\btell me (a|about) (story|joke)\b|\bwrite (me )?(a |an )?(poem|song|essay|story|code|script|email)\b|\btranslate\b",
    r"\bthoi tiet\b|\bchuyen cuoi\b|\bcau do\b|\btruyen cuoi\b|\bbong da\b|\bco phieu\b|\bdau tu (vao )?(co phieu|bitcoin|vang)\b|\bban khoe khong\b|\bket qua xo so\b|\bke (cho )?(toi )?(nghe )?(mot )?(cau )?chuyen\b",
    r"\bwetter\b|\bwitz\b|\bgedicht\b|\bhoroskop\b|\baktien?\b|\bfussball\b|\bwie geht(\W|s)|\brezept\b|\berzahl\w* mir (eine? )?(geschichte|witz)",
)

# questions no standard tool can answer (statistics, what-if, row lists): said so honestly, without an AI call.
# Deliberately short and generic; the AI planner is the main detector of "needs a custom query".
ADHOC = _rx(
    r"\bmedian\b|\bpercentile\b|\bstandard deviation\b|\bcorrelat|\bregression\b",
    r"\btrung vi\b|\bdo lech chuan\b|\btuong quan\b|\bphan vi\b",
    r"\bkorrelation\b|\bstandardabweichung\b|\bperzentil\b",
    r"\b(list|show|give me)\s+(me\s+)?(every|all|each)\s+(single\s+)?(orders?|transactions?|customers?|invoices?|lines?)\b",
    r"\bwhat would\b.{0,60}\b(have been|be)\b.{0,40}\bif\b|\bwhat if\b",
    r"\b(liste|zeige|gib)\w*\s+(mir\s+)?(alle|jede)\w*\s+(bestellungen|kunden|rechnungen|transaktionen)\b|\bwie hoch ware\b.{0,80}\bwenn\b|\bwas ware wenn\b",
    r"\b(cho toi |liet ke |danh sach )(tat ca |moi )?(danh sach )?(don hang|khach hang|giao dich)\b|\bneu (loai bo|giu nguyen|khong)\b|\bgia su\b",
)

# a question that is a real business question even without a metric word
INTENT = _rx(
    r"\bwhy\b|\bhow did we do\b|\bhow are we doing\b|\bworry\b|\battention\b|\bwarning|\balert|\bshould (i|we)\b|\bwhat (should|can) (i|we)\b|\bnext (week|month|days?)\b|\bforecast|\bexpect",
    r"\bsegments?\b|\btop\b|\bbest\b|\bworst\b|\bbiggest\b|\blargest\b|\bmost\b|\bleast\b|\bgood\b|\bbad\b|\bwent (well|badly)\b|\bsummary\b|\boverview\b|\bperformance\b",
    r"\bvi sao\b|\btai sao\b|\bkinh doanh\b|\blo ngai\b|\bcanh bao\b|\bnen lam\b|\bnen (xu ly|chu y)\b|\bdu bao\b|\btuan toi\b|\bthang toi\b|\bphan khuc\b|\bnhieu nhat\b|\bit nhat\b|\btom tat\b|\btong quan\b|\btinh hinh\b",
    r"\bwarum\b|\bwieso\b|\bweshalb\b|\bwie (lief|laufen|stehen)\b|\bsorge\w*|\bwarnung|\bwas (soll|sollte|kann) (ich|man)\b|\bprognose\b|\bnachste (woche|monat)\b|\bsegment|\bbeste[nrs]?\b|\bschlechteste|\bgrosste|\bam meisten\b|\bubersicht\b|\bzusammenfassung\b|\bhandeln\b",
)
# words that show the question leans on an earlier turn
FOLLOW = _rx(
    r"^(and|also|then|what about|how about|ok(ay)?[, ]+(and|what about))\b",
    r"^(the other|that one|those|them|it)\b|\bthe other one\b|\bsame (for|but)\b|\bwhat about\b|\band (the )?(month|week|quarter|year|day)s? before\b",
    r"^(con|vay|the con|con nua)\b|\bcai kia\b|\bcai do\b|\bnhu vay\b|\bso sanh voi (nam ngoai|ky truoc|thang truoc)\b",
    r"^(und|oder|dazu|was ist mit|wie sieht es (mit|bei))\b|\bdas andere\b|\bdasselbe\b|\bund (der|die|das) (monat|woche|quartal|jahr)\b",
)


def route(text: str, pctx: PlanningContext, has_state: bool = False, best_kpi_score: float = 0.0,
          has_period: bool = False) -> Route:
    q = fold(text).strip()
    lang = pctx.lang
    if not q:
        return Route("clarify", "empty", msg("clarify", lang))
    if INJECTION.search(q):
        return Route("refuse", "injection", msg("refuse_scope", lang))
    if SQL.search(q):
        return Route("refuse", "sql", msg("refuse_scope", lang))
    if CROSS_TENANT.search(q):
        return Route("refuse", "cross_tenant", msg("refuse_data", lang))
    if ACTION.search(q):
        return Route("refuse", "action", msg("refuse_action", lang))
    if PII_DUMP.search(q):
        return Route("refuse", "pii_dump", msg("refuse_data", lang))
    if OFF_TOPIC.search(q):
        return Route("refuse", "off_topic", msg("refuse_scope", lang))
    if ADHOC.search(q):
        return Route("adhoc", "needs_custom_query", msg("adhoc", lang))
    leaning = bool(FOLLOW.search(q))
    if (leaning or best_kpi_score < ROUTER_MIN) and not has_state and not INTENT.search(q):
        # nothing to lean on and no metric named: ask, do not guess. A question with a metric word passes even if short.
        if leaning or best_kpi_score < ROUTER_MIN:
            reason = "needs_context" if leaning else "no_metric"
            return Route("clarify", reason, msg("clarify_metric" if has_period else "clarify", lang))
    return Route("pass")
