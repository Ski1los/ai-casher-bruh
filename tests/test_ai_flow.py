"""Сквозной тест ИИ в Telegram: команды и кнопки через настоящий aiogram, ИИ на локальном фейк-сервере."""

import asyncio
import datetime as dt
import json

from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.ai import AIAssistant, Endpoint, LLMClient
from tests.test_ai import FakeLLM, serve
from tests.test_telegram_flow import OWNER, STRANGER, send


async def press(b, data, user=OWNER, markup=None):
    msg = Message(
        message_id=99, date=dt.datetime.now(), chat=Chat(id=user, type="private"),
        text="сообщение с кнопками", reply_markup=markup,
    )
    cb = CallbackQuery(
        id="1", from_user=User(id=user, is_bot=False, first_name="Тест"), chat_instance="x", message=msg, data=data
    )
    await b.dp.feed_update(b.bot, Update(update_id=2, callback_query=cb))


def buttons(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row] if markup else []


def test_without_ai_key_bot_explains_how_to_connect(bot_env):
    b, session = bot_env

    async def run():
        assert not b.ai.configured
        await send(b, "/ai")
        assert "console.groq.com" in session.sent[-1][1] and "AI_API_KEY" in session.sent[-1][1]
        await send(b, "почему нет прибыли?")
        assert "/ai" in session.sent[-1][1]
        await send(b, "/unknown_cmd")
        assert "/help" in session.sent[-1][1]
        # анализ без ИИ работает всегда
        await send(b, "/scan")
        assert session.markups[-1] is None  # без ИИ кнопки «Разбор» нет
        await send(b, "/analysis")
        text = session.sent[-1][1]
        assert "Комиссии" in text and "ExA" in text and "окупается от 0.30%" in text
        await send(b, "/fees")
        assert "0.100%" in session.sent[-1][1]

    asyncio.run(run())


def test_ai_review_question_buttons_and_explain(bot_env):
    b, session = bot_env

    def reply(body):
        last = body["messages"][-1]["content"]
        if "СИГНАЛЫ" in last:
            return "PEPE/USDT: вывод съест прибыль. Вердикт: пропустить."
        if "разбор работы" in last:
            return "**Итог**: комиссии съедают всё.\nSET tri_min=0.3 — запас\nSET amount=50 — ближе к твоей сумме"
        return f"Ответ на: {last}"

    async def run():
        fake = FakeLLM(reply=reply)
        runner, base = await serve(fake)
        b.ai = AIAssistant(LLMClient([Endpoint("groq", base, "gsk_ok")]))
        try:
            await send(b, "/scan")
            assert "ai_x:1" in buttons(session.markups[-1])  # под сигналами кнопка «Разбор»

            await press(b, "ai_x:1")
            assert "Вердикт: пропустить" in session.sent[-1][1]
            signals = fake.requests[-1]["messages"][-1]["content"]
            assert "PEPE/USDT" in signals and "withdraw_fee_usd" in signals

            await send(b, "/ai")
            text, kb = session.sent[-1][1], session.markups[-1]
            assert "<b>Итог</b>" in text and "Предлагаю изменить" in text
            assert buttons(kb) == ["set:tri_min:0.3", "set:amount:50", "dismiss"]
            context = fake.requests[-1]["messages"][0]["content"]
            assert '"last_scan"' in context and '"fees_taker_percent"' in context

            # чужой не может применить
            await press(b, "set:tri_min:0.3", user=STRANGER, markup=kb)
            assert b.settings.tri_min_profit_percent == 0.1
            # владелец применяет одну кнопку, вторая остаётся
            await press(b, "set:tri_min:0.3", markup=kb)
            assert b.settings.tri_min_profit_percent == 0.3
            assert "0.1 → <b>0.3</b>" in session.sent[-1][1]
            edits = [c for c in session.calls if isinstance(c, EditMessageReplyMarkup)]
            assert buttons(edits[-1].reply_markup) == ["set:amount:50", "dismiss"]
            # поддельная кнопка с недопустимым значением не применяется
            await press(b, "set:interval:1")
            assert b.settings.scan_interval_sec != 1
            answers = [c for c in session.calls if isinstance(c, AnswerCallbackQuery)]
            assert answers[-1].show_alert and "допустимо" in answers[-1].text

            # обычный текст — вопрос ИИ, с историей
            await send(b, "что такое треугольник?")
            assert "Ответ на: что такое треугольник?" in session.sent[-1][1]
            await send(b, "/ai_status")
            assert "Groq" in session.sent[-1][1] and "gpt-oss-120b" in session.sent[-1][1]
            await send(b, "/ai_reset")
            assert not b.ai.history
        finally:
            await b.ai.client.close()
            await runner.cleanup()

    asyncio.run(run())


