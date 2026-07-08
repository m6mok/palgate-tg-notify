from json import loads as json_loads
from typing import Callable, List, Tuple

import pytest
from httpx import AsyncClient, ConnectError, MockTransport, Request, Response

from github_client import GithubClient, GithubError, Release


Handler = Callable[[Request], Response]


def make_client(handler: Handler) -> Tuple[GithubClient, List[Request]]:
    seen: List[Request] = []

    def recording_handler(request: Request) -> Response:
        seen.append(request)
        return handler(request)

    http = AsyncClient(transport=MockTransport(recording_handler))
    client = GithubClient(
        http=http, token="gh_token", repo="m6mok/palgate-tg-notify"
    )
    return client, seen


class TestReleases:
    @pytest.mark.asyncio
    async def test_returns_releases_newest_first(self) -> None:
        payload = [
            {
                "tag_name": "2.0.0",
                "name": "Release pipeline",
                "published_at": "2026-07-01T10:00:00Z",
                "body": "Adds the release pipeline.",
            },
            {"tag_name": "1.1.0"},
        ]
        client, seen = make_client(lambda _: Response(200, json=payload))

        releases = await client.releases()

        assert releases == [
            Release(
                tag="2.0.0",
                title="Release pipeline",
                published_at="2026-07-01T10:00:00Z",
                notes="Adds the release pipeline.",
            ),
            Release(tag="1.1.0", title=None, published_at=None, notes=None),
        ]
        request = seen[0]
        assert request.url.host == "api.github.com"
        assert request.url.path == "/repos/m6mok/palgate-tg-notify/releases"
        assert request.url.params["per_page"] == "5"
        assert request.headers["Authorization"] == "Bearer gh_token"
        assert request.headers["Accept"] == "application/vnd.github+json"

    @pytest.mark.asyncio
    async def test_empty_optional_fields_become_none(self) -> None:
        payload = [
            {"tag_name": "2.0.0", "name": "", "published_at": None, "body": 1}
        ]
        client, _ = make_client(lambda _: Response(200, json=payload))

        releases = await client.releases()

        assert releases == [
            Release(tag="2.0.0", title=None, published_at=None, notes=None)
        ]

    @pytest.mark.asyncio
    async def test_malformed_entries_are_skipped(self) -> None:
        payload = [{"tag_name": "2.0.0"}, {"id": 1}, "junk", {"tag_name": ""}]
        client, _ = make_client(lambda _: Response(200, json=payload))

        releases = await client.releases()

        assert [release.tag for release in releases] == ["2.0.0"]

    @pytest.mark.asyncio
    async def test_non_200_raises(self) -> None:
        client, _ = make_client(lambda _: Response(502))

        with pytest.raises(GithubError, match="502"):
            await client.releases()

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self) -> None:
        client, _ = make_client(lambda _: Response(200, content=b"not json"))

        with pytest.raises(GithubError, match="invalid JSON"):
            await client.releases()

    @pytest.mark.asyncio
    async def test_non_list_payload_raises(self) -> None:
        client, _ = make_client(lambda _: Response(200, json={"oops": 1}))

        with pytest.raises(GithubError, match="unexpected payload"):
            await client.releases()

    @pytest.mark.asyncio
    async def test_transport_failure_raises(self) -> None:
        def broken(_: Request) -> Response:
            raise ConnectError("connection refused")

        client, _ = make_client(broken)

        with pytest.raises(GithubError, match="unreachable"):
            await client.releases()


class TestReleaseTags:
    @pytest.mark.asyncio
    async def test_returns_tag_names_newest_first(self) -> None:
        releases = [{"tag_name": "2.0.0"}, {"tag_name": "1.1.0"}]
        client, _ = make_client(lambda _: Response(200, json=releases))

        assert await client.release_tags() == ["2.0.0", "1.1.0"]


class TestDispatchDeploy:
    @pytest.mark.asyncio
    async def test_posts_the_workflow_dispatch(self) -> None:
        client, seen = make_client(lambda _: Response(204))

        await client.dispatch_deploy("1.1.0")

        request = seen[0]
        assert request.url.path == (
            "/repos/m6mok/palgate-tg-notify"
            "/actions/workflows/rollback.yml/dispatches"
        )
        assert json_loads(request.content) == {
            "ref": "master",
            "inputs": {"image_tag": "1.1.0"},
        }

    @pytest.mark.asyncio
    async def test_non_204_raises(self) -> None:
        client, _ = make_client(
            lambda _: Response(422, json={"message": "no such workflow"})
        )

        with pytest.raises(GithubError, match="422"):
            await client.dispatch_deploy("1.1.0")

    @pytest.mark.asyncio
    async def test_transport_failure_raises(self) -> None:
        def broken(_: Request) -> Response:
            raise ConnectError("connection refused")

        client, _ = make_client(broken)

        with pytest.raises(GithubError, match="unreachable"):
            await client.dispatch_deploy("1.1.0")
