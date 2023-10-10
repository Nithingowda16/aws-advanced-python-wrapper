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

from typing import Optional, Protocol


class ExceptionHandler(Protocol):
    def is_network_exception(self, error: Optional[Exception] = None, sql_state: Optional[str] = None) -> bool:
        pass

    def is_login_exception(self, error: Optional[Exception] = None, sql_state: Optional[str] = None) -> bool:
        pass


class ExceptionManager:
    custom_handler: Optional[ExceptionHandler] = None

    @staticmethod
    def set_custom_handler(handler: ExceptionHandler):
        ExceptionManager.custom_handler = handler

    @staticmethod
    def reset_custom_handler():
        ExceptionManager.custom_handler = None

    def is_network_exception(self, dialect: Optional[Dialect], error: Optional[Exception] = None,
                             sql_state: Optional[str] = None) -> bool:
        handler = self._get_handler(dialect)
        if handler is not None:
            return handler.is_network_exception(error=error, sql_state=sql_state)
        return False

    def is_login_exception(self, dialect: Optional[Dialect], error: Optional[Exception] = None,
                           sql_state: Optional[str] = None) -> bool:
        handler = self._get_handler(dialect)
        if handler is not None:
            return handler.is_login_exception(error=error, sql_state=sql_state)
        return False

    def _get_handler(self, dialect: Optional[Dialect]) -> Optional[ExceptionHandler]:
        if dialect is None:
            return None
        return self.custom_handler if self.custom_handler else dialect.exception_handler
