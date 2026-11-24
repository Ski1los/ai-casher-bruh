"""🧠 Консилиум ИИ: один вопрос — нескольким разным ИИ сразу, итог — по согласию, а не по одному мнению.

Зачем: у каждой модели свои «тараканы» — одна любит рисковать, другая всё запрещает, третья выдумывает цифры.
Если Groq, Gemini, Mistral, DeepSeek и GPT по одним и тем же данным бота независимо говорят «ждать» — это гораздо
надёжнее, чем ответ одного. Каждый ИИ отвечает в строгом формате (ВЕРДИКТ / УВЕРЕННОСТЬ / ГЛАВНОЕ / ИДЕЯ / РИСК),
бот считает голоса с весом уверенности.

Правила честности:
* «торговать» итогом бывает, только если ответили минимум 2 ИИ, за это ≥ 2/3 голосов и никто уверенно (≥70%) не
  говорит «ждать». Иначе итог понижается до «осторожно»;
* ничья — в пользу более осторожного варианта;
* ответил один ИИ — так и пишем: это не консилиум, а мнение одного.

В торговле: перед тем как ♾ Автомат сам переведёт доказанную стратегию на настоящие деньги, он спрашивает консилиум
(если подключено ≥2 ИИ). Консилиум может только ЗАПРЕТИТЬ (сделать осторожнее), разрешить сверх правил — нет.
Все сделки по-прежнему в лимитах риска, вывод денег бот не умеет.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field

from .ai import Answer, guard_withdraw

TRADE, CAREFUL, WAIT = "trade", "careful", "wait"
CAUTION = {TRADE: 0, CAREFUL: 1, WAIT: 2}  # больше — осторожнее
LABEL = {TRADE: "🟢 ТОРГОВАТЬ", CAREFUL: "🟡 ОСТОРОЖНО", WAIT: "🔴 ЖДАТЬ"}
WORDS = (  # порядок важен: «не торговать» раньше «торговать»
    (WAIT, ("ЖДАТЬ", "НЕ ТОРГОВАТЬ", "ПРОПУСТИТЬ", "WAIT", "HOLD OFF", "SKIP")),
    (CAREFUL, ("ОСТОРОЖНО", "МАЛОЙ СУММОЙ", "ТОЛЬКО ДЕМО", "CAREFUL", "CAUTIOUS")),
    (TRADE, ("ТОРГОВАТЬ", "ПОКУПАТЬ", "ВХОДИТЬ", "TRADE", "BUY")),
)
STRONG_WAIT = 70  # «ждать» с такой уверенностью блокирует итог «торговать»
FRESH_SEC = 12 * 3600  # мнение консилиума для автомата годно 12 часов

TASK = """Ты — один из нескольких НЕЗАВИСИМЫХ ИИ-аналитиков в консилиуме. Другие ИИ отвечают на тот же вопрос \
отдельно, итог посчитают по голосам. Поэтому думай сам, опирайся только на ДАННЫЕ бота, цифры не выдумывай.

ВОПРОС: {question}

