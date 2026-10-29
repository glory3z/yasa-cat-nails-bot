import os
import secrets
import json
import uuid
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Header, BackgroundTasks
from fastapi.responses import HTMLResponse, Response
import httpx
from work_photos import replace_work_photos
from booking_availability import install, CLIENT_SCRIPT
from booking_messages import ADMIN_CSS
from booking_runtime import BookingRuntime

TOKEN = os.environ.get('BOT_TOKEN', '')
SECRET = os.environ.get('WEBHOOK_SECRET', '')
URL = os.environ.get('APP_URL', '').rstrip('/')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
MASTER_ID = 7689570674
DIAGNOSTIC_IDS = {MASTER_ID}
MOSCOW = timezone(timedelta(hours=3))
ALLOWED_TIMES = ('11:00', '13:30', '16:00', '18:30', '20:30')
RECEIPT_TYPES = {'image/jpeg':'jpg','image/png':'png','image/webp':'webp','application/pdf':'pdf'}
MAX_RECEIPT_SIZE = 10 * 1024 * 1024
RECEIPT_BUCKET = 'nail-private-receipts'
BUILD = 'receipt-flow-v3-single-admin'
app = FastAPI()
HTTP_CLIENT = None
TELEGRAM_SETUP_TASK = None

ADMIN_ACCESS_JS = r'''
(function(){
 const style=document.createElement('style');
 style.textContent='#admin-open{display:none!important}html.sole-admin-access #admin-open{display:block!important}';
 document.head.appendChild(style);
 fetch('/api/me',{headers:{'X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''}})
 .then(async response=>{if(!response.ok)return;const me=await response.json();document.documentElement.classList.toggle('sole-admin-access',me.is_admin===true);})
 .catch(()=>document.documentElement.classList.remove('sole-admin-access'));
})();
'''

def get_http():
    global HTTP_CLIENT
    if HTTP_CLIENT is None:
        HTTP_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), limits=httpx.Limits(max_keepalive_connections=20, max_connections=50))
    return HTTP_CLIENT

with open('index.html', encoding='utf-8') as f:
    HTML_PAGE = replace_work_photos(f.read())
with open('static/client-enhance.js', encoding='utf-8') as f:
    CLIENT_JS = ADMIN_ACCESS_JS + '\n' + f.read() + '\n' + CLIENT_SCRIPT
with open('admin.html', encoding='utf-8') as f:
    ADMIN_PAGE = f.read().replace('<main>', '<main><a href="/" style="display:inline-block;margin-bottom:16px;color:#77554e;font-size:16px;text-decoration:none;padding:10px 0">← Выйти из кабинета</a>', 1).replace('</head>',ADMIN_CSS+'</head>',1)

def sb_headers(content_type=None):
    headers = {'apikey':SUPABASE_KEY,'Authorization':f'Bearer {SUPABASE_KEY}'}
    if content_type:
        headers['Content-Type'] = content_type
    return headers

async def sb_request(method, path, json_body=None, content=None, headers=None, params=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, 'База данных не настроена.')
    request_headers = sb_headers('application/json' if json_body is not None else None)
    if headers:
        request_headers.update(headers)
    try:
        response = await get_http().request(method, SUPABASE_URL+path, json=json_body, content=content, headers=request_headers, params=params)
    except httpx.HTTPError:
        raise HTTPException(503, 'Не удалось связаться с базой данных.')
    if response.status_code >= 400:
        if response.status_code == 409:
            raise HTTPException(409, 'Это время уже занято. Выбери другое.')
        try:
            data = response.json()
            message = data.get('message') or data.get('error') or 'Ошибка базы данных.'
        except ValueError:
            message = 'Ошибка базы данных.'
        raise HTTPException(502, message)
    return response

