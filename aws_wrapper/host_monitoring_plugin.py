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
    from aws_wrapper.dialect import Dialect
    from aws_wrapper.pep249 import Connection
    from aws_wrapper.plugin_service import PluginService

from concurrent.futures import Future, ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass
from logging import getLogger
from queue import Queue
from threading import Event, Lock, RLock
from time import perf_counter_ns, sleep
from typing import Any, Callable, ClassVar, Dict, FrozenSet, Optional, Set

from aws_wrapper.errors import AwsWrapperError
from aws_wrapper.hostinfo import HostAvailability, HostInfo
from aws_wrapper.plugin import CanReleaseResources, Plugin, PluginFactory
from aws_wrapper.utils.atomic import AtomicInt
from aws_wrapper.utils.concurrent import ConcurrentDict
from aws_wrapper.utils.messages import Messages
from aws_wrapper.utils.notifications import HostEvent
from aws_wrapper.utils.properties import Properties, WrapperProperties
from aws_wrapper.utils.rdsutils import RdsUtils
from aws_wrapper.utils.timeout import timeout
from aws_wrapper.utils.utils import QueueUtils, SubscribedMethodUtils

logger = getLogger(__name__)


class HostMonitoringPluginFactory(PluginFactory):
    def get_instance(self, plugin_service: PluginService, props: Properties) -> Plugin:
        return HostMonitoringPlugin(plugin_service, props)


