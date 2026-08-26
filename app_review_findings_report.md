# Web App Review Findings

Date: April 22, 2026
Review type: Static code review only

## Findings

1. High: the normal customer dashboard checkout is debiting the admin wallet by the customer-facing price, not the admin/base price.
   `base_amount` is tracked separately, but each branch adds `amt_total` into `total_processing_amount`, and that full aggregate is what gets deducted from the admin wallet and stored as `charged_amount`.
   References:
   - `checkout.py:1919`
   - `checkout.py:2012`
   - `checkout.py:2871`
   - `checkout.py:2949`
   - `checkout.py:2968`

2. High: public store checkout does not debit the admin wallet at all.
   The store flow records the Paystack transaction and credits the admin Paystack ledger, then creates the order with `charged_amount = total_processing_amount`, but there is no matching `balances_col` debit in that handler.
   References:
   - `routes/store_page.py:2798`
   - `routes/store_page.py:2802`
   - `routes/store_page.py:4251`

3. High: guest/public store refunds can credit the wrong wallet.
   Store orders fall back to `user_id = store owner` when there is no logged-in buyer. The refund path then credits `order.user_id` for non-wallet payments. For a guest store purchase, that means a refund can end up in the store owner or admin wallet instead of going back to the external payer.
   References:
   - `routes/store_page.py:4245`
   - `admin_orders.py:1185`
   - `admin_orders.py:1198`

4. Medium: several customer-side purchase flows still charge the end-user wallet directly instead of following the admin-wallet model.
   I confirmed this for dashboard AFA, Bulk SMS, and checker purchase.
   References:
   - `customer_dashboard.py:548`
   - `bulk_sms.py:393`
   - `purchase_checker.py:129`

5. Medium: the customer dashboard does not show all services.
   The backend loads all visible services for the admin, but the template only renders a hard-coded `desired_order` list by exact service names. Bulk SMS and AFA are added separately, but anything outside that fixed list will never appear. I also did not find a Results Checker block on the dashboard.
   References:
   - `customer_dashboard.py:712`
   - `customer_dashboard.py:718`
   - `templates/customer_dashboard.html:1554`
   - `templates/customer_dashboard.html:1567`
   - `templates/customer_dashboard.html:1442`
   - `templates/customer_dashboard.html:1668`

6. Medium: the store page is closer to full service coverage, but it is split across different mechanisms.
   Generic services come from `_load_services_for_store_view`, while AFA, checker, and Bulk SMS are separate store modules. The store editor also deliberately removes Bulk SMS from the generic service picker. So "all services on store" is partly true on the public page, but not through one uniform service model.
   References:
   - `routes/store_page.py:1094`
   - `routes/store_page.py:1602`
   - `routes/store_page.py:1021`
   - `routes/store_page.py:1056`
   - `templates/store_page.html:1318`
   - `templates/store_page.html:1479`
   - `templates/store_page.html:1535`
   - `templates/store_page.html:1668`

7. Medium: store profit and Paystack inflow ledgers appear to grow, but refunds do not appear to reverse those ledgers.
   Store profit is credited into `store_accounts`, and Paystack credit is recorded into the admin Paystack ledger, while the refund path only credits a wallet and writes a refund transaction. That can leave accounting overstated after refunds.
   References:
   - `routes/store_page.py:1984`
   - `routes/store_page.py:4300`
   - `routes/store_page.py:2802`
   - `admin_orders.py:1198`
   - `admin_orders.py:1207`

## Notes

The strongest conclusion is that the hierarchy rule is not implemented consistently right now.

- The dashboard checkout over-debits admin wallets.
- The store checkout does not debit admin wallets.
- Some special services still charge the customer or agent wallet directly.

Service coverage is also uneven.

- The store page is mostly capable of showing all configured services plus special modules.
- The customer dashboard frontend is definitely not showing all services because of the hard-coded name list.

## Scope

This was a static code review only.

- I did not run live checkout scenarios.
- I did not run live refund scenarios.
- I did not validate the flows in a browser.
