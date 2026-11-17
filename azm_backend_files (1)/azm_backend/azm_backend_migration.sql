-- Run this AFTER azm_miner_initial_schema.sql in the AZM Supabase project only.
-- Seeds the exact paid miner prices/rates shown in the supplied AZM frontend.
-- Adds the atomic database operations used by api/index.py.

ALTER TABLE public.azm_user_miners
  ADD COLUMN IF NOT EXISTS last_claim_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='azm_user_miners_unique_user_miner') THEN
  ALTER TABLE public.azm_user_miners ADD CONSTRAINT azm_user_miners_unique_user_miner UNIQUE (telegram_user_id, miner_code);
 END IF;
END $$;

-- Frontend currently displays these check-in rewards: days 1-10 = 10,
-- days 11-20 = 20, days 21-30 = 50 AZM.
INSERT INTO public.azm_checkin_rewards(day_number,reward_amount)
SELECT d, CASE WHEN d<=10 THEN 10 WHEN d<=20 THEN 20 ELSE 50 END
FROM generate_series(1,30) AS d
ON CONFLICT(day_number) DO UPDATE SET reward_amount=EXCLUDED.reward_amount;

-- Miner catalog matches index(2).html. Price is stored in nanoTON.
INSERT INTO public.azm_miners
 (miner_code,miner_name,price_nano,mining_rate_per_hour,is_paid,is_active,sort_order)
VALUES
 ('rookie','Rookie Miner',1000000000,15,TRUE,TRUE,2),
 ('advanced','Advanced Miner',2000000000,35,TRUE,TRUE,3),
 ('pro','Pro Miner',3000000000,50,TRUE,TRUE,4),
 ('elite','Elite Miner',5000000000,75,TRUE,TRUE,5),
 ('titan','Titan Miner',7500000000,100,TRUE,TRUE,6),
 ('free','Free Miner',0,18,FALSE,TRUE,1)
ON CONFLICT(miner_code) DO UPDATE SET
 miner_name=EXCLUDED.miner_name,price_nano=EXCLUDED.price_nano,
 mining_rate_per_hour=EXCLUDED.mining_rate_per_hour,is_paid=EXCLUDED.is_paid,
 is_active=EXCLUDED.is_active,sort_order=EXCLUDED.sort_order;

