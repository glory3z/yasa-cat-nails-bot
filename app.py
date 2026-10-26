import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
import httpx

TOKEN = os.environ.get('BOT_TOKEN', '')
SECRET = os.environ.get('WEBHOOK_SECRET', '')
URL = os.environ.get('APP_URL', '').rstrip('/')

app = FastAPI()

with open('index.html', 'r', encoding='utf-8') as f:
    HTML_PAGE = f.read()

@app.get('/')
def home():
    return HTMLResponse(HTML_PAGE, headers={'Cache-Control': 'no-store'})

@app.get('/health')
def health():
    return {'ok': True}

@app.post('/telegram/webhook')
async def webhook(r: Request):
    if SECRET and r.headers.get('X-Telegram-Bot-Api-Secret-Token', '') != SECRET:
        raise HTTPException(403)
    x = await r.json()
    if m := x.get('message'):
        if m.get('chat', {}).get('type') == 'private':
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
    if SECRET and r.headers.get('X-Admin-Secret', '') != SECRET:
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
