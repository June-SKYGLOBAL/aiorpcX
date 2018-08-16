import asyncio
import logging
import time
from contextlib import suppress
from functools import partial

import pytest

from aiorpcx import *
from util import RaiseTest


def raises_method_not_found(message):
    return RaiseTest(JSONRPC.METHOD_NOT_FOUND, message, RPCError)


class MyServerSession(ServerSession):

    current_server = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.notifications = []
        MyServerSession.current_server = self

    async def handle_request(self, request):
        handler = getattr(self, f'on_{request.method}', None)
        invocation = handler_invocation(handler, request)
        return await invocation()

    async def on_unexpected_response(self):
        # Send an unexpected response
        message = self.connection._protocol.response_message(-1, -1)
        self._send_messages((message, ), framed=False)

    async def on_echo(self, value):
        return value

    async def on_notify(self, thing):
        self.notifications.append(thing)

    async def on_bug(self):
        raise ValueError

    async def on_sleepy(self):
        await sleep(10)


def in_caplog(caplog, message):
    return any(message in record.message for record in caplog.records)


# This runs all the tests one with plain asyncio, then again with uvloop
@pytest.fixture(scope="session", autouse=True, params=(False, True))
def use_uvloop(request):
    if request.param:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


@pytest.fixture
def server(event_loop, unused_tcp_port):
    port = unused_tcp_port
    server = Server(MyServerSession, 'localhost', port, loop=event_loop)
    event_loop.run_until_complete(server.listen())
    yield server
    event_loop.run_until_complete(server.close())


class TestServer:

    def test_constructor_loop(self, event_loop):
        loop = asyncio.get_event_loop()
        assert loop != event_loop
        s = Server(None)
        assert s.loop == loop
        s = Server(None, loop=None)
        assert s.loop == loop
        s = Server(None, loop=event_loop)
        assert s.loop == event_loop

    @pytest.mark.asyncio
    async def test_close_not_listening(self, event_loop):
        server = Server(None, loop=event_loop)
        assert server.server is None
        # Return immediately - the server isn't listening
        await server.close()

    @pytest.mark.asyncio
    async def test_close_listening(self, server):
        asyncio_server = server.server
        assert asyncio_server is not None
        assert asyncio_server.sockets
        await server.close()
        assert server.server is None
        assert not asyncio_server.sockets


