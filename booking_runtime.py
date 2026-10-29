import asyncio
import logging
from datetime import datetime, timezone
from booking_messages import BookingMessages

log = logging.getLogger(__name__)
MASTER_NOTIFICATION_RECIPIENT_IDS = (7689570674, 5206277381)

class BookingRuntime(BookingMessages):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runtime = {'webhook_state':'not_checked','master_is_bot':None,'master_has_access':None,'last_start_seen':None,'last_callback_seen':None}
        self.bot_id = None

    def seen(self, event):
        self.runtime[event] = datetime.now(timezone.utc).isoformat()

    async def configure(self, secret):
        if not self.token or not secret or not self.url.startswith('https://'):
            self.runtime['webhook_state'] = 'missing_configuration'
            log.error('Telegram configuration incomplete: check BOT_TOKEN, WEBHOOK_SECRET, APP_URL')
            return
        for attempt in range(3):
            try:
                identity = await self.telegram('getMe', {})
                self.bot_id = identity['id']
                self.runtime['master_is_bot'] = self.master == self.bot_id
                log.warning('Telegram identity: bot_id=%s configured_master_id=%s same_account=%s',self.bot_id,self.master,self.runtime['master_is_bot'])
                await self.telegram('setWebhook', {'url':self.url+'/telegram/webhook','secret_token':secret,'allowed_updates':['message','callback_query']})
                info = await self.telegram('getWebhookInfo', {})
                valid = info.get('url') == self.url+'/telegram/webhook' and {'message','callback_query'}.issubset(set(info.get('allowed_updates') or []))
                self.runtime['webhook_state'] = 'configured' if valid else 'configuration_mismatch'
                self.runtime['pending_updates'] = info.get('pending_update_count',0)
                self.runtime['webhook_error_present'] = bool(info.get('last_error_message'))
                log.warning('Telegram webhook verification: state=%s pending=%s previous_error_present=%s',self.runtime['webhook_state'],self.runtime['pending_updates'],self.runtime['webhook_error_present'])
                try:
                    self.runtime['master_has_access'] = await self.is_admin(self.master)
                except Exception:
                    self.runtime['master_has_access'] = None
                if self.runtime['master_is_bot']:
                    log.error('MASTER_RECIPIENT_IS_THIS_BOT: set MASTER_TELEGRAM_ID to the human account shown by /id')
                if valid:
                    return
            except Exception as error:
                self.runtime['webhook_state'] = 'registration_failed'
                log.error('Telegram setup attempt %s failed: %s',attempt+1,type(error).__name__)
            if attempt < 2:
                await asyncio.sleep(2**attempt)

    def webhook_reply(self, chat_id, sender, command):
        if command == '/id':
            text = 'Твой личный Telegram ID: '+str(sender.get('id'))+'\nID этого бота: '+str(self.bot_id or 'ещё проверяется')+'\nЭто разные назначения: мастером должен быть личный аккаунт.'
        else:
            text = 'Привет! Здесь можно записаться на маникюр к Ярославе 💅'
        result = {'method':'sendMessage','chat_id':chat_id,'text':text}
        if self.url.startswith('https://'):
            result['reply_markup'] = {'inline_keyboard':[[{'text':'💅 Записаться','web_app':{'url':self.url}}]]}
        return result

    async def client_card(self, b, title='Твоя запись'):
        if b['status'] == 'awaiting_receipt':
            return None
        if b['status'] == 'pending_review':
            text = '🧾 Чек отправлен на проверку.\nОжидай подтверждения записи мастером.'
        else:
            titles = {'confirmed':'Запись подтверждена ✅','payment_not_found':'Оплату не удалось подтвердить','cancelled':'Запись отменена','declined':'Заявка отклонена','reschedule_requested':'Нужно согласовать перенос','expired':'Срок заявки истёк'}
            text = self.card(b,titles.get(b['status'],title),client=True)
            if b['status'] == 'payment_not_found':
                text += '\n\nСвяжись с мастером для уточнения оплаты.'
        return await self.telegram('sendMessage',{'chat_id':b['telegram_id'],'text':text,'reply_markup':self.client_keyboard(b)})

    async def master_card(self, b, title):
        if self.bot_id is None:
            self.bot_id = (await self.telegram('getMe', {}))['id']
        self.runtime['master_is_bot'] = self.master == self.bot_id
        receipts = await self.receipts(b['id'])
        text = self.card(b,title)
        markup = self.keyboard(b,bool(receipts))
        recipients = tuple(dict.fromkeys(MASTER_NOTIFICATION_RECIPIENT_IDS))

        async def send_to(recipient):
            if recipient == self.bot_id:
                raise RuntimeError('Notification recipient is this bot')
            if not await self.is_admin(recipient):
                raise PermissionError('Notification recipient is not an active administrator')
            return await self.telegram('sendMessage',{'chat_id':recipient,'text':text,'reply_markup':markup})

        results = await asyncio.gather(*(send_to(recipient) for recipient in recipients),return_exceptions=True)
        for recipient,result in zip(recipients,results):
            if isinstance(result,Exception):
                log.error('Master notification failed: recipient_id=%s type=%s',recipient,type(result).__name__)

    async def notify(self, b, title='Новая заявка'):
        if b['status'] == 'awaiting_receipt':
            return
        if b['status'] == 'pending_review':
            title = 'Новая заявка: чек на проверке'
        results = await asyncio.gather(self.master_card(b,title),self.client_card(b),return_exceptions=True)
        for role,result in zip(('master','client'),results):
            if isinstance(result,Exception):
                log.error('Booking notification failed: role=%s recipient_id=%s type=%s',role,self.master if role=='master' else b['telegram_id'],type(result).__name__)
