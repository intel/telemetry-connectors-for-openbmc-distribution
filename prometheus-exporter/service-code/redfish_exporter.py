#!/usr/bin/python3
################################################################################
# BSD 3-Clause License
# 
# Copyright (c) 2016-2020, telemetry-connectors-for-openbmc-distribution
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# 
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# 
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
################################################################################

import base64
import redfish
import json
import threading
import time
import logging
import yaml
from multiprocessing import Process, Manager
from prometheus_client import start_http_server, Summary, CONTENT_TYPE_LATEST
from prometheus_client.core import GaugeMetricFamily, REGISTRY
from prometheus_client.utils import floatToGoString
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from wsgiref.simple_server import make_server, WSGIRequestHandler


class ConfigurationError(Exception):
    pass

class RedfishCollector():

    def load_config(self, group):
        try:
            with open("/etc/openbmc-exporter.yaml") as file:
                all_groups = yaml.safe_load(file)
        except Exception as e:
            raise ConfigurationError("Failed to load configuration file: {}".format(e))
        return self._get_group_config(group, all_groups)

    def load_credentials(self, target):
        try:
            with open("/etc/secrets/openbmc-credentials.yaml") as file:
                all = yaml.safe_load(file)
        except Exception as e:
            raise ConfigurationError("Failed to load credentials file")
        return self._get_target_credentials(target, all)

    def _get_target_credentials(self, target, all):
        if target not in all:
            raise ConfigurationError("Credentials for the target not found")
        return (all[target]['user'], all[target]['password'])

    def _get_group_config(self, group, all):
        for group_config in all.get('groups',[]):
            if group_config['name']==group:
                return group_config
        else:
            raise ConfigurationError("Group not found")

    def login(self, target, username, password, config):
            self.rc = redfish.redfish_client(base_url=target,
                username=username,
                password=password,
                default_prefix=config['redfish_base_path'],
                timeout=30,
                max_retry=None)
            self.rc.login(auth="session")

    def logout(self):
        self.rc.logout()

    def redfish_read_endpoint(self, target, config, endpoint_path):
        result = {}
        try:
            response = self.rc.get(endpoint_path, None)
            if response.status == 200:
                text = response.read
                result= json.loads(text)
            logging.warn("{}{} - response code {}".format(target, endpoint_path,response.status))
        except Exception as e:
            logging.warn("Exception reading endpoint: {}".format(e))
        return result

    def parse_readings(self, redfish_response, reading):
        results = []
        for ritem in reading.get('items'):
            data = redfish_response.get(ritem['redfish_object'], [])
            for item in data:
                if item.get('Status').get('State') == 'Enabled':
                    reading = item.get(ritem['redfish_reading'])
                    try:
                        reading = float(reading)
                    except TypeError:
                        reading = "NaN"
                    name = item.get('Name')
                    if 'ReadingUnits' in item:
                        unit = item.get('ReadingUnits').lower()
                        prom_metric_name="{}_{}".format(ritem['prometheus_metric_name'], unit)
                        prom_metric_health_name="{}_{}_ok".format(ritem['prometheus_metric_name'], unit)
                    else:
                        prom_metric_name=ritem['prometheus_metric_name']
                        prom_metric_health_name=ritem['prometheus_metric_health_name']
                    health_status = 1.0
                    if item.get('Status').get('Health') != 'OK':
                        health_status=0.0
                    results.append((prom_metric_name, name, reading))
                    results.append((prom_metric_health_name, name, health_status))
        return results

    def get_system_health(self, target, group): 
        config = self.load_config(group)
        (username, password) = self.load_credentials(target)
        health_endpoint = config.get('system_health', {}).get('endpoint')
        redfish_response = self.redfish_read_endpoint(target, config, health_endpoint)
        health_status = 1.0
        if redfish_response.get('Status',dict()).get('Health', 'NOT REALLY') != 'OK':
            health_status = 0.0
        return health_status

    def read_telemetry(self, target, group):
        results = []
        config = self.load_config(group)
        (username, password) = self.load_credentials(target)
        readings = config.get('readings',[])

        logging.warn("Logging in: {}".format(target))
        try:
            self.login(target, username, password, config)
            logging.warn("Reading: {}".format(target))
            for reading in readings:
                redfish_response = self.redfish_read_endpoint(target, config ,reading['endpoint'])
                parsed = self.parse_readings(redfish_response, reading)
                results.extend(parsed)
            platform_health = self.get_system_health(target, group)
        except Exception as rte:
            logging.warn("Exception caught in read_telemetry: {}".format(rte))
            raise
        finally:
            self.logout()
        return (platform_health, results)

    def collect(self, target, group):
        sys_metrics = {}
        platform_health, raw = self.read_telemetry(target, group)
        for (metric_name, sensor_name, val) in raw:
            if metric_name not in sys_metrics:
                sys_metrics[metric_name] = GaugeMetricFamily(metric_name, '', labels=['sensor'])
            sys_metrics[metric_name].add_metric([sensor_name], val)
        for metric in sys_metrics.values():
            yield metric
        yield GaugeMetricFamily('bmc_platform_health', 'Overall platform health', value=platform_health)

    def generate_latest(self, target, group):
        output=[]
        for metric in self.collect(target, group):
            output.append('# HELP {0} {1}'.format(metric.name, metric.documentation.replace('\\', r'\\').replace('\n', r'\n')))
            output.append('\n# TYPE {0} {1}\n'.format(metric.name, metric.type))
            for sample in metric.samples:
                name = sample.name
                labels = sample.labels
                value= sample.value
                if labels:
                    labelstr = '{{{0}}}'.format(','.join(
                        ['{0}="{1}"'.format(
                         k, v.replace('\\', r'\\').replace('\n', r'\n').replace('"', r'\"'))
                         for k, v in sorted(labels.items())]))
                else:
                    labelstr = ''
                output.append('{0}{1} {2}\n'.format(name, labelstr, floatToGoString(value)))
        return ''.join(output).encode('utf-8')

def make_wsgi_app(registry=RedfishCollector()):
    """Create a WSGI app which serves the metrics from a registry."""

    def prometheus_app(environ, start_response):
        params = parse_qs(environ.get('QUERY_STRING', ''))
        r = registry
        logging.warn(params)
        if 'target' in params:
            try:
                target = params['target'][0]
                group = params.get('group',['default'])[0]
                output = registry.generate_latest(target, group)
                status = str('200 OK')
            except Exception as e:
                logging.error("Exception caught: {}".format(str(e)))
                status = str('401 Unauthorized')
                output = 'Login to BMC failed'.encode('utf-8')
        else:
            status = str('500 Internal Server Error')
            output = 'specify target'.encode('utf-8')
        headers = [(str('Content-type'), CONTENT_TYPE_LATEST)]
        start_response(status, headers)
        return [output]

    return prometheus_app

class _SilentHandler(WSGIRequestHandler):
    """WSGI handler that does not log requests."""

    def log_message(self, format, *args):
        """Log nothing."""


def start_wsgi_server(port, addr='', registry=REGISTRY):
    """Starts a WSGI server for prometheus metrics as a daemon thread."""
    app = make_wsgi_app(registry)
    httpd = make_server(addr, port, app, handler_class=_SilentHandler)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    t.start()

def main():
    rf_collector = RedfishCollector()
    start_wsgi_server(8000, registry = rf_collector)
    logging.warn("server started")
    while True:
        time.sleep(60)

if __name__ == "__main__":
    logging.basicConfig(format='ts=%(asctime)s level=%(levelname)s msg=%(message)s', level=logging.WARNING)
    main()
