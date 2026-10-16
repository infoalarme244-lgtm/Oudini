import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record):
        d = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for k in ("request_id", "shop_id", "path", "status", "action", "job_id", "kind", "entity", "entity_id"):
            if hasattr(record, k):
                d[k] = getattr(record, k)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(d, ensure_ascii=False, default=str)


def setup(level=logging.INFO):
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger("shopforge")
    root.handlers = [h]
    root.setLevel(level)
    root.propagate = False
