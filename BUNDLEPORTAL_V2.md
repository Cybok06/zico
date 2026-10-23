# BundlePortal on Azico

BundlePortal was already present in the provider dropdown and checkout routing.
This update migrates its transport from v1 to v2, adds signed delivery callbacks,
and fixes missing provider controls in the service modals.

## Main admin setup

In **Main Admin → Services**, select **BundlePortal** and turn API ordering ON
for each service you want to route. Changing a base service's provider also
updates its existing tenant copies. Other providers are unchanged; OFF services
remain manual. The default network mapping is:

| Service | BundlePortal network |
| --- | --- |
| MTN Normal | `mtn` |
| MTN Express | `mtn` |
| Telecel | `telecel` |
| AT iShare | `ishare` |
| AT Bigtime | `airteltigo` |

BundlePortal does not document a `bigtime` slug. Orders use `airteltigo` and must
match an available size in that catalogue. A saved `bundleportal_network` field
can explicitly select `mtn_2` or `mtn_3`; the default provider dropdown does not
automatically assign different MTN routes to Normal and Express.

Both the public store and authenticated customer/agent dashboard use the shared
v2 client. Saved service settings take precedence over stale cart provider labels.
`size_gb` preserves fractional sizes; explicit GB values are preferable to
legacy volume fields. MB labels/volumes use the site's existing 1000 MB per GB
convention and are validated against the provider catalogue. Existing selling prices and wallet debits remain
in place. Store payments must still pass server-side verification before an
order becomes eligible for provider submission.

## Render deployment

1. Deploy **this `zico-main` project**, not the parent USSD project. Pause
   BundlePortal sales during setup and keep the receiving service continuously
   available. The supplied API documentation says callbacks time out after five
   seconds and are not retried.
2. Configure the variables shown in `.env.bundleportal.example` in Render.
   Use your current API key. The embedded key fallback has been removed; rotate
   the old key if it is active. Remove any `/v1` endpoint override.
3. Register the callback explicitly from a trusted shell with the API key set:

   ```text
   python bundleportal_setup.py --url https://azico.site/bundleportal/webhook
   ```

   The command refuses to replace an existing webhook unless `--rotate` is
   explicitly supplied. It displays the new webhook secret once. Save that value
   in Render as `BUNDLEPORTAL_WEBHOOK_SECRET`, then restart/redeploy. Registration
   is never performed automatically at startup. Do not put the secret in Git.
4. Confirm `get_webhook` reports this URL. An unsigned POST to the deployed
   endpoint must return 401 after configuration, without a login redirect.
   Make a controlled live test only when ready, and check delivery in both the
   provider dashboard and Azico before reopening sales.

The webhook requires no customer session and is exempt from host-based login
redirects. HMAC authentication is mandatory. It stores valid callbacks in
`bundleportal_webhook_events` before replying. Workers resume durable work after
restart and match callbacks to individual order references, including split
store/dashboard carts. Either the web process or the existing provider-worker
process can process the queue; database leases prevent concurrent submission of
the same line. Configure the same secrets on both Render services if both run.

**Shared accounts:** if Nagonu and Azico use the same BundlePortal webhook
registration, replacing its URL can redirect Nagonu's callbacks to Azico.
Use a separate provider account/registration for this application, or retain a
shared callback router that can locate both applications' orders. This Azico
receiver processes only the configured Zico database. Review the current
registration before using `--rotate`.

## Failure handling

- Recipient approval and the selected route's catalogue are checked before the
  first spending attempt. Catalogue reads are cached for 60 seconds.
- Retrying an uncertain submission uses its original `order_id`, never a new
  reference. Retries are persisted with backoff/jitter and respect Retry-After;
  six unsuccessful attempts require review. `check_status` is never called.
- `cached` is a paid order awaiting delivery. A final webhook cannot be overwritten
  by a late submission response. Unknown or ambiguous callback references are
  retained for investigation rather than applied to an unrelated order.
- `provider_status` records the exact provider outcome. A provider failure,
  cancellation or refund sets `refund_review_required` and leaves the local line
  processing for admin review. **Provider refunds do not credit local wallets.**
  Azico's existing admin refund action must perform the actual local wallet
  credit; its `refunded` status is reserved for that operation. A later provider
  callback preserves a local refund already recorded by an admin. No automatic
  Paystack refund or store-profit reversal is introduced.
- Missing configuration, approval requirements, locked accounts and exhausted
  retries are recorded on the line in `provider_status` / `api_response`.
  After resolving the reason, an operator can resume a nonfinal line by setting
  `api_status=retry_pending`, `bp_attempts=0` and `bp_next_attempt` to the current
  UTC time. Preserve the original reference and `bp_submission_started` marker.
- Existing accepted orders are not replayed during migration. Reconcile any
  missed callbacks with the BundlePortal dashboard. Inspect inbox entries whose
  status is `unmatched`, `ambiguous` or `invalid_timestamp`; after resolving a
  reference issue, set that event back to `pending` for reprocessing.

## Verification

```text
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests exercise the actual service-save endpoint, rendered provider controls,
store checkout and dashboard checkout using in-memory MongoDB. Payment
verification/provider HTTP and notifications are mocked; no live purchase is
made. Coverage includes all five networks, propagation to tenant copies, OFF
services, unverified payments, fractional sizes, explicit MTN routes, signed
callbacks, split-order matching, duplicate delivery and timeout recovery.
