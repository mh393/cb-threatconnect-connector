#
# Copyright 2013 CarbonBlack, Inc
#

import os
import sys
import time
from time import gmtime, strftime
import logging
from logging.handlers import RotatingFileHandler
import threading
import version

import cbint.utils.json
import cbint.utils.feed
import cbint.utils.flaskfeed
import cbint.utils.cbserver
import cbint.utils.filesystem
from cbint.utils.daemon import CbIntegrationDaemon

from Threatconnect import ThreatConnectFeedGenerator, ConnectionException
import traceback

from cbapi.response import CbResponseAPI, Feed
from cbapi.example_helpers import get_object_by_name_or_id
from cbapi.errors import ServerError

logger = logging.getLogger(__name__)


class CarbonBlackThreatConnectBridge(CbIntegrationDaemon):
    def __init__(self, name, configfile, logfile=None, pidfile=None, debug=False):

        CbIntegrationDaemon.__init__(self, name, configfile=configfile, logfile=logfile, pidfile=pidfile, debug=debug)
        template_folder = "/usr/share/cb/integrations/cb-threatconnect-connector/content"
        self.flask_feed = cbint.utils.flaskfeed.FlaskFeed(__name__, False, template_folder)
        self.bridge_options = {}
        self.bridge_auth = {}
        self.api_urns = {}
        self.validated_config = False
        self.cb = None
        self.sync_needed = False
        self.feed_name = "threatconnectintegration"
        self.display_name = "ThreatConnect"
        self.feed = {}
        self.directory = template_folder
        self.cb_image_path = "/carbonblack.png"
        self.integration_image_path = "/threatconnect.png"
        self.integration_image_small_path = "/threatconnect-small.png"
        self.json_feed_path = "/threatconnect/json"
        self.feed_lock = threading.RLock()
        self.logfile = logfile

        self.flask_feed.app.add_url_rule(self.cb_image_path, view_func=self.handle_cb_image_request)
        self.flask_feed.app.add_url_rule(self.integration_image_path, view_func=self.handle_integration_image_request)
        self.flask_feed.app.add_url_rule(self.json_feed_path, view_func=self.handle_json_feed_request, methods=['GET'])
        self.flask_feed.app.add_url_rule("/", view_func=self.handle_index_request, methods=['GET'])
        self.flask_feed.app.add_url_rule("/feed.html", view_func=self.handle_html_feed_request, methods=['GET'])

        self.initialize_logging()

        logger.debug("generating feed metadata")
        with self.feed_lock:
            self.feed = cbint.utils.feed.generate_feed(
                self.feed_name,
                summary="Threat intelligence data provided by ThreatConnect to the Carbon Black Community",
                tech_data="There are no requirements to share any data to receive this feed.",
                provider_url="http://www.threatconnect.com/",
                icon_path="%s/%s" % (self.directory, self.integration_image_path),
                small_icon_path="%s/%s" % (self.directory, self.integration_image_small_path),
                display_name=self.display_name,
                category="Partner")
            self.last_sync = "No sync performed"
            self.last_successful_sync = "No sync performed"

    def initialize_logging(self):

        if not self.logfile:
            log_path = "/var/log/cb/integrations/%s/" % self.name
            cbint.utils.filesystem.ensure_directory_exists(log_path)
            self.logfile = "%s%s.log" % (log_path, self.name)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.handlers = []

        rlh = RotatingFileHandler(self.logfile, maxBytes=524288, backupCount=10)
        rlh.setFormatter(logging.Formatter(fmt="%(asctime)s: %(module)s: %(levelname)s: %(message)s"))
        root_logger.addHandler(rlh)

    @property
    def integration_name(self):
        return 'Cb ThreatConnect Connector 1.2.9'

    def serve(self):
        if "https_proxy" in self.bridge_options:
            os.environ['HTTPS_PROXY'] = self.bridge_options.get("https_proxy", "")
            os.environ['no_proxy'] = '127.0.0.1,localhost'

        address = self.bridge_options.get('listener_address', '127.0.0.1')
        port = self.bridge_options['listener_port']
        logger.info("starting flask server: %s:%s" % (address, port))
        self.flask_feed.app.run(port=port, debug=self.debug,
                                host=address, use_reloader=False)

    def handle_json_feed_request(self):
        with self.feed_lock:
            json = self.flask_feed.generate_json_feed(self.feed)
        return json

    def handle_html_feed_request(self):
        with self.feed_lock:
            html = self.flask_feed.generate_html_feed(self.feed, self.display_name)
        return html

    def handle_index_request(self):
        with self.feed_lock:
            index = self.flask_feed.generate_html_index(self.feed, self.bridge_options, self.display_name,
                                                        self.cb_image_path, self.integration_image_path,
                                                        self.json_feed_path, self.last_sync)
        return index

    def handle_cb_image_request(self):
        return self.flask_feed.generate_image_response(image_path="%s%s" % (self.directory, self.cb_image_path))

    def handle_integration_image_request(self):
        return self.flask_feed.generate_image_response(image_path="%s%s" %
                                                                  (self.directory, self.integration_image_path))

    def run(self):
        logger.info("starting Carbon Black <-> ThreatConnect Connector | version %s" % version.__version__)
        logger.debug("starting continuous feed retrieval thread")
        work_thread = threading.Thread(target=self.perform_continuous_feed_retrieval)
        work_thread.setDaemon(True)
        work_thread.start()

        logger.debug("starting flask")
        self.serve()

    def validate_config(self):
        self.validated_config = True
        logger.info("Validating configuration file ...")

        if 'bridge' in self.options:
            self.bridge_options = self.options['bridge']
        else:
            logger.error("Configuration does not contain a [bridge] section")
            return False

        if 'auth' in self.options:
            self.bridge_auth = self.options['auth']
        else:
            logger.error("configuration does not contain a [auth] section")
            return False

        if 'sources' in self.options:
            self.api_urns = self.options["sources"]
        else:
            logger.error("configuration does not contain a [sources] section")
            return False

        opts = self.bridge_options
        auth = self.bridge_auth
        config_valid = True
        msgs = []

        if len(self.api_urns) <= 0:
            msgs.append('No data sources are configured under [sources]')
            config_valid = False

        item = 'listener_port'
        if not (item in opts and opts[item].isdigit() and 0 < int(opts[item]) <= 65535):
            msgs.append('the config option listener_port is required and must be a valid port number')
            config_valid = False
        else:
            opts[item] = int(opts[item])

        item = 'listener_address'
        if not (item in opts and opts[item] is not ""):
            msgs.append('the config option listener_address is required and cannot be empty')
            config_valid = False

        item = 'feed_retrieval_minutes'
        if not (item in opts and opts[item].isdigit() and 0 < int(opts[item])):
            msgs.append('the config option feed_retrieval_minutes is required and must be greater than 1')
            config_valid = False
        else:
            opts[item] = int(opts[item])

        item = 'ioc_min_score'
        if item in opts:
            if not (opts[item].isdigit() and 0 <= int(opts[item]) <= 100):
                msgs.append('The config option ioc_min_score must be a number in the range 0 - 100')
                config_valid = False
            else:
                opts[item] = int(opts[item])
        else:
            logger.warning("No value provided for ioc_min_score. Using 1")
            opts[item] = 1

        item = 'api_key'
        if not (item in auth and auth[item].isdigit()):
            msgs.append('The config option api_key is required under section [auth] and must be a numeric value')
            config_valid = False

        item = 'url'
        if not (item in auth and auth[item] is not ""):
            msgs.append('The config option url is required under section [auth] and cannot be blank')
            config_valid = False

        if 'secret_key_encrypted' in auth and 'secret_key' not in auth:
            msgs.append("Encrypted API secret key no longer supported. Use unencrypted 'secret_key' form.")
            config_valid = False
        elif 'secret_key' in auth and auth['secret_key'] != "":
            auth['api_secret_key'] = self.bridge_auth.get("secret_key")
        else:
            msgs.append('The config option secret_key under section [auth] must be provided')
            config_valid = False

        # Convert all 1 or 0 values to true/false
        opts["ignore_ioc_md5"] = opts.get("disable_ioc_md5", "0") == "1"
        opts["ignore_ioc_ip"] = opts.get("disable_ioc_ip", "0") == "1"
        opts["ignore_ioc_host"] = opts.get("disable_ioc_host", "0") == "1"

        # create a cbapi instance
        ssl_verify = self.get_config_boolean("carbonblack_server_sslverify", False)
        server_url = self.get_config_string("carbonblack_server_url", "https://127.0.0.1")
        server_token = self.get_config_string("carbonblack_server_token", "")
        try:
            self.cb = CbResponseAPI(url=server_url,
                                    token=server_token,
                                    ssl_verify=False,
                                    integration_name=self.integration_name)
            self.cb.info()
        except:
            logger.error(traceback.format_exc())
            return False

        if not config_valid:
            for msg in msgs:
                sys.stderr.write("%s\n" % msg)
                logger.error(msg)
            return False
        else:
            return True

    def _filter_results(self, results):
        logger.debug("Number of IOCs before filtering applied: %d", len(results))
        opts = self.bridge_options
        filter_min_score = opts["ioc_min_score"]

        # Filter out those scores lower than  the minimum score
        if filter_min_score > 0:
            results = filter(lambda x: x["score"] >= filter_min_score, results)
            logger.debug("Number of IOCs after scores less than %d discarded: %d", filter_min_score,
                         len(results))

        # For end user simplicity we call "dns" entries "host" and ipv4 entries "ip"
        # format: {"flag_name" : ("official_name", "friendly_name")}
        ignore_ioc_mapping = {"ignore_ioc_md5": ("md5", "md5"),
                              "ignore_ioc_ip": ("ipv4", "ip"),
                              "ignore_ioc_host": ("dns", "host")}

        # On a per flag basis discard md5s, ips, or host if the user has requested we do so
        # If we don't discard then check if an exclusions file has been specified and discard entries
        # that match those in the exclusions file
        for ignore_flag in ignore_ioc_mapping:
            exclude_type = ignore_ioc_mapping[ignore_flag][0]
            exclude_type_friendly_name = ignore_ioc_mapping[ignore_flag][1]
            if opts[ignore_flag]:
                results = filter(lambda x: exclude_type not in x["iocs"], results)
                logger.debug("Number of IOCs after %s entries discarded: %d", exclude_type, len(results))
            elif 'exclusions' in self.options and exclude_type_friendly_name in self.options['exclusions']:
                file_path = self.options['exclusions'][exclude_type_friendly_name]
                if not os.path.exists(file_path):
                    logger.debug("Exclusions file %s not found", file_path)
                    continue
                with open(file_path, 'r') as exclude_file:
                    data = frozenset([line.strip() for line in exclude_file])
                    results = filter(lambda x: exclude_type not in x["iocs"] or x["iocs"][exclude_type][0] not in data,
                                     results)
                logger.debug("Number of IOCs after %s exclusions file applied: %d",
                             exclude_type_friendly_name, len(results))

        return results

    def perform_continuous_feed_retrieval(self, loop_forever=True):
        try:
            # config validation is critical to this connector working correctly
            if not self.validated_config:
                self.validate_config()

            opts = self.bridge_options
            auth = self.bridge_auth

            while True:
                logger.debug("Starting retrieval iteration")

                try:
                    tc = ThreatConnectFeedGenerator(auth["api_key"], auth['api_secret_key'],
                                                    auth["url"], self.api_urns.items())
                    tmp = tc.get_threatconnect_iocs()

                    tmp = self._filter_results(tmp)
                    with self.feed_lock:
                        self.feed["reports"] = tmp
                        self.last_sync = strftime("%a, %d %b %Y %H:%M:%S +0000", gmtime())
                        self.last_successful_sync = strftime("%a, %d %b %Y %H:%M:%S +0000", gmtime())
                    logger.info("Successfully retrieved data at %s" % self.last_successful_sync)

                except ConnectionException as e:
                    logger.error("Error connecting to Threat Connect: %s" % e.value)
                    self.last_sync = self.last_successful_sync + " (" + str(e.value) + ")"
                    if not loop_forever:
                        sys.stderr.write("Error connecting to Threat Connect: %s\n" % e.value)
                        sys.exit(2)

                except Exception as e:
                    logger.error(traceback.format_exc())
                    time.sleep(opts.get('feed_retrieval_minutes') * 60)

                # synchronize feed with Carbon Black server

                if not "skip_cb_sync" in opts:
                    try:
                        feeds = get_object_by_name_or_id(self.cb, Feed, name=self.feed_name)
                    except Exception as e:
                        logger.error(e.message)
                        feeds = None

                    if not feeds:
                        logger.info("Feed {} was not found, so we are going to create it".format(self.feed_name))
                        f = self.cb.create(Feed)
                        f.feed_url = "http://{0}:{1}/threatconnect/json".format(
                            self.bridge_options.get('feed_host', '127.0.0.1'),
                            self.bridge_options.get('listener_port', '6100'))
                        f.enabled = True
                        f.use_proxy = False
                        f.validate_server_cert = False
                        try:
                            f.save()
                        except ServerError as se:
                            if se.error_code == 500:
                                logger.info("Could not add feed:")
                                logger.info(
                                    " Received error code 500 from server. This is usually because the server cannot retrieve the feed.")
                                logger.info(
                                    " Check to ensure the Cb server has network connectivity and the credentials are correct.")
                            else:
                                logger.info("Could not add feed: {0:s}".format(str(se)))
                        except Exception as e:
                            logger.info("Could not add feed: {0:s}".format(str(e)))
                        else:
                            logger.info("Feed data: {0:s}".format(str(f)))
                            logger.info("Added feed. New feed ID is {0:d}".format(f.id))
                            f.synchronize(False)

                    elif len(feeds) > 1:
                        logger.warning("Multiple feeds found, selecting Feed id {}".format(feeds[0].id))

                    elif feeds:
                        feed_id = feeds[0].id
                        logger.info("Feed {} was found as Feed ID {}".format(self.feed_name, feed_id))
                        feeds[0].synchronize(False)

                logger.debug("ending feed retrieval loop")

                # Function should only ever return when loop_forever is set to false
                if not loop_forever:
                    return self.flask_feed.generate_json_feed(self.feed).data
                time.sleep(opts.get('feed_retrieval_minutes') * 60)
        except Exception:
            # If an exception makes us exit then log what we can for our own sake
            logger.fatal("FEED RETRIEVAL LOOP IS EXITING! Daemon should be restarted to restore functionality! ")
            logger.fatal("Fatal Error Encountered:\n %s" % traceback.format_exc())
            sys.stderr.write("FEED RETRIEVAL LOOP IS EXITING! Daemon should be restarted to restore functionality!\n")
            sys.stderr.write("Fatal Error Encountered:\n %s\n" % traceback.format_exc())
            sys.exit(3)

        # If we somehow get here the function is going to exit.
        # This is not normal so we LOUDLY log the fact
        logger.fatal("FEED RETRIEVAL LOOP IS EXITING! Daemon should be restarted to restore functionality!")
