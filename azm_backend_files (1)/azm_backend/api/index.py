import os, re, hmac, json, time, uuid, base64, hashlib, logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
from decimal import Decimal, InvalidOperation
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

log = logging.getLogger('azm-backend')
app = FastAPI(title='AZM Miner API', version='1.0.0')

FRONTEND_ORIGIN = os.getenv('FRONTEND_ORIGIN', '').rstrip('/')
MINI_APP_BOT_TOKEN = os.getenv('MINI_APP_BOT_TOKEN', '')
ADMIN_BOT_TOKEN = os.getenv('ADMIN_BOT_TOKEN', '')
ADMIN_TELEGRAM_ID = os.getenv('ADMIN_TELEGRAM_ID', '').strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET', '').strip()
SUPABASE_URL = os.getenv('SUPABASE_URL', '').rstrip('/')
SUPABASE_SECRET_KEY = os.getenv('SUPABASE_SECRET_KEY', '')
TONAPI_KEY = os.getenv('TONAPI_KEY', '')
TONAPI_BASE_URL = os.getenv('TONAPI_BASE_URL', 'https://tonapi.io').rstrip('/')
PAYMENT_WALLET = os.getenv('PAYMENT_WALLET', 'UQCo0-D4DWT4Q99BJ35ONA7wduh_qtNwwpni55UyMhsFcY-x').strip()
PAYMENT_WINDOW_SECONDS = int(os.getenv('PAYMENT_WINDOW_SECONDS', '600'))
INIT_DATA_MAX_AGE = int(os.getenv('INIT_DATA_MAX_AGE', '86400'))
SPECIAL_TASK_REWARD = Decimal(os.getenv('SPECIAL_TASK_REWARD', '0'))

app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN else [], allow_credentials=False, allow_methods=['GET','POST','OPTIONS'], allow_headers=['Content-Type','X-Telegram-Bot-Api-Secret-Token'])

class InitBody(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)
class CreatePaymentBody(InitBody):
    payment_type: str = Field(pattern='^(verification|miner)$')
    miner_code: str | None = None
class VerifyPaymentBody(InitBody):
    payment_request_id: uuid.UUID
    tx_hash: str = Field(min_length=8, max_length=512)
class WalletBody(InitBody):
    wallet_address: str = Field(min_length=24, max_length=128)
class WithdrawalBody(WalletBody):
    amount: Decimal = Field(ge=Decimal('7500'), le=Decimal('1000000000'))
class SpecialTaskBody(InitBody):
    target_channel: str = Field(pattern='^(NEJARS|Promoter0007)$')
    post_url: str = Field(min_length=12, max_length=500)
class CheckinBody(InitBody):
    pass
class MiningClaimBody(InitBody):
    pass

HEX64 = re.compile(r'^[0-9a-fA-F]{64}$')
B64URL = re.compile(r'^[A-Za-z0-9_-]{43}={0,1}$')

# ----------------------------- shared helpers -----------------------------
def now(): return datetime.now(timezone.utc)
def iso(dt): return dt.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
def parse_user(init_data: str):
    if not MINI_APP_BOT_TOKEN: raise HTTPException(500, 'MINI_APP_BOT_TOKEN is not configured')
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        values = dict(pairs); received = values.pop('hash')
        auth_date = int(values.get('auth_date','0'))
    except Exception: raise HTTPException(401, 'Invalid Telegram initData')
    if auth_date <= 0 or time.time()-auth_date > INIT_DATA_MAX_AGE or auth_date-time.time()>60:
        raise HTTPException(401, 'Telegram initData expired or invalid')
    check = '\n'.join(f'{k}={v}' for k,v in sorted(values.items()))
    secret = hmac.new(b'WebAppData', MINI_APP_BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received): raise HTTPException(401, 'Telegram initData signature is invalid')
    try:
        user = json.loads(values['user'])
        return str(int(user['id'])), user, values.get('start_param','')
    except Exception: raise HTTPException(401, 'Telegram user data missing')

async def sb(method, path, *, params=None, body=None, prefer='return=representation'):
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY: raise HTTPException(500, 'Supabase environment variables are missing')
    headers={'apikey':SUPABASE_SECRET_KEY,'Authorization':f'Bearer {SUPABASE_SECRET_KEY}','Content-Type':'application/json'}
    if prefer: headers['Prefer']=prefer
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            return await c.request(method, f'{SUPABASE_URL}/rest/v1/{path}', params=params, json=body, headers=headers)
    except httpx.HTTPError: raise HTTPException(503, 'Database temporarily unavailable')

