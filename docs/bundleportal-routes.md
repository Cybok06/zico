# Azico Bundle Portal routes

Admin Services now offers explicit choices for each supported service:

| Service | Choice | API network |
| --- | --- | --- |
| MTN Normal / Express | Bundle Portal MTN | `mtn` |
| MTN Normal / Express | Bundle Portal MTN2 | `mtn_2` |
| MTN Normal / Express | Bundle Portal MTN3 | `mtn_3` |
| Telecel / Vodafone | Bundle Portal Telecel | `telecel` |
| AT iShare | Bundle Portal AT iShare | `airteltigo` |

Azico retains its existing `provider: bundleportal` worker and stores the selected route in `bundleportal_network`. Main-admin base-service changes propagate both fields to tenant copies. Dashboard/store checkout read the saved service route, not stale cart provider labels. Existing orders retain their original route. The legacy generic BundlePortal option remains available for compatibility.

Keep the service ON/API. Main-admin permissions and tenant scope restrictions still apply. Selecting a route for a different network is rejected.

## Azico hosting environment

```env
BUNDLEPORTAL_API_KEY=bp_live_your_key
BUNDLEPORTAL_WEBHOOK_SECRET=your_registered_webhook_secret
BUNDLEPORTAL_BASE_URL=https://api.bundleportal.com/v2
BUNDLEPORTAL_WORKER_ENABLED=1
```

Register `https://www.azico.site/bundleportal/webhook` with the corresponding Bundle Portal account and store the returned secret. Confirm that the exact registered URL accepts POST requests without redirects. These are Azico's existing variable names; they differ from Nagonu's `BUNDLE_PORTAL_KEY` names.

Do not replace a webhook registration on an account shared with Nagonu/Campus: their current receiver does not handle Azico's generic-provider orders. Use a separate Bundle Portal account/webhook registration for Azico, or implement a shared receiver before sharing the account. This change does not modify any live webhook registration.

The existing Azico v2 workflow handles catalogue/recipient checks, persistent retries, stable order references, signed webhook persistence, and asynchronous delivery processing. No additional worker or alternate refund path was introduced.

## Verification

From `zico-main`, with application dependencies and `mongomock` installed:

```text
python -m unittest discover -s tests -p "test_bundleportal*.py" -v
```

Tests mock provider and payment calls. Explicit route tests exercise all three MTN routes for both MTN services and AT iShare/Telecel through both checkout paths. Live provider delivery has not been tested by this change.
