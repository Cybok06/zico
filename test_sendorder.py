import requests, json

BASE_URL = "http://127.0.0.1:5000"
API_KEY = "AZICO_VuF2drniEZ9IrOX_1ay5r8F4f3UeLT0x"

# Set this to the reference_id you already have
REFERENCE_ID = "API0530393625bb75c07c"

if not REFERENCE_ID:
    raise SystemExit("Set REFERENCE_ID first.")

status_res = requests.get(
    f"{BASE_URL}/api/response_regular.php",
    headers={"x-api-key": API_KEY},
    params={"reference_id": REFERENCE_ID},
    timeout=30,
)

print("STATUS HTTP", status_res.status_code)
try:
    print(json.dumps(status_res.json(), indent=2, ensure_ascii=False))
except Exception:
    print(status_res.text)