class HostMonitoringPlugin(Plugin, CanReleaseResources):
    _SUBSCRIBED_METHODS: Set[str] = {"*"}

    def __init__(self, plugin_service, props):
        self._props: Properties = props
        self._plugin_service: PluginService = plugin_service
        self._monitoring_host_info: Optional[HostInfo] = None
        self._rds_utils: RdsUtils = RdsUtils()
        self._monitor_service: MonitorService = MonitorService(plugin_service)
        self._lock: Lock = Lock()

    @property
    def subscribed_methods(self) -> Set[str]:
        return HostMonitoringPlugin._SUBSCRIBED_METHODS

    def connect(self, host_info: HostInfo, props: Properties,
                initial: bool, connect_func: Callable) -> Connection:
        return self._connect(host_info, connect_func)

    def force_connect(self, host_info: HostInfo, props: Properties,
                      initial: bool, force_connect_func: Callable) -> Connection:
        return self._connect(host_info, force_connect_func)

    def _connect(self, host_info: HostInfo, connect_func: Callable) -> Connection:
        conn = connect_func()
        if conn:
            rds_type = self._rds_utils.identify_rds_type(host_info.host)
            if rds_type.is_rds_cluster:
                host_info.reset_aliases()
                self._plugin_service.fill_aliases(conn, host_info)
        return conn

    def execute(self, target: object, method_name: str, execute_func: Callable, *args: Any) -> Any:
        connection = self._plugin_service.current_connection
        if connection is None:
            raise AwsWrapperError(Messages.get_formatted("HostMonitoringPlugin.NullConnection", method_name))

        host_info = self._plugin_service.current_host_info
        if host_info is None:
            raise AwsWrapperError(Messages.get_formatted("HostMonitoringPlugin.NullHostInfoForMethod", method_name))

        is_enabled = WrapperProperties.FAILURE_DETECTION_ENABLED.get_bool(self._props)
        if not is_enabled or method_name not in SubscribedMethodUtils.NETWORK_BOUND_METHODS:
            return execute_func()

        failure_detection_time_ms = WrapperProperties.FAILURE_DETECTION_TIME_MS.get_int(self._props)
        failure_detection_interval = WrapperProperties.FAILURE_DETECTION_INTERVAL_MS.get_int(self._props)
        failure_detection_count = WrapperProperties.FAILURE_DETECTION_COUNT.get_int(self._props)

        monitor_context = None
        result = None

        try:
            logger.debug(Messages.get_formatted("HostMonitoringPlugin.ActivatedMonitoring", method_name))
            monitor_context = self._monitor_service.start_monitoring(
                connection,
                self._get_monitoring_host_info().all_aliases,
                self._get_monitoring_host_info(),
                self._props,
                failure_detection_time_ms,
                failure_detection_interval,
                failure_detection_count
            )
            result = execute_func()
        finally:
            if monitor_context:
                with self._lock:
                    self._monitor_service.stop_monitoring(monitor_context)
                    if monitor_context.is_host_unavailable():
                        self._plugin_service.set_availability(
                            self._get_monitoring_host_info().all_aliases, HostAvailability.NOT_AVAILABLE)
                        dialect = self._plugin_service.dialect
                        if dialect is not None and not dialect.is_closed(connection):
                            try:
                                connection.close()
                            except Exception:
                                pass
                            raise AwsWrapperError(
                                Messages.get_formatted("HostMonitoringPlugin.UnavailableHost", host_info.as_alias()))
                logger.debug(Messages.get_formatted("HostMonitoringPlugin.MonitoringDeactivated", method_name))

        return result

    def notify_host_list_changed(self, changes: Dict[str, Set[HostEvent]]):
        if HostEvent.WENT_DOWN in changes or HostEvent.HOST_DELETED in changes:
            monitoring_aliases = self._get_monitoring_host_info().all_aliases
            if monitoring_aliases:
                self._monitor_service.stop_monitoring_host(monitoring_aliases)

        self._monitoring_host_info = None

    def _get_monitoring_host_info(self) -> HostInfo:
        if self._monitoring_host_info is None:
            current_host_info = self._plugin_service.current_host_info
            if current_host_info is None:
                raise AwsWrapperError("HostMonitoringPlugin.NullHostInfo")
            self._monitoring_host_info = current_host_info
            rds_type = self._rds_utils.identify_rds_type(self._monitoring_host_info.url)

            try:
                if rds_type.is_rds_cluster:
                    logger.debug(Messages.get("HostMonitoringPlugin.ClusterEndpointHostInfo"))
                    self._monitoring_host_info = self._plugin_service.identify_connection()
                    if self._monitoring_host_info is None:
                        raise AwsWrapperError(
                            Messages.get_formatted(
                                "HostMonitoringPlugin.UnableToIdentifyConnection",
                                current_host_info.host,
                                self._plugin_service.host_list_provider))
                    self._plugin_service.fill_aliases(host_info=self._monitoring_host_info)
            except Exception as e:
                message = Messages.get_formatted("HostMonitoringPlugin.ErrorIdentifyingConnection", e)
                logger.debug(message)
                raise AwsWrapperError(message) from e
        return self._monitoring_host_info

    def release_resources(self):
        if self._monitor_service is not None:
            self._monitor_service.release_resources()

        self._monitor_service = None


