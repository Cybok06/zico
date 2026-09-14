# AZICO Support Knowledge Base (From Code Scan)

This document summarizes the app’s features, user flows, roles, and common support actions, based on the current codebase. It is intended for training a support bot. It avoids secrets and API keys.

## 1) What The App Is
AZICO is a multi-tenant web app for selling digital services (telecom bundles/talktime), managing orders, and running public “stores” with Paystack payments. It supports admin, agent, and customer roles, plus a public order-status page.

Key modules and features:
- Customer/Agent dashboard for services, ordering, AFA registration, and order tracking.
- Admin dashboard for services, customers, orders, transactions, balances, and payouts.
- Public store pages with Paystack checkout.
- Complaints workflow with image evidence uploads.
- Referral links, announcements, and a developer Agent API.

## 2) Roles & Access
Roles used in sessions and authorization:
- `main_admin`: highest level, exempt from maintenance billing.
- `admin`: tenant owner (manages services, customers, orders, billing).
- `agent` / `customer`: can buy services, use wallet, and access their dashboard.

Access restrictions:
- Most pages require login and role checks.
- Maintenance overdue locks admins to billing-only, and blocks agents/customers from tenant access until paid.

## 3) Authentication & Sessions
Login:
- Route: `/login` (username + password).
- Blocked users cannot log in.
- Role-based redirect:
  - Admins -> `/admin/dashboard`
  - Agents/Customers -> `/customer/dashboard`

Signup:
- Route: `/signup` (admin signup only).
- Required fields: first name, last name, username, email, phone, business name, WhatsApp, password, confirm password.
- Ghana phone format required: `0XXXXXXXXX`.
- Optional referral code validates against existing referral codes.

Password reset:
- Route: `/forgot-password` (OTP via SMS).
- OTP is 6 digits, expires in 10 minutes.
- Phone format required: Ghana `0XXXXXXXXX`.

Customer profile:
- Route: `/customer/profile` (change password).

Admin profile & billing:
- Route: `/admin/profile` with tabs `profile` and `billing`.
- Admins pay maintenance fees here.

Branded tenant login:
- Admins can create branded auth pages.
- Routes: `/<slug>`, `/<slug>/login`, `/<slug>/signup`, `/<slug>/forgot-password`.

## 4) Wallets, Deposits, and Payments
Paystack is the payment gateway for:
- Wallet deposits (admin + customer/agent deposits).
- Store checkout.
- Maintenance billing.

Deposit (customer/agent):
- Route: `/deposit`, verify at `/verify_transaction`.
- Minimum deposit: GHS 10.
- Fee rate: 0.5% (net credit is computed from gross).

Admin wallet deposit:
- Route: `/admin/wallet`, verify at `/admin/verify_wallet_deposit`.

Maintenance billing:
- Monthly cycle (default 30 days) with a grace period (default 5 days).
- Fees vary by admin level.
- Overdue admins: locked to billing only.
- Overdue admins block agents/customers from tenant access.

Paystack keys config:
- Route: `/admin/settings`
- Separate Store keys + Deposit keys (per tenant for admins; global for main admin).

## 5) Customer/Agent Core Flows
Customer dashboard:
- Route: `/customer/dashboard`
- Shows services, prices, wallet balance, recent orders, sales KPIs, and AFA registration.

Cart API:
- GET `/api/cart`
- POST `/api/cart/add_bulk`
- POST `/api/cart/replace`
- POST `/api/cart/remove`
- POST `/api/cart/clear`
- POST `/api/cart/checkout_start` (snapshots and clears cart)

Validation:
- Phone format must be `0XXXXXXXXX`.

Checkout:
- Route: `/checkout` (POST)
- Invoice: `/invoice/<order_id>`
- Orders are processed through providers and tracked by status.

Orders:
- Route: `/customer/orders`
- Filters: status, date range, order_id, phone.
- Common statuses: `pending`, `processing`, `delivered`, `failed`, `refunded`, `completed`.

Transactions:
- Route: `/customer/transactions`

Order status (public):
- Route: `/check-status`
- Input: phone `0XXXXXXXXX`
- Returns last 50 orders for that phone, including per-line status.

AFA registration:
- API: `POST /api/afa/register`
- Requires name + phone; checks AFA settings (price, open, in-stock).
- Charges customer wallet.

## 6) Complaints
Submit complaint:
- Route: `/complaints` (GET/POST)
- Required: order number + 2 images (data balance + MSISDN).
- Image types: png, jpg, jpeg, webp.
- Max file size: 8MB per image.

View complaints:
- Route: `/view_complaints`
- Filter by status and date range.

## 7) Referrals
- Route: `/referral/invite`
- Generates a referral code and invite link to `/signup?ref=CODE`.

