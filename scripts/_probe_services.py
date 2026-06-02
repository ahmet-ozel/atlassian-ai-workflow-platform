import json
import urllib.request

for path in ("/admin/services", "/api/v1/services/external"):
    try:
        r = urllib.request.urlopen("http://localhost:8082" + path, timeout=10)
        d = json.loads(r.read())
        print("==", path, "type=", type(d).__name__)
        print(json.dumps(d)[:1200])
    except Exception as e:  # noqa: BLE001
        print("ERR", path, e)
