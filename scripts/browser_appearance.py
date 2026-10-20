"""Shared six-palette selection for synthetic browser acceptance journeys."""
from browser_navigation import wait_for_replacement

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select

APPEARANCES = tuple((mode, accent) for mode in ("dark", "light") for accent in ("cyan", "green", "amber"))


def select_appearance(driver, wait, mode, accent):
    """Use the visible picker and wait for each persisted page navigation."""
    for field, value in (("theme", mode), ("accent", accent)):
        root = driver.find_element(By.TAG_NAME, "html")
        if root.get_attribute(f"data-{field}") == value:
            continue
        picker = driver.find_element(By.ID, f"appearance-{field}")
        if not picker.is_displayed():
            menu = driver.find_element(By.CSS_SELECTOR, ".account-menu")
            if not menu.get_attribute("open"):
                menu.find_element(By.CSS_SELECTOR, "summary").click()
            picker = wait.until(EC.visibility_of_element_located((By.ID, f"account-appearance-{field}")))
        Select(picker).select_by_value(value)
        wait_for_replacement(driver, wait, root)
        wait.until(lambda _: driver.execute_script(
            "return document.readyState === 'complete' && document.documentElement.dataset[arguments[0]] === arguments[1]",
            field, value))
