import asyncio
import os
import re
import uuid
import logging
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from booking_messages import ADMIN_CSS

LOG = logging.getLogger(__name__)
ACTIVE = 'in.(awaiting_receipt,pending_review,payment_not_found,confirmed,cancel_requested,reschedule_requested)'
BUCKET = 'nail-work-photos'

def candidate_starts(day, windows, managed, allowed):
    if not managed:
        return [day.replace(hour=int(t[:2]), minute=int(t[3:])) for t in allowed]
    values = set()
    for start, end in windows:
        point = start
        while point < end:
            if point.date() == day.date():
                values.add(point)
            point += timedelta(minutes=30)
    return sorted(values)

def slot_results(day, minutes, windows, bookings, settings, allowed, now):
    managed = settings.get('schedule_enabled', False)
    result = []
    for start in candidate_starts(day, windows, managed, allowed):
        end = start + timedelta(minutes=minutes)
        reason = ('Запись закрыта' if not settings.get('booking_enabled') else
                  'Прошло' if start <= now else
                  'Не помещается в окно' if managed and not any(a <= start and end <= b for a,b in windows) else
                  'Занято' if any(start < b and end > a for a,b in bookings) else '')
        result.append({'time':start.strftime('%H:%M'),'available':not reason,'reason':reason})
    return result

