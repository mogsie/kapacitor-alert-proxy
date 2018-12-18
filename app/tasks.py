"""
Module: tasks

Created: 28.Sep.2018
Created by: Morten Hersson, <morten@escenic.com>

Copyright (c) 2018 Morten Hersson
"""
import time
import calendar
import datetime
import requests

from app import app, routes, LOGGER
from app.dbcontroller import DBController
from app.alert import Alert


class FlapDetective():
    """Flapping detection class """

    def __init__(self):
        super(FlapDetective, self).__init__()
        LOGGER.debug("Initiating flap detective")
        self.db = DBController()
        self.limit = 3

    def run(self):
        LOGGER.debug("Searching for flapping alerts")
        logged_alerts = self.db.get_log_count()
        flapping_alerts = self.db.get_flapping_alerts()
        flaphash = [x[0] for x in flapping_alerts]
        logged_hash = []
        for a in logged_alerts:
            logged_hash.append(a[0])
            if a[2] >= self.limit and a[0] not in flaphash:
                # Set flapping
                self.db.set_flapping(a[0], a[1])
                self.notify(a[1])
            elif a[2] >= self.limit and a[0] in flaphash:
                # Send reminder every hour if alert is still flapping
                flaptime = [x[2] for x in flapping_alerts if x[0] == a[0]]
                if 0 < (time.time() % 3600 - flaptime[0] % 3600) <= 60:
                    self.notify(a[1], reminder=True)
            elif a[2] < self.limit and a[0] in flaphash:
                # Too low count to be marked as flapping
                self.db.unset_flapping(a[0], a[1])
                self.notify(a[1], flapping=False)
        # If alert is no longer in the log it is not flapping, unset
        for a in [(x[0], x[1]) for x in flapping_alerts
                  if x[0] not in logged_hash]:
            self.db.unset_flapping(a[0], a[1])

    @staticmethod
    def notify(alertid, flapping=True, reminder=False):
        if app.config['SLACK_ENABLED']:
            if flapping:
                a = Alert(alertid, 0, "Flapping detected: " + alertid,
                          'CRITICAL', 'OK', None, None)
                if reminder:
                    a.message = "Flapping detected (Reminder): " + alertid
            else:
                a = Alert(alertid, 0, "Flapping resolved: " + alertid,
                          'OK', 'CRITICAL', None, None)
            routes.slack.post(a)


class KAOS():
    def __init__(self):
        LOGGER.debug("Initiating KAOS scheduler")
        self.db = DBController()

    def run(self):
        kaos_report = {app.config['KAOS_CUSTOMER']: []}
        mrules = self.db.get_active_maintenance_rules()
        for v in self.db.get_active_alerts():
            if (app.config['KAOS_IGNORE_MAINTENANCE'] or
                    not routes.affected_by_mrules(mrules, v)):
                if routes.contains_excluded_tags(
                        app.config['KAOS_EXCLUDED_TAGS'], v.tags):
                    continue
                # Create a copy we can play with
                al_dict = dict(v.__dict__)
                al_dict['message'] = routes.truncate_string(al_dict['message'])
                al_dict['time'] = self._fixtimezone(al_dict['time'])
                # GO-lint complains about underscores in variables
                # and there is no way do selectivly disable it,
                # so to make the go linter shut up when developing KAOS
                # I just renamed the keys here.
                al_dict['GrafanaURL'] = al_dict.pop('grafana_url')
                al_dict['PDIncidentKey'] = al_dict.pop('pd_incident_key')
                al_dict['JiraIssue'] = al_dict.pop('jira_issue')
                kaos_report[app.config['KAOS_CUSTOMER']].append(al_dict)
        self._send_report(kaos_report)

    @staticmethod
    def _send_report(kaos_report):
        LOGGER.debug("Sending KAOS report")
        try:
            requests.post(app.config['KAOS_URL'],
                          verify=app.config['KAOS_CERT'],
                          json=kaos_report, timeout=5)
        except requests.exceptions.RequestException:
            LOGGER.exception("Failed posting to KAOS")

    @staticmethod
    def _fixtimezone(s):
        tzdiff = calendar.timegm(
            time.localtime()) - calendar.timegm(time.gmtime())
        return s + tzdiff


class MaintenanceScheduler():
    def __init__(self):
        LOGGER.debug("Initiating maintenance scheduler")
        self.db = DBController()

    def run(self):
        now = datetime.datetime.today()
        LOGGER.debug("Checking maintenance schedule")
        for schedule in self.db.get_maintenance_schedule():
            if (self._check_day(now, schedule['days'])
                    and self._check_starttime(now, schedule['starttime'])):
                LOGGER.debug("Activating scheduled maintenance")
                self.db.activate_maintenance(
                    schedule['key'], schedule['value'], schedule['duration'])
                self.db.update_day_runcounter(
                    schedule['schedule_id'], now.weekday())
                if 0 not in self.db.get_schedule_runcounter(
                        schedule['schedule_id']) and not schedule['repeat']:
                    self.db.delete_maintenance_schedule(
                        schedule['schedule_id'])

    @staticmethod
    def _check_day(now, days):
        return bool(now.weekday() in days)

    @staticmethod
    def _check_starttime(now, starttime):
        hour, minute = starttime.split(":")
        if int(hour) == now.hour and int(minute) == now.minute:
            return True
        return False
