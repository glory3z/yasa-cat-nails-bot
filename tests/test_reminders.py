import asyncio
import hashlib
import hmac
import time
import uuid
from datetime import timezone
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from booking_reminders import BookingReminders, install

BOOKING={'booking_id':'b','telegram_id':555,'starts_at':'2030-05-01T10:30:00+00:00','service_name':'Маникюр','offset_minutes':120}
JOB={'id':'job','lease_token':'lease'}

class DB:
 def __init__(self,booking=None):self.calls=[];self.booking=BOOKING.copy() if booking is None else booking
 async def __call__(self,method,path,json_body):
  self.calls.append((path.split('/')[-1],json_body))
  return httpx.Response(200,json=self.booking if path.endswith('/begin_nail_reminder') else [JOB] if path.endswith('/claim_nail_reminders') else None)

def service(handler,db=None):
 db=db or DB()
 client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
 return BookingReminders(db,lambda:client,'123:secret','https://example.com','webhook-secret',timezone.utc,'Master','Address'),db,client

def result_for(response):
 r,db,c=service(lambda req:response)
 async def run():
  value=await r.run();await c.aclose();return value
 return asyncio.run(run()),db

def test_success_persists_receipt_without_token():
 result,db=result_for(httpx.Response(200,json={'ok':True,'result':{'message_id':42}}))
 assert result=={'sent':1}
 assert db.calls[-1][1]['p_outcome']=='sent'
 assert db.calls[-1][1]['p_message_id']==42
 assert 'secret' not in str(db.calls[-1][1])

def test_rate_limit_is_safe_to_retry():
 result,db=result_for(httpx.Response(429,json={'ok':False,'error_code':429,'parameters':{'retry_after':90}}))
 assert result=={'retry':1};assert db.calls[-1][1]['p_retry_seconds']==90

def test_blocked_user_is_terminal():
 result,db=result_for(httpx.Response(403,json={'ok':False,'error_code':403}))
 assert result=={'failed':1};assert db.calls[-1][1]['p_outcome']=='failed'

def test_unknown_telegram_response_is_never_automatically_repeated():
 for response in (httpx.Response(500,json={'ok':False}),httpx.Response(200,text='invalid JSON')):
  result,db=result_for(response)
  assert result=={'uncertain':1};assert db.calls[-1][1]['p_outcome']=='uncertain'

@pytest.mark.parametrize('error,outcome',[(httpx.ReadTimeout('x'),'uncertain'),(httpx.WriteError('x'),'uncertain'),(httpx.ConnectTimeout('x'),'retry')])
def test_transport_outcome(error,outcome):
 def handler(req):raise error
 r,db,c=service(handler)
 async def run():
  value=await r.run();await c.aclose();return value
 assert asyncio.run(run())=={outcome:1}
 assert db.calls[-1][1]['p_outcome']==outcome

def test_cancelled_between_claim_and_send_never_contacts_telegram():
 db=DB(booking={})
 def handler(req):raise AssertionError('must not send')
 r,db,c=service(handler,db)
 assert asyncio.run(r.run())=={'skipped':1}
 asyncio.run(c.aclose())

def test_endpoint_is_protected_and_disabled_makes_no_calls(monkeypatch):
 monkeypatch.setenv('REMINDERS_ENABLED','false')
 app=FastAPI();db=DB()
 async def admin(data):return {'id':1}
 r=install(app,db,None,'123:secret','https://example.com','a-secret',timezone.utc,admin)
 api=TestClient(app)
 assert api.post('/internal/reminders').status_code==403
 assert api.post('/internal/reminders',headers={'X-Reminder-Secret':'a-secret'}).status_code==403
 stamp=str(int(time.time()));nonce=str(uuid.uuid4());proof=hmac.new(r.secret.encode(),(stamp+'.'+nonce).encode(),hashlib.sha256).hexdigest()
 assert api.post('/internal/reminders',headers={'X-Reminder-Timestamp':stamp,'X-Reminder-Nonce':nonce,'X-Reminder-Signature':proof}).json()['result']=={'disabled':True}
 assert db.calls==[]

def test_config_registers_current_bot_and_offsets():
 r,db,c=service(lambda req:httpx.Response(200,json={}))
 asyncio.run(r.configure())
 assert r.state=='configured'
 assert db.calls[0][1]['p_bot_id']=='123'
 assert db.calls[0][1]['p_offsets']==[1440,120]
 assert db.calls[0][1]['p_secret']!='webhook-secret'
 asyncio.run(c.aclose())

def test_message_contains_local_date_service_master_and_address():
 r,db,c=service(lambda req:httpx.Response(200,json={}))
 text=r.message(BOOKING)
 for value in ('01.05.2030','10:30','Маникюр','Master','Address'):assert value in text
 assert '123:secret' not in text
 asyncio.run(c.aclose())


def test_tick_proof_expires_and_nonce_cannot_be_replayed():
 app=FastAPI();used=set();calls=[]
 async def db(method,path,json_body):
  calls.append(path)
  if path.endswith('consume_nail_reminder_tick'):
   nonce=json_body['p_nonce'];allowed=nonce not in used;used.add(nonce);return httpx.Response(200,json=allowed)
  if path.endswith('claim_nail_reminders'):return httpx.Response(200,json=[])
  raise AssertionError(path)
 async def admin(data):return {'id':1}
 r=install(app,db,None,'123:secret','https://example.com','a-secret',timezone.utc,admin)
 api=TestClient(app)
 def headers(age=0):
  stamp=str(int(time.time())-age);nonce=str(uuid.uuid4());proof=hmac.new(r.secret.encode(),(stamp+'.'+nonce).encode(),hashlib.sha256).hexdigest()
  return {'X-Reminder-Timestamp':stamp,'X-Reminder-Nonce':nonce,'X-Reminder-Signature':proof}
 proof=headers()
 assert api.post('/internal/reminders',headers=proof).status_code==200
 assert api.post('/internal/reminders',headers=proof).status_code==403
 assert api.post('/internal/reminders',headers=headers(age=300)).status_code==403
 assert len([p for p in calls if p.endswith('claim_nail_reminders')])==1