async def rpc(name, args):
    r=await sb('POST',f'rpc/{name}',body=args)
    if r.status_code not in (200,201,204):
        log.error('RPC %s failed: %s %s',name,r.status_code,r.text[:500]); raise HTTPException(502,f'Database operation {name} failed')
    return r.json() if r.content else None

def rows_or_502(r, what):
    if r.status_code not in (200,201,204):
        log.error('%s failed: %s %s',what,r.status_code,r.text[:400]); raise HTTPException(502,f'Could not {what}')
    return r.json() if r.content else []

def username_from(user): return user.get('username')
def norm_hash(value):
    h=value.strip()
    if '://' in h: h=h.split('?')[0].split('#')[0].rstrip('/').split('/')[-1]
    if HEX64.fullmatch(h) or B64URL.fullmatch(h): return h
    raise HTTPException(400,'Invalid transaction hash or explorer link')

# ------------------------------- TON helpers -------------------------------
def crc16(data):
    crc=0
    for b in data:
        crc ^= b << 8
        for _ in range(8): crc=((crc<<1)^0x1021) if crc&0x8000 else (crc<<1); crc &= 0xffff
    return crc

def parse_ton_address(addr):
    addr=(addr or '').strip()
    if ':' in addr:
        wc,h=addr.split(':',1)
        if not HEX64.fullmatch(h): raise ValueError('bad raw address')
        return int(wc),bytes.fromhex(h)
    if len(addr)!=48: raise ValueError('bad friendly address length')
    raw=base64.urlsafe_b64decode(addr.replace('+','-').replace('/','_'))
    if len(raw)!=36 or crc16(raw[:34])!=int.from_bytes(raw[34:],'big'): raise ValueError('bad TON address checksum')
    if (raw[0]&0x7f) not in (0x11,0x51): raise ValueError('bad TON address tag')
    return int.from_bytes(raw[1:2],'big',signed=True),raw[2:34]

def same_address(a,b):
    try:return parse_ton_address(a)==parse_ton_address(b)
    except Exception:return False

def walk(value):
    if isinstance(value,dict):
        yield value
        for v in value.values(): yield from walk(v)
    elif isinstance(value,list):
        for v in value: yield from walk(v)

def tx_hash_matches(item, submitted):
    h=item.get('hash') or item.get('transaction_id',{}).get('hash') if isinstance(item.get('transaction_id'),dict) else item.get('hash')
    return isinstance(h,str) and h.lower()==submitted.lower()

async def verify_ton_transfer(tx_hash, destination, amount_nano):
    if not TONAPI_KEY: raise HTTPException(500,'TONAPI_KEY is not configured; payment verification is disabled')
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r=await c.get(f'{TONAPI_BASE_URL}/v2/traces/{tx_hash}',headers={'Authorization':f'Bearer {TONAPI_KEY}'})
    except httpx.HTTPError: raise HTTPException(503,'TON API temporarily unavailable')
    if r.status_code==404: raise HTTPException(404,'Transaction not found yet. Wait and retry.')
    if r.status_code==429: raise HTTPException(503,'TON API rate limit. Retry shortly.')
    if r.status_code!=200: raise HTTPException(502,'TON transaction lookup failed')
    trace=r.json(); wanted=None
    for item in walk(trace):
        if not tx_hash_matches(item,tx_hash): continue
        msg=item.get('in_msg') or item.get('in_message') or {}
        dest=msg.get('destination') or msg.get('dest')
        if isinstance(dest,dict): dest=dest.get('address')
        value=msg.get('value') or msg.get('amount')
        try: value=int(value)
        except Exception: continue
        success=item.get('success') is True
        if success and same_address(str(dest or ''),destination) and value==int(amount_nano):
            wanted={'sender':msg.get('source') or msg.get('src'),'destination':dest,'amount_nano':value}
            break
    if not wanted: raise HTTPException(400,'Transaction does not match this payment request (hash, destination, amount, or success).')
    return wanted

