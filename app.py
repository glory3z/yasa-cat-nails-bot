import os
import secrets
import json
import uuid
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Header
from fastapi.responses import HTMLResponse, Response
import httpx

from work_photos import replace_work_photos

TOKEN = os.environ.get('BOT_TOKEN', '')
SECRET = os.environ.get('WEBHOOK_SECRET', '')
URL = os.environ.get('APP_URL', '').rstrip('/')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
MASTER_ID = 8950443320
DIAGNOSTIC_IDS = {MASTER_ID, 7689570674}

MOSCOW = timezone(timedelta(hours=3))
ALLOWED_TIMES = ('11:00', '13:30', '16:00', '18:30', '20:30')
RECEIPT_TYPES = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'application/pdf': 'pdf',
}
MAX_RECEIPT_SIZE = 10 * 1024 * 1024
RECEIPT_BUCKET = 'nail-private-receipts'

app = FastAPI()

with open('index.html', 'r', encoding='utf-8') as f:
    HTML_PAGE = replace_work_photos(f.read())

with open('static/client-enhance.js', 'r', encoding='utf-8') as f:
    CLIENT_JS = f.read()

with open('admin.html', 'r', encoding='utf-8') as f:
    ADMIN_PAGE = f.read()


def sb_headers(content_type=None):
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
    }
    if content_type:
        headers['Content-Type'] = content_type
    return headers


async def sb_request(method, path, json_body=None, content=None, headers=None, params=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=503, detail='База данных не настроена.')
    request_headers = sb_headers('application/json' if json_body is not None else None)
    if headers:
        request_headers.update(headers)
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.request(
                method,
                SUPABASE_URL + path,
                json=json_body,
                content=content,
                headers=request_headers,
                params=params,
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail='Не удалось связаться с базой данных.')
    if response.status_code >= 400:
        if response.status_code == 409:
            raise HTTPException(status_code=409, detail='Это время уже занято. Выбери другое.')
        raise HTTPException(status_code=502, detail='Ошибка сохранения в базе данных.')
    return response


def verify_init_data(raw_init_data):
    if not raw_init_data:
        raise HTTPException(status_code=401, detail='Открой приложение через Telegram.')
    pairs = dict(parse_qsl(raw_init_data, keep_blank_values=True))
    received_hash = pairs.pop('hash', '')
    if not received_hash:
        raise HTTPException(status_code=401, detail='Некорректные данные Telegram.')
    data_check_string = '\n'.join(f'{key}={value}' for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail='Не удалось подтвердить Telegram-аккаунт.')
    try:
        user = json.loads(pairs.get('user', '{}'))
    except ValueError:
        raise HTTPException(status_code=401, detail='Некорректные данные Telegram.')
    if not user.get('id'):
        raise HTTPException(status_code=401, detail='Не удалось определить Telegram ID.')
    return user


async def require_admin(raw_init_data):
    user = verify_init_data(raw_init_data)
    response = await sb_request(
        'GET',
        f"/rest/v1/nail_admins?telegram_id=eq.{user['id']}&select=telegram_id",
    )
    if not response.json():
        raise HTTPException(status_code=403, detail='Нет доступа к кабинету.')
    return user


@app.get('/')
def home():
    enhanced = HTML_PAGE.replace('</body>', '<script src="/static/client-enhance.js"></script></body>')
    return HTMLResponse(enhanced, headers={'Cache-Control': 'no-store'})


@app.get('/static/client-enhance.js')
def client_enhancer():
    return Response(CLIENT_JS, media_type='application/javascript', headers={'Cache-Control': 'no-store'})


@app.get('/admin')
def admin_page():
    return HTMLResponse(ADMIN_PAGE, headers={'Cache-Control': 'no-store'})


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/api/config')
async def api_config():
    services = (await sb_request('GET', '/rest/v1/nail_services?active=eq.true&order=sort_order.asc')).json()
    addons = (await sb_request('GET', '/rest/v1/nail_addons?active=eq.true&order=sort_order.asc')).json()
    settings_rows = (await sb_request('GET', '/rest/v1/nail_settings?id=eq.1')).json()
    settings = settings_rows[0] if settings_rows else {}
    return {
        'services': services,
        'addons': addons,
        'settings': {
            'deposit_rub': settings.get('deposit_rub', 500),
            'payment_phone': settings.get('payment_phone', ''),
            'payment_bank': settings.get('payment_bank', ''),
            'cancellation_terms': settings.get('cancellation_terms', ''),
            'reschedule_terms': settings.get('reschedule_terms', ''),
            'booking_enabled': settings.get('booking_enabled', False),
        },
    }


@app.get('/api/me')
async def api_me(request: Request):
    user = verify_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    response = await sb_request('GET', f"/rest/v1/nail_admins?telegram_id=eq.{user['id']}&select=telegram_id")
    return {'telegram_id': user['id'], 'is_admin': bool(response.json())}


