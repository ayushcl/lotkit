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
    assert "clearActiveRun({forgetStored: false});" in source


def test_frontend_has_no_signup_reset_or_remember_me_controls() -> None:
    source = _frontend_source().casefold()
    assert "sign up" not in source
    assert "signup" not in source
    assert "forgot password" not in source
    assert "reset password" not in source
    assert "remember me" not in source
    assert 'sessionstorage.setitem("lotkit_owner' not in source
    assert 'localstorage.setitem("lotkit_owner' not in source


def test_in_progress_run_has_resume_action_and_server_validated_restore() -> None:
    source = _frontend_source()

    assert 'id="resume-run-button"' in source
    assert ">\n                Resume run\n              </button>" in source
    assert "async function fetchResumableRun(runId)" in source
    assert "`/api/runs/${encodeURIComponent(runId)}/resume`" in source
    assert 'payload.status !== "in_progress"' in source
    assert 'payload.run_id !== runId' in source
    assert 'resumeRunButton.hidden = runStatus !== "in_progress";' in source
    assert 'resumeRunButton.hidden = run.status !== "in_progress";' in source
    assert "resumeRunButton.addEventListener" in source
    assert "reopenRunButton.addEventListener" in source


def test_resume_restores_form_dealership_artifacts_and_same_run_target() -> None:
    source = _frontend_source()
    restore_start = source.index("function restoreRunWorkflow(run)")
    restore_end = source.index("async function fetchResumableRun", restore_start)
    restore = source[restore_start:restore_end]

    assert "...runVehicle(run)" in restore
    assert "price: run.price" in restore
    assert "exterior_colour: run.exterior_colour" in restore
    assert "interior_colour: run.interior_colour" in restore
    assert "vinInput.value = normaliseVin(run.vin);" in restore
    assert "populateVehicleForm(vehicle);" in restore
    assert "applyRunDealership(run);" in restore
    assert "setActiveRun(run.run_id, run.vin, run.status);" in restore
    assert "restoreRunArtifacts(run);" in restore

    assert "outputs.sticker_pdf" in source
    assert "outputs.buyers_guide_pdf" in source
    assert "outputs.photos_zip" in source
    assert "already packaged for this Run" in source
    assert "appendActiveRun(requestData);" in source


def test_resume_rehydrates_from_server_after_refresh_and_login() -> None:
    source = _frontend_source()

    assert "await restoreStoredActiveRun();" in source
    assert "async function restoreStoredActiveRun()" in source
    assert "const payload = await fetchResumableRun(stored.run_id);" in source
    assert "restoreRunWorkflow(payload);" in source
    assert "clearActiveRun({forgetStored: false});" in source
    assert "sessionStorage.setItem(" in source
    assert "sessionStorage.removeItem(" in source