# ------------------------------- Telegram ----------------------------------
async def tg(method,payload):
    if not ADMIN_BOT_TOKEN: raise HTTPException(500,'ADMIN_BOT_TOKEN is not configured')
    try:
        async with httpx.AsyncClient(timeout=15) as c: r=await c.post(f'https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/{method}',json=payload)
    except httpx.HTTPError: raise HTTPException(503,'Telegram API unavailable')
    data=r.json()
    if r.status_code!=200 or not data.get('ok'):
        log.error('Telegram %s error: %s',method,r.text[:500]); raise HTTPException(502,f'Telegram {method} failed')
    return data.get('result')

async def notify_admin_payment(req):
    uname='@'+req['telegram_username'] if req.get('telegram_username') else '(none)'
    amount=int(req['amount_nano'])/1e9
    label='Account verification' if req['payment_type']=='verification' else f"Miner purchase ({req.get('miner_code')})"
    msg=(f'🟡 AZM PAYMENT REQUEST\n\nUser: {req["telegram_user_id"]}\nUsername: {uname}\nType: {label}\nAmount: {amount:g} TON\n'
         f'Transaction: <code>{req["tx_hash"]}</code>\nRequest: <code>{req["id"]}</code>\n\nTON transfer was checked by the backend. Confirm only after reviewing it.')
    return await tg('sendMessage',{'chat_id':ADMIN_TELEGRAM_ID,'text':msg,'parse_mode':'HTML','reply_markup':{'inline_keyboard':[[{'text':'✅ Confirm','callback_data':f'azmpay:approve:{req["id"]}'},{'text':'❌ Reject','callback_data':f'azmpay:reject:{req["id"]}'}]]}})

async def notify_admin_withdrawal(req):
    msg=(f'🟡 AZM WITHDRAWAL REQUEST\n\nUser: {req["telegram_user_id"]}\nUsername: @{req.get("telegram_username") or "(none)"}\n'
         f'Gross: {req["gross_amount"]} AZM\nFee: 500 AZM\nNet payout: {req["payout_amount"]} AZM\nTON wallet: <code>{req["wallet_address"]}</code>\nRequest: <code>{req["id"]}</code>\n\nSend the net payout manually, then Confirm. Reject refunds the gross amount.')
    return await tg('sendMessage',{'chat_id':ADMIN_TELEGRAM_ID,'text':msg,'parse_mode':'HTML','reply_markup':{'inline_keyboard':[[{'text':'✅ Confirm','callback_data':f'azmwd:approve:{req["id"]}'},{'text':'❌ Reject','callback_data':f'azmwd:reject:{req["id"]}'}]]}})

async def notify_special_task(req):
    msg=(f'📣 AZM SPECIAL TASK SUBMISSION\n\nUser: {req["telegram_user_id"]}\nUsername: @{req.get("telegram_username") or "(none)"}\nTarget: {req["target_channel"]}\nPost: {req["post_url"]}\nSubmission: <code>{req["id"]}</code>\nReward if approved: {req["reward_amount"]} AZM')
    return await tg('sendMessage',{'chat_id':ADMIN_TELEGRAM_ID,'text':msg,'disable_web_page_preview':True,'reply_markup':{'inline_keyboard':[[{'text':'✅ Confirm','callback_data':f'azmst:approve:{req["id"]}'},{'text':'❌ Reject','callback_data':f'azmst:reject:{req["id"]}'}]]}})

async def answer_callback(callback_id,text,alert=False):
    try: await tg('answerCallbackQuery',{'callback_query_id':callback_id,'text':text,'show_alert':alert})
    except HTTPException: log.exception('callback answer failed')

# --------------------------------- API -------------------------------------
@app.get('/health')
async def health(): return {'ok':True,'service':'azm-miner-api','version':'1.0.0'}

