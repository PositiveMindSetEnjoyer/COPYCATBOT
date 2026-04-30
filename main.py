import asyncio
import base64
import gc
import logging
import os
import time
from contextlib import suppress
from typing import Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from dotenv import load_dotenv, set_key
from keyjwt import encode_jwt_token

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
KLING_TOKEN = os.getenv('JWT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

WORKERS = int(os.getenv('WORKERS', '3'))
QUEUE_MAXSIZE = int(os.getenv('QUEUE_MAXSIZE', '100'))
QUEUE_ITEM_TTL = int(os.getenv('QUEUE_ITEM_TTL', '1800'))
IDLE_CLEAN_INTERVAL = int(os.getenv('IDLE_CLEAN_INTERVAL', '300'))
MAX_PROMPT_LEN = int(os.getenv('MAX_PROMPT_LEN', '4000'))
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))
IMAGE_TIMEOUT = int(os.getenv('IMAGE_TIMEOUT', '20'))
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '5'))
MAX_POLL_ATTEMPTS = int(os.getenv('MAX_POLL_ATTEMPTS', '120'))
MAX_IMAGE_MB = int(os.getenv('MAX_IMAGE_MB', '10'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
logger = logging.getLogger('video_bot')

queue: asyncio.Queue[Tuple[types.Message, str, str, str, float]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
http_session: Optional[aiohttp.ClientSession] = None
admin_waiting = {}
user_pending = {}

async def safe_send(message: types.Message, text: str, reply_markup=None):
    with suppress(Exception):
        await message.answer(text, reply_markup=reply_markup)

async def trim_memory():
    gc.collect()

async def image_url_to_base64(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=IMAGE_TIMEOUT), headers={'User-Agent': 'Mozilla/5.0'}) as resp:
            if resp.status != 200:
                return None
            if 'image' not in resp.headers.get('Content-Type', '').lower():
                return None
            data = await resp.read()
            if len(data) > MAX_IMAGE_MB * 1024 * 1024:
                return None
            return base64.b64encode(data).decode('utf-8')
    except Exception:
        logger.exception('Image load failed')
        return None

async def create_task(session, image_b64, prompt, duration):
    global KLING_TOKEN
    headers = {'Authorization': f'Bearer {KLING_TOKEN}', 'Content-Type': 'application/json'}
    payload = {
        'model_name': 'kling-v2-6',
        'image': image_b64,
        'prompt': prompt,
        'negative_prompt': '',
        'duration': duration,
        'mode': 'pro',
        'sound': 'off',
        'callback_url': '',
        'external_task_id': ''
    }
    try:
        async with session.post('https://api-singapore.klingai.com/v1/videos/image2video', json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if data.get('code') == 0:
                return data['data']['task_id']
    except Exception:
        logger.exception('Create task failed')
    return None

async def poll_task(session, task_id):
    global KLING_TOKEN
    headers = {'Authorization': f'Bearer {KLING_TOKEN}'}
    for _ in range(MAX_POLL_ATTEMPTS):
        await asyncio.sleep(POLL_INTERVAL)
        try:
            async with session.get(f'https://api-singapore.klingai.com/v1/videos/image2video/{task_id}', headers=headers) as resp:
                data = await resp.json(content_type=None)
                state = data.get('data', {}).get('task_status')
                if state == 'succeed':
                    return data['data']['task_result']['videos'][0]['url']
                if state in ('failed', 'error'):
                    return None
        except Exception:
            logger.exception('Poll failed')
    return None

async def generate_video(message, image_url, prompt, duration):
    image_b64 = await image_url_to_base64(http_session, image_url)
    if not image_b64:
        await safe_send(message, '❌ Не удалось загрузить изображение')
        return
    task_id = await create_task(http_session, image_b64, prompt, duration)
    del image_b64
    await trim_memory()
    if not task_id:
        await safe_send(message, '❌ Ошибка создания задачи')
        return
    video = await poll_task(http_session, task_id)
    if not video:
        await safe_send(message, '❌ Генерация завершилась ошибкой')
        return
    try:
        await message.answer_video(video)
    except Exception:
        await safe_send(message, video)

async def queue_janitor():
    while True:
        await asyncio.sleep(IDLE_CLEAN_INTERVAL)
        try:
            fresh = []
            now = time.time()
            while not queue.empty():
                item = queue.get_nowait()
                if now - item[4] <= QUEUE_ITEM_TTL:
                    fresh.append(item)
                queue.task_done()
            for item in fresh:
                await queue.put(item)
            await trim_memory()
        except Exception:
            logger.exception('Janitor error')

async def worker(worker_id):
    while True:
        message, image_url, prompt, duration, created = await queue.get()
        try:
            await generate_video(message, image_url, prompt, duration)
        except Exception:
            logger.exception('Worker error')
        finally:
            queue.task_done()

def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text='🎬 Новый запрос')],
        [KeyboardButton(text='❌ Отмена')]
    ], resize_keyboard=True)

