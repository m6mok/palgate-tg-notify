"""Integration tests against the real mock PalGate server (../palgate_server).

The mock server is imported in-process and served on an ephemeral port with
werkzeug, so the notifier exercises its full stack: real token generation
(pylgate AES crypto), real HTTP requests, real retry logic and real response
validation — nothing on the client side is faked except the delivery channel
(a recording notifier) and the state store (in-memory).

pylgate tokens embed the current timestamp (a token is valid for roughly five
seconds) and the mock server validates tokens by exact match against the ones
it generated at startup. To keep the tests deterministic, token generation is
pinned to a fixed timestamp on both sides through the public ``timestamp_ms``
parameter of ``pylgate.token_generator.generate_token``.

Both projects define top-level modules named ``models``, so the server modules
are imported under a temporary ``sys.path``/``sys.modules`` and never leak into
the notifier's import space.

The whole module is skipped when the palgate_server project is not checked out
next to this repository (e.g. in CI); set ``PALGATE_SERVER_DIR`` to override
the location.
"""

import os
import sys
from asyncio import Event, wait_for
from contextlib import chdir
from dataclasses import dataclass
from functools import partial
from importlib import import_module
from pathlib import Path
from threading import Thread
from types import ModuleType
from typing import Any, Callable, Dict, Iterable, Iterator, List, Tuple

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pylgate.token_generator import generate_token
from pylgate.types import TokenType
from werkzeug.serving import make_server

from models import LogItem
from notify import NotifyError
from palgate import AuthError, InvalidResponseError, PalgateClient
from service import GateWatcher, item_key
from state import MemoryStateStore
from tests.conftest import RecordingNotifier


SERVER_DIR = Path(
    os.environ.get(
        "PALGATE_SERVER_DIR",
        str(Path(__file__).resolve().parents[2] / "palgate_server"),
    )
)
SERVER_SRC = SERVER_DIR / "src"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not SERVER_SRC.is_dir(),
        reason="palgate_server project is not available",
    ),
]

# Both sides generate tokens for this instant, matching the real-world
# constraint that client and server must agree on the time window.
FROZEN_TS = 1_751_500_000

# Test data from palgate_server/config.json.
MAIN_SESSION_TOKEN = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
MAIN_USER_ID = 79123456789
MAIN_DEVICE = "4G12345678"  # users: John Doe, Jane Smith, Bob Johnson
SINGLE_USER_DEVICE = "4G87654321"  # users: Alice Williams only

OTHER_SESSION_TOKEN = "9876543210abcdef9876543210abcdef"
OTHER_USER_ID = 79987654321
EMPTY_DEVICE = "4G22222222"  # no users -> empty log list

FOREIGN_DEVICE = "4G11111111"  # exists, but not allowed for the main token
UNKNOWN_DEVICE = "4G99999999"

_SERVER_TOP_LEVEL = ("mock_server", "models", "auth", "handlers")