class MonitoringContext:
    def __init__(
            self,
            monitor: Monitor,
            connection: Connection,
            dialect: Dialect,
            failure_detection_time_ms: int,
            failure_detection_interval_ms: int,
            failure_detection_count: int):
        self._monitor: Monitor = monitor
        self._connection: Connection = connection
        self._dialect: Dialect = dialect
        self._failure_detection_time_ms: int = failure_detection_time_ms
        self._failure_detection_interval_ms: int = failure_detection_interval_ms
        self._failure_detection_count: int = failure_detection_count

        self._monitor_start_time_ns: int = 0  # Time of monitor context submission
        self._active_monitoring_start_time_ns: int = 0  # Time when the monitor should start checking the connection
        self._unavailable_host_start_time_ns: int = 0
        self._current_failure_count: int = 0
        self._is_host_unavailable: bool = False
        self._is_active: bool = True

    @property
    def failure_detection_interval_ms(self) -> int:
        return self._failure_detection_interval_ms

    @property
    def failure_detection_count(self) -> int:
        return self._failure_detection_count

    @property
    def active_monitoring_start_time_ns(self) -> int:
        return self._active_monitoring_start_time_ns

    @property
    def monitor(self) -> Monitor:
        return self._monitor

    @property
    def is_active(self) -> bool:
        return self._is_active

    @is_active.setter
    def is_active(self, is_active: bool):
        self._is_active = is_active

    def is_host_unavailable(self) -> bool:
        return self._is_host_unavailable

    def set_monitor_start_time_ns(self, start_time_ns: int):
        self._monitor_start_time_ns = start_time_ns
        self._active_monitoring_start_time_ns = start_time_ns + self._failure_detection_time_ms * 1_000_000

    def _abort_connection(self):
        if self._connection is None or not self._is_active:
            return
        try:
            self._dialect.abort_connection(self._connection)
        except Exception as e:
            # log and ignore
            logger.debug(Messages.get_formatted("MonitorContext.ExceptionAbortingConnection", e))

    def update_host_status(
            self, url: str, status_check_start_time_ns: int, status_check_end_time_ns: int, is_available: bool):
        if not self._is_active:
            return
        total_elapsed_time_ns = status_check_end_time_ns - self._monitor_start_time_ns

        if total_elapsed_time_ns > (self._failure_detection_time_ms * 1_000_000):
            self._set_host_availability(url, is_available, status_check_start_time_ns, status_check_end_time_ns)

    def _set_host_availability(
            self, url: str, is_available: bool, status_check_start_time_ns: int, status_check_end_time_ns: int):
        if is_available:
            self._current_failure_count = 0
            self._unavailable_host_start_time_ns = 0
            self._is_host_unavailable = False
            logger.debug("MonitorContext.HostAvailable")
            return

        self._current_failure_count += 1
        if self._unavailable_host_start_time_ns <= 0:
            self._unavailable_host_start_time_ns = status_check_start_time_ns
        unavailable_host_duration_ns = status_check_end_time_ns - self._unavailable_host_start_time_ns
        max_unavailable_host_duration_ms = \
            self._failure_detection_interval_ms * max(0, self._failure_detection_count)

        if unavailable_host_duration_ns > (max_unavailable_host_duration_ms * 1_000_000):
            logger.debug(Messages.get_formatted("MonitorContext.HostUnavailable", url))
            self._is_host_unavailable = True
            self._abort_connection()
            return

        logger.debug(Messages.get_formatted("MonitorContext.HostNotResponding", url, self._current_failure_count))
        return