## 8) WASSCE/BECE Checker Purchases
Customer purchase:
- Route: `/purchase_checker` (GET/POST)
- Requires sufficient customer wallet balance.
- Supports `type=wassce` or `type=bece`.

Purchase history:
- Route: `/purchases`

Admin management:
- Route: `/admin/wassce_checker`
- Create, update, delete checkers.

## 9) Stores (Public + Owner Tools)
Public store page:
- `/s/<slug>` or `/store/<slug>`
- Store checkout: `POST /store-checkout/<slug>`
- Store product catalog: `GET /api/store-products/<slug>`

Store creation & management:
- `/create-store` (GET)
- Media upload: `POST /api/media`
- Create store: `POST /api/stores`
- Preview: `POST /api/stores/preview`
- Store status: `POST /api/stores/<slug>/status`
- Delete store: `DELETE /api/stores/<slug>`

Store products:
- Image upload: `POST /api/store-products/upload_image`
- List: `GET /api/store-products/mine`
- Create: `POST /api/store-products`
- Delete: `DELETE /api/store-products/<product_id>`

Store owner payouts:
- Customer store area: `/customer/store`, `/customer/store/<slug>`, payout endpoints under `/api/customer/store/...`
- Admin store area: `/admin/stores` and `/admin/api/stores...` with withdrawal approvals.

## 10) Admin Tools
Admin dashboard:
- `/admin/dashboard`

Customers:
- `/admin/customers`
- Update, block/unblock, delete.

Phone numbers:
- `/admin/phone-numbers`
- Export to Excel/PDF, block/unblock.

Services:
- `/admin/services` and service create/update/delete.
- Pricing overrides (customer and stage).
- Status and availability controls.

Orders:
- `/admin/orders`
- Status updates, bulk deliver, export batches, schedule status updates.

Transactions:
- `/admin/transactions`

Complaints:
- `/admin/complaints`

Referrals:
- `/admin/referrals`

Balances (wallets):
- `/admin/balances` (search, adjust, deposit/withdraw).

Admins:
- `/admin/admins` (eligibility, level changes, block/delete).

Auth page branding:
- `/admin/auth-page`

Announcements:
- `/announcements` (admins can create).

Performance:
- `/admin/performance`

## 11) Agent API (Developer Integration)
Auth:
- API key is required in header `x-api-key` (or query `api_key`).

Key management:
- `/agent/api` (view current key)
- `/agent/api/generate` (generate new key)
- `/agent/api/docs` (docs page)

Endpoints (JSON):
- `GET /api/packages.php` (returns available packages)
- `GET /api/wallet.php` (wallet balance)
- `POST /api/send_order.php` (place order)
- `POST /api/initiate.php` (regular)
- `POST /api/special.php` (bigtime)
- `GET /api/response_regular.php?reference_id=...`
- `GET /api/response_big_time.php?reference_id=...`

Validation:
- Phone must be `0XXXXXXXXX`.

## 12) Common Support Answers (Suggested)
If user can’t log in:
- Check username/password.
- If blocked, they must contact admin support.
- If tenant maintenance is overdue, admin must pay in `/admin/profile?tab=billing`.

If user can’t reset password:
- Ensure phone is Ghana format `0XXXXXXXXX`.
- OTP expires in 10 minutes; request new OTP if expired.

If checkout fails:
- Admin wallet may be insufficient.
- Service may be closed or out of stock.
- Phone number format must be `0XXXXXXXXX`.

If order is delayed:
- Use `/check-status` with phone.
- Check order status (pending/processing vs delivered/failed).
- For failed orders, admins can update or refund in `/admin/orders`.

If complaint rejected:
- Ensure order number is correct.
- Upload both screenshots (balance + phone number).
- File type must be png/jpg/jpeg/webp; size <= 8MB.

If store checkout fails:
- Store Paystack keys may be missing (admin must set keys at `/admin/settings`).

## 13) Environment-Driven Support Contacts (Defaults)
From app context:
- Support email/phone/WhatsApp can be set in env:
  - `SUPPORT_EMAIL`, `SUPPORT_PHONE`, `SUPPORT_WHATSAPP`
- Company name: `COMPANY_NAME`

## 14) Collections (Data Model Quick List)
Major collections referenced in code:
- `users`, `balances`, `orders`, `transactions`, `services`, `complaints`
- `carts`, `referrals`, `auth_pages`
- `afa_registrations`, `afa_settings`
- `store_products`, `stores`, `store_accounts`, `store_withdraw_requests`, `store_payouts`
- `announcement_comments`, `announcements`
- `login_logs`, `activity_logs`
- `wassce_checker`, `purchase_history`

---
If you want this distilled into a narrower “Customer FAQ” or “Admin SOP” for the bot, specify the target audience and I’ll generate a smaller version.