class _CountingMiddleware:
    """WSGI middleware counting every request the server receives."""

    def __init__(self, wsgi_app: Callable[..., Iterable[bytes]]) -> None:
        self._wsgi_app = wsgi_app
        self.count = 0

    def __call__(
        self,
        environ: Dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        self.count += 1
        return self._wsgi_app(environ, start_response)


@dataclass
class MockServer:
    module: ModuleType
    base_url: str
    requests: _CountingMiddleware


@dataclass
class Harness:
    watcher: GateWatcher
    palgate: PalgateClient
    store: MemoryStateStore
    notifier: RecordingNotifier

    async def marker(self) -> str | None:
        return await self.store.get_marker("integration", "telegram")


def _import_mock_server() -> Tuple[ModuleType, ModuleType]:
    """Import the mock server without polluting the notifier's module space.

    The notifier's ``models``/co. modules are stashed away during the import
    and restored afterwards; the server modules are dropped from
    ``sys.modules`` once loaded (the returned module objects keep working
    through their own bound references).
    """
    stashed: Dict[str, ModuleType] = {}
    for name in list(sys.modules):
        if name.partition(".")[0] in _SERVER_TOP_LEVEL:
            stashed[name] = sys.modules.pop(name)
    sys.path.insert(0, str(SERVER_SRC))
    try:
        server_module = import_module("mock_server")
        tokens_module = sys.modules["handlers.get_tokens_handler"]
    finally:
        sys.path.remove(str(SERVER_SRC))
        for name in list(sys.modules):
            if name.partition(".")[0] in _SERVER_TOP_LEVEL:
                del sys.modules[name]
        sys.modules.update(stashed)
    return server_module, tokens_module


@pytest.fixture
def mock_server() -> Iterator[MockServer]:
    """A freshly initialized mock PalGate server on an ephemeral port."""
    module, tokens_module = _import_mock_server()

    # Pin the server-side tokens to the frozen time window.
    tokens_module.generate_token = partial(
        generate_token, timestamp_ms=FROZEN_TS
    )

    with chdir(SERVER_DIR):  # config.json is read relative to the server cwd
        module.initialize_handlers()

    requests_counter = _CountingMiddleware(module.app.wsgi_app)
    module.app.wsgi_app = requests_counter

    httpd = make_server("127.0.0.1", 0, module.app)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield MockServer(
            module=module,
            base_url=f"http://127.0.0.1:{httpd.server_port}",
            requests=requests_counter,
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest_asyncio.fixture
async def make_harness(
    mock_server: MockServer, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Harness]]:
    """Factory for GateWatcher stacks pointed at the live mock server."""
    clients: List[AsyncClient] = []

    def factory(
        *,
        device_id: str = MAIN_DEVICE,
        session_token: str = MAIN_SESSION_TOKEN,
        user_id: int = MAIN_USER_ID,
        token_type: TokenType = TokenType.SMS,
        url_path: str = "/api/log?id={device_id}",
        client_ts: int = FROZEN_TS,
        tries: int = 1,
    ) -> Harness:
        # Pin the client-side token to the same (or a deliberately
        # different) time window as the server.
        monkeypatch.setattr(
            "palgate.generate_token",
            partial(generate_token, timestamp_ms=client_ts),
        )
        http = AsyncClient()
        clients.append(http)
        palgate = PalgateClient(
            http=http,
            url=mock_server.base_url + url_path.format(device_id=device_id),
            session_token=bytes.fromhex(session_token),
            user_id=user_id,
            token_type=token_type,
            tries=tries,
            delay=0,
        )
        store = MemoryStateStore()
        notifier = RecordingNotifier(name="telegram")
        watcher = GateWatcher(
            source="integration",
            client=palgate,
            store=store,
            notifiers=(notifier,),
            cron_delay=0,
            max_backoff=0,
        )
        return Harness(
            watcher=watcher, palgate=palgate, store=store, notifier=notifier
        )

    yield factory
    for http in clients:
        await http.aclose()


async def _add_entry(
    server: MockServer, device_id: str, time_shift: int = 1
) -> Dict[str, Any]:
    """Add a log entry server-side, shifted into its own one-second slot.

    Entries generated within the same second could otherwise randomly collide
    with an already cached entry of the same user and be deduplicated away.
    """
    entry: Dict[str, Any] | None = await server.module.add_log_entry_handler(
        device_id
    )
    assert entry is not None
    entry["time"] += time_shift
    return entry


class TestPollingHappyPath:
    """Full client-server flow over real HTTP with real tokens."""

    @pytest.mark.asyncio
    async def test_first_poll_primes_marker_without_notification(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness()

        assert await harness.watcher.poll_once() is True

        server_logs = await mock_server.module.get_logs_handler(MAIN_DEVICE)
        head = LogItem.model_validate(server_logs[0])
        assert await harness.marker() == item_key(head)
        assert harness.notifier.sent == []

    @pytest.mark.asyncio
    async def test_repeated_polls_without_new_entries_stay_silent(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness()

        for _ in range(3):
            await harness.watcher.poll_once()

        assert mock_server.requests.count == 3
        assert harness.notifier.sent == []

    @pytest.mark.asyncio
    async def test_new_entry_is_notified_and_becomes_new_head(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness()
        await harness.watcher.poll_once()

        entry = await _add_entry(mock_server, MAIN_DEVICE)
        await harness.watcher.poll_once()

        assert len(harness.notifier.sent) == 1
        message = harness.notifier.sent[0]
        assert entry["firstname"] in message
        assert entry["sn"] in message
        assert await harness.marker() == "%s:%s" % (
            entry["time"],
            entry["sn"],
        )

    @pytest.mark.asyncio
    async def test_multiple_new_entries_batched_oldest_first(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness()
        await harness.watcher.poll_once()

        older = await _add_entry(mock_server, MAIN_DEVICE, time_shift=1)
        newer = await _add_entry(mock_server, MAIN_DEVICE, time_shift=2)
        await harness.watcher.poll_once()

        assert len(harness.notifier.sent) == 1
        lines = harness.notifier.sent[0].split("\n")
        assert len(lines) == 2
        assert older["sn"] in lines[0]
        assert newer["sn"] in lines[1]

        # The newest entry becomes the dedup anchor for the next poll.
        assert await harness.marker() == "%s:%s" % (
            newer["time"],
            newer["sn"],
        )

    @pytest.mark.asyncio
    async def test_notification_message_format(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(device_id=SINGLE_USER_DEVICE)
        await harness.watcher.poll_once()

        await _add_entry(mock_server, SINGLE_USER_DEVICE)
        await harness.watcher.poll_once()

        assert len(harness.notifier.sent) == 1
        message = harness.notifier.sent[0]
        # "Alice Williams <a href="+79002222222">79002222222</a> 📞[ ❌]"
        assert message.startswith(
            'Alice Williams <a href="+79002222222">79002222222</a>'
        )
        signs = message.split("</a>")[1].split()
        assert signs[0] in ("📞", "📱")
        assert signs[1:] in ([], ["❌"])

    @pytest.mark.asyncio
    async def test_sixth_request_receives_auto_generated_entry(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        # The mock server adds a new random entry on every fifth request.
        harness = make_harness()

        sizes = []
        for _ in range(6):
            response = await harness.palgate.fetch_log()
            assert response.log is not None
            sizes.append(len(response.log))

        assert sizes == [3, 3, 3, 3, 3, 4]

    @pytest.mark.asyncio
    async def test_watcher_polls_live_server_continuously(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness()

        try:
            await wait_for(harness.watcher.run(Event()), timeout=2.0)
        except TimeoutError:
            pass

        assert mock_server.requests.count >= 2
        assert await harness.marker() is not None


class TestDeliveryReliability:
    """At-least-once delivery against the live server."""

    @pytest.mark.asyncio
    async def test_failed_delivery_is_redelivered_next_poll(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness()
        await harness.watcher.poll_once()
        primed_marker = await harness.marker()

        entry = await _add_entry(mock_server, MAIN_DEVICE)
        harness.notifier.fail_with = NotifyError("telegram outage")
        assert await harness.watcher.poll_once() is False

        # Nothing delivered — the marker must not move.
        assert await harness.marker() == primed_marker

        harness.notifier.fail_with = None
        assert await harness.watcher.poll_once() is True
        assert len(harness.notifier.sent) == 1
        assert entry["sn"] in harness.notifier.sent[0]


class TestAuthentication:
    """Token validation against the server's real token mapping."""

    @pytest.mark.asyncio
    async def test_wrong_session_token_is_rejected(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(
            session_token="00000000000000000000000000000000"
        )

        with pytest.raises(AuthError) as exc_info:
            await harness.palgate.fetch_log()

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_token_type_is_rejected(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        # Same session token, but a PRIMARY token where the server expects
        # SMS produces a different derived token.
        harness = make_harness(token_type=TokenType.PRIMARY)

        with pytest.raises(AuthError) as exc_info:
            await harness.palgate.fetch_log()

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_token_from_another_time_window_is_rejected(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        # Real-world failure mode: pylgate tokens are only valid for a few
        # seconds, so a client clock far from the server's is rejected.
        harness = make_harness(client_ts=FROZEN_TS + 60)

        with pytest.raises(AuthError) as exc_info:
            await harness.palgate.fetch_log()

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_second_configured_token_has_its_own_devices(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(
            session_token=OTHER_SESSION_TOKEN,
            user_id=OTHER_USER_ID,
            device_id=FOREIGN_DEVICE,
        )

        response = await harness.palgate.fetch_log()

        assert response.log is not None
        assert response.log[0].firstname == "Charlie"


class TestServerErrorHandling:
    """How the notifier survives every error the server can produce."""

    @pytest.mark.asyncio
    async def test_auth_failures_are_not_retried(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        # 4xx means the request itself is bad — hammering the API with the
        # same credentials would only waste the rate budget.
        harness = make_harness(
            session_token="00000000000000000000000000000000", tries=3
        )

        with pytest.raises(AuthError):
            await harness.palgate.fetch_log()

        assert mock_server.requests.count == 1

    @pytest.mark.asyncio
    async def test_unknown_device_returns_404(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(device_id=UNKNOWN_DEVICE)

        with pytest.raises(AuthError) as exc_info:
            await harness.palgate.fetch_log()

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_foreign_device_is_not_authorized_for_token(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(device_id=FOREIGN_DEVICE)

        with pytest.raises(AuthError) as exc_info:
            await harness.palgate.fetch_log()

        assert exc_info.value.status_code == 402

    @pytest.mark.asyncio
    async def test_missing_device_id_parameter_returns_400(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(url_path="/api/log")

        with pytest.raises(AuthError) as exc_info:
            await harness.palgate.fetch_log()

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_device_log_fails_response_validation(
        self, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(
            session_token=OTHER_SESSION_TOKEN,
            user_id=OTHER_USER_ID,
            device_id=EMPTY_DEVICE,
        )

        with pytest.raises(InvalidResponseError):
            await harness.palgate.fetch_log()

    @pytest.mark.asyncio
    async def test_watcher_keeps_polling_through_server_errors(
        self, mock_server: MockServer, make_harness: Callable[..., Harness]
    ) -> None:
        harness = make_harness(device_id=UNKNOWN_DEVICE)

        try:
            await wait_for(harness.watcher.run(Event()), timeout=1.0)
        except TimeoutError:
            pass

        assert mock_server.requests.count >= 2
        assert harness.notifier.sent == []
