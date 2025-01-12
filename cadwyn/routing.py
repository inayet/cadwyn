import bisect
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import date
from functools import cached_property
from logging import getLogger
from typing import Any

from fastapi.routing import APIRouter
from starlette.datastructures import URL
from starlette.responses import RedirectResponse
from starlette.routing import BaseRoute, Match
from starlette.types import Receive, Scope, Send

from .route_generation import InternalRepresentationOf, generate_versioned_routers

# TODO: Remove this in a major version. This is only here for backwards compatibility
__all__ = ["InternalRepresentationOf", "generate_versioned_routers"]

_logger = getLogger(__name__)


class _RootHeaderAPIRouter(APIRouter):
    """
    this class should be a root router of the FastAPI app when using header based
    versioning. It will be used to route the requests to the correct versioned route
    based on the headers. It also supports waterflowing the requests to the latest
    version of the API if the request header doesn't match any of the versions.

    If the app has two versions: 2022-01-02 and 2022-01-05, and the request header
    is 2022-01-03, then the request will be routed to 2022-01-02 version as it the closest
    version, but lower than the request header.

    Exact match is always preferred over partial match and a request will never be
    matched to the higher versioned route
    """

    def __init__(
        self,
        *args: Any,
        api_version_header_name: str,
        api_version_var: ContextVar[date] | ContextVar[date | None],
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.versioned_routers: dict[date, APIRouter] = {}
        self.api_version_header_name = api_version_header_name.lower()
        self.api_version_var = api_version_var

    @cached_property
    def sorted_versions(self):
        return sorted(self.versioned_routers.keys())

    @cached_property
    def min_routes_version(self):
        return min(self.sorted_versions)

    def find_closest_date_but_not_new(self, request_version: date) -> date:
        index = bisect.bisect_left(self.sorted_versions, request_version)
        # as bisect_left returns the index where to insert item x in list a, assuming a is sorted
        # we need to get the previous item and that will be a match
        return self.sorted_versions[index - 1]

    def pick_version(self, request_header_value: date) -> list[BaseRoute]:
        request_version = request_header_value.isoformat()

        if self.min_routes_version > request_header_value:
            # then the request version is older that the oldest route we have
            _logger.info(
                f"Request version {request_version} "
                f"is older than the oldest "
                f"version {self.min_routes_version.isoformat()} ",
            )
            return []
        version_chosen = self.find_closest_date_but_not_new(request_header_value)
        _logger.info(
            f"Partial match. The endpoint with {version_chosen} "
            f"version was selected for API call version {request_version}",
        )
        return self.versioned_routers[version_chosen].routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The main entry point to the Router class.
        """
        if "router" not in scope:  # pragma: no cover
            scope["router"] = self

        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
            return

        header_value = self.api_version_var.get(None)

        # if header_value is None, then it's an unversioned request and we need to use the unversioned routes
        # if there will be a value, we search for the most suitable version
        if not header_value:
            routes = self.routes
        elif header_value in self.versioned_routers:
            routes = self.versioned_routers[header_value].routes
        else:
            routes = self.pick_version(request_header_value=header_value)
        await self.process_request(scope=scope, receive=receive, send=send, routes=routes)

    async def process_request(self, scope: Scope, receive: Receive, send: Send, routes: Sequence[BaseRoute]) -> None:
        """
        its a copy-paste from starlette.routing.Router
        but in this version self.routes were replaced with routes from the function arguments
        """

        partial = None
        partial_scope = {}
        for route in routes:
            # Determine if any route matches the incoming scope,
            # and hand over to the matching route if found.
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                await route.handle(scope, receive, send)
                return None
            if match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            #  Handle partial matches. These are cases where an endpoint is
            # able to handle the request, but is not a preferred option.
            # We use this in particular to deal with "405 Method Not Allowed".
            scope.update(partial_scope)
            return await partial.handle(scope, receive, send)

        if scope["type"] == "http" and self.redirect_slashes and scope["path"] != "/":
            redirect_scope = dict(scope)
            if scope["path"].endswith("/"):
                redirect_scope["path"] = redirect_scope["path"].rstrip("/")
            else:
                redirect_scope["path"] = redirect_scope["path"] + "/"

            for route in routes:
                match, child_scope = route.matches(redirect_scope)
                if match != Match.NONE:
                    redirect_url = URL(scope=redirect_scope)
                    response = RedirectResponse(url=str(redirect_url))
                    await response(scope, receive, send)
                    return None

        return await self.default(scope, receive, send)
