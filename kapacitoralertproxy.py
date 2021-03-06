#!/usr/bin/env python
# vim:set shiftwidth=4 softtabstop=4 expandtab:
'''
Module: kapacitoralertproxy.py

Created: 27.Mar.2018
Created by: Morten Hersson, <mhersson@gmail.com>
'''
from app import app
from app.dbcontroller import DBController
from app.tasks import MaintenanceScheduler, KAOS, FlapDetective
from app.tasks import AWSInfoCollector, SlackAlertSummary
from apscheduler.schedulers.background import BackgroundScheduler


if __name__ == '__main__':
    DBController().create_tables()
    scheduler = BackgroundScheduler()
    ms = MaintenanceScheduler()
    scheduler.add_job(ms.run, 'interval', seconds=60)
    if app.config['FLAPPING_DETECTION_ENABLED']:
        fp = FlapDetective()
        scheduler.add_job(fp.run, 'interval', seconds=60)
    if app.config['KAOS_ENABLED']:
        kaos = KAOS()
        scheduler.add_job(kaos.run, 'interval', seconds=30)
    if app.config['AWS_API_ENABLED']:
        awscollector = AWSInfoCollector()
        awscollector.run()  # Run on startup to get updated info
        scheduler.add_job(awscollector.run, 'interval', seconds=60)
    if app.config['SLACK_ENABLED'] and app.config['SLACK_SUMMARY']:
        slacksummary = SlackAlertSummary()
        scheduler.add_job(slacksummary.run, 'interval', seconds=60)
    scheduler.start()
    app.run(host=app.config['SERVER_ADDRESS'], port=app.config['SERVER_PORT'],
            debug=False, threaded=True)
    scheduler.shutdown()
