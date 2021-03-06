#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Connection/Session management module."""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Awaitable, Callable, Dict, Union, TYPE_CHECKING

from pyee import AsyncIOEventEmitter
import websockets

from pyppeteer.errors import NetworkError
from pyppeteer.util import merge_dict
from pyppeteer.helper import debugError

if TYPE_CHECKING:
    from typing import Optional  # noqa: F401

logger = logging.getLogger(__name__)
logger_connection = logging.getLogger(__name__ + '.Connection')
logger_session = logging.getLogger(__name__ + '.CDPSession')


class Connection(AsyncIOEventEmitter):
    """Connection management class."""
    Events = SimpleNamespace(
        Disconnected='Events.Connection.Disconnected'
    )

    def __init__(self, url: str, loop: asyncio.AbstractEventLoop,
                 delay: int = 0) -> None:
        """Make connection.

        :arg str url: WebSocket url to connect devtool.
        :arg int delay: delay to wait before processing received messages.
        """
        super().__init__()
        self._url = url
        self._lastId = 0
        self._callbacks: Dict[int, asyncio.Future] = dict()
        self._delay = delay / 1000
        self._loop = loop
        self._sessions: Dict[str, CDPSession] = dict()
        self.connection: CDPSession
        self._connected = False
        self._ws = websockets.client.connect(
            self._url, max_size=None, loop=self._loop, ping_interval=None,
            ping_timeout=None,
            close_timeout=3600)
        self._recv_fut = self._loop.create_task(self._recv_loop())
        self._closeCallback: Optional[Callable[[], None]] = None

    def session(self, sessionid: str) -> 'CDPSession':
        return self._sessions.get(sessionid, None)

    @property
    def url(self) -> str:
        """Get connected WebSocket url."""
        return self._url

    async def _recv_loop(self) -> None:
        async with self._ws as connection:
            self._connected = True
            self.connection = connection
            while self._connected:
                try:
                    resp = await self.connection.recv()
                    if resp:
                        await self._on_message(resp)
                except (websockets.ConnectionClosed, ConnectionResetError) as e:
                    logger.info('connection closed')
                    break
                except Exception as e:
                    debugError(logger, e)
                    break
                await asyncio.sleep(0)
        if self._connected:
            self._loop.create_task(self.dispose())

    async def _async_send(self, msg: str, callback_id: int) -> None:
        while not self._connected:
            await asyncio.sleep(self._delay)
        try:
            await self.connection.send(msg)
        except websockets.ConnectionClosed:
            logger.error('connection unexpectedly closed')
            callback = self._callbacks.get(callback_id, None)
            if callback and not callback.done():
                callback.set_result(None)
                await self.dispose()

    def _rawSend(self, message=None, **kwargs):
        message = merge_dict(message, kwargs)
        self._lastId += 1
        _id = self._lastId
        msg = json.dumps({**message, 'id': _id})
        logger_connection.debug(f'SEND: {msg}')
        self._loop.create_task(self._async_send(msg, _id))
        return _id

    def send(self, method: str, params: dict = None) -> Awaitable:
        """Send message via the connection."""
        # Detect connection availability from the second transmission
        if self._lastId and not self._connected:
            raise ConnectionError('Connection is closed')
        if params is None:
            params = dict()
        _id = self._rawSend(method=method, params=params)
        callback = self._loop.create_future()
        self._callbacks[_id] = callback
        callback.error: Exception = NetworkError()  # type: ignore
        callback.method: str = method  # type: ignore
        return callback

    def _on_response(self, msg: dict) -> None:
        callback = self._callbacks.pop(msg.get('id', -1))
        if msg.get('error'):
            callback.set_exception(
                _createProtocolError(
                    callback.error,  # type: ignore
                    callback.method,  # type: ignore
                    msg
                )
            )
        else:
            callback.set_result(msg.get('result'))

    def setClosedCallback(self, callback: Callable[[], None]) -> None:
        """Set closed callback."""
        self._closeCallback = callback

    async def _on_message(self, message: str) -> None:
        await asyncio.sleep(self._delay)
        logger_connection.debug(f'RECV: {message}')
        msg = json.loads(message)
        method = msg.get('method', '')
        params = msg.get('params', {})
        sessionId = params.get('sessionId', None)
        if method == 'Target.attachedToTarget':
            self._sessions[sessionId] = CDPSession(
                self, params['targetInfo']['type'], sessionId, self._loop)
        elif method == 'Target.detachedFromTarget':
            session = self._sessions.pop(sessionId, None)
            if session:
                session._on_closed()
        if msg.get('sessionId'):
            session = self._sessions.get(msg['sessionId'], None)
            if session:
                session._on_message(message)
        elif msg.get('id') in self._callbacks:
            self._on_response(msg)
        else:
            self.emit(method, params)

    async def _on_close(self) -> None:
        if self._closeCallback:
            self._closeCallback()
            self._closeCallback = None

        for cb in self._callbacks.values():
            if not cb.done():
                cb.set_exception(_rewriteError(
                    cb.error,  # type: ignore
                    f'Protocol error {cb.method}: Target closed.',
                    # type: ignore
                ))
        self._callbacks.clear()

        for session in self._sessions.values():
            session._on_closed()
        self._sessions.clear()
        self.emit(Connection.Events.Disconnected)
        # close connection
        if hasattr(self, 'connection'):  # may not have connection
            await self.connection.close()
        if not self._recv_fut.done():
            self._recv_fut.cancel()

    async def dispose(self) -> None:
        """Close all connection."""
        self._connected = False
        await self._on_close()

    async def createSession(self, targetInfo: Dict) -> 'CDPSession':
        """Create new session."""
        sessionId = (await self.send(
            'Target.attachToTarget',
            {'targetId': targetInfo['targetId'], "flatten": True}
        )).get('sessionId')
        return self._sessions.get(sessionId)