-- Atomic payment decision. Only a payment that was already TON-verified and
-- moved to awaiting_admin by azm_claim_payment_request can be approved.
CREATE OR REPLACE FUNCTION public.azm_decide_payment(
 p_request_id UUID,p_approved BOOLEAN,p_admin_note TEXT DEFAULT NULL
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE v_req public.azm_payment_requests%ROWTYPE;
BEGIN
 SELECT * INTO v_req FROM public.azm_payment_requests WHERE id=p_request_id FOR UPDATE;
 IF NOT FOUND OR v_req.status<>'awaiting_admin' THEN
  RETURN jsonb_build_object('ok',false,'reason','request_not_pending');
 END IF;
 IF p_approved THEN
  UPDATE public.azm_payment_requests SET status='approved',decided_at=NOW(),admin_note=p_admin_note WHERE id=p_request_id;
  IF v_req.payment_type='verification' THEN
   UPDATE public.azm_users SET is_verified=TRUE,verification_approved_at=COALESCE(verification_approved_at,NOW()),updated_at=NOW()
    WHERE telegram_user_id=v_req.telegram_user_id;
   INSERT INTO public.azm_user_miners(telegram_user_id,miner_code,payment_request_id)
    VALUES(v_req.telegram_user_id,'free',v_req.id) ON CONFLICT(telegram_user_id,miner_code) DO NOTHING;
   INSERT INTO public.azm_transactions(telegram_user_id,transaction_type,amount,status,reference_id,description)
    VALUES(v_req.telegram_user_id,'verification_payment',-0.5,'completed',v_req.id::TEXT,'AZM account verification payment');
  ELSE
   INSERT INTO public.azm_user_miners(telegram_user_id,miner_code,payment_request_id)
    VALUES(v_req.telegram_user_id,v_req.miner_code,v_req.id)
    ON CONFLICT(telegram_user_id,miner_code) DO NOTHING;
   INSERT INTO public.azm_transactions(telegram_user_id,transaction_type,amount,status,reference_id,description)
    VALUES(v_req.telegram_user_id,'miner_purchase',0,'completed',v_req.id::TEXT,'Purchased miner: '||v_req.miner_code);
  END IF;
  RETURN jsonb_build_object('ok',true,'status','approved','payment_type',v_req.payment_type);
 ELSE
  UPDATE public.azm_payment_requests SET status='rejected',decided_at=NOW(),admin_note=p_admin_note WHERE id=p_request_id;
  RETURN jsonb_build_object('ok',true,'status','rejected');
 END IF;
END; $$;

CREATE OR REPLACE FUNCTION public.azm_daily_checkin(p_telegram_user_id TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE v_day SMALLINT; v_reward NUMERIC(24,3); v_balance NUMERIC(24,3); v_date DATE:=CURRENT_DATE;
BEGIN
 PERFORM 1 FROM public.azm_users WHERE telegram_user_id=p_telegram_user_id FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'reason','user_not_found'); END IF;
 IF EXISTS(SELECT 1 FROM public.azm_daily_checkins WHERE telegram_user_id=p_telegram_user_id AND checkin_date=v_date) THEN
  RETURN jsonb_build_object('ok',false,'reason','already_checked_in_today');
 END IF;
 SELECT CASE WHEN COALESCE(MAX(day_number),0)>=30 THEN 1 ELSE COALESCE(MAX(day_number),0)+1 END
  INTO v_day FROM public.azm_daily_checkins WHERE telegram_user_id=p_telegram_user_id;
 SELECT reward_amount INTO v_reward FROM public.azm_checkin_rewards WHERE day_number=v_day;
 IF v_reward IS NULL THEN RETURN jsonb_build_object('ok',false,'reason','checkin_rewards_not_configured'); END IF;
 INSERT INTO public.azm_daily_checkins(telegram_user_id,checkin_date,day_number,reward_amount)
  VALUES(p_telegram_user_id,v_date,v_day,v_reward);
 UPDATE public.azm_users SET balance=balance+v_reward,updated_at=NOW() WHERE telegram_user_id=p_telegram_user_id RETURNING balance INTO v_balance;
 INSERT INTO public.azm_balance_ledger(telegram_user_id,entry_type,amount,balance_after,reference_id,description)
  VALUES(p_telegram_user_id,'daily_checkin',v_reward,v_balance,v_date::TEXT,'Daily check-in day '||v_day);
 INSERT INTO public.azm_transactions(telegram_user_id,transaction_type,amount,status,reference_id,description)
  VALUES(p_telegram_user_id,'daily_checkin',v_reward,'completed',v_date::TEXT,'Daily check-in reward');
 RETURN jsonb_build_object('ok',true,'day_number',v_day,'reward_amount',v_reward,'balance',v_balance);
END; $$;

CREATE OR REPLACE FUNCTION public.azm_claim_mining(p_telegram_user_id TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE v_now TIMESTAMPTZ:=NOW(); v_reward NUMERIC(24,3); v_balance NUMERIC(24,3); v_count INTEGER;
BEGIN
 PERFORM 1 FROM public.azm_users WHERE telegram_user_id=p_telegram_user_id FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'reason','user_not_found'); END IF;
 SELECT COUNT(*) INTO v_count FROM public.azm_user_miners um
  JOIN public.azm_miners m ON m.miner_code=um.miner_code
  WHERE um.telegram_user_id=p_telegram_user_id AND um.is_active AND m.is_active;
 IF v_count=0 THEN RETURN jsonb_build_object('ok',false,'reason','no_active_miner'); END IF;
 -- Accrue each miner independently so a recently purchased miner cannot
 -- receive rewards for hours before it was purchased.
 SELECT ROUND(COALESCE(SUM(m.mining_rate_per_hour *
    LEAST(8,GREATEST(0,EXTRACT(EPOCH FROM (v_now-um.last_claim_at))/3600.0))),0),3)
  INTO v_reward
  FROM public.azm_user_miners um JOIN public.azm_miners m ON m.miner_code=um.miner_code
  WHERE um.telegram_user_id=p_telegram_user_id AND um.is_active AND m.is_active;
 IF v_reward<=0 THEN RETURN jsonb_build_object('ok',false,'reason','mining_not_ready'); END IF;
 UPDATE public.azm_user_miners SET last_claim_at=v_now WHERE telegram_user_id=p_telegram_user_id AND is_active;
 UPDATE public.azm_users SET balance=balance+v_reward,updated_at=v_now WHERE telegram_user_id=p_telegram_user_id RETURNING balance INTO v_balance;
 INSERT INTO public.azm_balance_ledger(telegram_user_id,entry_type,amount,balance_after,reference_id,description)
  VALUES(p_telegram_user_id,'mining_claim',v_reward,v_balance,NULL,'Claimed mining rewards');
 INSERT INTO public.azm_transactions(telegram_user_id,transaction_type,amount,status,description)
  VALUES(p_telegram_user_id,'mining_claim',v_reward,'completed','Mining reward claim');
 RETURN jsonb_build_object('ok',true,'reward_amount',v_reward,'balance',v_balance);
END; $$;

CREATE OR REPLACE FUNCTION public.azm_decide_special_task(
 p_submission_id UUID,p_approved BOOLEAN,p_reward_amount NUMERIC DEFAULT 0,p_admin_note TEXT DEFAULT NULL
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE v_req public.azm_special_task_submissions%ROWTYPE; v_balance NUMERIC(24,3); v_reward NUMERIC(24,3);
BEGIN
 SELECT * INTO v_req FROM public.azm_special_task_submissions WHERE id=p_submission_id FOR UPDATE;
 IF NOT FOUND OR v_req.status<>'awaiting_admin' THEN RETURN jsonb_build_object('ok',false,'reason','submission_not_pending'); END IF;
 IF p_approved THEN
  v_reward=GREATEST(COALESCE(p_reward_amount,0),0);
  UPDATE public.azm_users SET balance=balance+v_reward,updated_at=NOW() WHERE telegram_user_id=v_req.telegram_user_id RETURNING balance INTO v_balance;
  UPDATE public.azm_special_task_submissions SET status='approved',reward_amount=v_reward,admin_note=p_admin_note,decided_at=NOW() WHERE id=p_submission_id;
  IF v_reward>0 THEN
   INSERT INTO public.azm_balance_ledger(telegram_user_id,entry_type,amount,balance_after,reference_id,description)
    VALUES(v_req.telegram_user_id,'task_reward',v_reward,v_balance,p_submission_id::TEXT,'Approved special task: '||v_req.target_channel);
   INSERT INTO public.azm_transactions(telegram_user_id,transaction_type,amount,status,reference_id,description)
    VALUES(v_req.telegram_user_id,'task_reward',v_reward,'completed',p_submission_id::TEXT,'Special task reward');
  END IF;
  RETURN jsonb_build_object('ok',true,'status','approved','reward_amount',v_reward);
 ELSE
  UPDATE public.azm_special_task_submissions SET status='rejected',admin_note=p_admin_note,decided_at=NOW() WHERE id=p_submission_id;
  RETURN jsonb_build_object('ok',true,'status','rejected');
 END IF;
END; $$;

REVOKE ALL ON FUNCTION public.azm_decide_payment(UUID,BOOLEAN,TEXT) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.azm_decide_payment(UUID,BOOLEAN,TEXT) TO service_role;
REVOKE ALL ON FUNCTION public.azm_daily_checkin(TEXT) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.azm_daily_checkin(TEXT) TO service_role;
REVOKE ALL ON FUNCTION public.azm_claim_mining(TEXT) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.azm_claim_mining(TEXT) TO service_role;
REVOKE ALL ON FUNCTION public.azm_decide_special_task(UUID,BOOLEAN,NUMERIC,TEXT) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.azm_decide_special_task(UUID,BOOLEAN,NUMERIC,TEXT) TO service_role;
