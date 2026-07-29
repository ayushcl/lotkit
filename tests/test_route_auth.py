from fastapi.routing import APIRoute

from api.auth import current_owner_id
from api.main import app


def _dependency_calls(route: APIRoute) -> set[object]:
    calls: set[object] = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependency = pending.pop()
        calls.add(dependency.call)
        pending.extend(dependency.dependencies)
    return calls


def test_route_table_enforces_owner_authentication_invariant() -> None:
    api_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
    ]
    public_api_methods: set[tuple[str, str]] = set()
    for route in api_routes:
        methods = route.methods or set()
        depends_on_owner = current_owner_id in _dependency_calls(route)

        for method in methods:
            if route.path.startswith("/api/") and not depends_on_owner:
                public_api_methods.add((method, route.path))
            if route.path.startswith("/d/"):
                assert not depends_on_owner, (method, route.path)

    assert public_api_methods == {("POST", "/api/auth/login")}

    by_path_method = {
        (method, route.path): route
        for route in api_routes
        for method in (route.methods or set())
    }
    assert current_owner_id in _dependency_calls(
        by_path_method[("GET", "/api/auth/me")]
    )
    assert current_owner_id in _dependency_calls(
        by_path_method[("POST", "/api/auth/logout")]
    )
    assert current_owner_id in _dependency_calls(
        by_path_method[("POST", "/api/decode")]
    )
