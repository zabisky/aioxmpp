"""
:mod:`~aioxmpp.stream` --- Stanza stream
########################################

The stanza stream is the layer of abstraction above the XML stream. It deals
with sending and receiving stream-level elements, mainly stanzas. It also
handles stream liveness and stream management.

It provides ways to track stanzas on their way to the remote, as far as that is
possible.

.. autoclass:: StanzaStream

.. autoclass:: StanzaToken

.. autoclass:: StanzaState

"""

import asyncio
import functools
import logging

from datetime import datetime, timedelta
from enum import Enum

from . import stanza, errors, custom_queue, stream_xsos, callbacks

from .plugins import xep0199
from .utils import namespaces


class PingEventType(Enum):
    SEND_OPPORTUNISTIC = 0
    SEND_NOW = 1
    TIMEOUT = 2


class StanzaState(Enum):
    """
    The various states an outgoing stanza can have.

    .. attribute:: ACTIVE

       The stanza has just been enqueued for sending and has not been taken
       care of by the StanzaStream yet.

    .. attribute:: SENT

       The stanza has been sent over a stream with Stream Management enabled,
       but not acked by the remote yet.

    .. attribute:: ACKED

       The stanza has been sent over a stream with Stream Management enabled
       and has been acked by the remote. This is a final state.

    .. attribute:: SENT_WITHOUT_SM

       The stanza has been sent over a stream without Stream Management enabled
       or has been sent over a stream with Stream Management enabled, but for
       which resumption has failed before the stanza has been acked.

       This is a final state.

    .. attribute:: ABORTED

       The stanza has been retracted before it left the active queue.

       This is a final state.

    """
    ACTIVE = 0
    SENT = 1
    ACKED = 2
    SENT_WITHOUT_SM = 3
    ABORTED = 4


class StanzaErrorAwareListener(callbacks.TagListener):
    def __init__(self, forward_to):
        super().__init__(forward_to.data, forward_to.error)

    def data(self, stanza_obj):
        if stanza_obj.type_ == "error":
            return super().error(stanza_obj.error.to_exception())
        return super().data(stanza_obj)


class StanzaToken:
    """
    A token to follow the processing of a *stanza*.

    *on_state_change* may be a function which will be called with the token and
    the new :class:`StanzaState` whenever the state of the token changes.

    .. autoattribute:: state

    .. automethod:: abort
    """

    def __init__(self, stanza, *, on_state_change=None):
        self.stanza = stanza
        self._state = StanzaState.ACTIVE
        self.on_state_change = on_state_change

    @property
    def state(self):
        """
        The current :class:`StanzaState` of the token. Tokens are created with
        :attr:`StanzaState.ACTIVE`.
        """

        return self._state

    def _set_state(self, new_state):
        self._state = new_state
        if self.on_state_change is not None:
            self.on_state_change(self, new_state)

    def abort(self):
        """
        Abort the stanza. Attempting to call this when the stanza is in any
        non-:class:`~StanzaState.ACTIVE`, non-:class:`~StanzaState.ABORTED`
        state results in a :class:`RuntimeError`.

        When a stanza is aborted, it will reside in the active queue of the
        stream, not will be sent and instead discarded silently.
        """
        if     (self._state != StanzaState.ACTIVE and
                self._state != StanzaState.ABORTED):
            raise RuntimeError("cannot abort stanza (already sent)")
        self._state = StanzaState.ABORTED

    def __repr__(self):
        return "<StanzaToken id=0x{:016x}>".format(id(self))


