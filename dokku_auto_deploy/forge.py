"""What the deploy cycle needs from a forge (the service hosting the repository), independent of which one it is."""

import dataclasses
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any, Literal

HTTP_TIMEOUT = 30

# missing: no CI run for the commit yet; pending: queued or running; failure: finished without success (cancelled and
# skipped included, as they didn't prove the commit works).
CIStatus = Literal["missing", "pending", "success", "failure"]


class ForgeError(RuntimeError):
    pass


class _CredentialSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib copies every header to the redirect target: drop the credentials when it's another origin (host, port
    or scheme), so a redirect can't send the token elsewhere or over plain HTTP."""

    CREDENTIAL_HEADERS = ("Authorization", "Private-token")  # As `Request` stores them (`str.capitalize`)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        old_origin = urllib.parse.urlsplit(req.full_url)[:2]
        if new is not None and urllib.parse.urlsplit(newurl)[:2] != old_origin:
            for name in self.CREDENTIAL_HEADERS:
                new.remove_header(name)
        return new


_opener = urllib.request.build_opener(_CredentialSafeRedirectHandler)


@dataclasses.dataclass(frozen=True)
class Change:
    """A merged pull request (GitHub, Forgejo) or merge request (GitLab)."""

    number: int
    reference: str  # How the forge writes it in text: "#12" or "!12"
    title: str
    url: str
    shas: frozenset[str]  # Commits whose presence in the deployed range means this change was merged in it


@dataclasses.dataclass
class MergedChanges:
    changes: list[Change]
    page_full: bool  # The forge returned a full page: older changes exist and weren't fetched


class Forge:
    """Base class: one instance per repository. Subclasses set `name` and implement the API calls."""

    name = "forge"

    def __init__(self, api: str) -> None:
        self.api = api.rstrip("/")

    def headers(self) -> dict[str, str]:
        return {}

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, payload: Any = None) -> Any:
        """JSON request to `api` + `path`; errors raise `ForgeError` with the API's message (never the token)."""
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", **self.headers()}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _opener.open(request, timeout=HTTP_TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ForgeError(f"{self.name} API {method} {path}: HTTP {exc.code} {_error_message(exc)}") from None
        except urllib.error.URLError as exc:
            raise ForgeError(f"could not reach {self.name} API at {self.api}: {exc.reason}") from None
        except TimeoutError:
            raise ForgeError(f"{self.name} API at {self.api} did not answer in {HTTP_TIMEOUT}s") from None
        except (OSError, http.client.HTTPException) as exc:  # Connection dropped mid-response, etc.
            raise ForgeError(f"{self.name} API {method} {path}: {type(exc).__name__}: {exc}") from None
        except json.JSONDecodeError:
            raise ForgeError(f"{self.name} API {method} {path}: the answer is not JSON") from None
        except ValueError as exc:  # Only the type: e.g. an invalid header value would be the token itself
            raise ForgeError(f"{self.name} API {method} {path}: invalid request ({type(exc).__name__})") from None

    def branch_head(self, branch: str) -> str:
        raise NotImplementedError

    def ci_status(self, sha: str, branch: str, workflow: str) -> CIStatus:
        """Status of the latest CI run of `workflow` for `sha` pushed to `branch` (a successful re-run wins)."""
        raise NotImplementedError

    def commits_between(self, base: str, head: str) -> set[str]:
        raise NotImplementedError

    def merged_changes(self, branch: str) -> MergedChanges:
        """Recently merged changes into `branch` (one page); callers keep the ones whose `shas` are in the deployed
        range."""
        raise NotImplementedError

    def comment(self, change: Change, body: str) -> None:
        raise NotImplementedError

    def commit_url(self, sha: str) -> str:
        raise NotImplementedError


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.load(exc)
    except ValueError:
        return str(exc.reason)
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or ""
        return str(message) or str(exc.reason)
    return str(exc.reason)