def install(app, sb, verify, zone, allowed, legacy):
    def stamp(value):
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(zone)

    async def admin(request):
        user = verify(request.headers.get('X-Telegram-Init-Data',''))
        rows = (await sb('GET','/rest/v1/nail_admins',params={'telegram_id':'eq.'+str(user['id']),'is_active':'eq.true','select':'telegram_id'})).json()
        if not rows:
            raise HTTPException(403,'Нет доступа к кабинету.')
        return user

    async def all_rows(table, params):
        result = []
        for offset in range(0,20000,1000):
            rows = (await sb('GET','/rest/v1/'+table,params={**params,'limit':'1000','offset':str(offset)})).json()
            result.extend(rows)
            if len(rows) < 1000:
                return result
        raise HTTPException(503,'Слишком много данных. Обратись к администратору.')

    async def gallery():
        rows = await all_rows('nail_work_photos',{'select':'id,object_path,caption,sort_order,active','order':'sort_order.asc,created_at.asc'})
        hidden = {r['object_path'][7:] for r in rows if r['object_path'].startswith('legacy/') and not r['active']}
        initial = [p for p in legacy if p['id'][7:] not in hidden]
        root = os.environ.get('SUPABASE_URL','').rstrip('/')+'/storage/v1/object/public/'+BUCKET+'/'
        return initial + [{'id':r['id'],'url':root+r['object_path'],'caption':r['caption']} for r in rows if r['active'] and not r['object_path'].startswith('legacy/')]

    @app.get('/admin')
    async def cabinet():
        page = Path('admin.html').read_text(encoding='utf-8')
        page = page.replace('<main>','<main><a href="/" style="display:inline-block;padding:12px 0;color:#77554e">← Выйти из кабинета</a>',1)
        extra = Path('static/master-tools.js').read_text(encoding='utf-8')
        css = '<style>input,select,textarea{box-sizing:border-box;min-width:0;max-width:100%}input[type=date],input[type=time]{width:100%;min-inline-size:0;-webkit-appearance:none}.tabs{grid-template-columns:repeat(5,minmax(0,1fr))}.tabs button{font-size:11px}.calendar-tools{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}.calendar-tools button{padding:9px 0;min-width:0}.calendar-tools .selected{background:#302b29;color:white}.window-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) 44px;gap:8px;margin:10px 0}select{width:100%;padding:12px 4px;font:inherit;background:white;color:#302b29;border:1px solid #e9e2dc;border-radius:10px}.photo-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.photo-grid img{width:100%;height:160px;object-fit:cover;border-radius:10px}.photo-grid .card{margin:0;overflow:hidden}.muted{line-height:1.5}.toolbar{display:flex;gap:8px;justify-content:space-between;align-items:center;margin:12px 0}</style>'
        return HTMLResponse(page.replace('</head>',ADMIN_CSS+css+'</head>',1).replace('</body>','<script>'+extra+'</script></body>'),headers={'Cache-Control':'no-store','X-Cabinet-Version':'master-tools-v1'})

    @app.get('/api/work-photos')
    async def public_photos():
        return {'photos':await gallery()}

    @app.post('/api/admin/work-photos')
    async def upload_photo(request: Request):
        user = await admin(request)
        form = await request.form()
        photo = form.get('file')
        if not photo or not hasattr(photo,'read'):
            raise HTTPException(400,'Выбери фотографию.')
        data = await photo.read(10485761)
        if not data or len(data)>10485760:
            raise HTTPException(413,'Фото должно быть не больше 10 МБ.')
        kind = ('image/jpeg','jpg') if data.startswith(b'\xff\xd8\xff') else ('image/png','png') if data.startswith(b'\x89PNG\r\n\x1a\n') else ('image/webp','webp') if data[:4]==b'RIFF' and data[8:12]==b'WEBP' else None
        if kind is None:
            raise HTTPException(415,'Выбери фото JPG, PNG или WEBP.')
        caption = str(form.get('caption') or '').strip()[:300]
        path = uuid.uuid4().hex+'.'+kind[1]
        await sb('POST','/storage/v1/object/'+BUCKET+'/'+path,content=data,headers={'Content-Type':kind[0]})
        try:
            await sb('POST','/rest/v1/nail_work_photos',json_body={'object_path':path,'caption':caption,'uploaded_by':user['id']})
        except Exception:
            try:
                await sb('DELETE','/storage/v1/object/'+BUCKET,json_body={'prefixes':[path]})
            except Exception:
                LOG.error('Orphan work photo cleanup failed')
            raise
        return {'ok':True}

    @app.delete('/api/admin/work-photos/{photo_id}')
    async def delete_photo(photo_id: str, request: Request):
        user = await admin(request)
        if photo_id.startswith('legacy-'):
            if photo_id not in {p['id'] for p in legacy}:
                raise HTTPException(404,'Фото не найдено.')
            await sb('POST','/rest/v1/nail_work_photos',params={'on_conflict':'object_path'},json_body={'object_path':'legacy/'+photo_id[7:],'active':False,'uploaded_by':user['id']},headers={'Prefer':'resolution=merge-duplicates'})
            return {'ok':True,'file_deleted':False,'legacy':True}
        try:
            photo_id = str(uuid.UUID(photo_id))
        except ValueError:
            raise HTTPException(400,'Некорректный номер фото.')
        rows = (await sb('GET','/rest/v1/nail_work_photos',params={'id':'eq.'+photo_id,'select':'object_path'})).json()
        if not rows:
            raise HTTPException(404,'Фото не найдено.')
        await sb('PATCH','/rest/v1/nail_work_photos',params={'id':'eq.'+photo_id},json_body={'active':False})
        deleted = True
        try:
            await sb('DELETE','/storage/v1/object/'+BUCKET,json_body={'prefixes':[rows[0]['object_path']]})
        except Exception:
            deleted = False
            LOG.error('Work photo hidden but storage deletion failed')
        return {'ok':True,'file_deleted':deleted}

    @app.get('/api/admin/config')
    async def admin_config(request: Request):
        await admin(request)
        services,addons,settings,windows,photos = await asyncio.gather(
            all_rows('nail_services',{'select':'*','order':'sort_order.asc,id.asc'}),
            all_rows('nail_addons',{'select':'*','order':'sort_order.asc,id.asc'}),
            sb('GET','/rest/v1/nail_settings',params={'id':'eq.1','select':'*'}),
            all_rows('nail_availability',{'enabled':'eq.true','ends_at':'gt.'+datetime.now(zone).replace(hour=0,minute=0,second=0,microsecond=0).isoformat(),'select':'*','order':'starts_at.asc'}), gallery())
        return {'services':services,'addons':addons,'settings':next(iter(settings.json()),{}),'availability':windows,'photos':photos}

    async def save_catalog(request, group):
        await admin(request)
        data = await request.json()
        items = data.get(group)
        if not isinstance(items,list) or not 1 <= len(items) <= 200:
            raise HTTPException(400,'Укажи от 1 до 200 позиций.')
        rows=[]
        ids=set()
        for index,item in enumerate(items):
            if not isinstance(item,dict):
                raise HTTPException(400,'Некорректная услуга.')
            ident=item.get('id','')
            name=str(item.get('name') or '').strip()
            price=item.get('price'); minutes=item.get('minutes')
            if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident) or ident in ids:
                raise HTTPException(400,'Некорректный или повторяющийся номер услуги.')
            ids.add(ident)
            if not 1<=len(name)<=150:
                raise HTTPException(400,'Название должно содержать от 1 до 150 символов.')
            if type(price) is not int or not 0<=price<=1000000 or type(minutes) is not int or not (5 if group=='services' else 0)<=minutes<=720:
                raise HTTPException(400,'Проверь цену и длительность: цена от 0 до 1 000 000 ₽, длительность до 720 минут; для услуги — минимум 5 минут.')
            if type(item.get('active')) is not bool:
                raise HTTPException(400,'Некорректный статус услуги.')
            row={'id':ident,'name':name,'price':price,'minutes':minutes,'active':item['active'],'sort_order':index,'updated_at':datetime.now(zone).isoformat()}
            if group=='services':
                row['description']=str(item.get('description') or '')[:2000]
            rows.append(row)
        await sb('POST','/rest/v1/nail_'+group,params={'on_conflict':'id'},json_body=rows,headers={'Prefer':'resolution=merge-duplicates'})
        return {'ok':True}

    @app.put('/api/admin/services')
    async def services(request: Request):
        return await save_catalog(request,'services')

    @app.put('/api/admin/addons')
    async def addons(request: Request):
        return await save_catalog(request,'addons')

    @app.post('/api/admin/schedule')
    async def schedule(request: Request):
        user=await admin(request)
        body=await request.json()
        days=body.get('days')
        if not isinstance(days,list) or not 1<=len(days)<=62:
            raise HTTPException(400,'Выбери от 1 до 62 дней.')
        try:
            await sb('POST','/rest/v1/rpc/save_nail_schedule',json_body={'p_actor':user['id'],'p_days':days})
        except HTTPException as error:
            if error.status_code==502:
                raise HTTPException(400,error.detail)
            raise
        return {'ok':True}

    async def context(body, start, end):
        service_id=body.get('service_id'); addon_ids=body.get('addon_ids') or []
        if not isinstance(service_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',service_id):
            raise HTTPException(400,'Выбери услугу.')
        if not isinstance(addon_ids,list) or len(addon_ids)>20 or any(not isinstance(x,str) for x in addon_ids):
            raise HTTPException(400,'Некорректные дополнения.')
        sr,ar,st,br,wr=await asyncio.gather(
            sb('GET','/rest/v1/nail_services',params={'id':'eq.'+service_id,'active':'eq.true','select':'minutes'}),
            sb('GET','/rest/v1/nail_addons',params={'active':'eq.true','select':'id,minutes'}),
            sb('GET','/rest/v1/nail_settings',params={'id':'eq.1','select':'booking_enabled,schedule_enabled'}),
            all_rows('nail_bookings',{'status':ACTIVE,'starts_at':'lt.'+(end+timedelta(days=11)).isoformat(),'ends_at':'gt.'+start.isoformat(),'select':'starts_at,ends_at','order':'starts_at.asc,id.asc'}),
            all_rows('nail_availability',{'enabled':'eq.true','starts_at':'lt.'+end.isoformat(),'ends_at':'gt.'+start.isoformat(),'select':'starts_at,ends_at','order':'starts_at.asc,id.asc'}))
        if not sr.json():
            raise HTTPException(400,'Услуга недоступна.')
        selected=set(addon_ids)
        if not selected.issubset({a['id'] for a in ar.json()}):
            raise HTTPException(400,'Дополнение недоступно. Выбери услугу заново.')
        minutes=sr.json()[0]['minutes']+sum(a['minutes'] for a in ar.json() if a['id'] in selected)
        intervals=[(stamp(x['starts_at']),stamp(x['ends_at'])) for x in br]
        windows=[(stamp(x['starts_at']),stamp(x['ends_at'])) for x in wr]
        return minutes,windows,intervals,next(iter(st.json()),{})

    @app.post('/api/booking-times')
    async def times(request: Request):
        verify(request.headers.get('X-Telegram-Init-Data',''))
        body=await request.json()
        try:
            day=datetime.strptime(str(body.get('date','')),'%Y-%m-%d').replace(tzinfo=zone)
        except ValueError:
            raise HTTPException(400,'Выбери корректную дату.')
        minutes,windows,intervals,settings=await context(body,day,day+timedelta(days=1))
        return {'slots':slot_results(day,minutes,windows,intervals,settings,allowed,datetime.now(zone)),'minutes':minutes}

    @app.post('/api/booking-availability')
    async def month(request: Request):
        verify(request.headers.get('X-Telegram-Init-Data',''))
        body=await request.json()
        try:
            day=datetime.strptime(str(body.get('month','')),'%Y-%m').replace(tzinfo=zone)
        except ValueError:
            raise HTTPException(400,'Выбери корректный месяц.')
        end=(day+timedelta(days=32)).replace(day=1)
        minutes,windows,intervals,settings=await context(body,day,end)
        days={}; now=datetime.now(zone)
        while day<end:
            days[day.strftime('%Y-%m-%d')]={'available':any(s['available'] for s in slot_results(day,minutes,windows,intervals,settings,allowed,now))}
            day+=timedelta(days=1)
        return {'days':days,'minutes':minutes}

    @app.post('/api/bookings')
    async def create_booking(request: Request):
        user=verify(request.headers.get('X-Telegram-Init-Data',''))
        body=await request.json()
        date=str(body.get('date') or ''); time=str(body.get('time') or '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d',time):
            raise HTTPException(400,'Выбери дату и время.')
        try:
            start=datetime.strptime(date+' '+time,'%Y-%m-%d %H:%M').replace(tzinfo=zone)
        except ValueError:
            raise HTTPException(400,'Выбери корректную дату.')
        minutes,windows,intervals,settings=await context(body,start.replace(hour=0,minute=0),start.replace(hour=0,minute=0)+timedelta(days=1))
        slots=slot_results(start.replace(hour=0,minute=0),minutes,windows,intervals,settings,allowed,datetime.now(zone))
        if not any(s['time']==time and s['available'] for s in slots):
            raise HTTPException(409,'Это время больше недоступно. Выбери другое.')
        name=str(body.get('name') or '').strip()
        if not 1<=len(name)<=150:
            raise HTTPException(400,'Укажи имя длиной до 150 символов.')
        try:
            response=await sb('POST','/rest/v1/rpc/create_nail_booking',json_body={'p_telegram_id':user['id'],'p_service_id':body['service_id'],'p_addon_ids':body.get('addon_ids') or [],'p_starts_at':start.isoformat(),'p_client_name':name,'p_telegram_username':str(body.get('tg') or '').strip()[:100],'p_client_comment':str(body.get('comment') or '').strip()[:2000]},headers={'Prefer':'return=representation'})
        except HTTPException as error:
            if error.status_code==502:
                raise HTTPException(400,error.detail)
            raise
        return response.json()
