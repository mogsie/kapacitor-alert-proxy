"""
Module: tasks

Created: 28.Sep.2018
Created by: Morten Hersson, <morten@escenic.com>

Copyright (c) 2018 Morten Hersson
"""
import time
import boto3
import calendar
import datetime
import requests
from botocore.exceptions import NoCredentialsError, ProfileNotFound
from botocore.exceptions import NoRegionError, ClientError

from app import app, LOGGER
from app.alertcontroller import AlertController
from app.dbcontroller import DBController


class AWSInfoCollector():
    """AWSInfoCollector collects aws instance info through AWS API"""

    def __init__(self):
        super(AWSInfoCollector, self).__init__()
        self._db = DBController()

    @staticmethod
    def _collect():
        LOGGER.debug("Collecting instance info from AWS API")
        try:
            session = boto3.Session(region_name=app.config['AWS_REGION'])
            client = session.client('ec2')
            response = client.describe_instances()
            instances = []
            for reserv in response['Reservations']:
                instances.extend(reserv['Instances'])
            return instances
        except (NoRegionError, NoCredentialsError,
                ProfileNotFound, ClientError) as e:
            LOGGER.error(e)
        except KeyError:
            LOGGER.error("Failed to collect instance info")

    def get_info_from_api(self):
        instances = self._collect()
        if not instances:
            LOGGER.error("Got empty instance list from AWS")
            return None
        instance_info = []
        LOGGER.debug("Updating instances and status codes")
        for i in instances:
            try:
                x = [tag['Value']
                     for tag in i.get('Tags') if tag['Key'] == 'Name']
            except TypeError:
                # Skip instance if no tags are set
                continue
            if x:
                env = [tag['Value'] for tag in i.get('Tags')
                       if tag['Key'] == 'Environment']
                if env:
                    instance_info.append((x[0], env[0], i['State']['Code']))
                else:
                    instance_info.append((x[0], None, i['State']['Code']))
        return instance_info

    def run(self):
        LOGGER.info("Updating AWS instance info")
        current = self._db.get_aws_instance_info()
        new = self.get_info_from_api()
        updates = list(set(current) & set(new))
        inserts = list(set(new) - set(current))
        deletes = list(set(current) - set(new))
        if updates:
            self._db.update_aws_instance_info(updates)
        if inserts:
            self._db.insert_aws_instance_info(inserts)
        if deletes:
            self._db.delete_aws_instance_info(deletes)


class FlapDetective():
    """Flapping detection class """

    def __init__(self):
        super(FlapDetective, self).__init__()
        LOGGER.info("Initiating flap detective")
        self.alertctrl = AlertController()
        self.db = DBController()
        self.limit = app.config['FLAPPING_LIMIT']
        self.slack_enabled = app.config['SLACK_ENABLED']
        self.excluded_tags = app.config['SLACK_EXCLUDED_TAGS']

    def run(self):
        LOGGER.info("Searching for flapping alerts")
        logged_alerts = self.db.get_log_count_interval()
        flapping_alerts = self.db.get_flapping_alerts()
        flaphash = [x[0] for x in flapping_alerts]
        logged_hash = []
        for a in logged_alerts:
            logged_hash.append(a[0])
            if a[3] > self.limit and a[0] not in flaphash:
                # Set flapping
                self.db.set_flapping(a[0], a[1], a[2], a[3])
                self.notify(a[1], a[2], a[3])
            elif a[3] > self.limit and a[0] in flaphash:
                # Send reminder every hour if alert is still flapping
                self.db.update_flapping(a[0], a[4])
                flaptime = [x[2] for x in flapping_alerts if x[0] == a[0]]
                if 0 < (time.time() % 3600 - flaptime[0] % 3600) <= 60:
                    self.notify(a[1], a[2], a[3], reminder=True)
            elif a[3] <= self.limit and a[0] in flaphash:
                # Too low count to be marked as flapping
                # Check that time now is bigger than
                # modified + quarantine interval
                quarantine = [x[3] + x[4] for x in flapping_alerts
                              if x[0] == a[0]]
                if time.time() > quarantine[0]:
                    self.db.unset_flapping(a[0], a[1])
        # If alert is no longer in the log it is not flapping, unset
        for a in [(x[0], x[1]) for x in flapping_alerts
                  if x[0] not in logged_hash]:
            self.db.unset_flapping(a[0], a[1])

    def notify(self, alertid, environ, count, flapping=True, reminder=False):
        tag = [{'key': 'Environment', 'value': environ}]
        ignore = self.alertctrl.contains_excluded_tags(self.excluded_tags, tag)
        if self.slack_enabled and not ignore:
            if flapping:
                title = "Flapping detected"
                message = "%s, flap count: %d" % (alertid, count)
                if reminder:
                    title = "Flapping detected (Reminder)"
                self.alertctrl.slack.post_message(title, message)


class SlackAlertSummary():
    def __init__(self):
        super(SlackAlertSummary, self).__init__()
        LOGGER.info("Initiating SlackAlertSummary")
        self.alertctrl = AlertController()
        self.db = DBController()
        self.url = "http://" + \
            app.config['SERVER_FQDN'] + "/kap/log?environment="

    def run(self):
        if int(time.time()) % 3600 <= 60:
            LOGGER.info("Creating alert summary")
            summary = self.db.get_alert_summary()
            if summary:
                t = 0
                s = 0
                p = 0
                for x in summary:
                    if x[0] == 'test':
                        t = x[1]
                    if x[0] == 'staging':
                        s = x[1]
                    if x[0] == 'production':
                        p = x[1]
                msg = ("<{url}test|Test>: {test}, "
                       "<{url}staging|Staging>: {staging}, "
                       "<{url}production|Production>: {production}".format(
                           url=self.url, test=t, staging=s, production=p))
                title = "Last hour alert statistics"
                self.alertctrl.slack.post_message(title, msg)


class KAOS():

    def __init__(self):
        LOGGER.info("Initiating KAOS scheduler")
        self.db = DBController()
        self.alertctrl = AlertController()

    def run(self):
        kaos_report = {app.config['KAOS_CUSTOMER']: []}
        mrules = self.db.get_active_maintenance_rules()
        for v in self.db.get_active_alerts():
            if not self.alertctrl.affected_by_mrules(mrules, v):
                if self.alertctrl.contains_excluded_tags(
                        app.config['KAOS_EXCLUDED_TAGS'], v.tags):
                    continue
                # Create a copy we can play with
                al_dict = dict(v.__dict__)
                al_dict['message'] = self.truncate_string(
                    al_dict['message'])
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
        LOGGER.info("Sending KAOS report")
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

    @staticmethod
    def truncate_string(s):
        if len(s) > 200:
            return s[:197] + "..."
        return s


class MaintenanceScheduler():

    def __init__(self):
        LOGGER.info("Initiating maintenance scheduler")
        self.db = DBController()

    def run(self):
        now = datetime.datetime.today()
        LOGGER.info("Checking maintenance schedule")
        for schedule in self.db.get_maintenance_schedule():
            if (self._check_day(now, schedule['days'])
                    and self._check_starttime(now, schedule['starttime'])):
                LOGGER.info("Activating scheduled maintenance")
                self.db.activate_maintenance(
                    schedule['key'], schedule['value'],
                    schedule['duration'], schedule['comment'])
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