async def expire_request(user_id: int):
    await asyncio.sleep(30)
    if user_pending.get(user_id, {}).get('state') == 'waiting_input':
        user_pending.pop(user_id, None)
        try:
            await bot.send_message(user_id, '⌛ Время истекло', reply_markup=main_menu())
        except Exception:
            pass

@dp.message(lambda m: m.text == '/start')
async def start_cmd(message: types.Message):
    await safe_send(message, 'Добро пожаловать', main_menu())

@dp.message(lambda m: m.chat.id == ADMIN_ID and m.text == '/admin')
async def admin_panel(message: types.Message):
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='🔑 Обновить JWT')]], resize_keyboard=True)
    await safe_send(message, 'Админ панель', kb)

@dp.message(lambda m: m.chat.id == ADMIN_ID and m.text == '🔑 Обновить JWT')
async def admin_jwt(message: types.Message):
    admin_waiting[message.chat.id] = True
    await safe_send(message, 'Отправьте данные:api_keysecret_key')

@dp.message(lambda m: bool(m.text))
async def handle_input(message: types.Message):
    global KLING_TOKEN
    text = message.text.strip()
    uid = message.chat.id

    if uid == ADMIN_ID and admin_waiting.get(uid):
        parts = text.split('\n')
        if len(parts) < 2:
            await safe_send(message, 'Формат: api_key secret_key')
            return
        try:
            token = encode_jwt_token(parts[0].strip(), parts[1].strip())
            KLING_TOKEN = token
            set_key('.env', 'JWT_TOKEN', token)
            admin_waiting.pop(uid, None)
            await safe_send(message, '✅ JWT обновлен', main_menu())
            return
        except Exception:
            await safe_send(message, '❌ Ошибка обновления JWT')
            return

    if text == '❌ Отмена':
        user_pending.pop(uid, None)
        await safe_send(message, 'Запрос отменён', main_menu())
        return

    if text == '🎬 Новый запрос':
        user_pending[uid] = {'state': 'waiting_input'}
        asyncio.create_task(expire_request(uid))
        await safe_send(message, 'Отправьте:ссылка_на_картинку промпт ⌛ У вас 30 секунд', main_menu())
        return

    if uid in user_pending and user_pending[uid].get('state') == 'waiting_duration':
        if text not in ('5', '10'):
            await safe_send(message, 'Выберите кнопкой: 5 или 10 секунд')
            return
        data = user_pending.pop(uid)
        if queue.full():
            await safe_send(message, '⛔ Очередь переполнена', main_menu())
            return
        await queue.put((message, data['image_url'], data['prompt'], text, time.time()))
        await safe_send(message, f'⏳ Вы в очереди: {queue.qsize()}', main_menu())
        return

    if uid in user_pending and user_pending[uid].get('state') == 'waiting_input':
        lines = text.split('\n')
        if len(lines) < 2:
            await safe_send(message, 'Формат: ссылка_на_картинку промпт')
            return
        image_url = lines[0].strip()
        prompt = ''.join(lines[1:]).strip()[:MAX_PROMPT_LEN]
        user_pending[uid] = {'state': 'waiting_duration', 'image_url': image_url, 'prompt': prompt}
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='5'), KeyboardButton(text='10')]], resize_keyboard=True)
        await safe_send(message, '⏱ Сколько секунд видео?', kb)
        return

    await safe_send(message, 'Нажмите 🎬 Новый запрос', main_menu())

async def main():
    global http_session
    http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), connector=aiohttp.TCPConnector(limit=100))
    workers = [asyncio.create_task(worker(i)) for i in range(WORKERS)]
    janitor = asyncio.create_task(queue_janitor())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        janitor.cancel()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, janitor, return_exceptions=True)
        await http_session.close()
        await bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())