class Monitor:
    _INACTIVE_SLEEP_MS = 100
    _MIN_HOST_CHECK_TIMEOUT_MS = 3000
    _MONITORING_PROPERTY_PREFIX = "monitoring-"

    def __init__(
            self,
            plugin_service: PluginService,
            host_info: HostInfo,
            props: Properties,
            monitor_service: MonitorService):
        self._plugin_service: PluginService = plugin_service
        self._host_info: HostInfo = host_info
        self._props: Properties = props
        self._monitor_service: MonitorService = monitor_service

        self._lock: Lock = Lock()
        self._active_contexts: Queue[MonitoringContext] = Queue()
        self._new_contexts: Queue[MonitoringContext] = Queue()
        self._monitoring_conn: Optional[Connection] = None
        self._is_stopped: Event = Event()
        self._monitor_disposal_time_ms: int = WrapperProperties.MONITOR_DISPOSAL_TIME_MS.get_int(props)
        self._context_last_used_ns: int = 0
        self._host_check_timeout_ms: int = Monitor._MIN_HOST_CHECK_TIMEOUT_MS

    @dataclass
    class HostStatus:
        is_available: bool
        elapsed_time_ns: int

    @property
    def is_stopped(self):
        return self._is_stopped.is_set()

    def start_monitoring(self, context: MonitoringContext):
        current_time_ns = perf_counter_ns()
        context.set_monitor_start_time_ns(current_time_ns)
        self._context_last_used_ns = current_time_ns
        self._new_contexts.put(context)

    def stop_monitoring(self, context: MonitoringContext):
        if context is None:
            logger.warning(Messages.get("Monitor.NullContext"))
            return

        context.is_active = False
        self._context_last_used_ns = perf_counter_ns()

    def clear_contexts(self):
        QueueUtils.clear(self._new_contexts)
        QueueUtils.clear(self._active_contexts)

    def run(self):
        try:
            self._is_stopped.clear()

            while True:
                current_time_ns = perf_counter_ns()
                first_added_new_context = None

                # Process new contexts
                while (new_monitor_context := QueueUtils.get(self._new_contexts)) is not None:
                    if first_added_new_context == new_monitor_context:
                        # This context has already been processed.
                        # Add it back to the queue and process it in the next round.
                        self._new_contexts.put(new_monitor_context)
                        break

                    if not new_monitor_context.is_active:
                        # Discard inactive contexts
                        continue

                    if current_time_ns >= new_monitor_context.active_monitoring_start_time_ns:
                        # Submit the context for active monitoring
                        self._active_contexts.put(new_monitor_context)
                        continue

                    # The active monitoring start time has not been hit yet.
                    # Add the context back to the queue and check it later.
                    self._new_contexts.put(new_monitor_context)
                    if first_added_new_context is None:
                        first_added_new_context = new_monitor_context

                if self._active_contexts.empty():
                    if (perf_counter_ns() - self._context_last_used_ns) >= self._monitor_disposal_time_ms * 1_000_000:
                        self._monitor_service.notify_unused(self)
                        break

                    sleep(Monitor._INACTIVE_SLEEP_MS / 1000)
                    continue

                status_check_start_time_ns = perf_counter_ns()
                self._context_last_used_ns = status_check_start_time_ns
                status = self._check_host_status(self._host_check_timeout_ms)
                delay_ms = -1
                first_added_new_context = None

                monitor_context: MonitoringContext
                while (monitor_context := QueueUtils.get(self._active_contexts)) is not None:
                    with self._lock:
                        if not monitor_context.is_active:
                            # Discard inactive contexts
                            continue

                        if first_added_new_context == monitor_context:
                            # This context has already been processed by this loop.
                            # Add it back to the queue and exit the loop.
                            self._active_contexts.put(monitor_context)
                            break

                        # Process the context
                        monitor_context.update_host_status(
                            self._host_info.url,
                            status_check_start_time_ns,
                            status_check_start_time_ns + status.elapsed_time_ns,
                            status.is_available)

                        if not monitor_context.is_active or monitor_context.is_host_unavailable():
                            continue

                        # The context is still active and the host is still available. Continue monitoring the context.
                        self._active_contexts.put(monitor_context)
                        if first_added_new_context is None:
                            first_added_new_context = monitor_context

                        if delay_ms == -1 or delay_ms > monitor_context.failure_detection_interval_ms:
                            delay_ms = monitor_context.failure_detection_interval_ms

                if delay_ms == -1:
                    delay_ms = Monitor._INACTIVE_SLEEP_MS
                else:
                    # Subtract the time taken for the status check from the delay
                    delay_ms -= (status.elapsed_time_ns / 1_000_000)
                    delay_ms = max(delay_ms, Monitor._MIN_HOST_CHECK_TIMEOUT_MS)
                    # Use this delay for all active contexts
                    self._host_check_timeout_ms = delay_ms

                sleep(delay_ms / 1000)
        except InterruptedError:
            # Do nothing
            pass
        finally:
            if self._monitoring_conn is not None:
                try:
                    self._monitoring_conn.close()
                except Exception:
                    # Do nothing
                    pass
            self._is_stopped.set()

    def _check_host_status(self, host_check_timeout_ms: int) -> HostStatus:
        start_ns = perf_counter_ns()
        try:
            dialect = self._plugin_service.dialect
            if dialect is None:
                self._plugin_service.update_dialect()
                dialect = self._plugin_service.dialect
                if dialect is None:
                    raise AwsWrapperError(Messages.get("Monitor.NullDialect"))

            if self._monitoring_conn is None or dialect.is_closed(self._monitoring_conn):
                props_copy: Properties = copy(self._props)
                for key, value in self._props.items():
                    if key.startswith(Monitor._MONITORING_PROPERTY_PREFIX):
                        props_copy[key[len(Monitor._MONITORING_PROPERTY_PREFIX):len(key)]] = value
                        props_copy.pop(key, None)

                logger.debug(Messages.get_formatted("Monitor.OpeningMonitorConnection", self._host_info.url))
                start_ns = perf_counter_ns()
                self._monitoring_conn = self._plugin_service.force_connect(self._host_info, props_copy, None)
                logger.debug(Messages.get_formatted("Monitor.OpenedMonitorConnection", self._host_info.url))
                return Monitor.HostStatus(True, perf_counter_ns() - start_ns)

            start_ns = perf_counter_ns()
            is_available = self._is_host_available(self._monitoring_conn, host_check_timeout_ms / 1000)
            return Monitor.HostStatus(is_available, perf_counter_ns() - start_ns)
        except Exception:
            return Monitor.HostStatus(False, perf_counter_ns() - start_ns)

    @staticmethod
    def _is_host_available(conn: Connection, timeout_sec: float) -> bool:
        try:
            check_conn_with_timeout = timeout(timeout_sec)(lambda: Monitor._execute_conn_check(conn))
            check_conn_with_timeout()
            return True
        except TimeoutError:
            return False

    @staticmethod
    def _execute_conn_check(conn: Connection):
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")


