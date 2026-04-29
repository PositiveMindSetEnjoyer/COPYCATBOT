import asyncio
import aiohttp
import os
import base64
import logging
import sys

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
KLING_TOKEN = os.getenv("JWT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not KLING_TOKEN:
    raise RuntimeError("JWT_TOKEN is missing")

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tg-bot")

queue = asyncio.Queue()
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@dp.message()
async def log_all_messages(message: Message):
    logger.info(
        "UPDATE received | user_id=%s chat_id=%s text=%r",
        message.from_user.id if message.from_user else None,
        message.chat.id if message.chat else None,
        message.text[:300] if message.text else None,
    )


@dp.message(lambda message: message.text)
async def handle_input(message: types.Message):
    try:
        text = message.text.strip()
        lines = text.split("\n")

        if len(lines) < 2:
            await message.answer(
                "❌ Формат:\n\n"
                "ссылка_на_картинку\nпромпт"
            )
            return

        image_url = lines[0].strip()
        prompt = "\n".join(lines[1:]).strip()

        await queue.put((message.chat.id, message.from_user.id, image_url, prompt))
        logger.info("Job queued | user_id=%s queue_size=%s", message.from_user.id, queue.qsize())

        await message.answer(f"⏳ Вы в очереди: {queue.qsize()}")
    except Exception:
        logger.exception("handle_input failed")
        await message.answer("❌ Внутренняя ошибка при обработке сообщения.")


async def image_url_to_base64(session: aiohttp.ClientSession, url: str):
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                logger.warning("Image load bad status=%s url=%s", resp.status, url)
                return None

            content_type = resp.headers.get("Content-Type", "")
            if "image" not in content_type:
                logger.warning("Not an image | content_type=%s url=%s", content_type, url)
                return None

            data = await resp.read()

            if len(data) > 10 * 1024 * 1024:
                logger.warning("Image too large | size=%s url=%s", len(data), url)
                return None

            return base64.b64encode(data).decode("utf-8")
    except Exception:
        logger.exception("Image download/base64 failed | url=%s", url)
        return None


async def generate_video(session: aiohttp.ClientSession, chat_id: int, image_url: str, prompt: str):
    headers = {
        "Authorization": f"Bearer {KLING_TOKEN}",
        "Content-Type": "application/json",
    }

    image_base64 = await image_url_to_base64(session, image_url)
    if not image_base64:
        await bot.send_message(chat_id, "❌ Не удалось загрузить изображение. Попробуй другую ссылку.")
        return

    payload = {
        "model_name": "kling-v2-6",
        "image": image_base64,
        "prompt": prompt,
        "negative_prompt": "",
        "duration": "5",
        "mode": "pro",
        "sound": "off",
        "callback_url": "",
        "external_task_id": "",
    }

    try:
        logger.info("Creating Kling task | chat_id=%s", chat_id)
        timeout = aiohttp.ClientTimeout(total=60)

        async with session.post(
            "https://api-singapore.klingai.com/v1/videos/image2video",
            json=payload,
            headers=headers,
            timeout=timeout,
        ) as resp:
            raw_text = await resp.text()
            logger.info("Kling create response | status=%s body=%s", resp.status, raw_text[:1000])

            try:
                result = await resp.json()
            except Exception:
                await bot.send_message(chat_id, f"❌ Ошибка API:\n{raw_text[:1000]}")
                return

        if result.get("code") != 0:
            await bot.send_message(chat_id, f"❌ Ошибка создания задачи:\n{result}")
            return

        task_id = result["data"]["task_id"]
        logger.info("Task created | task_id=%s", task_id)

        deadline = asyncio.get_event_loop().time() + 1800

        while True:
            if asyncio.get_event_loop().time() > deadline:
                await bot.send_message(chat_id, "❌ Таймаут ожидания результата.")
                return

            await asyncio.sleep(5)

            async with session.get(
                f"https://api-singapore.klingai.com/v1/videos/image2video/{task_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw_text = await resp.text()
                logger.info("Kling status response | status=%s body=%s", resp.status, raw_text[:1000])

                try:
                    status = await resp.json()
                except Exception:
                    await bot.send_message(chat_id, f"❌ Ошибка статуса:\n{raw_text[:1000]}")
                    return

            data = status.get("data", {})
            state = data.get("task_status")
            logger.info("Task status | task_id=%s state=%s", task_id, state)

            if state == "succeed":
                video_url = data["task_result"]["videos"][0]["url"]
                break

            if state in ["failed", "error"]:
                await bot.send_message(chat_id, f"❌ Генерация провалилась:\n{status}")
                return

        await bot.send_message(chat_id, "✅ Готово!")

        try:
            await bot.send_video(chat_id, video_url)
        except Exception:
            logger.exception("send_video failed, fallback to text")
            await bot.send_message(chat_id, f"🎥 Видео:\n{video_url}")

    except Exception:
        logger.exception("generate_video failed | chat_id=%s", chat_id)
        await bot.send_message(chat_id, "❌ Внутренняя ошибка при генерации видео.")


async def worker(session: aiohttp.ClientSession):
    logger.info("Worker started")
    while True:
        chat_id, user_id, image_url, prompt = await queue.get()
        logger.info("Worker picked job | user_id=%s queue_size=%s", user_id, queue.qsize())

        try:
            await bot.send_message(chat_id, "🎬 Начинаю генерацию...")
            await generate_video(session, chat_id, image_url, prompt)
        except Exception:
            logger.exception("Worker job failed | user_id=%s", user_id)
            try:
                await bot.send_message(chat_id, "❌ Ошибка при обработке задания.")
            except Exception:
                logger.exception("Failed to notify user about job error")
        finally:
            queue.task_done()
            logger.info("Worker finished job | user_id=%s", user_id)


async def main():
    logger.info("Bot starting")

    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        asyncio.create_task(worker(session))
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())