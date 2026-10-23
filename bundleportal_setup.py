"""Explicit, operator-run webhook registration; never runs at app startup."""
import argparse
from urllib.parse import urlparse

from bundleportal import call


def main():
    parser = argparse.ArgumentParser(description="Register a BundlePortal webhook and display its one-time secret.")
    parser.add_argument("--url", required=True, help="Public HTTPS URL of this backend's /bundleportal/webhook route")
    parser.add_argument("--rotate", action="store_true", help="Replace an existing webhook and rotate its secret")
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        parser.error("Use a public HTTPS URL without credentials or fragments.")
    existing = call("get_webhook")
    if not existing.get("success"):
        parser.exit(1, "Could not read existing webhook configuration; check your API key and provider dashboard.\n")
    if (existing.get("data") or {}).get("webhook_url") and not args.rotate:
        parser.exit(1, "A webhook is already registered. Re-run with --rotate only when ready to replace its URL and secret.\n")
    result = call("set_webhook", webhook_url=args.url)
    secret = (result.get("data") or {}).get("webhook_secret")
    if not result.get("success") or not secret:
        parser.exit(1, "Registration was not confirmed. Inspect the provider dashboard before retrying; registration may have rotated the secret.\n")
    print("Save the following as BUNDLEPORTAL_WEBHOOK_SECRET in Render. This secret is shown only once:")
    print(secret)


if __name__ == "__main__":
    main()
