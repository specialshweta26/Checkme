# Plans and billing

Lumio sells one paid plan, **Pro**, through Stripe. It's off until the Stripe keys are set. Without them, everyone is
on the free plan and the billing page says paid plans aren't offered.

## What each plan gets

- **Free:** the daily cap from `DAILY_USER_TOKEN_BUDGET` / `DAILY_USER_BUDGET_USD` (docs/ROLLOUT.md).
- **Pro:** `PLAN_PRO_TOKEN_BUDGET` / `PLAN_PRO_BUDGET_USD`. 0 means no daily cap.
- `BUDGET_EXEMPT` emails have no cap on either plan.

## How it works

- **Plan and billing** (`/billing`) shows the plan, today's use against the limit, and buttons.
- **Upgrade to Pro** opens Stripe Checkout for `STRIPE_PRICE_PRO`, with the user's id attached.
- **Manage billing** opens Stripe's customer portal (card, invoices, cancel).
- Lumio never sees card details.
- Plans change only through Stripe's signed webhooks (`POST /api/billing/webhook`), recorded in the `subscriptions`
  table and the audit log:
  - `checkout.session.completed` links the Stripe customer and starts Pro.
  - `customer.subscription.updated` keeps the status and renewal date. `active`, `trialing` and `past_due` (while
    Stripe retries the card) count as Pro.
  - `customer.subscription.deleted` returns the user to Free.
- An unsigned or wrongly signed webhook gets 400 and changes nothing. So does one older than five minutes.

## Setting it up

1. In Stripe, create a product "Lumio Pro" with a recurring price and copy its id (`price_…`).
2. Add a webhook endpoint `https://<your web address>/api/billing/webhook` for these events:
   - `checkout.session.completed`
   - `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`

   Copy its signing secret (`whsec_…`).
3. Turn on the customer portal (Settings → Billing → Customer portal).
4. On `checkme-api` and the worker, set:
   - `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO`
   - `PLAN_PRO_TOKEN_BUDGET` (or `PLAN_PRO_BUDGET_USD`)

Start with Stripe's test mode keys (`sk_test_…`) and card 4242 4242 4242 4242 to try the whole flow before going live.
