-- Run after reminders.sql. Starts one timer; no messages before the app registers its endpoint.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;
-- pg_net may expose request metadata to SQL roles. Send only short-lived, single-use proofs; never send the key.
CREATE OR REPLACE FUNCTION booking_private.dispatch_reminder_tick() RETURNS bigint
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c booking_private.reminder_config; request_id bigint; nonce_value uuid; stamp text; proof text;
BEGIN
 SELECT * INTO c FROM booking_private.reminder_config WHERE id=1;
 IF NOT c.enabled OR c.endpoint IS NULL OR c.request_secret IS NULL THEN RETURN NULL; END IF;
 DELETE FROM booking_private.reminder_ticks WHERE expires_at<now();
 nonce_value:=gen_random_uuid();
 stamp:=floor(extract(epoch from now()))::bigint::text;
 proof:=encode(extensions.hmac(stamp||'.'||nonce_value::text,c.request_secret,'sha256'),'hex');
 INSERT INTO booking_private.reminder_ticks(nonce,expires_at) VALUES(nonce_value,now()+interval '3 minutes');
 -- The master key stays in booking_private. A captured proof cannot authorize another request.
 SELECT net.http_post(url:=c.endpoint,body:='{}'::jsonb,
 headers:=jsonb_build_object('Content-Type','application/json','X-Reminder-Timestamp',stamp,'X-Reminder-Nonce',nonce_value::text,'X-Reminder-Signature',proof),
 timeout_milliseconds:=120000) INTO request_id;
 RETURN request_id;
END $$;
REVOKE ALL ON FUNCTION booking_private.dispatch_reminder_tick() FROM PUBLIC,anon,authenticated,service_role;
SELECT cron.schedule('nail-appointment-reminders','* * * * *','SELECT booking_private.dispatch_reminder_tick();');
COMMIT;
