import asyncio
import base64
import hashlib
import re
from datetime import datetime, timedelta
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from work_photos import replace_work_photos

CLIENT_SCRIPT = r'''
(function(){
'use strict';
let sequence=0;
const home=document.getElementById('home');
if(home&&!document.getElementById('admin-open')){const button=document.createElement('button');button.id='admin-open';button.className='secondary';button.style.marginTop='10px';button.textContent='Кабинет мастера';button.onclick=()=>window.location.assign('/admin');home.appendChild(button);}
const box=document.getElementById('times'),hint=document.getElementById('time-hint'),next=document.getElementById('time-next');
const style=document.createElement('style');style.textContent='.time:disabled{opacity:.45;background:#eee9e4;color:#746c66}.time small{display:block;font-size:10px;margin-top:3px}';document.head.appendChild(style);
let controller;
renderTimes=async function(){
 const id=++sequence;
 controller?.abort();controller=new AbortController();
 next.disabled=true;
 if(!state.date||!state.service){box.replaceChildren();hint.textContent='Выбери дату в календаре.';return;}
 const selectedDate=state.date;
 box.replaceChildren();hint.textContent='Проверяем свободное время…';
 try{
  const response=await fetch('/api/booking-times',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''},body:JSON.stringify({date:selectedDate,service_id:state.service.id,addon_ids:state.addons.map(a=>a.id)})});
  const data=await response.json();
  if(id!==sequence)return;
  if(!response.ok)throw Error(data.detail||'Не удалось проверить время');
  if(!data.slots.some(s=>s.available&&s.time===state.time))state.time=null;
  hint.textContent=prettyDate(selectedDate)+' · время московское'+(data.slots.some(s=>s.available)?'':' · свободного времени нет');
  box.replaceChildren();
  data.slots.forEach(slot=>{
   const button=document.createElement('button');button.type='button';button.className='time'+(state.time===slot.time?' selected':'');button.dataset.time=slot.time;button.disabled=!slot.available;button.textContent=slot.time;
   if(!slot.available){const label=document.createElement('small');label.textContent=slot.reason;button.appendChild(label);}
   button.onclick=()=>{if(id!==sequence||button.disabled)return;state.time=slot.time;box.querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b===button));next.disabled=false;};box.appendChild(button);
  });
  next.disabled=!state.time;
 }catch(error){if(id!==sequence)return;state.time=null;box.replaceChildren();hint.textContent=error.message+' — нажми, чтобы повторить';next.disabled=true;}
};
hint.addEventListener('click',()=>{if(!document.getElementById('datetime').hidden)renderTimes();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!document.getElementById('datetime').hidden)renderTimes();});
if(!document.getElementById('datetime').hidden)renderTimes();
})();
'''