class StanzaStream:
    """
    A stanza stream. This is the next layer of abstraction above the XMPP XML
    stream, which mostly deals with stanzas (but also with certain other
    stream-level elements, such as XEP-0198 Stream Management Request/Acks).

    It is independent from a specific :class:`~aioxmpp.protocol.XMLStream`
    instance. A :class:`StanzaStream` can be started with one XML stream,
    stopped later and then resumed with another XML stream. The user of the
    :class:`StanzaStream` has to make sure that the XML streams are compatible,
    identity-wise (use the same JID).

    *loop* may be used to explicitly specify the :class:`asyncio.BaseEventLoop`
    to use, otherwise the current event loop is used.

    *base_logger* can be used to explicitly specify a :class:`logging.Logger`
    instance to fork off the logger from. The :class:`StanzaStream` will use a
    child logger of *base_logger* called ``StanzaStream``.

    The stanza stream takes care of ensuring stream liveness. For that, pings
    are sent in a periodic interval. If stream management is enabled, stream
    management ack requests are used as pings, otherwise XEP-0199 pings are
    used.

    The general idea of pinging is, to save computing power, to send pings only
    when other stanzas are also about to be sent, if possible. The time window
    for waiting for other stanzas is defined by
    :attr:`ping_opportunistic_interval`. The general time which the
    :class:`StanzaStream` waits between the reception of the previous ping and
    contemplating the sending of the next ping is controlled by
    :attr:`ping_interval`. See the attributes descriptions for details:

    .. attribute:: ping_interval = timedelta(seconds=15)

       A :class:`datetime.timedelta` instance which controls the time between a
       ping response and starting the next ping. When this time elapses,
       opportunistic mode is engaged for the time defined by
       :attr:`ping_opportunistic_interval`.

    .. attribute:: ping_opportunistic_interval = timedelta(seconds=15)

       This is the time interval after :attr:`ping_interval`. During that
       interval, :class:`StanzaStream` waits for other stanzas to be sent. If a
       stanza gets send during that interval, the ping is fired. Otherwise, the
       ping is fired after the interval.

    After a ping has been sent, the response must arrive in a time of
    :attr:`ping_interval` for the stream to be considered alive. If the
    response fails to arrive within that interval, the stream fails (see
    :attr:`on_failure`).

    Reacting to failures:

    .. attribute:: on_failure

       A :class:`Signal` which will fire when the stream has failed. A failure
       occurs whenever the main task of the :class:`StanzaStream` (the one
       started by :meth:`start`) terminates with an exception.

       Examples are :class:`ConnectionError` as raised upon a ping timeout and
       any exceptions which may be raised by the
       :meth:`aioxmpp.protocol.XMLStream.send_xso` method.

       The signal fires with the exception as the only argument.

    Starting/Stopping the stream:

    .. automethod:: start

    .. automethod:: stop

    .. autoattribute:: running

    .. automethod:: flush_incoming

    Sending stanzas:

    .. automethod:: enqueue_stanza

    .. automethod:: send_iq_and_wait_for_reply

    Receiving stanzas:

    .. automethod:: register_iq_request_coro

    .. automethod:: register_iq_response_future

    .. automethod:: register_iq_response_callback

    .. automethod:: register_message_callback

    .. automethod:: register_presence_callback

    Using stream management:

    .. automethod:: start_sm

    .. automethod:: resume_sm

    .. automethod:: stop_sm

    .. autoattribute:: sm_enabled

    Stream management state inspection:

    .. autoattribute:: sm_outbound_base

    .. autoattribute:: sm_inbound_ctr

    .. autoattribute:: sm_unacked_list

    """

    on_failure = callbacks.Signal()

    def __init__(self,
                 *,
                 loop=None,
                 base_logger=logging.getLogger("aioxmpp")):
        super().__init__()
        self._loop = loop or asyncio.get_event_loop()
        self._logger = base_logger.getChild("StanzaStream")
        self._task = None

        self._active_queue = custom_queue.AsyncDeque(loop=self._loop)
        self._incoming_queue = custom_queue.AsyncDeque(loop=self._loop)

        self._iq_response_map = callbacks.TagDispatcher()
        self._iq_request_map = {}
        self._message_map = {}
        self._presence_map = {}

        self._ping_send_opportunistic = False
        self._next_ping_event_at = None
        self._next_ping_event_type = None

        self._xmlstream_exception = None

        self.ping_interval = timedelta(seconds=15)
        self.ping_opportunistic_interval = timedelta(seconds=15)

        self._sm_enabled = False

    def _done_handler(self, task):
        """
        Called when the main task (:meth:`_run`, :attr:`_task`) returns.
        """
        try:
            task.result()
        except asyncio.CancelledError:
            # normal termination
            pass
        except Exception as err:
            if self.on_failure:
                self.on_failure(err)
            raise

    def _xmlstream_failed(self, exc):
        self._xmlstream_exception = exc
        self.stop()

    def _iq_request_coro_done(self, request, task):
        """
        Called when an IQ request handler coroutine returns. *request* holds
        the IQ request which triggered the excecution of the coroutine and
        *task* is the :class:`asyncio.Task` which tracks the running coroutine.

        Compose a response and send that response.
        """
        try:
            response = task.result()
        except errors.XMPPError as err:
            response = request.make_reply(type_="error")
            response.error = stanza.Error.from_exception(err)
        except Exception:
            response = request.make_reply(type_="error")
            response.error = stanza.Error(
                condition=(namespaces.stanzas, "undefined-condition"),
                type_="cancel",
            )
        self.enqueue_stanza(response)

    def _process_incoming_iq(self, stanza_obj):
        """
        Process an incoming IQ stanza *stanza_obj*. Calls the response handler,
        spawns a request handler coroutine or drops the stanza while logging a
        warning if no handler can be found.
        """
        self._logger.debug("incoming iq: %r", stanza_obj)
        if stanza_obj.type_ == "result" or stanza_obj.type_ == "error":
            # iq response
            self._logger.debug("iq is response")
            key = (stanza_obj.from_, stanza_obj.id_)
            try:
                self._iq_response_map.unicast(key, stanza_obj)
            except KeyError:
                self._logger.warning(
                    "unexpected IQ response: from=%r, id=%r",
                    *key)
                return
        else:
            # iq request
            self._logger.debug("iq is request")
            key = (stanza_obj.type_, type(stanza_obj.payload))
            try:
                coro = self._iq_request_map[key]
            except KeyError:
                self._logger.warning(
                    "unhandleable IQ request: from=%r, id=%r, payload=%r",
                    stanza_obj.from_,
                    stanza_obj.id_,
                    stanza_obj.payload
                )
                response = stanza_obj.make_reply(type_="error")
                response.error = stanza.Error(
                    condition=(namespaces.stanzas,
                               "feature-not-implemented"),
                )
                self.enqueue_stanza(response)
                return

            task = asyncio.async(coro(stanza_obj))
            task.add_done_callback(
                functools.partial(
                    self._iq_request_coro_done,
                    stanza_obj))
            self._logger.debug("started task to handle request: %r", task)

    def _process_incoming_message(self, stanza_obj):
        """
        Process an incoming message stanza *stanza_obj*.
        """
        self._logger.debug("incoming messgage: %r", stanza_obj)
        keys = [(stanza_obj.type_, stanza_obj.from_),
                (stanza_obj.type_, None),
                (None, None)]

        for key in keys:
            try:
                cb = self._message_map[key]
            except KeyError:
                continue
            self._logger.debug("dispatching message using key: %r", key)
            self._loop.call_soon(cb, stanza_obj)
            break
        else:
            self._logger.warning(
                "unsolicited message dropped: from=%r, type=%r, id=%r",
                stanza_obj.from_,
                stanza_obj.type_,
                stanza_obj.id_
            )

    def _process_incoming_presence(self, stanza_obj):
        """
        Process an incoming presence stanza *stanza_obj*.
        """
        self._logger.debug("incoming presence: %r", stanza_obj)
        keys = [(stanza_obj.type_, stanza_obj.from_),
                (stanza_obj.type_, None)]
        for key in keys:
            try:
                cb = self._presence_map[key]
            except KeyError:
                continue
            self._logger.debug("dispatching presence using key: %r", key)
            self._loop.call_soon(cb, stanza_obj)
            break
        else:
            self._logger.warning(
                "unhandled presence dropped: from=%r, type=%r, id=%r",
                stanza_obj.from_,
                stanza_obj.type_,
                stanza_obj.id_
            )

    def _process_incoming(self, xmlstream, stanza_obj):
        """
        Dispatch to the different methods responsible for the different stanza
        types or handle a non-stanza stream-level element from *stanza_obj*,
        which has arrived over the given *xmlstream*.
        """

        if self._sm_enabled:
            self._sm_inbound_ctr += 1

        if isinstance(stanza_obj, stanza.IQ):
            self._process_incoming_iq(stanza_obj)
        elif isinstance(stanza_obj, stanza.Message):
            self._process_incoming_message(stanza_obj)
        elif isinstance(stanza_obj, stanza.Presence):
            self._process_incoming_presence(stanza_obj)
        elif isinstance(stanza_obj, stream_xsos.SMAcknowledgement):
            self._logger.debug("received SM ack: %r", stanza_obj)
            if not self._sm_enabled:
                self._logger.warning("received SM ack, but SM not enabled")
                return
            self.sm_ack(stanza_obj.counter)

            if self._next_ping_event_type == PingEventType.TIMEOUT:
                self._logger.debug("resetting ping timeout")
                self._next_ping_event_type = PingEventType.SEND_OPPORTUNISTIC
                self._next_ping_event_at = (datetime.utcnow() +
                                            self.ping_interval)

        elif isinstance(stanza_obj, stream_xsos.SMRequest):
            self._logger.debug("received SM request: %r", stanza_obj)
            if not self._sm_enabled:
                self._logger.warning("received SM request, but SM not enabled")
                return
            response = stream_xsos.SMAcknowledgement()
            response.counter = self._sm_inbound_ctr
            self._logger.debug("sending SM ack: %r", stanza_obj)
            xmlstream.send_xso(response)
        else:
            raise RuntimeError(
                "unexpected stanza class: {}".format(stanza_obj))

    def flush_incoming(self):
        """
        Flush all incoming queues to the respective processing methods. The
        handlers are called as usual, thus it may require at least one
        iteration through the asyncio event loop before effects can be seen.

        The incoming queues are empty after a call to this method.

        It is legal (but pretty useless) to call this method while the stream
        is :attr:`running`.
        """
        while True:
            try:
                stanza_obj = self._incoming_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._process_incoming(None, stanza_obj)

    def _send_stanza(self, xmlstream, token):
        """
        Send a stanza token *token* over the given *xmlstream*.

        Only sends if the *token* has not been aborted (see
        :meth:`StanzaToken.abort`). Sends the state of the token acoording to
        :attr:`sm_enabled`.
        """
        if token.state == StanzaState.ABORTED:
            return

        xmlstream.send_xso(token.stanza)
        if self._sm_enabled:
            token._set_state(StanzaState.SENT)
            self._sm_unacked_list.append(token)
        else:
            token._set_state(StanzaState.SENT_WITHOUT_SM)

    def _process_outgoing(self, xmlstream, token):
        """
        Process the current outgoing stanza *token* and also any other outgoing
        stanza which is currently in the active queue. After all stanzas have
        been processed, use :meth:`_send_ping` to allow an opportunistic ping
        to be sent.
        """

        self._send_stanza(xmlstream, token)
        # try to send a bulk
        while True:
            try:
                token = self._active_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._send_stanza(xmlstream, token)

        self._send_ping(xmlstream)

    def _recv_pong(self, stanza):
        """
        Process the reception of a XEP-0199 ping reply.
        """

        if not self.running:
            return
        if self._next_ping_event_type != PingEventType.TIMEOUT:
            return
        self._next_ping_event_type = PingEventType.SEND_OPPORTUNISTIC
        self._next_ping_event_at = datetime.utcnow() + self.ping_interval

    def _send_ping(self, xmlstream):
        """
        Opportunistically send a ping over the given *xmlstream*.

        If stream management is enabled, an SM request is always sent,
        independent of the current ping state. Otherwise, a XEP-0199 ping is
        sent if and only if we are currently in the opportunistic ping interval
        (see :attr:`ping_opportunistic_interval`).

        If a ping is sent, and we are currently not waiting for a pong to be
        received, the ping timeout is configured.
        """
        if not self._ping_send_opportunistic:
            return

        if self._sm_enabled:
            self._logger.debug("sending SM req")
            xmlstream.send_xso(stream_xsos.SMRequest())
        else:
            request = stanza.IQ(type_="get")
            request.payload = xep0199.Ping()
            request.autoset_id()
            self.register_iq_response_callback(
                None,
                request.id_,
                self._recv_pong
            )
            self._logger.debug("sending XEP-0199 ping: %r", request)
            xmlstream.send_xso(request)
            self._ping_send_opportunistic = False

        if self._next_ping_event_type != PingEventType.TIMEOUT:
            self._logger.debug("configuring ping timeout")
            self._next_ping_event_at = datetime.utcnow() + self.ping_interval
            self._next_ping_event_type = PingEventType.TIMEOUT

    def _process_ping_event(self, xmlstream):
        """
        Process a ping timed event on the current *xmlstream*.
        """
        if self._next_ping_event_type == PingEventType.SEND_OPPORTUNISTIC:
            self._logger.debug("ping: opportunistic interval started")
            self._next_ping_event_at += self.ping_opportunistic_interval
            self._next_ping_event_type = PingEventType.SEND_NOW
            # ping send opportunistic is always true for sm
            if not self._sm_enabled:
                self._ping_send_opportunistic = True
        elif self._next_ping_event_type == PingEventType.SEND_NOW:
            self._logger.debug("ping: requiring ping to be sent now")
            self._send_ping(xmlstream)
        elif self._next_ping_event_type == PingEventType.TIMEOUT:
            self._logger.warning("ping: response timeout tripped")
            raise ConnectionError("ping timeout")
        else:
            raise RuntimeError("unknown ping event type: {!r}".format(
                self._next_ping_event_type))

    def register_iq_response_callback(self, from_, id_, cb):
        """
        Register a callback function *cb* to be called when a IQ stanza with
        type ``result`` or ``error`` is recieved from the
        :class:`~aioxmpp.structs.JID` *from_* with the id *id_*.

        The callback is called at most once.
        """

        self._iq_response_map.add_listener(
            (from_, id_),
            callbacks.OneshotAsyncTagListener(cb, loop=self._loop)
        )
        self._logger.debug("iq response callback registered: from=%r, id=%r",
                           from_, id_)

    def register_iq_response_future(self, from_, id_, fut):
        """
        Register a future *fut* for an IQ stanza with type ``result`` or
        ``error`` from the :class:`~aioxmpp.structs.JID` *from_* with the id
        *id_*.

        If the type of the IQ stanza is ``result``, the stanza is set as result
        to the future. If the type of the IQ stanza is ``error``, the stanzas
        error field is converted to an exception and set as the exception of
        the future.
        """

        self._iq_response_map.add_listener(
            (from_, id_),
            StanzaErrorAwareListener(
                callbacks.OneshotAsyncTagListener(
                    fut.set_result,
                    fut.set_exception,
                    loop=self._loop)
            )
        )
        self._logger.debug("iq response future registered: from=%r, id=%r",
                           from_, id_)

    def register_iq_request_coro(self, type_, payload_cls, coro):
        """
        Register a coroutine *coro* to IQ requests of type *type_* which have a
        payload of the given *payload_cls* class.

        Whenever a matching IQ stanza is received, the coroutine is started
        with the stanza as its only argument. It is expected to return an IQ
        stanza which is sent as response; returning :data:`None` will cause the
        stream to fail.

        Raising an exception will convert the exception to an IQ error stanza;
        if the exception is a subclass of :class:`aioxmpp.errors.XMPPError`, it
        is converted directly, otherwise it is wrapped in a
        :class:`aioxmpp.errors.XMPPCancelError` with ``undefined-condition``.
        """
        self._iq_request_map[type_, payload_cls] = coro
        self._logger.debug(
            "iq request coroutine registered: type=%r, payload=%r",
            type_, payload_cls)

    def register_message_callback(self, type_, from_, cb):
        """
        Register a callback function *cb* to be called whenever a message
        stanza of the given *type_* from the given
        :class:`~aioxmpp.structs.JID` *from_* arrives.

        Both *type_* and *from_* can be :data:`None`, each, to indicate a
        wildcard match. It is not allowed for both *type_* and *from_* to be
        :data:`None` at the same time.

        More specific callbacks win over less specific callbacks. That is, a
        callback registered for type ``"chat"`` and from a specific JID
        will win over a callback registered for type ``"chat"`` with from set
        to :data:`None`.
        """
        self._message_map[type_, from_] = cb
        self._logger.debug(
            "message callback registered: type=%r, from=%r",
            type_, from_)

    def register_presence_callback(self, type_, from_, cb):
        """
        Register a callback function *cb* to be called whenever a presence
        stanza of the given *type_* arrives from the given
        :class:`~aioxmpp.structs.JID`.

        *from_* may be :data:`None` to indicate a wildcard. Like with
        :meth:`register_message_callback`, more specific callbacks win over
        less specific callbacks.

        .. note::

           A *type_* of :data:`None` is a valid value for
           :class:`aioxmpp.stanza.Presence` stanzas and is **not** a wildcard
           here.

        """
        self._presence_map[type_, from_] = cb
        self._logger.debug(
            "presence callback registered: type=%r, from=%r",
            type_, from_)

    def start(self, xmlstream):
        """
        Start or resume the stanza stream on the given
        :class:`aioxmpp.protocol.XMLStream` *xmlstream*.

        This starts the main broker task, registers stanza classes at the
        *xmlstream* and reconfigures the ping state.
        """

        if self.running:
            raise RuntimeError("already started")
        self._task = asyncio.async(self._run(xmlstream), loop=self._loop)
        self._task.add_done_callback(self._done_handler)
        self._logger.debug("broker task started as %r", self._task)

        self._xmlstream_failure_token = xmlstream.on_failure.connect(
            self._xmlstream_failed
        )

        xmlstream.stanza_parser.add_class(stanza.IQ, self.recv_stanza)
        xmlstream.stanza_parser.add_class(stanza.Message, self.recv_stanza)
        xmlstream.stanza_parser.add_class(stanza.Presence, self.recv_stanza)

        if self._sm_enabled:
            self._logger.debug("using SM")
            xmlstream.stanza_parser.add_class(stream_xsos.SMAcknowledgement,
                                              self.recv_stanza)
            xmlstream.stanza_parser.add_class(stream_xsos.SMRequest,
                                              self.recv_stanza)

        self._next_ping_event_at = datetime.utcnow() + self.ping_interval
        self._next_ping_event_type = PingEventType.SEND_OPPORTUNISTIC
        self._ping_send_opportunistic = self._sm_enabled

    def stop(self):
        """
        Send a signal to the main broker task to terminate. You have to check
        :attr:`running` and possibly wait for it to become :data:`False` ---
        the task takes at least one loop through the event loop to terminate.

        It is guarenteed that the task will not attempt to send stanzas over
        the existing *xmlstream* after a call to :meth:`stop` has been made.

        It is legal to call :meth:`stop` even if the task is already
        stopped. It is a no-op in that case.
        """
        if not self.running:
            return
        self._logger.debug("sending stop signal to task")
        self._task.cancel()

    @asyncio.coroutine
    def _run(self, xmlstream):
        active_fut = asyncio.async(self._active_queue.get(),
                                   loop=self._loop)
        incoming_fut = asyncio.async(self._incoming_queue.get(),
                                     loop=self._loop)

        try:
            while True:
                timeout = self._next_ping_event_at - datetime.utcnow()
                if timeout.total_seconds() < 0:
                    timeout = timedelta()

                done, pending = yield from asyncio.wait(
                    [
                        active_fut,
                        incoming_fut,
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=timeout.total_seconds())

                if active_fut in done:
                    self._process_outgoing(xmlstream, active_fut.result())
                    active_fut = asyncio.async(
                        self._active_queue.get(),
                        loop=self._loop)

                if incoming_fut in done:
                    self._process_incoming(xmlstream, incoming_fut.result())
                    incoming_fut = asyncio.async(
                        self._incoming_queue.get(),
                        loop=self._loop)

                timeout = self._next_ping_event_at - datetime.utcnow()
                if timeout.total_seconds() <= 0:
                    self._process_ping_event(xmlstream)

        finally:
            # make sure we rescue any stanzas which possibly have already been
            # caught by the calls to get()
            self._logger.debug("task terminating, rescuing stanzas and "
                               "clearing handlers")
            if incoming_fut.done() and not incoming_fut.exception():
                self._incoming_queue.putleft_nowait(incoming_fut.result())
            else:
                incoming_fut.cancel()

            if active_fut.done() and not active_fut.exception():
                self._active_queue.putleft_nowait(active_fut.result())
            else:
                active_fut.cancel()

            xmlstream.stanza_parser.remove_class(stanza.Presence)
            xmlstream.stanza_parser.remove_class(stanza.Message)
            xmlstream.stanza_parser.remove_class(stanza.IQ)
            if self._sm_enabled:
                xmlstream.stanza_parser.remove_class(
                    stream_xsos.SMRequest)
                xmlstream.stanza_parser.remove_class(
                    stream_xsos.SMAcknowledgement)

            xmlstream.on_failure.remove(
                self._xmlstream_failure_token
            )

            if self._xmlstream_exception:
                exc = self._xmlstream_exception
                self._xmlstream_exception = None
                raise exc

    def recv_stanza(self, stanza):
        """
        Inject a *stanza* into the incoming queue.
        """
        self._incoming_queue.put_nowait(stanza)

    def enqueue_stanza(self, stanza, **kwargs):
        """
        Enqueue a *stanza* to be sent. Return a :class:`StanzaToken` to track
        the stanza. The *kwargs* are passed to the :class:`StanzaToken`
        constructor.
        """
        token = StanzaToken(stanza, **kwargs)
        self._active_queue.put_nowait(token)
        self._logger.debug("enqueued stanza %r with token %r",
                           stanza, token)
        return token

    @property
    def running(self):
        """
        :data:`True` if the broker task is currently running, and :data:`False`
        otherwise.
        """
        return self._task is not None and not self._task.done()

    def start_sm(self):
        """
        Configure the :class:`StanzaStream` to use stream management. This must
        be called while the stream is not running.

        This initializes a new stream management session. The stream management
        state attributes become available and :attr:`sm_enabled` becomes
        :data:`True`.
        """
        if self.running:
            raise RuntimeError("cannot start Stream Management while"
                               " StanzaStream is running")

        self._logger.info("starting SM handling")
        self._sm_outbound_base = 0
        self._sm_inbound_ctr = 0
        self._sm_unacked_list = []
        self._sm_enabled = True

    @property
    def sm_enabled(self):
        """
        :data:`True` if stream management is currently enabled on the stream,
        :data:`False` otherwise.
        """

        return self._sm_enabled

    @property
    def sm_outbound_base(self):
        """
        The last value of the remote stanza counter.

        .. note::

           Accessing this attribute when :attr:`sm_enabled` is :data:`False`
           raises :class:`RuntimeError`.

        """

        if not self.sm_enabled:
            raise RuntimeError("Stream Management not enabled")
        return self._sm_outbound_base

    @property
    def sm_inbound_ctr(self):
        """
        The current value of the inbound stanza counter.

        .. note::

           Accessing this attribute when :attr:`sm_enabled` is :data:`False`
           raises :class:`RuntimeError`.

        """

        if not self.sm_enabled:
            raise RuntimeError("Stream Management not enabled")
        return self._sm_inbound_ctr

    @property
    def sm_unacked_list(self):
        """
        A **copy** of the list of stanza tokens which have not yet been acked
        by the remote party.

        .. note::

           Accessing this attribute when :attr:`sm_enabled` is :data:`False`
           raises :class:`RuntimeError`.

           Accessing this attribute is expensive, as the list is copied. In
           general, access to this attribute should not be neccessary at all.

        """

        if not self.sm_enabled:
            raise RuntimeError("Stream Management not enabled")
        return self._sm_unacked_list[:]

    def resume_sm(self, remote_ctr):
        """
        Resume a stream management session, using the remote stanza counter
        with the value *remote_ctr*.

        Attempting to call this method while the stream is running or on a
        stream without enabled stream management results in a
        :class:`RuntimeError`.

        Any stanzas which were not acked (including the *remote_ctr* value) by
        the remote will be re-queued at the tip of the queue to be resent
        immediately when the stream is resumed using :meth:`start`.
        """

        if self.running:
            raise RuntimeError("Cannot resume Stream Management while"
                               " StanzaStream is running")

        self._logger.info("resuming SM stream with remote_ctr=%d", remote_ctr)
        # remove any acked stanzas
        self.sm_ack(remote_ctr)
        # reinsert the remaining stanzas
        for token in self._sm_unacked_list:
            self._active_queue.putleft_nowait(token)
        self._sm_unacked_list.clear()

    def stop_sm(self):
        """
        Disable stream management on the stream.

        Attempting to call this method while the stream is running or without
        stream management enabled results in a :class:`RuntimeError`.

        Any sent stanzas which have not been acked by the remote yet are put
        into :attr:`StanzaState.SENT_WITHOUT_SM` state.
        """
        if self.running:
            raise RuntimeError("Cannot stop Stream Management while"
                               " StanzaStream is running")
        if not self.sm_enabled:
            raise RuntimeError("Cannot stop Stream Management while"
                               " StanzaStream is running")

        self._logger.info("stopping SM stream")
        self._sm_enabled = False
        del self._sm_outbound_base
        del self._sm_inbound_ctr
        for token in self._sm_unacked_list:
            token._set_state(StanzaState.SENT_WITHOUT_SM)
        del self._sm_unacked_list

    def sm_ack(self, remote_ctr):
        """
        Process the remote stanza counter *remote_ctr*. Any acked stanzas are
        dropped from :attr:`sm_unacked_list` and put into
        :attr:`StanzaState.ACKED` state and the counters are increased
        accordingly.

        Attempting to call this without Stream Management enabled results in a
        :class:`RuntimeError`.
        """

        if not self._sm_enabled:
            raise RuntimeError("Stream Management is not enabled")

        self._logger.debug("sm_ack(%d)", remote_ctr)
        to_drop = remote_ctr - self._sm_outbound_base
        if to_drop < 0:
            self._logger.warning(
                "remote stanza counter is *less* than before "
                "(outbound_base=%d, remote_ctr=%d)",
                self._sm_outbound_base,
                remote_ctr)
            return

        acked = self._sm_unacked_list[:to_drop]
        del self._sm_unacked_list[:to_drop]
        self._sm_outbound_base = remote_ctr

        if acked:
            self._logger.debug("%d stanzas acked by remote", len(acked))
        for token in acked:
            token._set_state(StanzaState.ACKED)

    @asyncio.coroutine
    def send_iq_and_wait_for_reply(self, iq, *,
                                   timeout=None):
        """
        Send an IQ stanza *iq* and wait for the response. If *timeout* is not
        :data:`None`, it must be the time in seconds for which to wait for a
        response.

        The response stanza is returned as stanza object, if it is a
        ``result``. If it is an ``error``, the error is raised as a
        :class:`aioxmpp.errors.XMPPError` exception.

        .. warning::

           If *timeout* is :data:`None`, this may block forever! For example,
           if the stanza is sent over a dead, not stream managed stream, of
           which the deadness is only detected *after* the stanza has been
           sent.

           This is a very common case in most networking scenarios, so you
           should generally use a timeout or apply a timeout on a higher level.

        """
        fut = asyncio.Future(loop=self._loop)
        self.register_iq_response_callback(
            iq.to,
            iq.id_,
            fut.set_result)
        self.enqueue_stanza(iq)
        if not timeout:
            return fut
        else:
            return asyncio.wait_for(fut, timeout=timeout,
                                    loop=self._loop)
