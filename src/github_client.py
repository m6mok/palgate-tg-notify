from json import JSONDecodeError
from typing import Protocol

from httpx import AsyncClient, TransportError


class GithubError(Exception):
    """GitHub API call failed."""


class RollbackGateway(Protocol):
    """Lists released versions and dispatches the rollback workflow."""

    async def release_tags(self, limit: int = 5) -> list[str]: ...

    async def dispatch_rollback(self, image_tag: str) -> None: ...


class GithubClient:
    """Minimal GitHub REST client behind the ops bot's /rollback command.

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

    async def release_tags(self, limit: int = 5) -> list[str]:
        """Release tag names, newest first."""
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
            releases = response.json()
        except JSONDecodeError as err:
            raise GithubError("GitHub sent invalid JSON: %s" % err) from err
        if not isinstance(releases, list):
            raise GithubError("GitHub sent an unexpected payload")
        tags: list[str] = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            tag = release.get("tag_name")
            if isinstance(tag, str) and tag:
                tags.append(tag)
        return tags

    async def dispatch_rollback(self, image_tag: str) -> None:
        """Trigger rollback.yml on master with the given image tag."""
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
