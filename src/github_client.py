from dataclasses import dataclass
from json import JSONDecodeError
from typing import Protocol

from httpx import AsyncClient, TransportError


class GithubError(Exception):
    """GitHub API call failed."""


@dataclass(frozen=True)
class Release:
    """One GitHub Release; listings are newest first."""

    tag: str
    title: str | None
    published_at: str | None
    notes: str | None


class ReleaseGateway(Protocol):
    """Lists releases and dispatches the redeploy workflow."""

    async def releases(self, limit: int = 5) -> list[Release]: ...

    async def release_tags(self, limit: int = 5) -> list[str]: ...

    async def dispatch_deploy(self, image_tag: str) -> None: ...


class GithubClient:
    """Minimal GitHub REST client behind the ops bot's release commands.

    Needs a PAT with Actions read+write (workflow dispatch) and Contents
    read (releases list) on the repository.
    """

    def __init__(
        self,
        http: AsyncClient,
        token: str,
        repo: str,
        timeout: float = 10,
    ) -> None:
        self._http = http
        self._base_url = "https://api.github.com/repos/%s" % repo
        self._headers = {
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._timeout = timeout

    async def releases(self, limit: int = 5) -> list[Release]:
        """Releases, newest first."""
        try:
            response = await self._http.get(
                self._base_url + "/releases",
                params={"per_page": limit},
                headers=self._headers,
                timeout=self._timeout,
            )
        except TransportError as err:
            raise GithubError("GitHub unreachable: %s" % err) from err
        if response.status_code != 200:
            raise GithubError("GitHub responded %d" % response.status_code)
        try:
            payload = response.json()
        except JSONDecodeError as err:
            raise GithubError("GitHub sent invalid JSON: %s" % err) from err
        if not isinstance(payload, list):
            raise GithubError("GitHub sent an unexpected payload")
        releases: list[Release] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            tag = entry.get("tag_name")
            if not isinstance(tag, str) or not tag:
                continue
            releases.append(
                Release(
                    tag=tag,
                    title=_optional_str(entry.get("name")),
                    published_at=_optional_str(entry.get("published_at")),
                    notes=_optional_str(entry.get("body")),
                )
            )
        return releases

    async def release_tags(self, limit: int = 5) -> list[str]:
        """Release tag names, newest first."""
        return [release.tag for release in await self.releases(limit)]

    async def dispatch_deploy(self, image_tag: str) -> None:
        """Trigger rollback.yml on master with the given image tag.

        The workflow redeploys an already-built image without creating
        tags or moving ``latest``, so it serves both /rollback and
        /release.
        """
        try:
            response = await self._http.post(
                self._base_url + "/actions/workflows/rollback.yml/dispatches",
                json={"ref": "master", "inputs": {"image_tag": image_tag}},
                headers=self._headers,
                timeout=self._timeout,
            )
        except TransportError as err:
            raise GithubError("GitHub unreachable: %s" % err) from err
        if response.status_code != 204:
            raise GithubError(
                "GitHub refused the dispatch: %d %s"
                % (response.status_code, response.text[:200])
            )


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
