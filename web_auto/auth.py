from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


def _close_login_modal(page) -> None:
    try:
        if page.locator("text=Необходимо войти на сайт!").first.is_visible(timeout=2000):
            if page.locator("#alert_close").count():
                page.locator("#alert_close").click(timeout=3000, force=True)
            else:
                page.locator(".modal").locator("button:has-text('Закрыть')").first.click(timeout=3000, force=True)
            page.evaluate(
                """
                () => {
                  const backdrops = document.querySelectorAll('.modal-backdrop');
                  backdrops.forEach(b => b.remove());
                  const modal = document.querySelector('#alert_text')?.closest('.modal');
                  if (modal) { modal.classList.remove('show'); modal.style.display='none'; }
                }
                """
            )
    except Exception:
        pass


def generate_storage_state(
    *,
    base_url: str,
    token: str,
    output_path: str,
    profile_dir: str | None = None,
    headless: bool = False,
) -> None:
    output_path = str(Path(output_path))

    with sync_playwright() as p:
        if profile_dir:
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=headless,
            )
        else:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()

        page = context.new_page()
        page.goto(f"{base_url}?token={token}", wait_until="domcontentloaded")
        _close_login_modal(page)

        try:
            page.wait_for_selector("#mid_header", timeout=15000)
        except Exception:
            pass

        context.storage_state(path=output_path)
        context.close()
