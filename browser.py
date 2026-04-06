"""
Hlídač slev – Playwright browser modul
Headless browser pro e-shopy, které:
  1) Renderují ceny JavaScriptem (Kasa, AboutYou, Zalando)
  2) Blokují HTTP požadavky přes Cloudflare (Alza, Notino) – pokusíme se obejít

Playwright se používá JEN jako fallback, když normální HTTP scraping selže.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Globální browser instance – sdílená pro celou session
_browser = None
_playwright = None
_browser_lock = asyncio.Lock()

# Playwright stealth nastavení – skryjeme automatizaci
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--window-size=1920,1080",
    "--start-maximized",
    "--disable-extensions",
]

# Stealth JS – spustí se na každé stránce před načtením
STEALTH_JS = """
// Skryj webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => false});

// Skryj automatizační plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
        {name: 'Native Client', filename: 'internal-nacl-plugin'},
    ]
});

// Nastav reálné languages
Object.defineProperty(navigator, 'languages', {get: () => ['cs-CZ', 'cs', 'sk', 'en-US', 'en']});

// Skryj Headless Chrome v user agent
if (navigator.userAgent.includes('Headless')) {
    Object.defineProperty(navigator, 'userAgent', {
        get: () => navigator.userAgent.replace('Headless', '')
    });
}

// Chrome runtime objekt (chybí v headless)
if (!window.chrome) {
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
}

// Permissions API – vrať realistické odpovědi
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
);
"""

# Blokujeme nepotřebné zdroje pro zrychlení
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_PATTERNS = [
    "google-analytics", "googletagmanager", "facebook.net",
    "doubleclick", "hotjar", "analytics", "adservice",
    "seznam.cz/ssp", "heureka.cz/direct", "sklik",
]


async def get_browser():
    """Vrátí sdílenou browser instanci (lazy init)."""
    global _browser, _playwright
    async with _browser_lock:
        if _browser and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=STEALTH_ARGS,
            )
            logger.info("Playwright browser spuštěn")
            return _browser
        except Exception as e:
            logger.error(f"Nelze spustit Playwright: {e}")
            return None


async def close_browser():
    """Zavře browser (volat při shutdown)."""
    global _browser, _playwright
    async with _browser_lock:
        if _browser:
            await _browser.close()
            _browser = None
        if _playwright:
            await _playwright.stop()
            _playwright = None


async def fetch_page_with_browser(url: str, wait_for_selector: str | None = None,
                                   timeout_ms: int = 15000) -> Optional[str]:
    """
    Načte stránku přes headless browser a vrátí plné HTML po JS renderování.
    Vrátí None pokud selže.
    """
    browser = await get_browser()
    if not browser:
        return None

    context = None
    page = None
    try:
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="cs-CZ",
            timezone_id="Europe/Prague",
            java_script_enabled=True,
        )

        # Stealth script na každé stránce
        await context.add_init_script(STEALTH_JS)

        # Blokuj nepotřebné zdroje (rychlost)
        page = await context.new_page()
        await page.route("**/*", _route_handler)

        # Navigace
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        if not response:
            return None

        status = response.status
        if status == 403:
            # Cloudflare challenge – počkáme déle, třeba se vyřeší
            logger.info(f"Playwright: 403 na {url}, čekám na Cloudflare challenge…")
            await page.wait_for_timeout(5000)
            # Zkontroluj jestli se stránka přenačetla
            html = await page.content()
            if "cf-browser-verification" in html or "challenge-platform" in html:
                logger.warning(f"Playwright: Cloudflare challenge na {url} – nelze obejít")
                return None

        # Počkej na renderování cen
        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector, timeout=5000)
            except Exception:
                pass  # Pokračuj i bez selektoru

        # Počkej na JS renderování
        await page.wait_for_timeout(2000)

        html = await page.content()
        return html if len(html) > 500 else None

    except Exception as e:
        logger.warning(f"Playwright chyba pro {url}: {e}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass


async def _route_handler(route):
    """Blokuje nepotřebné zdroje pro zrychlení."""
    request = route.request
    resource_type = request.resource_type

    if resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return

    url_lower = request.url.lower()
    if any(pattern in url_lower for pattern in BLOCKED_URL_PATTERNS):
        await route.abort()
        return

    await route.continue_()


async def fetch_with_browser_batch(urls: list[str], max_concurrent: int = 3,
                                    timeout_ms: int = 15000) -> dict[str, str]:
    """
    Načte více stránek paralelně přes browser.
    Vrátí dict {url: html} pro úspěšně načtené stránky.
    max_concurrent je nízký – každá stránka je těžká operace.
    """
    results = {}
    sem = asyncio.Semaphore(max_concurrent)

    async def fetch_one(url: str):
        async with sem:
            html = await fetch_page_with_browser(url, timeout_ms=timeout_ms)
            if html:
                results[url] = html

    tasks = [asyncio.create_task(fetch_one(u)) for u in urls]
    await asyncio.gather(*tasks, return_exceptions=True)
    return results


def is_playwright_available() -> bool:
    """Zkontroluje jestli je Playwright nainstalovaný."""
    try:
        import playwright
        return True
    except ImportError:
        return False