@app.post('/api/bookings')
async def create_booking(request: Request):
    user = verify_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    body = await request.json()
    service_id = str(body.get('service_id') or '')
    addon_ids = body.get('addon_ids') or []
    booking_date = str(body.get('date') or '')
    booking_time = str(body.get('time') or '')
    client_name = str(body.get('name') or '').strip()
    telegram_username = str(body.get('tg') or '').strip()
    client_comment = str(body.get('comment') or '').strip()[:2000]

    if not service_id or not booking_date or not booking_time or not client_name:
        raise HTTPException(status_code=400, detail='Заполни имя, услугу и время.')

    settings_rows = (await sb_request('GET', '/rest/v1/nail_settings?id=eq.1')).json()
    if not settings_rows or not settings_rows[0].get('booking_enabled'):
        raise HTTPException(status_code=403, detail='Запись временно закрыта.')

    services = (await sb_request('GET', f"/rest/v1/nail_services?id=eq.{service_id}&select=*")).json()
    if not services:
        raise HTTPException(status_code=400, detail='Услуга недоступна.')
    service = services[0]

    addons = []
    if addon_ids:
        ids = ','.join(str(item) for item in addon_ids[:20])
        addons = (await sb_request('GET', f'/rest/v1/nail_addons?id=in.({ids})&select=*')).json()

    if booking_time not in ALLOWED_TIMES:
        raise HTTPException(status_code=400, detail='Выбери время из списка.')
    try:
        starts_at = datetime.strptime(f'{booking_date} {booking_time}', '%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW)
    except ValueError:
        raise HTTPException(status_code=400, detail='Некорректная дата или время.')
    if starts_at < datetime.now(MOSCOW):
        raise HTTPException(status_code=400, detail='Нельзя записаться на прошедшее время.')

    duration_minutes = service['minutes'] + sum(addon.get('minutes', 0) for addon in addons)
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    total_rub = service['price'] + sum(addon.get('price', 0) for addon in addons)
    deposit_rub = settings_rows[0].get('deposit_rub', 500)

    payload = {
        'telegram_id': user['id'],
        'client_name': client_name,
        'telegram_username': telegram_username,
        'service_id': service['id'],
        'service_name': service['name'],
        'addons': [{'id': addon['id'], 'name': addon['name'], 'price': addon['price']} for addon in addons],
        'starts_at': starts_at.isoformat(),
        'ends_at': ends_at.isoformat(),
        'total_rub': total_rub,
        'deposit_rub': deposit_rub,
        'status': 'awaiting_receipt',
        'hold_expires_at': (datetime.now(MOSCOW) + timedelta(minutes=30)).isoformat(),
        'payment_status': 'unverified',
        'client_comment': client_comment,
        'payment_details_snapshot': {
            'payment_phone': settings_rows[0].get('payment_phone', ''),
            'payment_bank': settings_rows[0].get('payment_bank', ''),
        },
    }
    response = await sb_request(
        'POST',
        '/rest/v1/nail_bookings?select=*',
        json_body=payload,
        headers={'Prefer': 'return=representation'},
    )
    return response.json()[0]


@app.post('/api/bookings/{booking_id}/receipt')
async def upload_receipt(
    booking_id: str,
    file: UploadFile = File(...),
    comment: str = Form(''),
    x_telegram_init_data: str = Header(default=''),
):
    user = verify_init_data(x_telegram_init_data)
    bookings = (await sb_request('GET', f"/rest/v1/nail_bookings?id=eq.{booking_id}&select=*")).json()
    if not bookings:
        raise HTTPException(status_code=404, detail='Запись не найдена.')
    booking = bookings[0]
    if booking['telegram_id'] != user['id']:
        raise HTTPException(status_code=403, detail='Это не твоя запись.')
    if booking['status'] != 'awaiting_receipt':
        raise HTTPException(status_code=409, detail='Чек уже отправлен или запись закрыта.')

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail='Файл пустой.')
    if len(content) > MAX_RECEIPT_SIZE:
        raise HTTPException(status_code=413, detail='Файл больше 10 МБ.')
    extension = RECEIPT_TYPES.get(file.content_type)
    if not extension:
        raise HTTPException(status_code=415, detail='Поддерживаются JPG, PNG, WEBP или PDF.')

    object_path = f"{booking_id}/{uuid.uuid4().hex}.{extension}"
    await sb_request(
        'POST',
        f'/storage/v1/object/{RECEIPT_BUCKET}/{object_path}',
        content=content,
        headers={'Content-Type': file.content_type},
    )
    await sb_request(
        'POST',
        '/rest/v1/nail_uploaded_receipts',
        json_body={
            'booking_id': booking_id,
            'uploader_telegram_id': user['id'],
            'object_path': object_path,
            'original_filename': file.filename or 'receipt',
            'mime_type': file.content_type,
            'size_bytes': len(content),
        },
    )
    await sb_request(
        'PATCH',
        f"/rest/v1/nail_bookings?id=eq.{booking_id}",
        json_body={
            'status': 'pending_review',
            'client_comment': comment.strip()[:2000],
            'updated_at': datetime.now(MOSCOW).isoformat(),
        },
    )
    return {'ok': True}


