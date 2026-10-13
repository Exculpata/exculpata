"""Real browser export-and-exit steps for disposable synthetic acceptance apps."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import zipfile

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from case_intelligence.identity import SESSION_COOKIE
from case_intelligence.matter_archive_verifier import verify_matter_archive


def completed_zip(directory: Path, before: set[Path]) -> Path | None:
    paths = list(directory.iterdir())
    if any(path.suffix == ".crdownload" for path in paths):
        return None
    for path in paths:
        if path in before or path.suffix != ".zip":
            continue
        try:
            if path.is_file() and zipfile.is_zipfile(path):
                return path
        except OSError:
            continue  # Chrome can still be renaming the completed download.
    return None


class BrowserPage:
    def __init__(self, driver, base: str):
        self.driver, self.base = driver, base
        self.wait = WebDriverWait(driver, 60)

    def ready(self):
        self.wait.until(lambda driver: driver.execute_script("return document.readyState") == "complete")
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    def detached(self, element):
        try:
            return EC.staleness_of(element)(self.driver)
        except WebDriverException as exc:
            if "Node with given id does not belong to the document" not in exc.msg:
                raise
            return True

    def go(self, path: str):
        previous = self.driver.find_element(By.TAG_NAME, "html")
        self.driver.get(self.base + path)
        self.wait.until(lambda _: self.detached(previous))
        self.ready()

    def click(self, selector: str, *, submit: bool = False):
        element = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", element)
        element.click()
        if submit:
            self.wait.until(lambda _: self.detached(element))
            self.ready()


def preview_login(driver, base: str, subject: str = "taylor-morgan") -> None:
    """Choose a synthetic identity through the normal login form and session."""
    page = BrowserPage(driver, base)
    driver.delete_all_cookies()
    page.go("/auth/login?next=/matters/manage")
    page.click(f'button[name="identity_subject"][value="{subject}"]', submit=True)
    assert driver.get_cookie(SESSION_COOKIE), "Preview sign-in did not create a session"


class BrowserCaseExit(BrowserPage):
    """Use UI actions for all lifecycle changes; only synthetic time is injected."""

    def __init__(self, driver, base: str, app, matter, downloads: Path, *, preview_subject: str | None = None):
        super().__init__(driver, base)
        self.app, self.matter, self.downloads = app, matter, downloads
        self.bench = app.state.workbench
        self.prefix = f"/matters/{matter.slug}"
        self.preview_subject = preview_subject

    def prepare(self):
        self.go(self.prefix + "/close")
        self.click(f'form[action="{self.prefix}/close/begin"] button[type=submit]', submit=True)
        assert self.bench.workspace.matter_lifecycle(self.matter.matter_id).state == "closing"
        assert not self.driver.find_elements(By.ID, "confirmed-name")
        self.click(f'form[action="{self.prefix}/close/export"] button[type=submit]', submit=True)
        current = self.bench.workspace.case_lifecycle.current_exit(self.matter.matter_id)
        assert current.export_id, self.driver.find_element(By.TAG_NAME, "body").text
        before = set(self.downloads.iterdir())
        self.click(f'a[href="{self.prefix}/close/export"]')
        downloaded = self.wait.until(lambda _: completed_zip(self.downloads, before))
        manifest = verify_matter_archive(downloaded, expected_sha256=current.archive_digest)
        assert manifest["exit_id"] == current.exit_id
        assert manifest["export_id"] == current.export_id
        assert manifest["original_source_files_included"] is True
        self.fresh_confirmation()
        return downloaded, manifest

    def fresh_confirmation(self):
        current = self.bench.workspace.case_lifecycle.current_exit(self.matter.matter_id)
        eligible = datetime.fromisoformat(current.eligible_after.replace("Z", "+00:00"))
        # The app and data belong only to this disposable synthetic harness.
        # Dates are not edited, and deletion is still approved by browser forms.
        now = max(self.bench.workspace.current_time(), eligible + timedelta(seconds=1))
        self.bench.workspace._clock = lambda: now
        self.app.state.identity._clock = lambda: now
        previous_session = self.driver.get_cookie(SESSION_COOKIE)
        self.driver.delete_all_cookies()
        if self.preview_subject:
            preview_login(self.driver, self.base, self.preview_subject)
        # Existing Kerberos CDP headers obtain a fresh normal server session.
        self.go(self.prefix + "/close")
        session = self.driver.get_cookie(SESSION_COOKIE)
        assert session and (not previous_session or session["value"] != previous_session["value"])
        assert self.bench.workspace.matter_lifecycle(self.matter.matter_id).state == "closing"
        self.click(f'form[action="{self.prefix}/close/confirm"] button[type=submit]', submit=True)
        self.wait.until(EC.visibility_of_element_located((By.ID, "confirmed-name")))
        assert self.driver.find_element(By.CSS_SELECTOR, '.close-matter-form input[name="token"]').get_attribute("value")

    def confirm_name(self, name: str):
        field = self.driver.find_element(By.ID, "confirmed-name")
        field.clear()
        field.send_keys(name)
        self.click('input[name="export_acknowledged"]')
        self.click('input[name="acknowledge"]')
        self.click(".close-matter-form button[type=submit]", submit=True)
