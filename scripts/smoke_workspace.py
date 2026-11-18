"""Run the Chromium workspace smoke test against the isolated server started by CI."""
import argparse
import re
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright


def check_workspace(page: Page, base_url: str, screenshots: Path, mobile: bool) -> None:
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    mode = "fresh"

    # Keep the transport open but silent so the monitor exercises its HTTP
    # fallback. The real status endpoint supplies the rest of the payload.
    page.route_web_socket(re.compile(r"/ws\?"), lambda socket: None)

    def device_status(route):
        response = route.fetch()
        status = response.json()
        connected = mode != "replay"
        status.update(connected=connected, sdk_available=True, device="Synthetic headband",
                      status="connected" if connected else "disconnected")
        if connected:
            status["contact"] = {"state": "waiting", "ready": False, "channels": [],
                                 "message": "Ожидание измерений сопротивления."}
        status["eeg_artifacts"] = None if mode == "replay" else {
            "state": mode, "age_s": 25 if mode == "stale" else 0.5,
            "channel_count": 2,
            "channels": [{"index": 0, "flags": 2, "quality": 0.75},
                         {"index": 1, "flags": 0, "quality": None}],
        }
        route.fulfill(json=status)

    page.route("**/api/device/status", device_status)
    page.route("**/api/replay/status", lambda route: route.fulfill(json={
        "active": mode == "replay", "session_id": "session_synthetic", "speed": 1,
    }))
    page.goto(base_url + "/#sessions")
    query = page.locator("#session-query")
    row = page.locator("[data-session-analysis='session_synthetic']")
    expect(row).to_be_visible()
    query.fill("no-matching-recording")
    expect(row).to_have_count(0)
    query.fill("synthetic")
    expect(row).to_be_visible()
    label = "mobile" if mobile else "desktop"
    page.screenshot(path=screenshots / f"{label}-archive.png")

    menu = page.locator("#menu-button")
    if mobile:
        menu.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#menu-close")).to_be_focused()
        page.keyboard.press("Escape")
        expect(menu).to_be_focused()
        expect(menu).to_have_attribute("aria-expanded", "false")
        menu.press("Enter")

    live = page.locator("#sidebar [href='#live']")
    live.focus()
    page.keyboard.press("Enter")
    expect(live).to_have_attribute("aria-current", "page")
    if mobile:
        expect(menu).to_have_attribute("aria-expanded", "false")
    for group in ("motion", "sdk", "eeg"):
        # Native radios support both pointer and keyboard selection.
        radio = page.locator(f'input[name="live-group"][value="{group}"]')
        radio.focus()
        page.keyboard.press("Space")
        expect(radio).to_be_checked()
        expect(page.locator(f'[data-plot-group="{group}"]')).to_be_visible()
        expect(page.locator("[data-plot-group]:visible")).to_have_count(1)

    details = page.locator("#live-artifact-details")
    expect(details).to_have_attribute("data-state", "fresh")
    details.locator("summary").click()
    first_channel = details.get_by_role("row", name=re.compile("Канал 1"))
    expect(first_channel.get_by_role("cell").first).to_have_text("2")
    expect(first_channel.get_by_role("cell").last).to_have_text("0,75")
    mode = "stale"
    expect(details).to_have_attribute("data-state", "stale")
    expect(page.locator("#live-artifact-state")).to_contain_text("устарел")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=screenshots / f"{label}-capsule.png")
    mode = "replay"
    expect(details).to_be_hidden()
    expect(page.locator("#live-mode")).to_contain_text("Воспроизведение")
    assert not errors, errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("screenshots", type=Path)
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, height in ((1440, 900), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            check_workspace(page, args.base_url, args.screenshots, mobile=width < 800)
            page.close()
        browser.close()