class TestClientSession:

    @pytest.mark.asyncio
    async def test_proxy(self, server):
        proxy = SOCKSProxy(('localhost', 79), SOCKS5, None)
        with pytest.raises(OSError):
            async with ClientSession('localhost', server.port,
                                     proxy=proxy) as session:
                pass

    @pytest.mark.asyncio
    async def test_handlers(self, server):
        async with timeout_after(0.1):
            async with ClientSession('localhost', server.port) as client:
                with raises_method_not_found('something'):
                    await client.send_request('something')
                await client.send_notification('something')
        assert client.is_closing()

    @pytest.mark.asyncio
    async def test_send_request(self, server):
        async with ClientSession('localhost', server.port) as client:
            assert await client.send_request('echo', [23]) == 23

    @pytest.mark.asyncio
    async def test_send_request_buggy_handler(self, server):
        async with ClientSession('localhost', server.port) as client:
            with RaiseTest(JSONRPC.INTERNAL_ERROR, 'internal server error',
                           RPCError):
                await client.send_request('bug')

    @pytest.mark.asyncio
    async def test_unexpected_response(self, server, caplog):
        async with ClientSession('localhost', server.port) as client:
            # A request not a notification so we don't exit immediately
            await client.send_request('unexpected_response')
        assert in_caplog(caplog, 'unsent request')

    @pytest.mark.asyncio
    async def test_send_request_bad_args(self, server):
        async with ClientSession('localhost', server.port) as client:
            # ProtocolError as it's a protocol violation
            with RaiseTest(JSONRPC.INVALID_ARGS, 'list', ProtocolError):
                await client.send_request('echo', "23")

    @pytest.mark.asyncio
    async def test_send_request_timeout0(self, server):
        async with ClientSession('localhost', server.port) as client:
            with pytest.raises(TaskTimeout):
                async with timeout_after(0):
                    await client.send_request('echo', [23])

    @pytest.mark.asyncio
    async def test_send_request_timeout(self, server):
        async with ClientSession('localhost', server.port) as client:
            server_session = MyServerSession.current_server
            with pytest.raises(TaskTimeout):
                async with timeout_after(0.1):
                    await client.send_request('sleepy')
        # Assert the server doesn't treat cancellation as an error
        await sleep(0.001)
        assert server_session.errors == 0

    @pytest.mark.asyncio
    async def test_send_ill_formed(self, server):
        async with ClientSession('localhost', server.port) as client:
            server_session = MyServerSession.current_server
            server_session.max_errors = 1
            client._send_messages((b'', ), framed=False)
            await sleep(0.002)
            assert server_session.errors == 1
            # Check we got cut-off
            assert client.is_closing()
        await sleep(0.001)
        #assert 0

    @pytest.mark.asyncio
    async def test_send_notification(self, server):
        async with ClientSession('localhost', server.port) as client:
            await client.send_notification('notify', ['test'])
        await asyncio.sleep(0.001)
        assert MyServerSession.current_server.notifications == ['test']

    @pytest.mark.asyncio
    async def test_force_close(self, server):
        async with ClientSession('localhost', server.port) as client:
            await client.close(force_after=0.001)
        assert not client.transport

    @pytest.mark.asyncio
    async def test_verbose_logging(self, server, caplog):
        async with ClientSession('localhost', server.port) as client:
            client.verbosity = 4
            with caplog.at_level(logging.DEBUG):
                await client.send_request('echo', ['wait'])
            assert in_caplog(caplog, "Sending framed message b'{")
            assert in_caplog(caplog, "Received framed message b'{")

    @pytest.mark.asyncio
    async def test_framer_MemoryError(self, server, caplog):
        framer = NewlineFramer(5)
        async with ClientSession('localhost', server.port,
                                 framer=framer) as client:
            msg = 'w' * 50
            raw_msg = msg.encode()
            # Even though long it will be sent in one bit
            request = client.send_request('echo', [msg])
            assert await request == msg
            assert not caplog.records
            client.data_received(raw_msg)  # Unframed; no \n
            assert len(caplog.records) == 1
            assert in_caplog(caplog, 'dropping message over 5 bytes')

    @pytest.mark.asyncio
    async def test_peer_address(self, server):
        async with ClientSession('localhost', server.port) as client:
            pa = client.peer_address()
            if pa[0] == '::1':
                assert client.peer_address_str() == f'[::1]:{server.port}'
                assert pa[1:] == (server.port, 0, 0)
            else:
                assert pa[0].startswith('127.')
                assert pa[1:] == (server.port, )
                assert client.peer_address_str() == f'{pa[0]}:{server.port}'
            client._address = None
            assert client.peer_address_str() == 'unknown'
            client._address = '1.2.3.4', 56
            assert client.peer_address_str() == '1.2.3.4:56'
            client._address = '::1', 56, 0, 0
            assert client.peer_address_str() == '[::1]:56'

    @pytest.mark.asyncio
    async def test_resource_release(self, server):
        loop = asyncio.get_event_loop()
        tasks = asyncio.Task.all_tasks(loop)
        try:
            client = ClientSession('localhost', 0)
            await client.create_connection()
        except OSError:
            pass
        assert asyncio.Task.all_tasks(loop) == tasks

        async with ClientSession('localhost', server.port):
            pass

        await asyncio.sleep(0.005)  # Yield to event loop
        assert asyncio.Task.all_tasks(loop) == tasks

    @pytest.mark.asyncio
    async def test_pausing(self, server):
        called = []
        limit = None

        def my_write(data):
            called.append(data)
            if len(called) == limit:
                client.pause_writing()

        async with ClientSession('localhost', server.port) as client:
            try:
                client.transport.write = my_write
            except AttributeError:    # uvloop: transport.write is read-only
                return
            client._send_messages((b'a', ), framed=False)
            assert called
            called.clear()

            limit = 2
            msgs = b'A very long and boring meessage'.split()
            framed_msgs = [client.framer.frame((msg, )) for msg in msgs]
            client.pause_writing()
            for msg in msgs:
                client._send_messages((msg, ), framed=False)
            assert not called
            client.resume_writing()
            assert called == [b''.join(framed_msgs)]
            limit = None
            # Check idempotent
            client.resume_writing()

    @pytest.mark.asyncio
    async def test_concurrency(self, server):
        async with ClientSession('localhost', server.port) as client:
            # Test high bw usage crushes concurrency to 1
            client.bw_charge = 1000 * 1000 * 1000
            prior_mc = client.concurrency.max_concurrent
            await client._update_concurrency()
            assert 1 == client.concurrency.max_concurrent < prior_mc
            # Test passage of time restores it
            client.bw_time -= 1000 * 1000 * 1000
            await client._update_concurrency()
            assert client.concurrency.max_concurrent == prior_mc

    @pytest.mark.asyncio
    async def test_close_on_many_errors(self, server):
        try:
            async with ClientSession('localhost', server.port) as client:
                server_session = MyServerSession.current_server
                for n in range(client.max_errors + 5):
                    with suppress(RPCError):
                        await client.send_request('boo')
        except CancelledError:
            pass
        assert server_session.errors == server_session.max_errors
        assert client.transport is None

    @pytest.mark.asyncio
    async def test_send_empty_batch(self, server):
        async with ClientSession('localhost', server.port) as client:
            with RaiseTest(JSONRPC.INVALID_REQUEST, 'empty', ProtocolError):
                async with client.send_batch() as batch:
                    pass
            assert len(batch) == 0
            assert batch.batch is None
            assert batch.results is None

    @pytest.mark.asyncio
    async def test_send_batch(self, server):
        async with ClientSession('localhost', server.port) as client:
            async with client.send_batch() as batch:
                batch.add_request("echo", [1])
                batch.add_notification("echo", [2])
                batch.add_request("echo", [3])

            assert isinstance(batch.batch, Batch)
            assert len(batch) == 3
            assert isinstance(batch.results, tuple)
            assert len(batch.results) == 2
            assert batch.results == (1, 3)

    @pytest.mark.asyncio
    async def test_send_batch_errors_quiet(self, server):
        async with ClientSession('localhost', server.port) as client:
            async with client.send_batch() as batch:
                batch.add_request("echo", [1])
                batch.add_request("bug")

            assert isinstance(batch.batch, Batch)
            assert len(batch) == 2
            assert isinstance(batch.results, tuple)
            assert len(batch.results) == 2
            assert isinstance(batch.results[1], RPCError)

    @pytest.mark.asyncio
    async def test_send_batch_errors(self, server):
        async with ClientSession('localhost', server.port) as client:
            with pytest.raises(BatchError) as e:
                async with client.send_batch(raise_errors=True) as batch:
                    batch.add_request("echo", [1])
                    batch.add_request("bug")

            assert e.value.request is batch
            assert isinstance(batch.batch, Batch)
            assert len(batch) == 2
            assert isinstance(batch.results, tuple)
            assert len(batch.results) == 2
            assert isinstance(batch.results[1], RPCError)

    @pytest.mark.asyncio
    async def test_send_batch_bad_request(self, server):
        async with ClientSession('localhost', server.port) as client:
            with RaiseTest(JSONRPC.METHOD_NOT_FOUND, 'string', ProtocolError):
                async with client.send_batch() as batch:
                    batch.add_request(23)


@pytest.mark.asyncio
async def test_base_class_implementation():
    session = ClientSession()
    await session.handle_request(Request('', []))


def test_default_and_passed_connection():
    connection = JSONRPCConnection(JSONRPCv1)
    class MyClientSession(ClientSession):
        def default_connection(self):
            return connection

    session = MyClientSession()
    assert session.connection == connection

    session = ClientSession(connection=connection)
    assert session.connection == connection
