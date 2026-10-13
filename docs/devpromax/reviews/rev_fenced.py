import sys, tempfile, pathlib; sys.path.insert(0, "tests")
from test_contracts import *
d = pathlib.Path(tempfile.mkdtemp())
s = Settings(subnet="2001:db8:1::/64", bind_ipv4="127.0.0.1", interface="eth0", api_token=ADMIN,
             data_dir=d/"data", port_start=30000, port_end=30019)
with TestClient(create_app(s, TargetBackend)) as c:
    c.headers["Authorization"] = "Bearer " + ADMIN
    c.post("/proxies", json={}); c.post("/proxies", json={})
class FailOne(TargetBackend):  # API restart where one instance cannot come up (e.g. ExecStartPre fails)
    def reconcile(self, records):
        super().reconcile(records); self.live.pop(30000); return {30000}
with TestClient(create_app(s, FailOne)) as c:
    c.headers["Authorization"] = "Bearer " + ADMIN
    print("after restart, one fenced: GET /proxies ->", c.get("/proxies").status_code,
          "| include=status ->", [p["status"] for p in c.get("/proxies?include=status").json()])
