import os
import secrets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
import httpx
from work_photos import replace_work_photos

TOKEN = os.environ.get('BOT_TOKEN', '')
SECRET = os.environ.get('WEBHOOK_SECRET', '')
URL = os.environ.get('APP_URL', '').rstrip('/')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
MASTER_ID = 8950443320

app = FastAPI()

with open('index.html', 'r', encoding='utf-8') as f:
    HTML_PAGE = replace_work_photos(f.read())

async def check_database():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 'Не заданы SUPABASE_URL или SUPABASE_SECRET_KEY в Render.'
    if SUPABASE_URL != 'https://woblvdrbcriokqmqehuf.supabase.co':
        return 'SUPABASE_URL не совпадает с проектом записи. Проверьте Environment в Render.'
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            for table, column in [('nail_admins', 'telegram_id'), ('nail_availability', 'id'), ('nail_bookings', 'id'), ('nail_receipts', 'id')]:
                params = {'select': column, 'limit': '1'}
                if table == 'nail_admins':
                    params['telegram_id'] = f'eq.{MASTER_ID}'
                response = await client.get(
                    SUPABASE_URL + '/rest/v1/' + table,
                    headers={'apikey': SUPABASE_KEY, 'Cache-Control': 'no-store'},
                    params=params
                )
                if response.status_code != 200:
                    return f'Нет доступа к {table}: HTTP {response.status_code}. Проверьте серверный секретный ключ и права таблиц.'
                data = response.json()
                if not isinstance(data, list):
                    return 'База вернула неожиданный формат ответа.'
                if table == 'nail_admins' and not any(row.get('telegram_id') == MASTER_ID for row in data):
                    return 'Соединение есть, но ID мастера не найден в nail_admins.'
    except (httpx.HTTPError, ValueError):
        return 'Не удалось связаться с Supabase. Попробуйте позже; секретные значения не выводятся.'
    return '✅ Supabase подключён. Все 4 таблицы доступны, ID мастера найден. Это проверка соединения: запись, расписание и приём чеков ещё не включены.'

@app.get('/')
def home():
    return HTMLResponse(HTML_PAGE, headers={'Cache-Control': 'no-store'})

@app.get('/health')
def health():
    return {'ok': True}

@app.post('/telegram/webhook')
async def webhook(r: Request):
    supplied = r.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not SECRET or not secrets.compare_digest(supplied, SECRET):
        raise HTTPException(403)
    x = await r.json()
    if m := x.get('message'):
        if m.get('chat', {}).get('type') == 'private':
            command = m.get('text', '').split(maxsplit=1)
            command = command[0].split('@', 1)[0] if command else ''
            if command == '/dbcheck':
                if m.get('from', {}).get('id') != MASTER_ID:
                    return {'ok': True}
                text = await check_database()
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await client.post(
                        f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                        json={'chat_id': m['chat']['id'], 'text': text}
                    )
                    response.raise_for_status()
                    if not response.json().get('ok'):
                        raise HTTPException(502, 'Telegram delivery failed')
                return {'ok': True}
            text = "Привет! Запишись на маникюр к Ярославе (Yasa cat) через удобное мини-приложение ниже 👇"
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(
                    f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                    json={
                        'chat_id': m['chat']['id'],
                        'text': text,
                        'reply_markup': {
                            'inline_keyboard': [[
                                {'text': '💅 Записаться на маникюр', 'web_app': {'url': URL or 'https://google.com'}}
                            ]]
                        }
                    }
                )
    return {'ok': True}

@app.post('/admin/setup')
async def setup(r: Request):
    supplied = r.headers.get('X-Admin-Secret', '')
    if not SECRET or not secrets.compare_digest(supplied, SECRET):
        raise HTTPException(403)
    async with httpx.AsyncClient(timeout=8) as client:
        await client.post(
            f'https://api.telegram.org/bot{TOKEN}/setWebhook',
            json={'url': URL + '/telegram/webhook', 'secret_token': SECRET, 'allowed_updates': ['message']}
        )
        await client.post(
            f'https://api.telegram.org/bot{TOKEN}/setChatMenuButton',
            json={'menu_button': {'type': 'web_app', 'text': 'Записаться', 'web_app': {'url': URL}}}
        )
    return {'ok': True}
