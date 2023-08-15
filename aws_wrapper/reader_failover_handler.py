#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License").
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aws_wrapper.plugin_service import PluginService
    from aws_wrapper.utils.properties import Properties
    from aws_wrapper.pep249 import Connection

from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from copy import deepcopy
from logging import getLogger
from random import shuffle
from threading import Event
from time import sleep
from typing import List, Optional

from aws_wrapper.failover_result import ReaderFailoverResult
from aws_wrapper.hostinfo import HostAvailability, HostInfo, HostRole
from aws_wrapper.utils.failover_mode import FailoverMode, get_failover_mode
from aws_wrapper.utils.messages import Messages

logger = getLogger(__name__)


class ReaderFailoverHandler:
    @abstractmethod
    def failover(self, current_topology: List[HostInfo], current_host: HostInfo) -> ReaderFailoverResult:
        pass

    @abstractmethod
    def get_reader_connection(self, hosts: List[HostInfo]) -> ReaderFailoverResult:
        pass


class ReaderFailoverHandlerImpl(ReaderFailoverHandler):
    failed_reader_failover_result = ReaderFailoverResult(None, False, None, None)

    def __init__(
            self,
            plugin_service: PluginService,
            properties: Properties,
            max_timeout_sec: int = 60,
            timeout_sec: int = 30):
        self._plugin_service = plugin_service
        self._properties = properties
        self._max_failover_timeout_sec = max_timeout_sec
        self._timeout_sec = timeout_sec
        mode = get_failover_mode(self._properties)
        self._strict_reader_failover = True if mode is not None and mode == FailoverMode.STRICT_READER else False
        self._timeout_event = Event()

    @property
    def timeout_sec(self):
        return self._timeout_sec

    @timeout_sec.setter
    def timeout_sec(self, value):
        self._timeout_sec = value

    def failover(self, current_topology: List[HostInfo], current_host: HostInfo) -> ReaderFailoverResult:
        if current_topology is None or len(current_topology) == 0:
            logger.debug(Messages.get_formatted("ReaderFailoverHandler.InvalidTopology", "failover"))
            return ReaderFailoverHandlerImpl.failed_reader_failover_result

        result: ReaderFailoverResult = ReaderFailoverHandlerImpl.failed_reader_failover_result
        with ThreadPoolExecutor() as executor:
            future = executor.submit(self._internal_failover_task, current_topology, current_host)

            try:
                result = future.result(timeout=self._max_failover_timeout_sec)
                if result is None:
                    result = ReaderFailoverHandlerImpl.failed_reader_failover_result
            except TimeoutError:
                self._timeout_event.set()

        return result

    def _internal_failover_task(self, topology: List[HostInfo], current_host: HostInfo) -> ReaderFailoverResult:
        try:
            while not self._timeout_event.is_set():
                result = self._failover_internal(topology, current_host)
                if result is not None and result.is_connected:
                    if not self._strict_reader_failover:
                        return result  # any node is fine

                    # need to ensure that the new connection is to a reader node

                    self._plugin_service.force_refresh_host_list(result.connection)
                    if result.new_host is not None:
                        topology = self._plugin_service.hosts
                        for node in topology:
                            # found new connection host in the latest topology
                            if node.url == result.new_host.url and node.role == HostRole.READER:
                                return result

                    if result.connection is not None:
                        result.connection.close()

                sleep(1)  # Sleep for 1 second
        except Exception as err:
            return ReaderFailoverResult(None, False, None, err)

        return ReaderFailoverHandlerImpl.failed_reader_failover_result

    def _failover_internal(self, hosts: List[HostInfo], current_host: HostInfo) -> ReaderFailoverResult:
        if current_host is not None:
            self._plugin_service.set_availability(current_host.all_aliases, HostAvailability.NOT_AVAILABLE)

        hosts_by_priority = ReaderFailoverHandlerImpl.get_hosts_by_priority(hosts, self._strict_reader_failover)
        return self._get_connection_from_host_group(hosts_by_priority)

    def get_reader_connection(self, hosts: List[HostInfo]) -> ReaderFailoverResult:
        if hosts is None or len(hosts) == 0:
            logger.debug(Messages.get_formatted("ReaderFailoverHandler.InvalidTopology", "get_reader_connection"))
            return ReaderFailoverHandlerImpl.failed_reader_failover_result

        hosts_by_priority = ReaderFailoverHandlerImpl.get_reader_hosts_by_priority(hosts)
        return self._get_connection_from_host_group(hosts_by_priority)

    def _get_connection_from_host_group(self, hosts: List[HostInfo]) -> ReaderFailoverResult:
        for i in range(0, len(hosts), 2):
            result = self._get_result_from_next_task_batch(hosts, i)
            if result.is_connected or result.exception is not None:
                return result

            sleep(1)  # Sleep for 1 second

        return ReaderFailoverHandlerImpl.failed_reader_failover_result

    def _get_result_from_next_task_batch(self, hosts: List[HostInfo], i: int) -> ReaderFailoverResult:
        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(self.attempt_connection, hosts[i])]
            if i + 1 < len(hosts):
                futures.append(executor.submit(self.attempt_connection, hosts[i + 1]))

            try:
                for future in as_completed(futures, timeout=self.timeout_sec):
                    result = future.result()
                    if result.is_connected or result.exception is not None:
                        return result
            except TimeoutError:
                self._timeout_event.set()
            finally:
                self._timeout_event.set()

        return ReaderFailoverHandlerImpl.failed_reader_failover_result

    def attempt_connection(self, host: HostInfo) -> ReaderFailoverResult:
        props: Properties = deepcopy(self._properties)
        logger.debug(Messages.get_formatted("ReaderFailoverHandler.AttemptingReaderConnection", host.url, props))

        try:
            conn: Connection = self._plugin_service.force_connect(host, props, self._timeout_event)
            self._plugin_service.set_availability(host.all_aliases, HostAvailability.AVAILABLE)

            logger.debug(Messages.get_formatted("ReaderFailoverHandler.SuccessfulReaderConnection", host.url))
            return ReaderFailoverResult(conn, True, host, None)
        except Exception as ex:
            logger.debug(Messages.get_formatted("ReaderFailoverHandler.FailedReaderConnection", host.url))
            self._plugin_service.set_availability(host.all_aliases, HostAvailability.NOT_AVAILABLE)
            if not self._plugin_service.is_network_exception(ex):
                return ReaderFailoverResult(None, False, None, ex)

        return ReaderFailoverHandlerImpl.failed_reader_failover_result

    @classmethod
    def get_hosts_by_priority(cls, hosts, readers_only: bool):
        active_readers: List[HostInfo] = []
        down_hosts: List[HostInfo] = []
        writer_host: Optional[HostInfo] = None

        for host in hosts:
            if host.role == HostRole.WRITER:
                writer_host = host
                continue
            if host.availability == HostAvailability.AVAILABLE:
                active_readers.append(host)
            else:
                down_hosts.append(host)

        shuffle(active_readers)
        shuffle(down_hosts)

        hosts_by_priority = active_readers
        num_readers = len(active_readers) + len(down_hosts)
        if writer_host is not None and (not readers_only or num_readers == 0):
            hosts_by_priority.append(writer_host)
        hosts_by_priority += down_hosts

        return hosts_by_priority

    @classmethod
    def get_reader_hosts_by_priority(cls, hosts: List[HostInfo]) -> List[HostInfo]:
        active_readers: List[HostInfo] = []
        down_hosts: List[HostInfo] = []

        for host in hosts:
            if host.role == HostRole.WRITER:
                continue
            if host.availability == HostAvailability.AVAILABLE:
                active_readers.append(host)
            else:
                down_hosts.append(host)

        shuffle(active_readers)
        shuffle(down_hosts)

        return active_readers + down_hosts