@app.post('/bootstrap')
async def bootstrap(body:InitBody):
    uid,user,start_param=parse_user(body.init_data)
    ref_code='AZM'+hashlib.sha256(uid.encode()).hexdigest()[:10].upper()
    referrer=None
    if start_param.startswith('ref_'):
        lookup=await sb('GET','azm_users',params={'referral_code':f'eq.{start_param[4:]}','select':'telegram_user_id','limit':'1'})
        found=rows_or_502(lookup,'look up referral code')
        if found and found[0]['telegram_user_id']!=uid: referrer=found[0]['telegram_user_id']
    record={'telegram_user_id':uid,'telegram_username':username_from(user),'first_name':user.get('first_name'),'referral_code':ref_code}
    if referrer: record['referred_by']=referrer
    saved=await sb('POST','azm_users',params={'on_conflict':'telegram_user_id'},body=record,prefer='resolution=merge-duplicates,return=representation')
    rows=rows_or_502(saved,'create account')
    if referrer:
        await sb('POST','azm_referrals',body={'referrer_telegram_user_id':referrer,'referred_telegram_user_id':uid},prefer='resolution=ignore-duplicates,return=minimal')
    return {'ok':True,'user':rows[0] if rows else record}

@app.post('/me')
async def me(body:InitBody):
    uid,user,_=parse_user(body.init_data)
    u=rows_or_502(await sb('GET','azm_users',params={'telegram_user_id':f'eq.{uid}','select':'*','limit':'1'}),'load account')
    if not u: raise HTTPException(404,'Account not initialized. Call /bootstrap first.')
    miners=rows_or_502(await sb('GET','azm_user_miners',params={'telegram_user_id':f'eq.{uid}','is_active':'eq.true','select':'*,azm_miners(miner_code,miner_name,mining_rate_per_hour,is_paid)'}),'load miners')
    tasks=rows_or_502(await sb('GET','azm_community_tasks',params={'is_active':'eq.true','select':'task_code,title,target_url,reward_amount,sort_order','order':'sort_order.asc'}),'load tasks')
    return {'ok':True,'user':u[0],'miners':miners,'tasks':tasks,'referral_link':f"https://t.me/{os.getenv('MINI_APP_BOT_USERNAME','YOUR_BOT_USERNAME')}?start=ref_{u[0]['referral_code']}"}

@app.post('/save-wallet')
async def save_wallet(body:WalletBody):
    uid,user,_=parse_user(body.init_data)
    try: parse_ton_address(body.wallet_address)
    except ValueError: raise HTTPException(400,'Enter a valid TON wallet address')
    r=await sb('PATCH','azm_users',params={'telegram_user_id':f'eq.{uid}'},body={'wallet_address':body.wallet_address.strip(),'updated_at':iso(now())},prefer='return=representation')
    rows=rows_or_502(r,'save wallet')
    if not rows: raise HTTPException(404,'Account not found')
    return {'ok':True,'wallet_address':rows[0]['wallet_address']}

@app.post('/create-payment')
async def create_payment(body:CreatePaymentBody):
    uid,user,_=parse_user(body.init_data)
    if body.payment_type=='verification':
        if body.miner_code: raise HTTPException(400,'Verification request must not include miner_code')
        amount=500_000_000; miner=None
    else:
        if not body.miner_code: raise HTTPException(400,'miner_code is required for miner purchases')
        m=rows_or_502(await sb('GET','azm_miners',params={'miner_code':f'eq.{body.miner_code}','is_active':'eq.true','select':'*','limit':'1'}),'load miner')
        if not m or not m[0]['is_paid']: raise HTTPException(400,'Unknown paid miner')
        amount=int(m[0]['price_nano']); miner=m[0]['miner_code']
    if not PAYMENT_WALLET: raise HTTPException(500,'PAYMENT_WALLET is not configured')
    try: parse_ton_address(PAYMENT_WALLET)
    except ValueError: raise HTTPException(500,'PAYMENT_WALLET is not a valid TON address')
    t=now(); expires=t+timedelta(seconds=PAYMENT_WINDOW_SECONDS)
    pending=rows_or_502(await sb('GET','azm_payment_requests',params={'telegram_user_id':f'eq.{uid}','status':'eq.pending','expires_at':f'gt.{iso(t)}','select':'id','limit':'5'}),'check open payments')
    if len(pending)>=5: raise HTTPException(429,'Too many open payment requests')
    rec={'telegram_user_id':uid,'telegram_username':username_from(user),'payment_type':body.payment_type,'miner_code':miner,'amount_nano':amount,'destination_address':PAYMENT_WALLET,'status':'pending','created_at':iso(t),'expires_at':iso(expires)}
    saved=rows_or_502(await sb('POST','azm_payment_requests',body=rec),'create payment request')
    row=saved[0]
    return {'ok':True,'payment_request_id':row['id'],'payment_type':body.payment_type,'amount_ton':amount/1e9,'amount_nano':amount,'destination_address':PAYMENT_WALLET,'expires_in':PAYMENT_WINDOW_SECONDS,'created_at':iso(t),'expires_at':iso(expires)}

