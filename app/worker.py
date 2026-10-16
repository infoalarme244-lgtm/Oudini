"""Worker de tâches de fond : imports, analyses, veille, expiration des réservations.
Usage : python -m app.worker [--once]"""
import logging
import sys
import time

from sqlalchemy import select

from . import db as dbm
from .logging_setup import setup
from .models import Shop
from .services import inventory, jobs, mailer, watch

log = logging.getLogger("shopforge.worker")


def tick():
    with dbm.SessionLocal() as db:
        n = jobs.run_pending(db)
        watch.schedule_due(db)
        for shop in db.scalars(select(Shop)):
            inventory.expire_reservations(db, shop.id)
        db.commit()
        mailer.send_pending(db)
        return n


def main():
    setup()
    dbm.init_db()
    once = "--once" in sys.argv
    while True:
        n = tick()
        if once:
            break
        time.sleep(2 if n else 10)


if __name__ == "__main__":
    main()
