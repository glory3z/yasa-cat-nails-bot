import asyncio
import logging
import uuid
from datetime import datetime
from fastapi import HTTPException

log = logging.getLogger(__name__)
STATUS = {'awaiting_receipt':'Ожидается чек','pending_review':'Чек на проверке','confirmed':'Подтверждена','payment_not_found':'Оплата не найдена','declined':'Отклонена','cancelled':'Отменена','expired':'Истекла','reschedule_requested':'Согласуется перенос','cancel_requested':'Запрошена отмена'}
ACTIONS = {'pay':('confirm_payment','Оплата получена',{'pending_review'}),'missing':('payment_not_found','Оплата не найдена',{'pending_review'}),'decline':('decline','Отклонить',{'awaiting_receipt','reschedule_requested'}),'cancel':('cancel','Отменить',{'confirmed'}),'move':('request_reschedule','Запросить перенос',{'confirmed'}),'moved':('confirm_reschedule','Подтвердить перенос',{'reschedule_requested'})}
UPDATES = {'confirm_payment':{'payment_status':'received','status':'confirmed'},'payment_not_found':{'status':'payment_not_found'},'decline':{'status':'declined'},'cancel':{'status':'cancelled'},'request_reschedule':{'status':'reschedule_requested'},'confirm_reschedule':{'status':'confirmed'}}
ADMIN_CSS = '''<style>
.row{grid-template-columns:104px minmax(0,1fr);align-items:start;gap:12px;margin:0;padding:10px 0;border-bottom:1px solid #f3eeea;line-height:1.5}.row>:first-child{color:#837b76;text-align:left}.row>:last-child{text-align:left;white-space:pre-wrap;overflow-wrap:anywhere}.actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-items:stretch}.actions button{width:100%;min-height:46px;padding:10px 8px}.label{margin:14px 0 6px;line-height:1.4}input:not([type=checkbox]){min-height:46px}.pair{align-items:start}.check{align-items:center;line-height:1.5}.tabs button{min-height:46px}.card{padding:14px}.card>p{overflow-wrap:anywhere}#notice{white-space:pre-wrap}@media(max-width:340px){.row{grid-template-columns:88px minmax(0,1fr);gap:8px}.actions{grid-template-columns:1fr}}
</style>'''

