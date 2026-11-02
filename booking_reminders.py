"""Persistent appointment reminders, driven by the database scheduler, not web-worker timers."""
import asyncio
import hashlib
import hmac
import time
import uuid
import logging
import os
import secrets
from datetime import datetime

import httpx
from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


class BookingReminders:
    def __init__(self, sb, http, token, url, webhook_secret, zone, master_name='', address=''):
        self.sb, self.http, self.token = sb, http, token
        self.url, self.zone = url.rstrip('/'), zone
        self.master_name, self.address = master_name, address
        self.secret = hashlib.sha256(('appointment-reminders:' + webhook_secret).encode()).hexdigest() if webhook_secret else ''
        self.enabled = os.environ.get('REMINDERS_ENABLED', 'true').lower() not in ('0', 'false', 'no')
        raw = os.environ.get('REMINDER_OFFSETS_HOURS', '24,2')
        try:
            self.offsets = sorted({int(value.strip()) * 60 for value in raw.split(',')}, reverse=True)
            if not 1 <= len(self.offsets) <= 5 or any(not 1 <= value <= 10080 for value in self.offsets):
                raise ValueError()
        except ValueError:
            raise ValueError('REMINDER_OFFSETS_HOURS: use 1 to 5 whole hours between 1 and 168') from None
        self.state = 'not_configured'
        self.lock = asyncio.Lock()

    async def rpc(self, name, data):
        response = await self.sb('POST', '/rest/v1/rpc/' + name, json_body=data)
        return response.json() if response.content else None

    async def configure(self):
        if not self.token or not self.secret or not self.url.startswith('https://'):
            self.state = 'missing_configuration'
            return
        try:
            await self.rpc('configure_nail_reminders', {
                'p_endpoint': self.url + '/internal/reminders', 'p_secret': self.secret,
                'p_bot_id': self.token.split(':', 1)[0], 'p_enabled': self.enabled, 'p_offsets': self.offsets,
            })
            self.state = 'configured' if self.enabled else 'disabled'
        except Exception as error:
            self.state = 'configuration_failed'
            log.error('Reminder setup failed (%s); install database/reminders.sql and database/reminders-cron.sql', type(error).__name__)

    def message(self, booking):
        start = datetime.fromisoformat(booking['starts_at'].replace('Z', '+00:00')).astimezone(self.zone)
        parts = ['🔔 Напоминание о записи', '', str(booking['service_name'])[:200],
                 f'📅 {start:%d.%m.%Y} в {start:%H:%M} ({self.zone})']
        if self.master_name:
            parts.append('Мастер: ' + self.master_name[:100])
        if self.address:
            parts.append('📍 ' + self.address[:300])
        parts += ['', 'Ждём тебя! Если планы изменились, свяжись с мастером заранее.']
        return '\n'.join(parts)

    async def finish(self, job, outcome, *, message_id=None, error=None, retry_seconds=60):
        await self.rpc('finish_nail_reminder', {
            'p_id': job['id'], 'p_lease_token': job['lease_token'], 'p_outcome': outcome,
            'p_message_id': message_id, 'p_error': error, 'p_retry_seconds': retry_seconds,
        })

    async def deliver(self, job):
        booking = await self.rpc('begin_nail_reminder', {'p_id': job['id'], 'p_lease_token': job['lease_token']})
        if not booking:
            return 'skipped'
        data = {'chat_id': booking['telegram_id'], 'text': self.message(booking),
                'reply_markup': {'inline_keyboard': [[{'text': 'Моя запись', 'web_app': {'url': self.url}}]]}}
        try:
            response = await self.http().post('https://api.telegram.org/bot' + self.token + '/sendMessage', json=data)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            await self.finish(job, 'retry', error='connect_failed')
            return 'retry'
        except httpx.HTTPError:
            # A timeout after writing may mean Telegram already delivered the message.
            await self.finish(job, 'uncertain', error='delivery_outcome_unknown')
            return 'uncertain'
        try:
            result = response.json()
        except ValueError:
            await self.finish(job, 'uncertain', error='invalid_response')
            return 'uncertain'
        if response.is_success and isinstance(result, dict) and result.get('ok') is True:
            await self.finish(job, 'sent', message_id=result.get('result', {}).get('message_id'))
            return 'sent'
        code = result.get('error_code', response.status_code) if isinstance(result, dict) else response.status_code
        if code == 429:
            retry = result.get('parameters', {}).get('retry_after', 60)
            await self.finish(job, 'retry', error='rate_limited', retry_seconds=int(retry))
            return 'retry'
        outcome = 'failed' if code in (400, 401, 403, 404) else 'uncertain'
        await self.finish(job, outcome, error='telegram_' + str(code))
        return outcome

    async def run(self):
        if not self.enabled:
            return {'disabled': True}
        if self.lock.locked():
            return {'busy': True}
        async with self.lock:
            jobs = await self.rpc('claim_nail_reminders', {'p_limit': 10})
            semaphore = asyncio.Semaphore(4)

            async def one(job):
                async with semaphore:
                    try:
                        return await self.deliver(job)
                    except Exception as error:
                        # No automatic re-send: DB distinguishes a lost claim from a lost send.
                        log.error('Reminder worker failed (%s)', type(error).__name__)
                        return 'worker_error'

            outcomes = await asyncio.gather(*(one(job) for job in jobs))
            return {key: outcomes.count(key) for key in set(outcomes)}


def install(app, sb, http, token, url, webhook_secret, zone, require_admin, master_name='', address=''):
    reminders = BookingReminders(sb, http, token, url, webhook_secret, zone, master_name, address)

    @app.post('/internal/reminders')
    async def reminder_tick(request: Request):
        stamp = request.headers.get('X-Reminder-Timestamp', '')
        nonce = request.headers.get('X-Reminder-Nonce', '')
        proof = request.headers.get('X-Reminder-Signature', '')
        try:
            valid_time = -30 <= time.time() - int(stamp) <= 180
            valid_nonce = str(uuid.UUID(nonce)) == nonce
        except ValueError:
            raise HTTPException(403)
        expected = hmac.new(reminders.secret.encode(), (stamp + '.' + nonce).encode(), hashlib.sha256).hexdigest()
        if not reminders.secret or not valid_time or not valid_nonce or not secrets.compare_digest(proof, expected):
            raise HTTPException(403)
        if not reminders.enabled:
            return {'ok': True, 'result': {'disabled': True}}
        if not await reminders.rpc('consume_nail_reminder_tick', {'p_nonce': nonce}):
            raise HTTPException(403)
        return {'ok': True, 'result': await reminders.run()}

    @app.get('/api/admin/reminders')
    async def reminder_status(request: Request):
        await require_admin(request.headers.get('X-Telegram-Init-Data', ''))
        return {'state': reminders.state, 'database': await reminders.rpc('nail_reminder_stats', {})}

    return reminders