class CDPSession(AsyncIOEventEmitter):
    """Chrome Devtools Protocol Session.

    The :class:`CDPSession` instances are used to talk raw Chrome Devtools
    Protocol:

    * protocol methods can be called with :meth:`send` method.
    * protocol events can be subscribed to with :meth:`on` method.

    Documentation on DevTools Protocol can be found
    `here <https://chromedevtools.github.io/devtools-protocol/>`_.
    """

    Events = SimpleNamespace(
        Disconnected='Events.CDPSession.Disconnected'
    )

    def __init__(self, connection: Union[Connection, 'CDPSession'],
                 targetType: str, sessionId: str,
                 loop: asyncio.AbstractEventLoop) -> None:
        """Make new session."""
        super().__init__()
        self._lastId = 0
        self._callbacks: Dict[int, asyncio.Future] = {}
        self._connection: Optional[Connection] = connection
        self._targetType = targetType
        self._sessionId = sessionId
        self._sessions: Dict[str, CDPSession] = dict()
        self._loop = loop

    def send(self, method: str, params: dict = None) -> Awaitable:
        """Send message to the connected session.

        :arg str method: Protocol method name.
        :arg dict params: Optional method parameters.
        """
        if not params:
            params = dict()
        if not self._connection:
            raise NetworkError(
                f'Protocol Error ({method}): Session closed. Most likely the '
                f'{self._targetType} has been closed.'
            )
        _id = self._connection._rawSend(sessionId=self._sessionId,
                                        method=method, params=params)
        callback = self._loop.create_future()
        self._callbacks[_id] = callback
        callback.error: Exception = NetworkError()  # type: ignore
        callback.method: str = method  # type: ignore
        return callback

    def _on_message(self, msg: str) -> None:  # noqa: C901
        logger_session.debug(f'RECV: {msg}')
        obj = json.loads(msg)
        _id = obj.get('id')
        if _id and _id in self._callbacks:
            callback = self._callbacks.pop(_id, False)
            if obj.get('error'):
                callback.set_exception(_createProtocolError(
                    callback.error,  # type: ignore
                    callback.method,  # type: ignore
                    obj,
                ))
            else:
                result = obj.get('result')
                if callback and not callback.done():
                    callback.set_result(result)
        else:
            self.emit(obj.get('method'), obj.get('params'))

    async def detach(self) -> None:
        """Detach session from target.

        Once detached, session won't emit any events and can't be used to send
        messages.
        """
        if not self._connection:
            raise NetworkError('Connection already closed.')
        await self._connection.send('Target.detachFromTarget',
                                    {'sessionId': self._sessionId})

    def _on_closed(self) -> None:
        for cb in self._callbacks.values():
            if not cb.done():
                cb.set_exception(_rewriteError(
                    cb.error,  # type: ignore
                    f'Protocol error {cb.method}: Target closed.',
                    # type: ignore
                ))
        self._callbacks.clear()
        self._connection = None
        self.emit(CDPSession.Events.Disconnected)

    def _createSession(self, targetType: str, sessionId: str) -> 'CDPSession':
        session = CDPSession(self._connection, targetType, sessionId, self._loop)
        self._sessions[sessionId] = session
        return session


def _createProtocolError(error: Exception, method: str, obj: Dict
                         ) -> Exception:
    message = f'Protocol error ({method}): {obj["error"]["message"]}'
    if 'data' in obj['error']:
        message += f' {obj["error"]["data"]}'
    return _rewriteError(error, message)


def _rewriteError(error: Exception, message: str) -> Exception:
    error.args = (message,)
    return error