def verify_init_data(raw_init_data):
    if not raw_init_data:
        raise HTTPException(401, 'Открой приложение через Telegram.')
    pairs = dict(parse_qsl(raw_init_data, keep_blank_values=True))
    received_hash = pairs.pop('hash', '')
    if not received_hash:
        raise HTTPException(401, 'Некорректные данные Telegram.')
    data_check_string = '\n'.join(f'{key}={value}' for key,value in sorted(pairs.items()))
    secret_key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(calculated_hash, received_hash):
        raise HTTPException(401, 'Не удалось подтвердить Telegram-аккаунт.')
    try:
        user = json.loads(pairs.get('user','{}'))
    except ValueError:
        raise HTTPException(401, 'Некорректные данные Telegram.')
    if not user.get('id') or user.get('is_bot'):
        raise HTTPException(401, 'Не удалось определить личный Telegram-аккаунт.')
    return user

class SingleAdminRuntime(BookingRuntime):
    async def is_admin(self, telegram_id):
        if not isinstance(telegram_id,int) or telegram_id != MASTER_ID:
            return False
        rows = (await self.sb('GET','/rest/v1/nail_admins',params={'telegram_id':'eq.'+str(MASTER_ID),'is_active':'eq.true','select':'telegram_id'})).json()
        return bool(rows)

async def require_admin(raw_init_data):
    user = verify_init_data(raw_init_data)
    if not await BOT.is_admin(user['id']):
        raise HTTPException(403, 'Нет доступа к кабинету.')
    return user

install(app, sb_request, verify_init_data, MOSCOW, ALLOWED_TIMES)
BOT = SingleAdminRuntime(sb_request,get_http,TOKEN,MASTER_ID,URL,MOSCOW,RECEIPT_BUCKET)

@app.on_event('startup')
async def enable_telegram_buttons():
    global TELEGRAM_SETUP_TASK
    TELEGRAM_SETUP_TASK = asyncio.create_task(BOT.configure(SECRET))

async def send_telegram(chat_id, text):
    if not TOKEN or not chat_id:
        return
    try:
        await BOT.telegram('sendMessage',{'chat_id':chat_id,'text':text})
    except Exception:
        logging.getLogger(__name__).error('Telegram message delivery failed')

@app.get('/')
def home():
    return HTMLResponse(HTML_PAGE.replace('</body>','<script src="/static/client-enhance.js"></script></body>'),headers={'Cache-Control':'no-store'})

@app.get('/static/client-enhance.js')
def client_enhancer():
    return Response(CLIENT_JS,media_type='application/javascript',headers={'Cache-Control':'no-store'})

@app.get('/admin')
def admin_page():
    return HTMLResponse(ADMIN_PAGE,headers={'Cache-Control':'no-store'})

@app.get('/health')
@app.head('/health')
def health():
    return {'ok':True,'build':BUILD,'telegram':dict(BOT.runtime)}

@app.get('/api/config')
async def api_config():
    services,addons,settings = await asyncio.gather(sb_request('GET','/rest/v1/nail_services?active=eq.true&order=sort_order.asc'),sb_request('GET','/rest/v1/nail_addons?active=eq.true&order=sort_order.asc'),sb_request('GET','/rest/v1/nail_settings?id=eq.1'))
    rows = settings.json()
    s = rows[0] if rows else {}
    return {'services':services.json(),'addons':addons.json(),'settings':{'deposit_rub':s.get('deposit_rub',500),'payment_phone':s.get('payment_phone',''),'payment_bank':s.get('payment_bank',''),'cancellation_terms':s.get('cancellation_terms',''),'reschedule_terms':s.get('reschedule_terms',''),'booking_enabled':s.get('booking_enabled',False)}}

@app.get('/api/me')
async def api_me(request: Request):
    user = verify_init_data(request.headers.get('X-Telegram-Init-Data',''))
    return {'telegram_id':user['id'],'is_admin':await BOT.is_admin(user['id'])}