@app.get('/api/bookings/mine')
async def my_bookings(x_telegram_init_data: str = Header(default='')):
    user = verify_init_data(x_telegram_init_data)
    response = await sb_request(
        'GET',
        f"/rest/v1/nail_bookings?telegram_id=eq.{user['id']}&select=*&order=created_at.desc&limit=20",
    )
    return response.json()


@app.post('/api/bookings/{booking_id}/cancel')
async def cancel_booking(booking_id: str, x_telegram_init_data: str = Header(default='')):
    user = verify_init_data(x_telegram_init_data)
    bookings = (await sb_request('GET', f"/rest/v1/nail_bookings?id=eq.{booking_id}&select=*")).json()
    if not bookings or bookings[0]['telegram_id'] != user['id']:
        raise HTTPException(status_code=404, detail='Запись не найдена.')
    if bookings[0]['status'] not in ('awaiting_receipt', 'pending_review'):
        raise HTTPException(status_code=409, detail='Эту запись уже нельзя отменить.')
    await sb_request(
        'PATCH',
        f"/rest/v1/nail_bookings?id=eq.{booking_id}",
        json_body={'status': 'cancelled', 'updated_at': datetime.now(MOSCOW).isoformat()},
    )
    return {'ok': True}


@app.get('/api/admin/bookings')
async def admin_bookings(request: Request, status: str = ''):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    query = '/rest/v1/nail_bookings?select=*&order=created_at.desc&limit=100'
    if status:
        query += f'&status=eq.{status}'
    bookings = (await sb_request('GET', query)).json()
    receipt_map = {}
    if bookings:
        ids = ','.join(row['id'] for row in bookings)
        receipts = (await sb_request('GET', f'/rest/v1/nail_uploaded_receipts?booking_id=in.({ids})&select=*')).json()
        for receipt in receipts:
            receipt_map.setdefault(receipt['booking_id'], []).append(receipt)
    return {'bookings': bookings, 'receipts': receipt_map, 'admin_id': user['id']}


