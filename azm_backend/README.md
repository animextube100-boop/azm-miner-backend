# AZM Miner backend (separate from STORM)

FastAPI backend for the AZM Telegram Mini App. Deploy as a separate Vercel project and connect it only to the separate AZM Supabase project.

## Included

- Telegram Mini App `initData` signature validation
- AZM account bootstrap and referral-code capture
- Supabase service-role access (never expose the key in frontend code)
- 0.5 TON verification payment session
- Miner purchase payment sessions using the AZM miner catalog
- TONAPI trace validation for exact transaction hash, destination and amount
- Admin Confirm/Reject callbacks for payments, withdrawals and special-task submissions
- Withdrawal rules: minimum 7,500 AZM, fixed 500 AZM fee, at least one paid miner, no referral condition
- Wallet saving, payment/withdrawal status, check-in, mining claim and special-task submission endpoints

## Important deployment sequence

1. Confirm the original `azm_miner_initial_schema.sql` has been run in the NEW AZM Supabase project.
2. Run `azm_backend_migration.sql` in that same AZM project's SQL Editor. It seeds the exact miner catalog from the supplied AZM frontend and creates the RPC functions required by this API.
3. Create a separate AZM GitHub repository and upload the contents of this folder (the `api` folder must be at repository root).
4. Import that repository into Vercel. Set the environment variables from `.env.example` in Vercel Project Settings → Environment Variables.
5. Deploy. Test `https://YOUR-BACKEND.vercel.app/health`; expected response includes `"ok": true` and `"service": "azm-miner-api"`.
6. Set the AZM Telegram bot webhook to `https://YOUR-BACKEND.vercel.app/telegram-webhook` with the same `TELEGRAM_WEBHOOK_SECRET` configured in Vercel. Example (replace values):

   `https://api.telegram.org/bot<AZM_ADMIN_BOT_TOKEN>/setWebhook?url=https://YOUR-BACKEND.vercel.app/telegram-webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>`

7. Set `FRONTEND_ORIGIN` to the exact Vercel frontend domain. Add the backend URL to the frontend as `AZM_API_BASE_URL` (or replace its placeholder in the HTML).

## Environment variables

See `.env.example`. Use the AZM project's Supabase URL and service-role/secret key. Do not use STORM database credentials or bot tokens. Do not put `SUPABASE_SECRET_KEY`, bot tokens, or TONAPI key in the frontend.

`SPECIAL_TASK_REWARD` defaults to 0. Choose the reward before enabling paid approval of NEJARS/Promoter0007 submissions.

## API routes

All user routes except `/health` require JSON containing Telegram `init_data`:

- `POST /bootstrap`
- `POST /me`
- `POST /save-wallet` (`wallet_address`)
- `POST /create-payment` (`payment_type: verification` OR `payment_type: miner` and `miner_code`)
- `POST /verify-payment` (`payment_request_id`, `tx_hash`)
- `POST /payment-status`
- `POST /create-withdrawal` (`amount`, `wallet_address`)
- `POST /withdrawal-status`
- `POST /submit-special-task` (`target_channel: NEJARS|Promoter0007`, `post_url`)
- `POST /checkin`
- `POST /claim-mining`
- `POST /telegram-webhook` (Telegram only; secret header required)

Miner codes: `rookie`, `advanced`, `pro`, `elite`, `titan`. The free miner is granted when verification is approved.

## Notes / current frontend integration gaps

The uploaded AZM frontend currently connects only to `/create-payment` and `/verify-payment`; its purchase, withdrawal, wallet-save, mining-claim, check-in, profile and special-task UI actions are still preview/demo handlers. Those frontend handlers must be wired to the endpoints above before those features work end-to-end. The API does not pretend those preview buttons are already connected.

The mining RPC accrues up to 8 hours per claim using the active miner rates. The check-in RPC uses the displayed 30-day rewards (10/20/50 AZM). Community tasks are returned as redirect links and do not perform membership verification.

## Local run

Python 3.11+:

`pip install -r requirements.txt`

Set environment variables, then:

`uvicorn api.index:app --host 0.0.0.0 --port 8000`
