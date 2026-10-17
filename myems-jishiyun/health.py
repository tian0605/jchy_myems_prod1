"""Worker healthcheck entry point."""

import json

from myems_jishiyun.health import healthy


if __name__ == "__main__":
    status = healthy()
    print(json.dumps({"worker_loop_fresh": status, "data_completeness_checked": False}))
    raise SystemExit(0 if status else 1)