def install(app, sb_request, verify_init_data, moscow, allowed_times):
    photo_assets = {}
    with open('index.html', encoding='utf-8') as file:
        page = replace_work_photos(file.read())

    def external_image(match):
        mime, encoded = match.group(1), match.group(2)
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return match.group(0)
        digest = hashlib.sha256(data).hexdigest()
        photo_assets[digest] = (data, mime)
        return 'src="/static/work-photo/' + digest + '"'

    page = re.sub(r'src=[\"\']data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)[\"\']', external_image, page)
    def image_options(match):
        tag = match.group(0)
        if 'loading=' not in tag:
            tag = tag[:-1] + ' loading="lazy">'
        if 'decoding=' not in tag:
            tag = tag[:-1] + ' decoding="async">'
        return tag
    page = re.sub(r'<img\b[^>]*>', image_options, page)
    page = page.replace('</body>', '<script src="/static/client-enhance.js"></script></body>')

    @app.get('/static/work-photo/{digest}')
    async def work_photo(digest: str):
        asset = photo_assets.get(digest)
        if asset is None:
            raise HTTPException(404, 'Фото не найдено.')
        data, mime = asset
        return Response(data, media_type=mime, headers={'Cache-Control':'public, max-age=31536000, immutable'})

    @app.middleware('http')
    async def lightweight_home(request: Request, call_next):
        if request.method == 'GET' and request.url.path == '/':
            return HTMLResponse(page, headers={'Cache-Control':'no-store'})
        return await call_next(request)

    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=4)

    @app.post('/api/booking-times')
    async def booking_times(request: Request):
        verify_init_data(request.headers.get('X-Telegram-Init-Data', ''))
        body = await request.json()
        try:
            day = datetime.strptime(str(body.get('date', '')), '%Y-%m-%d').replace(tzinfo=moscow)
        except ValueError:
            raise HTTPException(400, 'Некорректная дата.')
        addon_ids = body.get('addon_ids', [])
        if not isinstance(addon_ids, list) or len(addon_ids) > 20 or any(not isinstance(x, str) for x in addon_ids):
            raise HTTPException(400, 'Некорректные дополнения.')
        service_id = body.get('service_id')
        if not isinstance(service_id, str):
            raise HTTPException(400, 'Выбери услугу.')
        service_response, addon_response, settings_response = await asyncio.gather(
            sb_request('GET', '/rest/v1/nail_services', params={'id':'eq.'+service_id,'active':'eq.true','select':'id,minutes'}),
            sb_request('GET', '/rest/v1/nail_addons', params={'active':'eq.true','select':'id,minutes'}),
            sb_request('GET', '/rest/v1/nail_settings', params={'id':'eq.1','select':'booking_enabled'}),
        )
        services = service_response.json()
        if not services:
            raise HTTPException(400, 'Услуга недоступна.')
        addons = addon_response.json()
        selected = set(addon_ids)
        if not selected.issubset({a['id'] for a in addons}):
            raise HTTPException(400, 'Дополнение недоступно. Выбери услугу заново.')
        minutes = services[0]['minutes'] + sum(a['minutes'] for a in addons if a['id'] in selected)
        settings = settings_response.json()
        enabled = bool(settings and settings[0]['booking_enabled'])
        bookings = (await sb_request('GET', '/rest/v1/nail_bookings', params={'select':'starts_at,ends_at','status':'in.(awaiting_receipt,pending_review,payment_not_found,confirmed,cancel_requested,reschedule_requested)','starts_at':'lt.'+(day+timedelta(days=1,minutes=minutes)).isoformat(),'ends_at':'gt.'+day.isoformat()})).json()
        intervals = [(datetime.fromisoformat(b['starts_at'].replace('Z','+00:00')),datetime.fromisoformat(b['ends_at'].replace('Z','+00:00'))) for b in bookings]
        now = datetime.now(moscow)
        slots = []
        for time in allowed_times:
            hour, minute = map(int,time.split(':'))
            start = day.replace(hour=hour,minute=minute)
            end = start+timedelta(minutes=minutes)
            reason = 'Запись закрыта' if not enabled else 'Прошло' if start <= now else 'Занято' if any(start < b and end > a for a,b in intervals) else ''
            slots.append({'time':time,'available':not reason,'reason':reason})
        return {'slots':slots,'minutes':minutes}

    @app.post('/api/booking-availability')
    async def booking_availability(request: Request):
        verify_init_data(request.headers.get('X-Telegram-Init-Data', ''))
        body = await request.json()
        try:
            month = datetime.strptime(str(body.get('month', '')), '%Y-%m')
        except ValueError:
            raise HTTPException(400, 'Некорректный месяц.')
        addon_ids = body.get('addon_ids', [])
        if not isinstance(addon_ids, list) or len(addon_ids) > 20 or any(not isinstance(x, str) for x in addon_ids):
            raise HTTPException(400, 'Некорректные дополнения.')
        service_id = body.get('service_id')
        if not isinstance(service_id, str):
            raise HTTPException(400, 'Выбери услугу.')
        service_response, addon_response, settings_response = await asyncio.gather(
            sb_request('GET', '/rest/v1/nail_services', params={'id':'eq.'+service_id,'active':'eq.true','select':'id,minutes'}),
            sb_request('GET', '/rest/v1/nail_addons', params={'active':'eq.true','select':'id,minutes'}),
            sb_request('GET', '/rest/v1/nail_settings', params={'id':'eq.1','select':'booking_enabled'}),
        )
        services = service_response.json()
        if not services:
            raise HTTPException(400, 'Услуга недоступна.')
        addons = addon_response.json()
        selected = set(addon_ids)
        if not selected.issubset({a['id'] for a in addons}):
            raise HTTPException(400, 'Дополнение недоступно. Выбери услугу заново.')
        minutes = services[0]['minutes'] + sum(a['minutes'] for a in addons if a['id'] in selected)
        settings = settings_response.json()
        enabled = bool(settings and settings[0]['booking_enabled'])
        month_start = month.replace(tzinfo=moscow)
        month_end = (month_start + timedelta(days=32)).replace(day=1)
        bookings = (await sb_request('GET', '/rest/v1/nail_bookings', params={'select':'starts_at,ends_at','status':'in.(awaiting_receipt,pending_review,payment_not_found,confirmed,cancel_requested,reschedule_requested)','starts_at':'lt.'+(month_end+timedelta(minutes=minutes)).isoformat(),'ends_at':'gt.'+month_start.isoformat()})).json()
        intervals = [(datetime.fromisoformat(b['starts_at'].replace('Z','+00:00')),datetime.fromisoformat(b['ends_at'].replace('Z','+00:00'))) for b in bookings]
        now = datetime.now(moscow)
        days = {}
        day = month_start
        while day < month_end:
            has_free_slot = False
            for time in allowed_times:
                hour, minute = map(int,time.split(':'))
                start = day.replace(hour=hour,minute=minute)
                end = start+timedelta(minutes=minutes)
                if enabled and start > now and not any(start < b_end and end > b_start for b_start,b_end in intervals):
                    has_free_slot = True
                    break
            days[day.strftime('%Y-%m-%d')] = {'available':has_free_slot}
            day += timedelta(days=1)
        return {'days':days,'minutes':minutes}
