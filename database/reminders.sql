-- Additive, repeatable installer for an existing bot or a new template database.
-- Does not change any existing booking or payment status.
BEGIN;
CREATE SCHEMA IF NOT EXISTS booking_private;
REVOKE ALL ON SCHEMA booking_private FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA booking_private TO service_role;
CREATE TABLE IF NOT EXISTS booking_private.reminder_config (
 id integer PRIMARY KEY CHECK(id=1),
 endpoint text,
 request_secret text,
 bot_id text,
 enabled boolean NOT NULL DEFAULT false,
 offsets_minutes integer[] NOT NULL DEFAULT ARRAY[1440,120],
 updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE booking_private.reminder_config ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON booking_private.reminder_config FROM PUBLIC, anon, authenticated;
GRANT SELECT,INSERT,UPDATE ON booking_private.reminder_config TO service_role;
INSERT INTO booking_private.reminder_config(id) VALUES(1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS booking_private.reminder_ticks (
 nonce uuid PRIMARY KEY,
 expires_at timestamptz NOT NULL
);
ALTER TABLE booking_private.reminder_ticks ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON booking_private.reminder_ticks FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,DELETE ON booking_private.reminder_ticks TO service_role;

CREATE OR REPLACE FUNCTION public.consume_nail_reminder_tick(p_nonce uuid) RETURNS boolean
LANGUAGE sql SECURITY INVOKER SET search_path=public,pg_temp AS $$
 WITH used AS (DELETE FROM booking_private.reminder_ticks WHERE nonce=p_nonce AND expires_at>now() RETURNING nonce)
 SELECT EXISTS(SELECT 1 FROM used);
$$;

CREATE TABLE IF NOT EXISTS public.nail_reminder_deliveries (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 booking_id uuid NOT NULL REFERENCES public.nail_bookings(id) ON DELETE CASCADE,
 starts_at timestamptz NOT NULL,
 offset_minutes integer NOT NULL CHECK(offset_minutes BETWEEN 1 AND 10080),
 due_at timestamptz NOT NULL,
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','claimed','sending','sent','skipped','failed','uncertain')),
 attempts integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(),
 lease_token uuid,
 locked_until timestamptz,
 sent_at timestamptz,
 telegram_message_id bigint,
 last_error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(booking_id,starts_at,offset_minutes)
);
ALTER TABLE public.nail_reminder_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.nail_reminder_deliveries FROM PUBLIC, anon, authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.nail_reminder_deliveries TO service_role;
CREATE INDEX IF NOT EXISTS nail_reminders_due ON public.nail_reminder_deliveries(due_at,next_attempt_at) WHERE status='queued';
CREATE INDEX IF NOT EXISTS nail_reminders_booking ON public.nail_reminder_deliveries(booking_id);

CREATE OR REPLACE FUNCTION public.sync_nail_booking_reminders() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE cfg booking_private.reminder_config; offset_value integer;
BEGIN
 IF TG_OP='UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status AND NEW.starts_at IS NOT DISTINCT FROM OLD.starts_at THEN RETURN NEW; END IF;
 UPDATE public.nail_reminder_deliveries SET status='skipped',last_error='booking_changed',updated_at=now()
 WHERE booking_id=NEW.id AND status IN ('queued','claimed') AND (NEW.status<>'confirmed' OR starts_at<>NEW.starts_at);
 SELECT * INTO cfg FROM booking_private.reminder_config WHERE id=1;
 IF NEW.status='confirmed' AND cfg.enabled AND NEW.starts_at>now() THEN
  FOREACH offset_value IN ARRAY cfg.offsets_minutes LOOP
   IF NEW.starts_at-make_interval(mins=>offset_value)>now() THEN
    INSERT INTO public.nail_reminder_deliveries(booking_id,starts_at,offset_minutes,due_at)
    VALUES(NEW.id,NEW.starts_at,offset_value,NEW.starts_at-make_interval(mins=>offset_value))
    ON CONFLICT(booking_id,starts_at,offset_minutes) DO UPDATE SET status='queued',last_error=NULL,next_attempt_at=now(),updated_at=now()
    WHERE nail_reminder_deliveries.status='skipped' AND nail_reminder_deliveries.sent_at IS NULL;
   END IF;
  END LOOP;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS sync_nail_booking_reminders ON public.nail_bookings;
CREATE TRIGGER sync_nail_booking_reminders AFTER INSERT OR UPDATE OF status,starts_at ON public.nail_bookings
FOR EACH ROW EXECUTE FUNCTION public.sync_nail_booking_reminders();

CREATE OR REPLACE FUNCTION public.configure_nail_reminders(p_endpoint text,p_secret text,p_bot_id text,p_enabled boolean,p_offsets integer[])
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE current_bot text;
BEGIN
 IF p_endpoint !~ '^https://[^/]+/internal/reminders$' OR length(p_secret)<32 OR coalesce(p_bot_id,'') !~ '^[0-9]+$' THEN
  RAISE EXCEPTION 'Invalid reminder endpoint configuration';
 END IF;
 IF cardinality(p_offsets) NOT BETWEEN 1 AND 5 OR EXISTS(SELECT 1 FROM unnest(p_offsets) x WHERE x IS NULL OR x NOT BETWEEN 1 AND 10080) THEN
  RAISE EXCEPTION 'Use 1 to 5 reminder offsets, between 1 minute and 7 days';
 END IF;
 SELECT bot_id INTO current_bot FROM booking_private.reminder_config WHERE id=1 FOR UPDATE;
 IF current_bot IS NOT NULL AND current_bot<>p_bot_id THEN RAISE EXCEPTION 'This database belongs to another bot'; END IF;
 UPDATE booking_private.reminder_config SET endpoint=p_endpoint,request_secret=p_secret,bot_id=p_bot_id,enabled=p_enabled,
 offsets_minutes=ARRAY(SELECT DISTINCT x FROM unnest(p_offsets) x ORDER BY x DESC),updated_at=now() WHERE id=1;
 UPDATE public.nail_reminder_deliveries SET status='skipped',last_error='configuration_changed',updated_at=now()
 WHERE status IN ('queued','claimed') AND (NOT p_enabled OR NOT offset_minutes=ANY(p_offsets));
 IF p_enabled THEN
  INSERT INTO public.nail_reminder_deliveries(booking_id,starts_at,offset_minutes,due_at)
  SELECT b.id,b.starts_at,o,b.starts_at-make_interval(mins=>o)
  FROM public.nail_bookings b CROSS JOIN unnest(p_offsets) o
  WHERE b.status='confirmed' AND b.starts_at-make_interval(mins=>o)>now()
  ON CONFLICT(booking_id,starts_at,offset_minutes) DO UPDATE SET status='queued',last_error=NULL,next_attempt_at=now(),updated_at=now()
  WHERE nail_reminder_deliveries.status='skipped' AND nail_reminder_deliveries.sent_at IS NULL;
 END IF;
END $$;

CREATE OR REPLACE FUNCTION public.claim_nail_reminders(p_limit integer DEFAULT 10)
RETURNS TABLE(id uuid,lease_token uuid) LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM booking_private.reminder_config WHERE reminder_config.id=1 AND enabled) THEN RETURN; END IF;
 -- A send with an unknown outcome must never be automatically repeated.
 UPDATE public.nail_reminder_deliveries d SET status='uncertain',last_error='worker_lost_during_send',updated_at=now()
 WHERE d.status='sending' AND d.locked_until<now();
 UPDATE public.nail_reminder_deliveries d SET status='queued',lease_token=NULL,locked_until=NULL,updated_at=now()
 WHERE d.status='claimed' AND d.locked_until<now();
 UPDATE public.nail_reminder_deliveries d SET status='skipped',last_error='stale_or_cancelled',updated_at=now()
 WHERE d.status IN ('queued','claimed') AND (d.due_at<now()-interval '20 minutes' OR d.starts_at<=now() OR
 NOT EXISTS(SELECT 1 FROM public.nail_bookings b WHERE b.id=d.booking_id AND b.status='confirmed' AND b.starts_at=d.starts_at));
 RETURN QUERY
 WITH due AS (
  SELECT d.id FROM public.nail_reminder_deliveries d JOIN public.nail_bookings b ON b.id=d.booking_id
  WHERE d.status='queued' AND d.due_at<=now() AND d.next_attempt_at<=now() AND d.starts_at>now()
   AND b.status='confirmed' AND b.starts_at=d.starts_at
  ORDER BY d.due_at,d.id FOR UPDATE OF d SKIP LOCKED LIMIT greatest(1,least(p_limit,10))
 ) UPDATE public.nail_reminder_deliveries d SET status='claimed',lease_token=gen_random_uuid(),locked_until=now()+interval '3 minutes',attempts=d.attempts+1,updated_at=now()
 FROM due WHERE d.id=due.id RETURNING d.id,d.lease_token;
END $$;

CREATE OR REPLACE FUNCTION public.begin_nail_reminder(p_id uuid,p_lease_token uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE b public.nail_bookings; d public.nail_reminder_deliveries; booking_key uuid;
BEGIN
 SELECT booking_id INTO booking_key FROM public.nail_reminder_deliveries WHERE id=p_id;
 -- Booking then delivery: same lock order as cancellation's booking trigger.
 SELECT * INTO b FROM public.nail_bookings WHERE id=booking_key FOR SHARE;
 SELECT * INTO d FROM public.nail_reminder_deliveries WHERE id=p_id FOR UPDATE;
 IF d.id IS NULL OR d.status<>'claimed' OR d.lease_token IS DISTINCT FROM p_lease_token OR d.locked_until<=now() THEN RETURN NULL; END IF;
 IF b.id IS NULL OR b.status<>'confirmed' OR b.starts_at<>d.starts_at OR b.starts_at<=now() OR d.due_at<now()-interval '20 minutes'
 OR NOT EXISTS(SELECT 1 FROM booking_private.reminder_config WHERE id=1 AND enabled) THEN
  UPDATE public.nail_reminder_deliveries SET status='skipped',last_error='booking_changed',updated_at=now() WHERE id=p_id;
  RETURN NULL;
 END IF;
 UPDATE public.nail_reminder_deliveries SET status='sending',updated_at=now() WHERE id=p_id;
 RETURN jsonb_build_object('booking_id',b.id,'telegram_id',b.telegram_id,'starts_at',b.starts_at,'service_name',b.service_name,'offset_minutes',d.offset_minutes);
END $$;

CREATE OR REPLACE FUNCTION public.finish_nail_reminder(p_id uuid,p_lease_token uuid,p_outcome text,p_message_id bigint DEFAULT NULL,p_error text DEFAULT NULL,p_retry_seconds integer DEFAULT 60)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
BEGIN
 IF p_outcome NOT IN ('sent','failed','uncertain','retry') THEN RAISE EXCEPTION 'Invalid reminder outcome'; END IF;
 UPDATE public.nail_reminder_deliveries SET
 status=CASE WHEN p_outcome='retry' AND attempts<5 THEN 'queued' WHEN p_outcome='retry' THEN 'failed' ELSE p_outcome END,
 sent_at=CASE WHEN p_outcome='sent' THEN now() ELSE sent_at END,telegram_message_id=p_message_id,
 last_error=left(p_error,100),next_attempt_at=now()+make_interval(secs=>greatest(1,least(p_retry_seconds,86400))),
 locked_until=NULL,updated_at=now()
 WHERE id=p_id AND lease_token=p_lease_token AND status='sending';
END $$;

CREATE OR REPLACE FUNCTION public.nail_reminder_stats() RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path=public,pg_temp AS $$
 SELECT jsonb_build_object('configured',c.endpoint IS NOT NULL,'enabled',c.enabled,'offsets_minutes',c.offsets_minutes,
 'deliveries',coalesce((SELECT jsonb_object_agg(status,n) FROM (SELECT status,count(*) n FROM public.nail_reminder_deliveries GROUP BY status) counts),'{}'::jsonb))
 FROM booking_private.reminder_config c WHERE id=1;
$$;

DO $$ DECLARE f record; BEGIN
 FOR f IN SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname IN ('consume_nail_reminder_tick','sync_nail_booking_reminders','configure_nail_reminders','claim_nail_reminders','begin_nail_reminder','finish_nail_reminder','nail_reminder_stats') LOOP
 EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated',f.signature);
 EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.signature);
 END LOOP;
END $$;
NOTIFY pgrst,'reload schema';
COMMIT;