@app.post('/verify-payment')
async def verify_payment(body:VerifyPaymentBody):
    uid,user,_=parse_user(body.init_data); rid=str(body.payment_request_id); tx=norm_hash(body.tx_hash)
    reqs=rows_or_502(await sb('GET','azm_payment_requests',params={'id':f'eq.{rid}','telegram_user_id':f'eq.{uid}','select':'*','limit':'1'}),'load payment request')
    if not reqs: raise HTTPException(404,'Payment request not found')
    req=reqs[0]
    if req['status']!='pending': raise HTTPException(409,'Payment request already submitted')
    if now()>datetime.fromisoformat(req['expires_at'].replace('Z','+00:00')): raise HTTPException(410,'Payment session expired')
    transfer=await verify_ton_transfer(tx,req['destination_address'],req['amount_nano'])
    claim=await rpc('azm_claim_payment_request',{'p_request_id':rid,'p_telegram_user_id':uid,'p_tx_hash':tx,'p_sender_address':transfer.get('sender')})
    if not claim or not claim.get('ok'):
        reason=(claim or {}).get('reason','payment_claim_failed')
        raise HTTPException(409,reason.replace('_',' '))
    req.update({'status':'awaiting_admin','tx_hash':tx,'sender_address':transfer.get('sender'),'telegram_username':username_from(user)})
    try: await notify_admin_payment(req)
    except Exception:
        log.exception('Admin notification failed for payment %s',rid)
        raise HTTPException(503,'Payment was verified and queued, but admin notification failed. Contact support; do not submit another payment.')
    return {'ok':True,'verified':True,'pending':True,'status':'awaiting_admin','payment_request_id':rid,'message':'TON transfer verified. Waiting for admin approval.'}

@app.post('/payment-status')
async def payment_status(body:InitBody):
    uid,_,_=parse_user(body.init_data)
    payments=rows_or_502(await sb('GET','azm_payment_requests',params={'telegram_user_id':f'eq.{uid}','select':'id,payment_type,miner_code,amount_nano,status,tx_hash,created_at','order':'created_at.desc','limit':'100'}),'load payment status')
    user=rows_or_502(await sb('GET','azm_users',params={'telegram_user_id':f'eq.{uid}','select':'is_verified','limit':'1'}),'load verification status')
    return {'ok':True,'verified':bool(user and user[0]['is_verified']),'payments':payments}

@app.post('/create-withdrawal')
async def create_withdrawal(body:WithdrawalBody):
    uid,user,_=parse_user(body.init_data)
    try: parse_ton_address(body.wallet_address)
    except ValueError: raise HTTPException(400,'Enter a valid TON wallet address')
    result=await rpc('azm_create_withdrawal',{'p_telegram_user_id':uid,'p_telegram_username':username_from(user),'p_gross_amount':str(body.amount),'p_wallet_address':body.wallet_address.strip()})
    if not result or not result.get('ok'):
        reason=(result or {}).get('reason','withdrawal_failed')
        messages={'minimum_withdrawal_7500':'Minimum withdrawal is 7,500 AZM','paid_miner_required':'Purchase at least one paid miner first','insufficient_balance':'Insufficient AZM balance','withdrawal_already_pending':'You already have a pending withdrawal','wallet_required':'TON wallet is required'}
        raise HTTPException(400,messages.get(reason,reason.replace('_',' ')))
    reqs=rows_or_502(await sb('GET','azm_withdrawal_requests',params={'id':f"eq.{result['withdrawal_request_id']}",'select':'*','limit':'1'}),'load created withdrawal')
    try: await notify_admin_withdrawal(reqs[0])
    except Exception:
        log.exception('Withdrawal admin notification failed')
        raise HTTPException(503,'Withdrawal saved but admin notification failed. Contact support; do not submit again.')
    return {'ok':True,**result,'status':'awaiting_admin'}