class BookingMessages:
    def __init__(self, sb, http, token, master_id, url, zone, bucket):
        self.sb,self.http,self.token,self.master,self.url,self.zone,self.bucket = sb,http,token,master_id,url,zone,bucket

    async def telegram(self, method, data=None, files=None):
        if not self.token:
            raise RuntimeError('Telegram token is not configured')
        if files:
            response = await self.http().post(f'https://api.telegram.org/bot{self.token}/{method}',data=data,files=files)
        else:
            response = await self.http().post(f'https://api.telegram.org/bot{self.token}/{method}',json=data)
        result = response.json()
        if not response.is_success or not result.get('ok'):
            log.error('Telegram %s rejected request: HTTP %s',method,response.status_code)
            raise RuntimeError('Не удалось доставить сообщение в Telegram.')
        return result.get('result')

    def card(self, b, title='Информация о записи', client=False):
        start = datetime.fromisoformat(b['starts_at'].replace('Z','+00:00')).astimezone(self.zone)
        end = datetime.fromisoformat(b['ends_at'].replace('Z','+00:00')).astimezone(self.zone)
        minutes = int((end-start).total_seconds()/60)
        addons = ', '.join(str(a.get('name','')) for a in b.get('addons',[])) or 'Нет'
        username = str(b.get('telegram_username') or '').strip().lstrip('@')
        lines = [title,'',f"Номер: {b['id']}",f"Клиент: {str(b.get('client_name',''))[:100]}",f"Telegram: {'@'+username if username else 'не указан'}",f"Услуга: {str(b.get('service_name',''))[:200]}",f"Дополнения: {addons[:700]}",f"Дата: {start:%d.%m.%Y}",f"Время: {start:%H:%M}–{end:%H:%M} МСК",f"Длительность: {minutes} мин",'Мастер: Ярослава · м. Новогиреево',f"Итого: {b.get('total_rub',0)} ₽",f"Предоплата: {b.get('deposit_rub',0)} ₽",f"Статус: {STATUS.get(b['status'],b['status'])}",f"Комментарий: {str(b.get('client_comment') or '—')[:900]}"]
        if client and b['status']=='awaiting_receipt':
            payment = b.get('payment_details_snapshot') or {}
            lines += ['',f"Для перевода: {payment.get('payment_phone') or 'см. мини-приложение'}",f"Банк: {payment.get('payment_bank') or 'см. мини-приложение'}",'Прикрепи чек в мини-приложении. Запись подтвердит мастер после проверки оплаты.']
        return '\n'.join(lines)

    def keyboard(self, b, has_receipt=False):
        rows=[]
        if has_receipt:
            rows.append([{'text':'Открыть чек','callback_data':f"bk:receipt:{b['id']}"}])
        for code,(_,title,states) in ACTIONS.items():
            if b['status'] in states:
                rows.append([{'text':title,'callback_data':f"bk:{code}:{b['id']}"}])
        rows.append([{'text':'Обновить статус','callback_data':f"bk:refresh:{b['id']}"}])
        if self.url:
            rows.append([{'text':'Кабинет мастера','web_app':{'url':self.url+'/admin'}}])
        return {'inline_keyboard':rows}

    async def receipts(self, booking_id):
        return (await self.sb('GET','/rest/v1/nail_uploaded_receipts',params={'booking_id':'eq.'+booking_id,'select':'id,object_path,original_filename,mime_type','order':'created_at.desc'})).json()

    async def booking(self, booking_id):
        try:
            booking_id=str(uuid.UUID(booking_id))
        except (ValueError,TypeError):
            raise HTTPException(400,'Некорректный номер записи.')
        rows=(await self.sb('GET','/rest/v1/nail_bookings',params={'id':'eq.'+booking_id,'select':'*'})).json()
        if not rows:
            raise HTTPException(404,'Запись больше не существует.')
        return rows[0]

    async def is_admin(self, telegram_id):
        return bool((await self.sb('GET','/rest/v1/nail_admins',params={'telegram_id':'eq.'+str(telegram_id),'select':'telegram_id'})).json())

    async def notify(self, b, title='Новая заявка'):
        try:
            receipts=await self.receipts(b['id'])
            jobs=[self.telegram('sendMessage',{'chat_id':self.master,'text':self.card(b,title),'reply_markup':self.keyboard(b,bool(receipts))}),self.client_card(b)]
            results=await asyncio.gather(*jobs,return_exceptions=True)
            if any(isinstance(r,Exception) for r in results):
                log.error('One or more booking notifications failed')
        except Exception:
            log.error('Unable to prepare booking notifications')

    async def client_card(self, b, title='Твоя запись'):
        payload={'chat_id':b['telegram_id'],'text':self.card(b,title,client=True)}
        if self.url:
            payload['reply_markup']={'inline_keyboard':[[{'text':'Открыть мини-приложение','web_app':{'url':self.url}}]]}
        return await self.telegram('sendMessage',payload)

    async def perform(self, actor, booking_id, action):
        if not await self.is_admin(actor):
            raise HTTPException(403,'Нет доступа к управлению записями.')
        rule=next((v for v in ACTIONS.values() if v[0]==action),None)
        if rule is None:
            raise HTTPException(400,'Неизвестное действие.')
        b=await self.booking(booking_id)
        if b['status'] not in rule[2]:
            raise HTTPException(409,'Статус уже изменился. Обнови карточку.')
        updates={**UPDATES[action],'updated_at':datetime.now(self.zone).isoformat()}
        rows=(await self.sb('PATCH','/rest/v1/nail_bookings',params={'id':'eq.'+b['id'],'status':'eq.'+b['status'],'select':'*'},json_body=updates,headers={'Prefer':'return=representation'})).json()
        if not rows:
            raise HTTPException(409,'Статус уже изменился. Обнови карточку.')
        await self.sb('POST','/rest/v1/nail_admin_audit',json_body={'actor_telegram_id':actor,'action':action,'entity_id':b['id'],'details':{'source':'booking_controls','previous_status':b['status']}})
        return rows[0]

    async def callback(self, q):
        qid=q.get('id')
        actor=q.get('from',{}).get('id')
        message=q.get('message') or {}
        chat=message.get('chat') or {}
        try:
            if chat.get('type')!='private' or chat.get('id')!=actor or not await self.is_admin(actor):
                raise HTTPException(403,'Доступно только мастеру.')
            parts=str(q.get('data','')).split(':')
            if len(parts)!=3 or parts[0]!='bk':
                raise HTTPException(400,'Неизвестная кнопка.')
            _,code,booking_id=parts
            b=await self.booking(booking_id)
            if code not in {'receipt','refresh','cancel_yes','decline_yes'} and code not in ACTIONS:
                raise HTTPException(400,'Неизвестное действие.')
            await self.telegram('answerCallbackQuery',{'callback_query_id':qid,'text':'Проверяю запись…'})
            if code in {'cancel','decline'}:
                rule=ACTIONS[code]
                if b['status'] not in rule[2]:
                    raise HTTPException(409,'Статус уже изменился. Обнови карточку.')
                markup={'inline_keyboard':[[{'text':'Да, '+('отменить' if code=='cancel' else 'отклонить'),'callback_data':f"bk:{code}_yes:{b['id']}"}],[{'text':'Назад','callback_data':f"bk:refresh:{b['id']}"}]]}
                await self.telegram('editMessageReplyMarkup',{'chat_id':actor,'message_id':message['message_id'],'reply_markup':markup})
                return
            if code=='receipt':
                rows=await self.receipts(b['id'])
                if not rows:
                    raise HTTPException(404,'Чек ещё не загружен.')
                r=rows[0]
                response=await self.sb('GET',f"/storage/v1/object/authenticated/{self.bucket}/{r['object_path']}")
                await self.telegram('sendDocument',data={'chat_id':str(actor),'caption':f"Чек к записи {b['id']}"},files={'document':(r['original_filename'],response.content,r['mime_type'])})
                return
            if code!='refresh':
                code=code.removesuffix('_yes')
                b=await self.perform(actor,b['id'],ACTIONS[code][0])
                try:
                    await self.client_card(b,'Статус твоей записи обновлён')
                except Exception:
                    log.error('Client status notification failed')
            rows=await self.receipts(b['id'])
            try:
                await self.telegram('editMessageText',{'chat_id':actor,'message_id':message['message_id'],'text':self.card(b),'reply_markup':self.keyboard(b,bool(rows))})
            except RuntimeError:
                await self.telegram('sendMessage',{'chat_id':actor,'text':self.card(b),'reply_markup':self.keyboard(b,bool(rows))})
        except Exception as error:
            text=error.detail if isinstance(error,HTTPException) else 'Не удалось выполнить действие. Проверь актуальный статус в кабинете.'
            try:
                await self.telegram('answerCallbackQuery',{'callback_query_id':qid,'text':text,'show_alert':True})
                if chat.get('type')=='private' and chat.get('id')==actor:
                    await self.telegram('sendMessage',{'chat_id':actor,'text':text})
            except Exception:
                log.error('Unable to deliver callback error')
