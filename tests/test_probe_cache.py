"""探测 /api/quota 的缓存行为：首次慢、后续秒回。"""

import json
import time
import urllib.request

URL = "http://127.0.0.1:8765/api/quota"

print("%-4s %8s  %s" % ("#", "耗时ms", "结果"))
print("-" * 62)

for i in range(1, 8):
    t0 = time.time()
    try:
        with urllib.request.urlopen(URL, timeout=45) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        ms = int((time.time() - t0) * 1000)
        print(
            "%-4d %8d  raw=%-5s html=%-5s cached=%-5s stale=%s"
            % (i, ms, d.get("raw"), d.get("html"), d.get("cached"), d.get("stale"))
        )
    except Exception as exc:  # noqa: BLE001
        ms = int((time.time() - t0) * 1000)
        print("%-4d %8d  FAIL %s" % (i, ms, type(exc).__name__))
