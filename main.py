import asyncio
import aiohttp
import os
from aiogram import Bot, Dispatcher, types
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
KLING_TOKEN = os.getenv("JWT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# 📩 Ввод: URL + промпт
@dp.message(lambda message: message.text)
async def handle_input(message: types.Message):
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

    await message.answer("🎬 Генерирую видео...")

    asyncio.create_task(generate_video(message, image_url, prompt))


# 🚀 Генерация видео
async def generate_video(message: types.Message, image_url: str, prompt: str):

    headers = {
        "Authorization": f"Bearer {KLING_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "model_name": "kling-v2-6",
        "image": image_url,
        "prompt": prompt,
        "negative_prompt": "",
        "duration": "5",
        "mode": "pro",
        "sound": "off",
        "callback_url": "",
        "external_task_id": ""
    }

    async with aiohttp.ClientSession() as session:

        # 📤 создаём задачу
        async with session.post(
            "https://api-singapore.klingai.com/v1/videos/image2video",
            json=payload,
            headers=headers
        ) as resp:

            raw_text = await resp.text()

            print("\n===== GENERATE RESPONSE =====")
            print("STATUS:", resp.status)
            print(raw_text[:2000])
            print("===== END =====\n")

            try:
                result = await resp.json()
            except:
                await message.answer(f"❌ Ошибка API:\n{raw_text[:1000]}")
                return

        if result.get("code") != 0:
            await message.answer(f"❌ Ошибка создания задачи:\n{result}")
            return

        task_id = result["data"]["task_id"]

        # 🔄 polling
        while True:
            await asyncio.sleep(5)

            async with session.get(
                f"https://api-singapore.klingai.com/v1/videos/image2video/{task_id}",
                headers=headers
            ) as resp:

                raw_text = await resp.text()

                print("\n===== STATUS =====")
                print(raw_text[:2000])
                print("===== END =====\n")

                try:
                    status = await resp.json()
                except:
                    await message.answer(f"Ошибка статуса:\n{raw_text[:1000]}")
                    return

            data = status.get("data", {})
            state = data.get("task_status")

            # ✅ ГОТОВО
            if state == "succeed":
                video_url = data["task_result"]["videos"][0]["url"]
                break

            # ❌ ОШИБКА
            if state in ["failed", "error"]:
                await message.answer(f"❌ Генерация провалилась:\n{status}")
                return

        # 📥 отправка результата
        await message.answer("✅ Готово!")

        try:
            await message.answer_video(video_url)
        except:
            await message.answer(f"🎥 Видео:\n{video_url}")


# ▶️ запуск
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())