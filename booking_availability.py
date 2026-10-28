from datetime import datetime, timedelta
from fastapi import HTTPException, Request

CLIENT_SCRIPT = r'''
(function(){
'use strict';
let sequence=0;
const box=document.getElementById('times'),hint=document.getElementById('time-hint'),next=document.getElementById('time-next');
const style=document.createElement('style');style.textContent='.time:disabled{opacity:.45;background:#eee9e4;color:#746c66}.time small{display:block;font-size:10px;margin-top:3px}';document.head.appendChild(style);
renderTimes=async function(){
 const id=++sequence;
 next.disabled=true;
 if(!state.date||!state.service){box.replaceChildren();hint.textContent='Выбери дату в календаре.';return;}
 const selectedDate=state.date;
 box.replaceChildren();hint.textContent='Проверяем свободное время…';
 try{
  const response=await fetch('/api/booking-times',{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''},body:JSON.stringify({date:selectedDate,service_id:state.service.id,addon_ids:state.addons.map(a=>a.id)})});
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
        services = (await sb_request('GET', '/rest/v1/nail_services', params={'id': 'eq.' + service_id, 'active': 'eq.true', 'select': 'id,minutes'})).json()
        if not services:
            raise HTTPException(400, 'Услуга недоступна.')
        addons = (await sb_request('GET', '/rest/v1/nail_addons', params={'active': 'eq.true', 'select': 'id,minutes'})).json()
        selected = set(addon_ids)
        if not selected.issubset({a['id'] for a in addons}):
            raise HTTPException(400, 'Дополнение недоступно. Выбери услугу заново.')
        minutes = services[0]['minutes'] + sum(a['minutes'] for a in addons if a['id'] in selected)
        settings = (await sb_request('GET', '/rest/v1/nail_settings', params={'id':'eq.1','select':'booking_enabled'})).json()
        enabled = bool(settings and settings[0]['booking_enabled'])
        bookings = (await sb_request('GET', '/rest/v1/nail_bookings', params={'select':'starts_at,ends_at','status':'in.(awaiting_receipt,pending_review,payment_not_found,confirmed,cancel_requested,reschedule_requested)','starts_at':'lt.'+(day+timedelta(days=1,minutes=minutes)).isoformat(),'ends_at':'gt.'+day.isoformat()})).json()
        intervals = [(datetime.fromisoformat(b['starts_at'].replace('Z','+00:00')), datetime.fromisoformat(b['ends_at'].replace('Z','+00:00'))) for b in bookings]
        now = datetime.now(moscow)
        slots = []
        for time in allowed_times:
            hour, minute = map(int, time.split(':'))
            start = day.replace(hour=hour, minute=minute)
            end = start + timedelta(minutes=minutes)
            reason = 'Запись закрыта' if not enabled else 'Прошло' if start <= now else 'Занято' if any(start < b and end > a for a,b in intervals) else ''
            slots.append({'time':time,'available':not reason,'reason':reason})
        return {'slots':slots,'minutes':minutes}
