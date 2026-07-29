from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app


def _frontend_source() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "api"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")


def test_root_has_login_ui_and_owner_interface_starts_hidden() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert 'id="login-form"' in response.text
    assert 'id="login-email"' in response.text
    assert 'id="login-password"' in response.text
    assert 'id="owner-app" hidden' in response.text
    assert 'id="logout-button"' in response.text
    assert 'id="photographer-name"' in response.text


def test_frontend_authenticates_before_loading_owner_data() -> None:
    source = _frontend_source()
    script_tail = source[source.index("<script>") :]
    assert 'apiFetch("/api/auth/me")' in script_tail
    assert "initialiseAuthentication();" in script_tail
    assert script_tail.rstrip().endswith("</script>\n  </body>\n</html>")
    initialisation_tail = script_tail[script_tail.rindex(
        "initialiseAuthentication();"
    ) :]
    assert "loadDealerships();" not in initialisation_tail
    assert "loadRunHistory();" not in initialisation_tail


def test_central_fetch_helper_attaches_csrf_without_forcing_content_type() -> None:
    source = _frontend_source()
    assert "async function apiFetch" in source
    assert 'cookieValue("lotkit_owner_csrf")' in source
    assert 'headers.set("X-CSRF-Token", csrfToken)' in source
    assert 'credentials: "same-origin"' in source
    assert 'url.pathname === "/api/auth/login"' in source
    assert "new Headers(init.headers || {})" in source
    assert 'headers.set("Content-Type"' not in source
    assert source.count("await fetch(") == 1


def test_frontend_handles_401_and_clears_authenticated_ui() -> None:
    source = _frontend_source()
    assert "response.status === 401" in source
    assert "showLogin(" in source
    assert "function clearOwnerInterface()" in source
    assert "ownerApp.hidden = true" in source
    assert "clearActiveRun();" in source


def test_frontend_has_no_signup_reset_or_remember_me_controls() -> None:
    source = _frontend_source().casefold()
    assert "sign up" not in source
    assert "signup" not in source
    assert "forgot password" not in source
    assert "reset password" not in source
    assert "remember me" not in source
    assert 'sessionstorage.setitem("lotkit_owner' not in source
    assert 'localstorage.setitem("lotkit_owner' not in source
