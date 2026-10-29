import base64
import hashlib
import re
from urllib.parse import quote
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from work_photos import replace_work_photos
from master_tools import install as install_master_tools

CLIENT_SCRIPT = r"""
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
async function reloadPhotos(){
 try{const response=await fetch('/api/work-photos',{cache:'no-store'});if(!response.ok)return;const data=await response.json(),grid=document.querySelector('.works-grid');if(!grid||!Array.isArray(data.photos))return;const fragment=document.createDocumentFragment();for(const photo of data.photos){const card=document.createElement('div'),image=document.createElement('img');card.className='work-card';image.src=photo.url;image.alt=photo.caption||'Работа мастера';image.loading='lazy';image.decoding='async';card.appendChild(image);if(photo.caption){const info=document.createElement('div');info.className='work-info';info.textContent=photo.caption;card.appendChild(info)}fragment.appendChild(card)}grid.replaceChildren(fragment)}catch(error){console.warn('Не удалось обновить примеры работ')}
}
reloadPhotos();document.addEventListener('visibilitychange',()=>{if(!document.hidden)reloadPhotos()});
})();
"""

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
    creator_url = 'https://t.me/delaupokaifu?text=' + quote('Привет! Хочу такого же бота для своего бизнеса. Расскажи про стоимость и запуск.', safe='')
    creator_button = ('<div style="margin-top:24px;padding-top:16px;border-top:1px solid var(--line)">'
        '<a id="creator-contact" class="secondary" style="display:block;text-align:center;text-decoration:none" href="' + creator_url + '" '
        'onclick="if(window.Telegram &amp;&amp; window.Telegram.WebApp &amp;&amp; window.Telegram.WebApp.openTelegramLink){event.preventDefault();window.Telegram.WebApp.openTelegramLink(this.href)}">'
        'Хочу такого же бота</a></div>')
    page = page.replace('</section>', creator_button + '</section>', 1)
    page = page.replace('</body>', '<script src="/static/client-enhance.js"></script></body>')
    legacy=[]
    for digest in photo_assets:
        source='/static/work-photo/'+digest
        match=re.search(r'<div class="work-card">\s*<img[^>]*src="'+re.escape(source)+r'"[^>]*>\s*<div class="work-info">(.*?)</div>',page,re.S)
        caption=re.sub(r'<[^>]+>',' ',match.group(1)).strip() if match else ''
        legacy.append({'id':'legacy-'+digest,'url':source,'caption':re.sub(r'\s+',' ',caption)})
    install_master_tools(app,sb_request,verify_init_data,moscow,allowed_times,legacy)

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