@app.post('/api/bookings')
async def create_booking(request: Request):
    user = verify_init_data(request.headers.get('X-Telegram-Init-Data',''))
    body = await request.json()
    service_id = str(body.get('service_id') or '')
    addon_ids = body.get('addon_ids') or []
    booking_date = str(body.get('date') or '')
    booking_time = str(body.get('time') or '')
    client_name = str(body.get('name') or '').strip()
    telegram_username = str(body.get('tg') or '').strip()
    client_comment = str(body.get('comment') or '').strip()[:2000]
    if not service_id or not booking_date or not booking_time or not client_name:
        raise HTTPException(400,'Заполни имя, услугу и время.')
    if booking_time not in ALLOWED_TIMES:
        raise HTTPException(400,'Выбери время из списка.')
    try:
        starts_at = datetime.strptime(f'{booking_date} {booking_time}','%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW)
    except ValueError:
        raise HTTPException(400,'Некорректная дата или время.')
    if starts_at < datetime.now(MOSCOW):
        raise HTTPException(400,'Нельзя записаться на прошедшее время.')
    response = await sb_request('POST','/rest/v1/rpc/create_nail_booking',json_body={'p_telegram_id':user['id'],'p_service_id':service_id,'p_addon_ids':addon_ids,'p_starts_at':starts_at.isoformat(),'p_client_name':client_name,'p_telegram_username':telegram_username,'p_client_comment':client_comment},headers={'Prefer':'return=representation'})
    return response.json()

@app.post('/api/bookings/{booking_id}/receipt')
async def upload_receipt(booking_id: str, file: UploadFile = File(...), comment: str = Form(''), x_telegram_init_data: str = Header(default='')):
    user = verify_init_data(x_telegram_init_data)
    booking = await BOT.booking(booking_id)
    if booking['telegram_id'] != user['id']:
        raise HTTPException(403,'Это не твоя запись.')
    if booking['status'] != 'awaiting_receipt':
        raise HTTPException(409,'Чек уже отправлен или запись закрыта.')
    content = await file.read()
    if not content:
        raise HTTPException(400,'Файл пустой.')
    if len(content) > MAX_RECEIPT_SIZE:
        raise HTTPException(413,'Файл больше 10 МБ.')
    extension = RECEIPT_TYPES.get(file.content_type)
    if not extension:
        raise HTTPException(415,'Поддерживаются JPG, PNG, WEBP или PDF.')
    object_path = f'{booking_id}/{uuid.uuid4().hex}.{extension}'
    await sb_request('POST',f'/storage/v1/object/{RECEIPT_BUCKET}/{object_path}',content=content,headers={'Content-Type':file.content_type})
    await sb_request('POST','/rest/v1/nail_uploaded_receipts',json_body={'booking_id':booking_id,'uploader_telegram_id':user['id'],'object_path':object_path,'original_filename':file.filename or 'receipt','mime_type':file.content_type,'size_bytes':len(content)})
    client_comment = booking.get('client_comment') or ''
    if comment.strip():
        client_comment = (client_comment+'\nКомментарий к чеку: '+comment.strip()).strip()[:2000]
    rows=(await sb_request('PATCH',f'/rest/v1/nail_bookings?id=eq.{booking_id}&status=eq.awaiting_receipt&select=*',json_body={'status':'pending_review','client_comment':client_comment,'updated_at':datetime.now(MOSCOW).isoformat()},headers={'Prefer':'return=representation'})).json()
    if not rows:
        raise HTTPException(409,'Статус записи изменился. Проверь «Мои записи».')
    asyncio.create_task(BOT.notify(rows[0],'Чек получен — нужна проверка'))
    return {'ok':True}

@app.get('/api/bookings/mine')
async def my_bookings(x_telegram_init_data: str = Header(default='')):
    user = verify_init_data(x_telegram_init_data)
    return (await sb_request('GET',f"/rest/v1/nail_bookings?telegram_id=eq.{user['id']}&select=*&order=created_at.desc&limit=20")).json()

@app.post('/api/bookings/{booking_id}/cancel')
async def cancel_booking(booking_id: str,x_telegram_init_data: str = Header(default='')):
    user = verify_init_data(x_telegram_init_data)
    b = await BOT.booking(booking_id)
    if b['telegram_id'] != user['id']:
        raise HTTPException(404,'Запись не найдена.')
    if b['status'] not in ('awaiting_receipt','pending_review'):
        raise HTTPException(409,'Эту запись уже нельзя отменить.')
    rows=(await sb_request('PATCH',f'/rest/v1/nail_bookings?id=eq.{booking_id}&status=eq.{b["status"]}&select=*',json_body={'status':'cancelled','updated_at':datetime.now(MOSCOW).isoformat()},headers={'Prefer':'return=representation'})).json()
    if not rows:
        raise HTTPException(409,'Статус записи уже изменился.')
    asyncio.create_task(BOT.notify(rows[0],'Запись отменена клиентом'))
    return {'ok':True}

@app.get('/api/admin/bookings')
async def admin_bookings(request: Request,status: str = ''):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    query = '/rest/v1/nail_bookings?select=*&order=created_at.desc&limit=100'
    if status:
        query += f'&status=eq.{status}'
    bookings = (await sb_request('GET',query)).json()
    receipt_map = {}
    if bookings:
        ids = ','.join(row['id'] for row in bookings)
        rows = (await sb_request('GET',f'/rest/v1/nail_uploaded_receipts?booking_id=in.({ids})&select=*')).json()
        for receipt in rows:
            receipt_map.setdefault(receipt['booking_id'],[]).append(receipt)
    return {'bookings':bookings,'receipts':receipt_map,'admin_id':user['id']}

@app.get('/api/admin/receipt/{receipt_id}')
async def admin_receipt(receipt_id: str,request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    rows = (await sb_request('GET',f'/rest/v1/nail_uploaded_receipts?id=eq.{receipt_id}&select=*')).json()
    if not rows:
        raise HTTPException(404,'Чек не найден.')
    response = await sb_request('POST',f"/storage/v1/object/sign/{RECEIPT_BUCKET}/{rows[0]['object_path']}",json_body={'expiresIn':600})
    return {'url':SUPABASE_URL+'/storage/v1'+response.json().get('signedURL','')}

@app.post('/api/admin/bookings/{booking_id}/action')
async def admin_booking_action(booking_id: str,request: Request):
    user = await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    body = await request.json()
    b = await BOT.perform(user['id'],booking_id,body.get('action'))
    asyncio.create_task(BOT.notify(b,'Статус записи обновлён'))
    return {'ok':True}

@app.get('/api/admin/config')
async def admin_config(request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    services,addons,settings,availability = await asyncio.gather(sb_request('GET','/rest/v1/nail_services?order=sort_order.asc'),sb_request('GET','/rest/v1/nail_addons?order=sort_order.asc'),sb_request('GET','/rest/v1/nail_settings?id=eq.1'),sb_request('GET','/rest/v1/nail_availability?enabled=eq.true&order=starts_at.asc&limit=200'))
    rows = settings.json()
    return {'services':services.json(),'addons':addons.json(),'settings':rows[0] if rows else {},'availability':availability.json()}

@app.put('/api/admin/settings')
async def update_settings(request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    body = await request.json()
    updates = {key:body[key] for key in ('deposit_rub','payment_phone','payment_bank','cancellation_terms','reschedule_terms','booking_enabled') if key in body}
    updates['updated_at'] = datetime.now(MOSCOW).isoformat()
    await sb_request('PATCH','/rest/v1/nail_settings?id=eq.1',json_body=updates)
    return {'ok':True}

@app.put('/api/admin/services')
async def update_services(request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    rows = (await request.json()).get('services') or []
    for row in rows:
        row['updated_at'] = datetime.now(MOSCOW).isoformat()
    await sb_request('POST','/rest/v1/nail_services?on_conflict=id',json_body=rows,headers={'Prefer':'resolution=merge-duplicates'})
    return {'ok':True}

@app.put('/api/admin/addons')
async def update_addons(request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    rows = (await request.json()).get('addons') or []
    for row in rows:
        row['updated_at'] = datetime.now(MOSCOW).isoformat()
    await sb_request('POST','/rest/v1/nail_addons?on_conflict=id',json_body=rows,headers={'Prefer':'resolution=merge-duplicates'})
    return {'ok':True}

@app.post('/api/admin/availability')
async def add_availability(request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    body = await request.json()
    slot_date = str(body.get('date') or '')
    slot_time = str(body.get('time') or '')
    minutes = int(body.get('minutes') or 60)
    starts_at = datetime.strptime(f'{slot_date} {slot_time}','%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW)
    ends_at = starts_at+timedelta(minutes=minutes)
    await sb_request('POST','/rest/v1/nail_availability',json_body={'starts_at':starts_at.isoformat(),'ends_at':ends_at.isoformat(),'enabled':True},headers={'Prefer':'return=representation'})
    return {'ok':True}

@app.delete('/api/admin/availability/{slot_id}')
async def delete_availability(slot_id: str,request: Request):
    await require_admin(request.headers.get('X-Telegram-Init-Data',''))
    await sb_request('DELETE',f'/rest/v1/nail_availability?id=eq.{slot_id}')
    return {'ok':True}

async def check_database():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 'Не заданы SUPABASE_URL или SUPABASE_SECRET_KEY в Render.'
    if SUPABASE_URL != 'https://woblvdrbcriokqmqehuf.supabase.co':
        return 'SUPABASE_URL не совпадает с проектом записи. Проверьте Environment в Render.'
    try:
        for table,column in [('nail_admins','telegram_id'),('nail_availability','id'),('nail_bookings','id'),('nail_receipts','id')]:
            params = {'select':column,'limit':'1'}
            if table == 'nail_admins':
                params['telegram_id'] = f'eq.{MASTER_ID}'
                params['is_active'] = 'eq.true'
            response = await get_http().get(SUPABASE_URL+'/rest/v1/'+table,headers={'apikey':SUPABASE_KEY,'Cache-Control':'no-store'},params=params)
            if response.status_code != 200:
                return f'Нет доступа к {table}: HTTP {response.status_code}. Проверьте серверный секретный ключ и права таблиц.'
            data = response.json()
            if not isinstance(data,list):
                return 'База вернула неожиданный формат ответа.'
            if table == 'nail_admins' and not any(row.get('telegram_id') == MASTER_ID for row in data):
                return 'Соединение есть, но активный ID администратора не найден в nail_admins.'
    except (httpx.HTTPError,ValueError):
        return 'Не удалось связаться с Supabase. Попробуйте позже; секретные значения не выводятся.'
    return '✅ Supabase подключён. Основные таблицы доступны, активный ID администратора найден.'

@app.post('/telegram/webhook')
async def webhook(request: Request, background_tasks: BackgroundTasks):
    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
    if not SECRET or not secrets.compare_digest(supplied,SECRET):
        logging.getLogger(__name__).warning('Telegram webhook rejected: secret mismatch')
        raise HTTPException(403)
    update = await request.json()
    if update.get('callback_query'):
        BOT.seen('last_callback_seen')
        background_tasks.add_task(BOT.callback,update['callback_query'])
        return {'ok':True}
    message = update.get('message')
    if message and message.get('chat',{}).get('type') == 'private':
        sender = message.get('from',{})
        if sender.get('is_bot'):
            return {'ok':True}
        parts = str(message.get('text') or '').split(maxsplit=1)
        command = parts[0].split('@',1)[0] if parts else ''
        if command in ('/start','/id'):
            BOT.seen('last_start_seen')
            logging.getLogger(__name__).warning('Telegram command received: %s',command)
            return BOT.webhook_reply(message['chat']['id'],sender,command)
        if command == '/dbcheck':
            if sender.get('id') not in DIAGNOSTIC_IDS:
                return {'ok':True}
            await send_telegram(message['chat']['id'],await check_database())
            return {'ok':True}
    return {'ok':True}

@app.post('/admin/setup')
async def setup(request: Request):
    supplied = request.headers.get('X-Admin-Secret','')
    if not SECRET or not secrets.compare_digest(supplied,SECRET):
        raise HTTPException(403)
    await BOT.configure(SECRET)
    if BOT.runtime['webhook_state'] != 'configured':
        raise HTTPException(502,'Регистрация вебхука не подтверждена. Проверь настройки сервера.')
    await BOT.telegram('setChatMenuButton',{'menu_button':{'type':'web_app','text':'Записаться','web_app':{'url':URL}}})
    return {'ok':True,'telegram':dict(BOT.runtime)}