@app.get('/api/admin/receipt/{receipt_id}')
async def admin_receipt(receipt_id: str, request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    rows = (await sb_request('GET', f"/rest/v1/nail_uploaded_receipts?id=eq.{receipt_id}&select=*")).json()
    if not rows:
        raise HTTPException(status_code=404, detail='Чек не найден.')
    sign_response = await sb_request(
        'POST',
        f"/storage/v1/object/sign/{RECEIPT_BUCKET}/{rows[0]['object_path']}",
        json_body={'expiresIn': 600},
    )
    signed_url = sign_response.json().get('signedURL', '')
    return {'url': SUPABASE_URL + '/storage/v1' + signed_url}


ACTION_UPDATES = {
    'confirm_payment': {'payment_status': 'received', 'status': 'confirmed'},
    'payment_not_found': {'status': 'payment_not_found'},
    'decline': {'status': 'declined'},
    'cancel': {'status': 'cancelled'},
    'request_reschedule': {'status': 'reschedule_requested'},
    'confirm_reschedule': {'status': 'confirmed'},
}


@app.post('/api/admin/bookings/{booking_id}/action')
async def admin_booking_action(booking_id: str, request: Request):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    body = await request.json()
    action = body.get('action')
    if action not in ACTION_UPDATES:
        raise HTTPException(status_code=400, detail='Неизвестное действие.')
    updates = dict(ACTION_UPDATES[action])
    updates['updated_at'] = datetime.now(MOSCOW).isoformat()
    await sb_request('PATCH', f"/rest/v1/nail_bookings?id=eq.{booking_id}", json_body=updates)
    await sb_request(
        'POST',
        '/rest/v1/nail_admin_audit',
        json_body={
            'actor_telegram_id': user['id'],
            'action': action,
            'entity_id': booking_id,
            'details': body,
        },
    )
    return {'ok': True}


@app.get('/api/admin/config')
async def admin_config(request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    services = (await sb_request('GET', '/rest/v1/nail_services?order=sort_order.asc')).json()
    addons = (await sb_request('GET', '/rest/v1/nail_addons?order=sort_order.asc')).json()
    settings_rows = (await sb_request('GET', '/rest/v1/nail_settings?id=eq.1')).json()
    availability = (await sb_request('GET', '/rest/v1/nail_availability?enabled=eq.true&order=starts_at.asc&limit=200')).json()
    return {
        'services': services,
        'addons': addons,
        'settings': settings_rows[0] if settings_rows else {},
        'availability': availability,
    }


@app.put('/api/admin/settings')
async def update_settings(request: Request):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    body = await request.json()
    updates = {
        key: body[key]
        for key in (
            'deposit_rub',
            'payment_phone',
            'payment_bank',
            'cancellation_terms',
            'reschedule_terms',
            'booking_enabled',
        )
        if key in body
    }
    updates['updated_at'] = datetime.now(MOSCOW).isoformat()
    await sb_request('PATCH', '/rest/v1/nail_settings?id=eq.1', json_body=updates)
    return {'ok': True}


@app.put('/api/admin/services')
async def update_services(request: Request):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    rows = (await request.json()).get('services') or []
    for row in rows:
        row['updated_at'] = datetime.now(MOSCOW).isoformat()
    await sb_request(
        'POST',
        '/rest/v1/nail_services?on_conflict=id',
        json_body=rows,
        headers={'Prefer': 'resolution=merge-duplicates'},
    )
    return {'ok': True}


@app.put('/api/admin/addons')
async def update_addons(request: Request):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    rows = (await request.json()).get('addons') or []
    for row in rows:
        row['updated_at'] = datetime.now(MOSCOW).isoformat()
    await sb_request(
        'POST',
        '/rest/v1/nail_addons?on_conflict=id',
        json_body=rows,
        headers={'Prefer': 'resolution=merge-duplicates'},
    )
    return {'ok': True}


@app.post('/api/admin/availability')
async def add_availability(request: Request):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    body = await request.json()
    slot_date = str(body.get('date') or '')
    slot_time = str(body.get('time') or '')
    minutes = int(body.get('minutes') or 60)
    starts_at = datetime.strptime(f'{slot_date} {slot_time}', '%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW)
    ends_at = starts_at + timedelta(minutes=minutes)
    await sb_request(
        'POST',
        '/rest/v1/nail_availability',
        json_body={'starts_at': starts_at.isoformat(), 'ends_at': ends_at.isoformat(), 'enabled': True},
        headers={'Prefer': 'return=representation'},
    )
    return {'ok': True}


@app.delete('/api/admin/availability/{slot_id}')
async def delete_availability(slot_id: str, request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
    await sb_request('DELETE', f'/rest/v1/nail_availability?id=eq.{slot_id}')
    return {'ok': True}


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
                    params=params,
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
    return '✅ Supabase подключён. Основные таблицы доступны, ID мастера найден.'


@app.post('/telegram/webhook')
async def webhook(request: Request):
    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not SECRET or not secrets.compare_digest(supplied, SECRET):
        raise HTTPException(status_code=403)
    update = await request.json()
    message = update.get('message')
    if message and message.get('chat', {}).get('type') == 'private':
        parts = message.get('text', '').split(maxsplit=1)
        command = parts[0].split('@', 1)[0] if parts else ''
        if command == '/dbcheck':
            if message.get('from', {}).get('id') not in DIAGNOSTIC_IDS:
                return {'ok': True}
            text = await check_database()
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(
                    f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                    json={'chat_id': message['chat']['id'], 'text': text},
                )
                response.raise_for_status()
                if not response.json().get('ok'):
                    raise HTTPException(status_code=502, detail='Telegram delivery failed')
            return {'ok': True}
        text = 'Привет! Запишись на маникюр к Ярославе (Yasa cat) через удобное мини-приложение ниже 👇'
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                json={
                    'chat_id': message['chat']['id'],
                    'text': text,
                    'reply_markup': {
                        'inline_keyboard': [[
                            {'text': '💅 Записаться на маникюр', 'web_app': {'url': URL or 'https://google.com'}}
                        ]]
                    },
                },
            )
    return {'ok': True}


@app.post('/admin/setup')
async def setup(request: Request):
    supplied = request.headers.get('X-Admin-Secret', '')
    if not SECRET or not secrets.compare_digest(supplied, SECRET):
        raise HTTPException(status_code=403)
    async with httpx.AsyncClient(timeout=8) as client:
        await client.post(
            f'https://api.telegram.org/bot{TOKEN}/setWebhook',
            json={
                'url': URL + '/telegram/webhook',
                'secret_token': SECRET,
                'allowed_updates': ['message'],
            },
        )
        await client.post(
            f'https://api.telegram.org/bot{TOKEN}/setChatMenuButton',
            json={
                'menu_button': {
                    'type': 'web_app',
                    'text': 'Записаться',
                    'web_app': {'url': URL},
                }
            },
        )
    return {'ok': True}