class MonitoringThreadContainer:
    _instance: ClassVar[Optional[MonitoringThreadContainer]] = None
    _lock: ClassVar[RLock] = RLock()
    _usage_count: ClassVar[AtomicInt] = AtomicInt()

    _monitor_map: ConcurrentDict[str, Monitor] = ConcurrentDict()
    _tasks_map: ConcurrentDict[Monitor, Future] = ConcurrentDict()
    _available_monitors: Queue[Monitor] = Queue()
    _executor: ThreadPoolExecutor = ThreadPoolExecutor()

    # This logic ensures that this class is a Singleton
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls, *args, **kwargs)
                    cls._usage_count.set(0)
        cls._usage_count.get_and_increment()
        return cls._instance

    def get_or_create_monitor(self, host_aliases: FrozenSet[str], monitor_supplier: Callable) -> Monitor:
        if not host_aliases:
            raise AwsWrapperError(Messages.get("MonitoringThreadContainer.EmptyNodeKeys"))

        monitor = None
        any_alias = next(iter(host_aliases))
        for host_alias in host_aliases:
            monitor = self._monitor_map.get(host_alias)
            any_alias = host_alias
            if monitor is not None:
                break

        def _get_or_create_monitor(_) -> Monitor:
            available_monitor = QueueUtils.get(self._available_monitors)
            if available_monitor is not None:
                if not available_monitor.is_stopped:
                    return available_monitor

                # TODO: Investigate how to cancel the future. This will only cancel it if it isn't currently running
                self._tasks_map.compute_if_present(available_monitor, MonitoringThreadContainer._cancel)

            supplied_monitor = monitor_supplier()
            if supplied_monitor is None:
                raise AwsWrapperError(Messages.get("MonitoringThreadContainer.NullMonitorReturnedFromSupplier"))
            self._tasks_map.compute_if_absent(supplied_monitor, lambda k: self._executor.submit(supplied_monitor.run))
            return supplied_monitor

        if monitor is None:
            monitor = self._monitor_map.compute_if_absent(any_alias, _get_or_create_monitor)
            if monitor is None:
                raise AwsWrapperError(
                    Messages.get_formatted("MonitoringThreadContainer.ErrorGettingMonitor", host_aliases))

        for host_alias in host_aliases:
            self._monitor_map.put_if_absent(host_alias, monitor)

        return monitor

    @staticmethod
    def _cancel(_, future: Future) -> None:
        # TODO: Investigate how to cancel the future. This will only cancel it if it isn't currently running
        future.cancel()
        return None

    def get_monitor(self, alias: str) -> Optional[Monitor]:
        return self._monitor_map.get(alias)

    def reset_resource(self, monitor: Monitor):
        self._monitor_map.remove_if(lambda k, v: v == monitor)
        self._available_monitors.put(monitor)

    def release_monitor(self, monitor: Monitor):
        self._monitor_map.remove_matching_values([monitor])
        self._tasks_map.compute_if_present(monitor, MonitoringThreadContainer._cancel)

    @staticmethod
    def release_instance():
        if MonitoringThreadContainer._instance is None:
            return

        if MonitoringThreadContainer._usage_count.decrement_and_get() <= 0:
            with MonitoringThreadContainer._lock:
                if MonitoringThreadContainer._instance is not None:
                    MonitoringThreadContainer._instance._release_resources()
                    MonitoringThreadContainer._instance = None
                    MonitoringThreadContainer._usage_count.set(0)

    def _release_resources(self):
        self._monitor_map.clear()
        # TODO: Investigate how to cancel the future. This will only cancel it if it isn't currently running
        self._tasks_map.apply_if(
            lambda monitor, future: not future.done() and not future.cancelled(),
            lambda monitor, future: future.cancel())
        self._tasks_map.clear()
        QueueUtils.clear(self._available_monitors)