@app.post('/withdrawal-status')
async def withdrawal_status(body:InitBody):
    uid,_,_=parse_user(body.init_data)
    items=rows_or_502(await sb('GET','azm_withdrawal_requests',params={'telegram_user_id':f'eq.{uid}','select':'*','order':'created_at.desc','limit':'50'}),'load withdrawal status')
    return {'ok':True,'withdrawals':items}

@app.post('/submit-special-task')
async def submit_special_task(body:SpecialTaskBody):
    uid,user,_=parse_user(body.init_data)
    url=body.post_url.strip()
    if not re.match(r'^https://t\.me/[A-Za-z0-9_+/.-]+',url): raise HTTPException(400,'Submit a valid public Telegram post link')
    rec={'telegram_user_id':uid,'telegram_username':username_from(user),'target_channel':body.target_channel,'post_url':url,'status':'awaiting_admin','reward_amount':str(SPECIAL_TASK_REWARD)}
    saved=await sb('POST','azm_special_task_submissions',body=rec)
    rows=rows_or_502(saved,'save special-task submission')
    try: await notify_special_task(rows[0])
    except Exception: log.exception('Special task admin notification failed'); raise HTTPException(503,'Submission saved but admin notification failed; contact support')
    return {'ok':True,'submission_id':rows[0]['id'],'status':'awaiting_admin'}

@app.post('/checkin')
async def checkin(body:CheckinBody):
    uid,_,_=parse_user(body.init_data)
    result=await rpc('azm_daily_checkin',{'p_telegram_user_id':uid})
    if not result or not result.get('ok'): raise HTTPException(409,(result or {}).get('reason','checkin unavailable').replace('_',' '))
    return result

@app.post('/claim-mining')
async def claim_mining(body:MiningClaimBody):
    uid,_,_=parse_user(body.init_data)
    result=await rpc('azm_claim_mining',{'p_telegram_user_id':uid})
    if not result or not result.get('ok'): raise HTTPException(409,(result or {}).get('reason','mining claim unavailable').replace('_',' '))
    return result

@app.post('/telegram-webhook')
async def telegram_webhook(request:Request):
    if not TELEGRAM_WEBHOOK_SECRET: raise HTTPException(500,'TELEGRAM_WEBHOOK_SECRET is not configured')
    supplied=request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
    if not hmac.compare_digest(supplied,TELEGRAM_WEBHOOK_SECRET): raise HTTPException(403,'Invalid webhook secret')
    update=await request.json(); cb=update.get('callback_query')
    if not cb: return {'ok':True}
    cbid=cb.get('id',''); actor=str(cb.get('from',{}).get('id','')); data=cb.get('data','')
    if actor!=ADMIN_TELEGRAM_ID:
        await answer_callback(cbid,'Not authorized',True); return {'ok':True}
    parts=data.split(':')
    if len(parts)!=3 or parts[1] not in ('approve','reject') or parts[0] not in ('azmpay','azmwd','azmst'):
        await answer_callback(cbid,'Invalid action',True); return {'ok':True}
    kind,action,rid=parts; approve=action=='approve'
    try:
        if kind=='azmpay': result=await rpc('azm_decide_payment',{'p_request_id':rid,'p_approved':approve,'p_admin_note':None})
        elif kind=='azmwd': result=await rpc('azm_decide_withdrawal',{'p_request_id':rid,'p_approved':approve,'p_admin_note':None})
        else: result=await rpc('azm_decide_special_task',{'p_submission_id':rid,'p_approved':approve,'p_reward_amount':str(SPECIAL_TASK_REWARD),'p_admin_note':None})
    except Exception:
        log.exception('Admin decision failed'); await answer_callback(cbid,'Could not save decision. Retry.',True); return {'ok':True}
    if not result or not result.get('ok'):
        await answer_callback(cbid,(result or {}).get('reason','Request already handled').replace('_',' '),True); return {'ok':True}
    await answer_callback(cbid,'Approved' if approve else 'Rejected')
    msg=cb.get('message',{}); chat=msg.get('chat',{}).get('id'); mid=msg.get('message_id')
    if chat and mid:
        try:
            await tg('editMessageReplyMarkup',{'chat_id':chat,'message_id':mid,'reply_markup':{'inline_keyboard':[]}})
            await tg('sendMessage',{'chat_id':chat,'text':f'AZM {kind} request {rid} {"approved" if approve else "rejected"}.'})
        except HTTPException: log.exception('Could not update admin message')
    return {'ok':True}