def test_ai_error_is_shown_not_crashing(bot_env):
    b, session = bot_env

    async def run():
        b.ai = AIAssistant(LLMClient([Endpoint("groq", "http://127.0.0.1:9/v1", "gsk_x", model="m")]))
        try:
            await send(b, "/ai почему?")
            assert "Не получилось" in session.sent[-1][1] and "/analysis" in session.sent[-1][1]
        finally:
            await b.ai.client.close()

    asyncio.run(run())


def test_daily_report_includes_ai_review(bot_env):
    b, session = bot_env

    async def run():
        fake = FakeLLM(reply="Сегодня всё съели комиссии.\nSET tri_min=0.2 — запас")
        runner, base = await serve(fake)
        b.ai = AIAssistant(LLMClient([Endpoint("groq", base, "gsk_ok")]))
        try:
            text, kb = await b._ai_daily_review()
        finally:
            await b.ai.client.close()
            await runner.cleanup()
        assert "Мнение ИИ" in text and "комиссии" in text
        assert "set:tri_min:0.2" in buttons(kb)

    asyncio.run(run())


def test_fee_override_and_settings_survive_restart(bot_env, tmp_path):
    import bot.telegram_bot as tb
    from bot.config import Settings

    b, session = bot_env

    async def run():
        await send(b, "/scan")  # рынки загружены
        await send(b, "/fee nope 0.02")
        assert "Биржи:" in session.sent[-1][1]
        await send(b, "/fee ExA 0,02")  # по названию биржи, а не только по id ccxt
        assert "0.02%" in session.sent[-1][1]
        assert b.scanner.market("a", "PEPE/USDT")["taker"] == 0.0002
        assert b.scanner.market("b", "PEPE/USDT")["taker"] == 0.001
        await send(b, "/fee a 9")
        assert "от 0 до 5" in session.sent[-1][1]
        await send(b, "/set amount 250")
        await send(b, "/settings")
        assert "Свои комиссии" in session.sent[-1][1]

        saved = json.loads((tmp_path / "settings.json").read_text())
        assert saved == {"values": {"amount": 250.0}, "fees": {"a": 0.0002}, "alerts": [], "phantom": "", "radar": [], "opps_notify": True}
        assert b._state()["settings"] == saved  # и в бэкапе Telegram

        # «перезапуск»: новый бот с настройками по умолчанию подхватывает сохранённое
        b2 = tb.ArbitrageBot("123456:" + "A" * 35, Settings(exchanges=[]), [OWNER])
        assert b2.settings.trade_amount_usd == 250
        assert b2.settings.fee_overrides == {"a": 0.0002}

        await send(b, "/fee a reset")
        assert b.scanner.market("a", "PEPE/USDT")["taker"] == 0.001
        assert json.loads((tmp_path / "settings.json").read_text())["fees"] == {"a": None}

    asyncio.run(run())


def test_cross_signal_accounts_for_withdraw_fee(bot_env):
    b, session = bot_env

    async def run():
        await send(b, "/scan")
        text = "\n".join(t for _, t in session.sent[-3:])
        assert "После вывода монеты (≈$0.50, оценка): ✅" in text
        result = b.scanner.last_result
        pepe = next(o for o in result.opportunities if o.symbol == "PEPE/USDT")
        assert b._should_alert(pepe)  # ≈+$2.8 на $100 минус $0.5 вывода: сигнал есть
        b.settings.demo_withdraw_fee_usd = 5  # дорогой вывод съедает прибыль: сигнала нет
        assert not b._should_alert(pepe)

    asyncio.run(run())