Ответь строго в таком формате, по-русски, без вступлений (каждая строка начинается с метки):
ВЕРДИКТ: ТОРГОВАТЬ или ОСТОРОЖНО или ЖДАТЬ
УВЕРЕННОСТЬ: число от 0 до 100
ГЛАВНОЕ: 1–2 предложения — почему, с цифрами из данных (комиссии, демо-результаты, доказанные стратегии)
ИДЕЯ: одна конкретная идея (что, где, какая сумма, где стоп) или «нет»
РИСК: главный риск одной фразой
ТОРГОВАТЬ — только если по данным есть доказанный плюс после комиссий. Нет данных или плюс не доказан — ОСТОРОЖНО или ЖДАТЬ."""

DEFAULT_QUESTION = ("Что боту делать с деньгами владельца прямо сейчас: торговать (какой стратегией и где), "
                    "торговать осторожно малой суммой или ждать?")


@dataclass
class Vote:
    provider: str
    model: str | None
    verdict: str | None = None
    confidence: int = 50
    main: str = ""
    idea: str = ""
    risk: str = ""
    error: str | None = None
    raw: str = ""


@dataclass
class Result:
    question: str
    votes: list[Vote] = field(default_factory=list)
    verdict: str | None = None
    agree: int = 0  # сколько ИИ за итог
    answered: int = 0
    note: str = ""
    at: float = 0.0

    def brief(self) -> dict:
        """Коротко для автомата, ИИ-контекста и Mini App."""
        return {"at": self.at, "verdict": self.verdict, "agree": self.agree, "answered": self.answered,
                "asked": len(self.votes), "note": self.note,
                "why": next((v.main for v in self.votes if v.verdict == self.verdict and v.main), "")[:200]}


def _line(text: str, label: str) -> str:
    m = re.search(rf"^\W*{label}\W*[:：-]\s*(.+)$", text, flags=re.I | re.M)
    return m.group(1).strip().strip("*").strip() if m else ""


def parse_vote(provider: str, model: str | None, text: str) -> Vote:
    v = Vote(provider, model, raw=text)
    head = _line(text, "ВЕРДИКТ") or _line(text, "VERDICT")
    pool = (head or text[:300]).upper().replace("Ё", "Е")
    for verdict, words in WORDS:
        if any(w in pool for w in words):
            v.verdict = verdict
            break
    conf = _line(text, "УВЕРЕННОСТЬ") or _line(text, "CONFIDENCE")
    m = re.search(r"\d{1,3}", conf)
    if m:
        v.confidence = max(0, min(100, int(m.group())))
    v.main = _line(text, "ГЛАВНОЕ")[:400]
    v.idea = _line(text, "ИДЕЯ")[:300]
    v.risk = _line(text, "РИСК")[:200]
    if not v.main and v.verdict:  # модель не соблюла формат: берём начало ответа
        v.main = re.sub(r"\s+", " ", text)[:300]
    return v


def tally(votes: list[Vote]) -> tuple[str | None, int, int, str]:
    """→ (итог, сколько за него, сколько ответили, примечание)."""
    ok = [v for v in votes if v.verdict]
    if not ok:
        return None, 0, 0, "ни один ИИ не ответил по делу"
    score = {k: 0 for k in CAUTION}
    for v in ok:
        score[v.verdict] += max(v.confidence, 1)
    best = max(score.values())
    # ничья — в пользу осторожности
    verdict = max((k for k, s in score.items() if s == best), key=CAUTION.get)
    note = ""
    if verdict == TRADE:
        n_trade = sum(1 for v in ok if v.verdict == TRADE)
        strong_wait = any(v.verdict == WAIT and v.confidence >= STRONG_WAIT for v in ok)
        if len(ok) < 2:
            verdict, note = CAREFUL, "«торговать» сказал только один ИИ — этого мало, итог понижен до «осторожно»"
        elif n_trade * 3 < len(ok) * 2:
            verdict, note = CAREFUL, "за «торговать» меньше 2/3 голосов — итог понижен до «осторожно»"
        elif strong_wait:
            verdict, note = CAREFUL, "кто-то уверенно против — итог понижен до «осторожно»"
    agree = sum(1 for v in ok if v.verdict == verdict)
    if len(ok) == 1 and not note:
        note = "ответил только один ИИ — это не консилиум, а мнение одного"
    return verdict, agree, len(ok), note


async def run(ai, context: dict, question: str | None = None, now: float | None = None) -> Result:
    """Спросить все подключённые ИИ (до 7) одновременно и посчитать голоса."""
    question = (question or "").strip()[:600] or DEFAULT_QUESTION
    sys = ai._system(context)
    messages = [sys, {"role": "user", "content": TASK.format(question=question)}]
    raw = await ai.client.chat_each(messages, max_tokens=700)
    votes = []
    for provider, model, text, err in raw:
        if err or not text:
            votes.append(Vote(provider, model, error=err or "пустой ответ"))
        else:
            votes.append(parse_vote(provider, model, guard_withdraw(text)))
    verdict, agree, answered, note = tally(votes)
    return Result(question, votes, verdict, agree, answered, note, time.time() if now is None else now)


def _short_err(err: str, provider: str) -> str:
    err = err.removeprefix(provider + ":").strip()
    return err[:120] + ("…" if len(err) > 120 else "")


def render(res: Result) -> str:
    asked = len(res.votes)
    lines = [f"🧠 <b>Консилиум ИИ</b> — ответили {res.answered} из {asked}"]
    if res.question != DEFAULT_QUESTION:
        lines.append(f"<i>{html.escape(res.question[:300])}</i>")
    if res.verdict:
        lines.append(f"\n<b>Итог: {LABEL[res.verdict]}</b> (за это {res.agree} из {res.answered})")
    else:
        lines.append("\n<b>Итога нет</b>: ни один ИИ не ответил по формату.")
    if res.note:
        lines.append(f"⚖️ {html.escape(res.note)}")
    for v in res.votes:
        name = html.escape(v.provider + (f" · {v.model.split('/')[-1]}" if v.model else ""))
        if v.error:
            lines.append(f"\n▫️ <b>{name}</b>: не ответил — {html.escape(_short_err(v.error, v.provider))}")
            continue
        if not v.verdict:
            lines.append(f"\n▫️ <b>{name}</b>: ответил не по формату — голос не засчитан")
            continue
        lines.append(f"\n{LABEL[v.verdict][:1]} <b>{name}</b>: {LABEL[v.verdict][2:]} · {v.confidence}%")
        if v.main:
            lines.append(html.escape(v.main))
        if v.idea and v.idea.lower().strip(" .«»\"") not in ("нет", "no", "-", "—"):
            lines.append(f"💡 {html.escape(v.idea)}")
        if v.risk:
            lines.append(f"⚠️ {html.escape(v.risk)}")
    if res.answered < 2:
        lines.append("\n➕ Чтобы был настоящий консилиум, добавь ещё бесплатные ключи ИИ: /council keys")
    lines.append("\n<i>Это мнение, а не гарантия. Сделки — только по кнопке и в лимитах риска; автомат на деньги "
                 "консилиум может только остановить.</i>")
    return "\n".join(lines)


def as_answer(res: Result) -> Answer:
    return Answer(render(res), [], None)


KEYS_ORDER = ("keyless", "nophone", "ru", "google", "phone")


def keys_text(ai) -> str:
    """Какие ИИ подключены и какие ещё можно добавить — по тому, что нужно для регистрации."""
    from .ai import ACCESS, PROVIDERS, provider_env_names
    have = {ep.provider for ep in ai.client.endpoints}
    lines = ["🧠 <b>ИИ для консилиума</b>", f"Подключено: {len(have)}. Чем больше разных ИИ — тем надёжнее итог."]
    for group in KEYS_ORDER:
        names = [n for n, p in PROVIDERS.items() if p.get("access") == group]
        if not names:
            continue
        lines.append(f"\n<b>{ACCESS[group]}</b>")
        for name in names:
            p = PROVIDERS[name]
            mark = "✅" if name in have else ("⏸" if group == "keyless" else "➕")
            env = provider_env_names(name)[0]
            how = (f"свой ключ (по желанию): <code>{env}</code>" if group == "keyless" else
                   f"ключ: <a href=\"{p.get('signup', '')}\">получить</a> → Render → <code>{env}</code>")
            lines.append(f"{mark} <b>{html.escape(p['title'])}</b> — {html.escape(p.get('free', ''))}\n    {how}")
    lines.append(GIGACHAT_HOW)
    lines.append("\nКлючи вставляй <b>только в Render</b> (Environment → Add → Save, бот перезапустится сам), "
                 "не присылай их в чат. Для GitHub нужен токен с правом <b>Models: read</b>.")
    return "\n".join(lines)


GIGACHAT_HOW = """
🇷🇺 <b>GigaChat за 5 минут:</b>
1. <a href="https://developers.sber.ru/studio">developers.sber.ru/studio</a> → войти по Сбер ID или номеру РФ.
2. <b>Создать проект</b> → <b>GigaChat API</b> (тариф Freemium для физлиц включается сам).
3. <b>Настройки API</b> → <b>Получить ключ</b> → скопируй <b>Authorization Key</b> (показывается один раз).
4. Render → Environment → <code>GIGACHAT_AUTH_KEY</code> = этот ключ → Save."""


__all__ = ["run", "render", "as_answer", "parse_vote", "tally", "keys_text", "Vote", "Result", "TRADE", "CAREFUL",
           "WAIT", "FRESH_SEC"]