class MonitorService:
    def __init__(self, plugin_service: PluginService):
        self._plugin_service: PluginService = plugin_service
        self._thread_container: MonitoringThreadContainer = MonitoringThreadContainer()
        self._cached_monitor_aliases: Optional[FrozenSet[str]] = None
        self._cached_monitor: Optional[Monitor] = None

    def start_monitoring(self,
                         conn: Connection,
                         host_aliases: FrozenSet[str],
                         host_info: HostInfo,
                         props: Properties,
                         failure_detection_time_ms: int,
                         failure_detection_interval_ms: int,
                         failure_detection_count: int) -> MonitoringContext:
        if not host_aliases:
            raise AwsWrapperError(Messages.get_formatted("MonitorService.EmptyAliasSet", host_info))

        if self._cached_monitor is None \
                or self._cached_monitor_aliases is None \
                or self._cached_monitor_aliases != host_aliases:
            monitor = self._thread_container.get_or_create_monitor(
                host_aliases, lambda: self._create_monitor(host_info, props))
            self._cached_monitor = monitor
            self._cached_monitor_aliases = host_aliases
        else:
            monitor = self._cached_monitor

        dialect = self._plugin_service.dialect
        if dialect is None:
            self._plugin_service.update_dialect()
            dialect = self._plugin_service.dialect
            if dialect is None:
                raise AwsWrapperError(Messages.get("MonitorService.NullDialect"))

        context = MonitoringContext(
            monitor, conn, dialect, failure_detection_time_ms, failure_detection_interval_ms, failure_detection_count)
        monitor.start_monitoring(context)
        return context

    def _create_monitor(self, host_info: HostInfo, props: Properties):
        return Monitor(self._plugin_service, host_info, props, self)

    @staticmethod
    def stop_monitoring(context: MonitoringContext):
        monitor = context.monitor
        monitor.stop_monitoring(context)

    def stop_monitoring_host(self, host_aliases: FrozenSet):
        for alias in host_aliases:
            monitor = self._thread_container.get_monitor(alias)
            if monitor is not None:
                monitor.clear_contexts()
                self._thread_container.reset_resource(monitor)
                return

    def release_resources(self):
        self._thread_container = None
        MonitoringThreadContainer.release_instance()

    def notify_unused(self, monitor: Monitor):
        self._thread_container.release_monitor(monitor)
