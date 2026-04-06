"""
Hlídač slev – scraper v2
Dva módy:
  1. Kategorie / URL se stránkou produktů → projde stránku + paginaci
  2. Homepage → XML sitemap → projde VŠECHNY produktové URL (ne náhodně)

Playwright fallback: když HTTP scraping selže (Cloudflare 403 nebo JS rendering),
automaticky se přepne na headless browser.
"""

import asyncio
import aiohttp
import json
import re
import gzip
import logging
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode, quote
from xml.etree import ElementTree as ET
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs,sk,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate",
}

MAX_CONCURRENT  = 20     # Souběžné požadavky (zvýšeno pro rychlost)
MAX_PRODUCTS    = 5000   # Maximální počet produktů ke skenování – chceme VŠECHNY
MAX_PAGES       = 25     # Max stránek kategorie
MIN_DISCOUNT    = 5      # Minimální sleva v %
MAX_DISCOUNT    = 95     # Maximální sleva (filter šumu)
TIME_LIMIT      = 180    # Sekundy – soft limit celé analýzy (3 minuty)

# Playwright: max souběžných záložek (browser je náročný na RAM)
BROWSER_MAX_CONCURRENT = 3
# Playwright: max produktů k scrapování přes browser (je pomalý)
BROWSER_MAX_PRODUCTS = 200

# E-shopy, které vyžadují Playwright (JS rendering nebo Cloudflare)
JS_RENDERED_SHOPS = [
    "kasa.cz", "aboutyou.cz", "aboutyou.sk", "zalando.cz", "zalando.sk",
    "zoot.cz", "zoot.sk", "bonami.cz",
]
CLOUDFLARE_SHOPS = [
    "alza.cz", "alza.sk", "czc.cz", "mall.cz", "mall.sk",
    "notino.cz", "notino.sk", "datart.cz",
]


# ─────────────────────────────────────────────────────────────
#  HLAVNÍ FUNKCE
# ─────────────────────────────────────────────────────────────

async def analyze_eshop(url: str) -> AsyncGenerator[dict, None]:
    """
    Yields SSE eventy:
      {"type":"step",     "id":str, "label":str, "status":"active"|"done"|"error"}
      {"type":"progress", "current":int, "total":int, "percent":int, "top3":list}
      {"type":"done",     "results":list, "total_scraped":int, "total_found":int}
      {"type":"error",    "message":str}
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed    = urlparse(url)
    base_url  = f"{parsed.scheme}://{parsed.netloc}"
    is_homepage = parsed.path.strip("/") == ""
    domain    = parsed.netloc.lower().replace("www.", "")

    # ── Detekce: potřebuje tento shop headless browser? ────────
    needs_browser = any(shop in domain for shop in JS_RENDERED_SHOPS + CLOUDFLARE_SHOPS)
    use_browser = False

    if needs_browser:
        from browser import is_playwright_available
        if is_playwright_available():
            use_browser = True
            is_cf = any(shop in domain for shop in CLOUDFLARE_SHOPS)
            browser_reason = "Cloudflare ochrana" if is_cf else "JS renderování cen"
            logger.info(f"Playwright aktivní pro {domain} ({browser_reason})")
        else:
            logger.warning(f"{domain} potřebuje Playwright, ale není nainstalovaný")

    connector = aiohttp.TCPConnector(ssl=False, limit=MAX_CONCURRENT + 10)
    timeout   = aiohttp.ClientTimeout(total=TIME_LIMIT + 30, connect=8, sock_read=15)

    async with aiohttp.ClientSession(
        headers=HEADERS, connector=connector, timeout=timeout
    ) as session:

        # ── Detekce platformy ──────────────────────────────────
        yield {"type": "step", "id": "platform", "label": "Detekuji platformu…", "status": "active"}

        platform, platform_name, final_base = await detect_platform(session, base_url)
        if final_base:
            base_url = final_base  # aktualizuj po přesměrování

        if use_browser:
            platform_name += " + Playwright"

        yield {"type": "step", "id": "platform", "label": f"Platforma: {platform_name}", "status": "done"}

        # ── Shopify: rychlá cesta přes /products.json ──────────
        if platform == "shopify":
            yield {"type": "step", "id": "sitemap", "label": "Čtu Shopify API…", "status": "active"}
            shopify_products = await get_shopify_products(session, base_url)
            if shopify_products:
                total = len(shopify_products)
                yield {"type": "step", "id": "sitemap", "label": f"Shopify API: {total} produktů", "status": "done"}
                yield {"type": "step", "id": "prices",  "label": "Zpracovávám ceny…", "status": "active"}
                results = process_shopify_data(shopify_products, base_url)
                yield {"type": "step", "id": "prices",  "label": f"Nalezeno {len(results)} se slevou", "status": "done"}
                yield {"type": "step", "id": "sort",    "label": "Seřazuji výsledky", "status": "done"}
                top10 = sorted(results, key=lambda x: x["discount"], reverse=True)[:10]
                yield {"type": "done", "results": top10, "total_scraped": total, "total_found": total}
                return

        # ── Kategorie nebo homepage? ────────────────────────────
        if not is_homepage:
            # ── Mód: kategorie / stránka výpisu ───────────────
            yield {
                "type": "step", "id": "sitemap",
                "label": "Hledám produkty na stránce…", "status": "active"
            }
            async for ev in scrape_category_mode(session, url, base_url, use_browser):
                yield ev
        else:
            # ── Mód: homepage → sitemap ────────────────────────
            yield {"type": "step", "id": "sitemap", "label": "Hledám sitemapu…", "status": "active"}
            async for ev in scrape_sitemap_mode(session, url, base_url, use_browser):
                yield ev


# ─────────────────────────────────────────────────────────────
#  MÓD 1: KATEGORIE – projdi výpis produktů + paginaci
# ─────────────────────────────────────────────────────────────

async def scrape_category_mode(
    session: aiohttp.ClientSession, start_url: str, base_url: str,
    use_browser: bool = False,
) -> AsyncGenerator[dict, None]:

    # Pokud potřebujeme browser, zkusíme načíst kategorii přes Playwright
    if use_browser:
        product_urls = await _collect_category_with_browser(start_url, base_url)
    else:
        product_urls = await collect_category_products(session, start_url, base_url)

    if not product_urls:
        yield {
            "type": "step", "id": "sitemap", "label": "Žádné produkty nenalezeny", "status": "error"
        }
        yield {
            "type": "error",
            "message": (
                "Na zadané stránce nebyly nalezeny žádné produkty. "
                "Zkuste URL konkrétní kategorie nebo zkontrolujte, zda stránka funguje v prohlížeči."
            ),
        }
        return

    total_found = len(product_urls)
    yield {
        "type": "step", "id": "sitemap",
        "label": f"Nalezeno {total_found} produktů na stránce", "status": "done"
    }

    async for ev in scrape_and_rank(session, product_urls, total_found, use_browser):
        yield ev


async def collect_category_products(
    session: aiohttp.ClientSession, start_url: str, base_url: str
) -> list[str]:
    """Projde stránku výpisu + paginaci, vrátí seznam produktových URL."""
    all_products: set[str] = set()
    visited: set[str] = set()
    current_url = start_url
    page = 1

    while page <= MAX_PAGES and current_url and current_url not in visited:
        visited.add(current_url)
        try:
            async with session.get(current_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    break
                html = await resp.text(encoding="utf-8", errors="replace")
        except Exception:
            break

        # Produktové URL na stránce
        found = extract_product_links(html, base_url, current_url)
        all_products.update(found)

        # Paginace
        next_url = find_next_page(html, current_url, base_url, page)
        if not next_url or next_url == current_url:
            break

        current_url = next_url
        page += 1

        if len(all_products) >= MAX_PRODUCTS:
            break

    return list(all_products)


def extract_product_links(html: str, base_url: str, page_url: str) -> list[str]:
    """
    Najde produktové URL na stránce výpisu.
    Nejdřív hledá linky se sémantickými CSS třídami produktových karet,
    pak fallback na URL heuristiku.
    """
    found: set[str] = set()

    def normalize(href: str) -> str | None:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            return None
        if href.startswith("/"):
            return base_url + href
        if not href.startswith("http"):
            return urljoin(page_url, href)
        return href

    # ── Metoda 1: sémantické CSS třídy produktových karet ──────────
    # Linky uvnitř karet produktů na výpisové stránce mají typicky tyto třídy:
    PRODUCT_LINK_CLASSES = (
        "link-detail", "product-link", "product-name", "product-title",
        "item-link", "card-link", "detail-link", "product__link",
        "product-item__link", "product-card__link", "woocommerce-LoopProduct-link",
        "product-box__link", "tile-link", "product__name", "product-box__name",
        # Shoptet
        "main-link", "product-item-title", "item-title",
        # WooCommerce
        "woocommerce-loop-product__link", "product_title",
        # PrestaShop
        "product-thumbnail", "product-title",
        # Obecné
        "product__title", "product-card__title", "product-card-title",
        "product-card__name", "card__heading", "card-title",
    )
    pattern = "|".join(re.escape(c) for c in PRODUCT_LINK_CLASSES)

    for m in re.finditer(
        rf'<a\s[^>]*class=["\'][^"\']*(?:{pattern})[^"\']*["\'][^>]*href=["\']([^"\'#?]+)["\']'
        r'|<a\s[^>]*href=["\']([^"\'#?]+)["\'][^>]*class=["\'][^"\']*(?:' + pattern + r')[^"\']*["\']',
        html, re.IGNORECASE,
    ):
        href = m.group(1) or m.group(2)
        url = normalize(href)
        if url and url.startswith(base_url):
            found.add(url)

    if found:
        return list(found)

    # ── Metoda 2: fallback – URL heuristika ────────────────────────
    for href in re.findall(r'href=["\']([^"\'#?][^"\']*)["\']', html):
        url = normalize(href)
        if url and url.startswith(base_url) and is_product_url(url, base_url):
            found.add(url)

    return list(found)


def find_next_page(html: str, current_url: str, base_url: str, current_page: int) -> str | None:
    """Najde URL další stránky výpisu."""
    # rel="next"
    m = re.search(r'<link[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if not m:
        m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']next["\']', html, re.IGNORECASE)
    if m:
        href = m.group(1)
        if not href.startswith("http"):
            href = urljoin(current_url, href)
        return href

    # Tlačítko/odkaz na další stránku
    next_patterns = [
        r'href=["\']([^"\']+)["\'][^>]*>(?:\s*(?:Další|Dalsi|Next|›|»|&rsaquo;|&raquo;)\s*)</a>',
        r'href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*(?:next|dalsi)[^"\']*["\']',
    ]
    for pat in next_patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            href = m.group(1)
            if not href.startswith("http"):
                href = urljoin(current_url, href)
            if href != current_url:
                return href

    # Zkus přidat ?page= nebo ?strana= nebo ?p=
    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    next_page = str(current_page + 1)

    for param in ["page", "strana", "p", "pg", "pagina", "stranica"]:
        if param in qs:
            qs[param] = [next_page]
            new_qs = urlencode({k: v[0] for k, v in qs.items()})
            new_url = urlunparse(parsed._replace(query=new_qs))
            # Ověř, že stránka s tímto číslem existuje (heuristika: jen vrátíme URL)
            return new_url

    # URL path segment: /strana-2/ → /strana-3/
    path_page_re = re.compile(r'(.*?[/-])(strana|page|p|str)[-/]?(\d+)(/?.*)', re.IGNORECASE)
    m = path_page_re.match(parsed.path)
    if m:
        new_path = f"{m.group(1)}{m.group(2)}-{int(m.group(3)) + 1}{m.group(4)}"
        new_url = urlunparse(parsed._replace(path=new_path))
        return new_url

    return None


# ─────────────────────────────────────────────────────────────
#  MÓD 2: SITEMAP – projdi VŠECHNY produktové URL (bez náhody)
# ─────────────────────────────────────────────────────────────

async def scrape_sitemap_mode(
    session: aiohttp.ClientSession, url: str, base_url: str,
    use_browser: bool = False,
) -> AsyncGenerator[dict, None]:

    try:
        product_urls, category_urls = await get_sitemap_urls(session, base_url)
    except Exception as e:
        # Pokud sitemap selhala a máme browser, zkusíme homepage přes browser
        if use_browser:
            yield {
                "type": "step", "id": "sitemap",
                "label": "Sitemap blokována, zkouším přes prohlížeč…", "status": "active"
            }
            product_urls = await _collect_category_with_browser(url, base_url)
            if product_urls:
                total_found = len(product_urls)
                yield {
                    "type": "step", "id": "sitemap",
                    "label": f"Prohlížeč: nalezeno {total_found} produktů", "status": "done"
                }
                async for ev in scrape_and_rank(session, product_urls, total_found, use_browser):
                    yield ev
                return
        yield {"type": "step", "id": "sitemap", "label": "Sitemap nenalezena", "status": "error"}
        yield {"type": "error", "message": f"Sitemap se nepodařilo načíst: {e}"}
        return

    # Sitemap má produkty přímo
    if product_urls:
        total_found = len(product_urls)
        max_limit = BROWSER_MAX_PRODUCTS if use_browser else MAX_PRODUCTS
        if total_found > max_limit:
            prioritized = _prioritize_urls(product_urls)
            product_urls = prioritized[:max_limit]
            label = f"Nalezeno {total_found} produktů, skenuji {len(product_urls)}"
        else:
            label = f"Nalezeno {total_found} produktů"
        if use_browser:
            label += " (přes prohlížeč – bude to chvíli trvat)"
        yield {"type": "step", "id": "sitemap", "label": label, "status": "done"}
        async for ev in scrape_and_rank(session, product_urls, total_found, use_browser):
            yield ev
        return

    # Sitemap má jen kategorie → projdeme je a najdeme produkty
    if category_urls:
        cats_to_try = category_urls[:12]
        yield {
            "type": "step", "id": "sitemap",
            "label": f"Sitemap má {len(category_urls)} kategorií, hledám produkty v nich…",
            "status": "active",
        }
        all_products: set[str] = set()
        for cat_url in cats_to_try:
            if use_browser:
                products_in_cat = await _collect_category_with_browser(cat_url, base_url)
            else:
                products_in_cat = await collect_category_products(session, cat_url, base_url)
            all_products.update(products_in_cat)
            if len(all_products) >= (BROWSER_MAX_PRODUCTS if use_browser else MAX_PRODUCTS):
                break

        if all_products:
            total_found = len(all_products)
            max_limit = BROWSER_MAX_PRODUCTS if use_browser else MAX_PRODUCTS
            product_urls = list(all_products)[:max_limit]
            yield {
                "type": "step", "id": "sitemap",
                "label": f"Nalezeno {total_found} produktů přes kategorie",
                "status": "done",
            }
            async for ev in scrape_and_rank(session, product_urls, total_found, use_browser):
                yield ev
            return

    # Pokud HTTP scraping nic nenašel a máme browser, zkusíme homepage přes browser
    if use_browser:
        yield {
            "type": "step", "id": "sitemap",
            "label": "Standardní cesta selhala, zkouším prohlížeč…", "status": "active"
        }
        product_urls = await _collect_category_with_browser(url, base_url)
        if product_urls:
            total_found = len(product_urls)
            yield {
                "type": "step", "id": "sitemap",
                "label": f"Prohlížeč: nalezeno {total_found} produktů", "status": "done"
            }
            async for ev in scrape_and_rank(session, product_urls, total_found, use_browser):
                yield ev
            return

    # Ani sitemap ani kategorie nevedou k produktům
    yield {"type": "step", "id": "sitemap", "label": "Produkty nenalezeny", "status": "error"}
    yield {
        "type": "error",
        "message": (
            "Nepodařilo se najít produkty. Zkuste zadat URL konkrétní kategorie nebo výprodeje "
            "(např. eshop.cz/vyprodej nebo eshop.cz/kategorie). "
            "Velké e-shopy jako Alza nebo Mall bohužel blokují automatické stahování dat."
        ),
    }


async def _collect_category_with_browser(start_url: str, base_url: str) -> list[str]:
    """
    Načte stránku kategorie přes Playwright a extrahuje produktové linky.
    Používá se pro e-shopy s Cloudflare/JS renderováním.
    """
    try:
        from browser import fetch_page_with_browser
    except ImportError:
        return []

    html = await fetch_page_with_browser(start_url, timeout_ms=20000)
    if not html:
        return []

    # Extrahuj produktové linky z HTML
    found = extract_product_links(html, base_url, start_url)

    # Pokud jsme na homepage a nenašli produktové linky, zkus najít kategorii slev
    if not found:
        sale_links = []
        for m in re.finditer(r'href=["\']([^"\'#]+)["\']', html):
            href = m.group(1)
            if not href.startswith("http"):
                href = urljoin(start_url, href)
            if href.startswith(base_url):
                lower = href.lower()
                if any(s in lower for s in ["/akce", "/sleva", "/vyprodej", "/sale", "/outlet", "/zlevneno"]):
                    sale_links.append(href)

        # Projdi max 3 slevové kategorie
        for sale_url in sale_links[:3]:
            sale_html = await fetch_page_with_browser(sale_url, timeout_ms=15000)
            if sale_html:
                products = extract_product_links(sale_html, base_url, sale_url)
                found.extend(products)

    # Limit pro browser mód
    return list(set(found))[:BROWSER_MAX_PRODUCTS]


def _prioritize_urls(urls: list[str]) -> list[str]:
    """Seřadí URL tak, aby nahoře byly ty s větší pravděpodobností slevy."""
    sale_signals = ["sleva", "akce", "vyprodej", "sale", "discount", "outlet", "zlevneno"]
    priority = []
    rest = []
    for u in urls:
        if any(s in u.lower() for s in sale_signals):
            priority.append(u)
        else:
            rest.append(u)
    return priority + rest


# ─────────────────────────────────────────────────────────────
#  SPOLEČNÉ: SKENOVÁNÍ CEN + ŘAZENÍ
# ─────────────────────────────────────────────────────────────

async def scrape_and_rank(
    session: aiohttp.ClientSession,
    product_urls: list[str],
    total_found: int,
    use_browser: bool = False,
) -> AsyncGenerator[dict, None]:

    sample_size = len(product_urls)
    mode_label = " (prohlížeč)" if use_browser else ""
    yield {
        "type": "step", "id": "prices",
        "label": f"Scrapuji ceny{mode_label} (0 / {sample_size})…", "status": "active"
    }

    results: list[dict] = []
    completed = 0
    progress_queue: asyncio.Queue = asyncio.Queue()
    concurrency = BROWSER_MAX_CONCURRENT if use_browser else MAX_CONCURRENT
    sem = asyncio.Semaphore(concurrency)

    async def scrape_one(product_url: str) -> None:
        async with sem:
            try:
                product = await extract_product_data(session, product_url, use_browser)
                if product:
                    results.append(product)
            except Exception:
                pass
            finally:
                await progress_queue.put(1)

    tasks = [asyncio.create_task(scrape_one(u)) for u in product_urls]
    report_every = max(1, sample_size // 20)

    while completed < sample_size:
        await progress_queue.get()
        completed += 1
        pct = int(completed / sample_size * 100)

        if completed % report_every == 0 or completed == sample_size:
            top3 = sorted(results, key=lambda x: x["discount"], reverse=True)[:3]
            yield {
                "type": "progress",
                "current": completed,
                "total": sample_size,
                "percent": pct,
                "top3": top3,
            }

    await asyncio.gather(*tasks, return_exceptions=True)

    # Pokud jsme použili browser, zavřeme ho
    if use_browser:
        try:
            from browser import close_browser
            await close_browser()
        except Exception:
            pass

    if not results:
        yield {
            "type": "error",
            "message": (
                "Žádné produkty se slevou nebyly nalezeny. "
                "E-shop pravděpodobně nepoužívá schema.org markup nebo nezobrazuje původní ceny."
            ),
        }
        return

    yield {"type": "step", "id": "sort", "label": "Seřazuji výsledky", "status": "done"}
    top10 = sorted(results, key=lambda x: x["discount"], reverse=True)[:10]
    yield {"type": "done", "results": top10, "total_scraped": completed, "total_found": total_found}


# ─────────────────────────────────────────────────────────────
#  DETEKCE PLATFORMY
# ─────────────────────────────────────────────────────────────

async def detect_platform(
    session: aiohttp.ClientSession, base_url: str
) -> tuple[str, str, str | None]:
    """Vrací (platform_id, platform_name, final_base_url)"""
    try:
        async with session.get(base_url, allow_redirects=True) as resp:
            html = await resp.text(encoding="utf-8", errors="replace")
            final_url = str(resp.url)
            final_parsed = urlparse(final_url)
            final_base = f"{final_parsed.scheme}://{final_parsed.netloc}"

            checks = [
                ("shopify",    "Shopify",    ["cdn.shopify.com", "Shopify.theme", "shopify.com/s/files"]),
                ("shoptet",    "Shoptet",    ["cdn.myshoptet.com", "shoptet.cz", "data-shoptet"]),
                ("woocommerce","WooCommerce",["wp-content/plugins/woocommerce", "woocommerce"]),
                ("prestashop", "PrestaShop", ["var prestashop", "prestashop"]),
                ("upgates",    "Upgates",    ["upgates.com", "upgates.cz"]),
                ("magento",    "Magento",    ["Mage.Cookies", "magento", "mage/"]),
            ]
            for pid, pname, signals in checks:
                if any(s.lower() in html.lower() or s.lower() in final_url.lower() for s in signals):
                    return pid, pname, final_base if final_base != base_url else None

            return "unknown", "Vlastní řešení", final_base if final_base != base_url else None

    except Exception:
        return "unknown", "Vlastní řešení", None


# ─────────────────────────────────────────────────────────────
#  SHOPIFY
# ─────────────────────────────────────────────────────────────

async def get_shopify_products(session: aiohttp.ClientSession, base_url: str) -> list[dict]:
    all_products = []
    page = 1
    while page <= 6:
        try:
            async with session.get(f"{base_url}/products.json?limit=250&page={page}") as resp:
                if resp.status != 200:
                    break
                data = await resp.json(content_type=None)
                products = data.get("products", [])
                if not products:
                    break
                all_products.extend(products)
                if len(products) < 250:
                    break
                page += 1
        except Exception:
            break
    return all_products


def process_shopify_data(products: list[dict], base_url: str) -> list[dict]:
    results = []
    for product in products:
        title = product.get("title", "Neznámý produkt")
        handle = product.get("handle", "")
        url = f"{base_url}/products/{handle}"
        best_discount, best = 0, None
        for variant in product.get("variants", []):
            try:
                price = float(variant.get("price") or 0)
                compare = float(variant.get("compare_at_price") or 0)
                if compare > price > 0:
                    discount = round((compare - price) / compare * 100)
                    if discount > best_discount:
                        best_discount = discount
                        best = {
                            "name": title[:100], "url": url,
                            "original_price": round(compare),
                            "current_price": round(price),
                            "discount": discount,
                        }
            except (ValueError, TypeError):
                pass
        if best and best_discount >= MIN_DISCOUNT:
            results.append(best)
    return results


# ─────────────────────────────────────────────────────────────
#  SITEMAP
# ─────────────────────────────────────────────────────────────

async def get_sitemap_urls(
    session: aiohttp.ClientSession, base_url: str
) -> tuple[list[str], list[str]]:
    """
    Vrátí (product_urls, category_urls).
    Nejdřív načte sitemapu, pak rozdělí URL podle is_product_url / is_category_url.
    """
    candidates = [
        f"{base_url}/sitemap.xml",
        f"{base_url}/sitemap_index.xml",
        f"{base_url}/sitemap-index.xml",
        f"{base_url}/sitemap-products.xml",
        f"{base_url}/sitemap/sitemap.xml",
        f"{base_url}/sitemap1.xml",
    ]
    try:
        async with session.get(f"{base_url}/robots.txt") as resp:
            if resp.status == 200:
                robots = await resp.text(errors="replace")
                for line in robots.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        if sm_url not in candidates:
                            candidates.insert(0, sm_url)
    except Exception:
        pass

    for sm_url in candidates:
        try:
            all_urls = await fetch_sitemap(session, sm_url, base_url, depth=0)
            if not all_urls:
                continue

            unique = list(set(all_urls))
            product_urls  = [u for u in unique if is_product_url(u, base_url)]
            category_urls = [u for u in unique if not is_product_url(u, base_url)
                             and is_category_url(u, base_url)]

            # Pokud sitemap vrátila alespoň něco, vracíme (i prázdné produktové)
            if product_urls or category_urls:
                return product_urls, category_urls

        except Exception:
            continue

    return [], []


async def fetch_sitemap(
    session: aiohttp.ClientSession, sitemap_url: str, base_url: str, depth: int
) -> list[str]:
    """Vrátí VŠECHNY URL z dané sitemapy (produktové i kategoriové). Klasifikaci dělá volající."""
    if depth > 3:
        return []
    try:
        async with session.get(sitemap_url) as resp:
            if resp.status != 200:
                return []
            raw = await resp.read()
    except Exception:
        return []

    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            return []

    # Odstraň neplatné kontrolní znaky, které rozbíjejí XML parser
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return []
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    ns_match = re.match(r"\{[^}]+\}", root.tag)
    ns = ns_match.group(0) if ns_match else ""

    urls: list[str] = []
    for sm in root.findall(f"{ns}sitemap"):
        loc = sm.find(f"{ns}loc")
        if loc is not None and loc.text:
            sub = await fetch_sitemap(session, loc.text.strip(), base_url, depth + 1)
            urls.extend(sub)
            if len(urls) > 15_000:
                break

    for url_elem in root.findall(f"{ns}url"):
        loc = url_elem.find(f"{ns}loc")
        if loc is not None and loc.text:
            u = loc.text.strip()
            if u.startswith(base_url):
                urls.append(u)

    return urls


# ─────────────────────────────────────────────────────────────
#  HEURISTIKA – produktová URL
# ─────────────────────────────────────────────────────────────

def is_product_url(url: str, base_url: str) -> bool:
    if not url.startswith(base_url):
        return False

    lower = url.lower()
    path = urlparse(url).path

    skip = [
        "/blog", "/news", "/novinky", "/clanek", "/clanky",
        "/kategori", "/category", "/categories", "/kolekce", "/collection",
        "/tag/", "/tags/", "/search", "/hledan", "/cart", "/kosik",
        "/checkout", "/login", "/prihlaseni", "/account", "/ucet",
        "/kontakt", "/contact", "/o-nas", "/about", "/faq", "/pomoc",
        "/sitemap", ".xml", ".json", "/img/", "/image/", "/media/",
        "/upload/", "/cdn-cgi/", "/wp-admin", "/admin",
        "/znacka/", "/brand/", "/vyrobce/",              # brand pages
        "/navody", "/tipy", "/kviz", "/recept",          # content pages
        "/recenze", "/hodnoceni", "/store/p/",            # review pages, PrestaShop cat
        "/klient/", "/zakaznik/", "/muj-ucet/", "/wishlist/",  # customer account pages
        "/poukaz", "/voucher", "/darkovy-poukaz", "/gift-card",  # dárkové poukazy
    ]
    for s in skip:
        if s in lower:
            return False

    # Explicitní produktové signály
    positive = [
        "/produkt", "/product", "/products", "/zbozi", "/item",
        "/p/", "/detail", "/eshop/", "/katalog/", "/goods/",
        "-p-", "-d-", "/shop/",
    ]
    for p in positive:
        if p in lower:
            return True

    # Alza/Mall style: název-dID.htm  (např. sony-wh1000xm5-d7654321.htm)
    if re.search(r"-d\d{5,}", lower):
        return True

    # Numerické ID v URL (kdekoliv, ne jen za /)
    if re.search(r"\d{5,}", path):
        return True

    # 4-ciferné ID jako prefix segmentu: /1400-snubni-prsten/ (Shoptet, WooCommerce)
    if re.search(r"/\d{3,5}-[a-z]", path, re.IGNORECASE):
        return True

    # Hluboká URL (aspoň 3 segmenty) = pravděpodobně produkt, ne kategorie
    parts = [x for x in path.split("/") if x]
    if len(parts) >= 3:
        return True

    # 2 segmenty, ale poslední vypadá jako slug s produktovým názvem (obsahuje více slov)
    if len(parts) == 2 and "-" in parts[-1] and len(parts[-1]) > 15:
        return True

    # 1 segment s výrazně popisným slugem = produkt (ne kratká kategorie)
    # Např: /fasciq-silikonove-oblicejove-banky-4ks/ nebo /hedvabny-povlak-na-polstar-slip/
    if len(parts) == 1:
        slug = parts[0]
        hyphens = slug.count("-")
        # 4+ pomlčky = pravděpodobně popis produktu s adjektivy/atributy
        if hyphens >= 4:
            return True
        # Slug obsahuje měrnou jednotku, množství nebo typický produktový identifikátor
        if re.search(r'\d+(ml|mg|kg|g|ks|cm|mm|l|pcs|pack)', slug, re.IGNORECASE):
            return True

    return False


def is_category_url(url: str, base_url: str) -> bool:
    """
    Vrátí True, pokud URL vypadá jako stránka kategorie/výpisu (ne produkt, ne statická).
    Slouží pro kaskádu sitemap→kategorie→produkty.
    """
    if not url.startswith(base_url):
        return False

    lower = url.lower()
    path  = urlparse(url).path

    # Přeskoč technické URL
    skip_always = [
        ".xml", ".json", ".jpg", ".png", ".gif", ".css", ".js",
        "/wp-admin", "/admin", "/cdn-cgi/", "/img/", "/image/",
        "/checkout", "/cart", "/kosik", "/login", "/account", "/ucet",
        "/sitemap", "/feed", "/rss",
        "/blog", "/news", "/novinky", "/clanek",
        "/recenze", "/hodnoceni",
        "/znacka/", "/brand/", "/vyrobce/",
    ]
    for s in skip_always:
        if s in lower:
            return False

    # Explicitní kategoriové signály
    category_signals = [
        "/kategori", "/category", "/categories",
        "/kolekce", "/collection", "/collections/",
        "/c/", "/cat/", "/vyprodej", "/akce", "/sleva", "/sale", "/outlet",
    ]
    for s in category_signals:
        if s in lower:
            return True

    # Mělká URL (1–2 segmenty, žádné numerické ID) = pravděpodobně kategorie
    parts = [x for x in path.split("/") if x]
    if len(parts) in (1, 2) and not re.search(r"\d{4,}", path):
        return True

    return False


# ─────────────────────────────────────────────────────────────
#  SCRAPOVÁNÍ PRODUKTOVÉ STRÁNKY
# ─────────────────────────────────────────────────────────────

async def extract_product_data(
    session: aiohttp.ClientSession, url: str, use_browser: bool = False
) -> dict | None:
    html = None

    if use_browser:
        # Playwright mód: načti stránku přes headless browser
        try:
            from browser import fetch_page_with_browser
            html = await fetch_page_with_browser(url)
        except Exception:
            pass
    else:
        # Normální HTTP mód
        try:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    if not html:
        return None

    prices = extract_prices(html)
    if not prices:
        return None

    original, current = prices
    if original <= current:
        return None

    discount = round((original - current) / original * 100)
    if not (MIN_DISCOUNT <= discount <= MAX_DISCOUNT):
        return None

    name = extract_product_name(html, url)

    # Filtruj falešné „slevy":
    # - dárkové poukazy/vouchery (varianty 300–5000 Kč nejsou sleva)
    # - bazar/použité zboží (nižší cena ≠ sleva, jen opotřebení)
    # - sety/balíčky kde se porovnává cena setu vs jednotlivých kusů
    name_lower = name.lower()
    if any(w in name_lower for w in [
        "poukaz", "voucher", "gift card", "dárkov", "darkov",
        "bazar", "bazár", "použit", "pouzit", "second hand", "secondhand",
        "repas", "zánovní", "zanovni", "bývší", "byvsi",
    ]):
        return None

    return {
        "name": name,
        "url": url,
        "original_price": round(original),
        "current_price": round(current),
        "discount": discount,
    }


# ─────────────────────────────────────────────────────────────
#  EXTRAKCE CEN
# ─────────────────────────────────────────────────────────────

def _isolate_main_product(html: str) -> str:
    """
    Ořízne HTML: odstraní <script> tagy a vše za sekcí 'doporučené/podobné produkty'.
    Tím zamezíme míchání cen hlavního produktu s cenami příbuzných produktů.
    """
    # 1) Smaž scripty (obsahují JS odkazy na CSS třídy s cenami)
    clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.I)

    # 2) Najdi konec hlavního produktu – ořízni před sekcí „doporučené", „mohlo by vás zajímat" apod.
    #    Jen TEXT-markery (viditelný obsah), ne CSS třídy (ty matchují v atributech)
    text_markers = [
        "mohlo by vás také zajímat", "mohlo by vas take zajimat",
        "doporučujeme k tomuto produktu", "doporucujeme k tomuto produktu",
        "podobné produkty", "podobne produkty",
        "mohlo by se vám líbit",
        "související produkty", "souvisejici produkty",
        "doplňkové zboží", "doplnkove zbozi",
        "customers also bought", "you might also like",
    ]
    # CSS class markery – specifičtější, aby nematchovaly v URL (cartupsell.css apod.)
    class_markers = [
        "products-related-header",
        "related-products-section",
        "also-bought-section",
    ]
    cut_pos = len(clean)
    lower = clean.lower()
    for marker in text_markers:
        idx = lower.find(marker)
        # Aspoň 3000 znaků – hlavní produkt musí být celý
        if 3000 < idx < cut_pos:
            cut_pos = idx
    for marker in class_markers:
        idx = lower.find(marker)
        if 3000 < idx < cut_pos:
            cut_pos = idx

    return clean[:cut_pos]


def extract_prices(html: str) -> tuple[float, float] | None:
    # JSON-LD a meta tagy prohledáváme v celém HTML (jsou unikátní a bezpečné)
    result = extract_from_jsonld(html)
    if result:
        return result

    # Pro microdata a HTML vzory: pracujeme jen s hlavní produktovou oblastí
    main_html = _isolate_main_product(html)

    return (
        extract_from_microdata(main_html)
        or extract_from_meta(html)
        or extract_from_html_patterns(main_html)
    )


def extract_from_jsonld(html: str) -> tuple[float, float] | None:
    scripts = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for script in scripts:
        try:
            data = json.loads(script.strip())
            items = data if isinstance(data, list) else [data]
            expanded = []
            for item in items:
                if isinstance(item, dict) and "@graph" in item:
                    expanded.extend(item["@graph"])
                else:
                    expanded.append(item)
            for item in expanded:
                # Přeskoč Heureka/Zbozi review widgety – injektují falešný Product schema
                if isinstance(item, dict):
                    name = str(item.get("name", "")).lower()
                    url_val = str(item.get("url", "")).lower()
                    if any(s in name or s in url_val for s in [
                        "heureka", "zbozi.cz", "hodnocení obchodu", "hodnoceni obchodu",
                        "recenze obchodu", "reviews"
                    ]):
                        continue
                prices = _parse_schema_product(item)
                if prices:
                    return prices
        except Exception:
            continue
    return None


def _parse_schema_product(data: dict) -> tuple[float, float] | None:
    if not isinstance(data, dict):
        return None
    if "Product" not in str(data.get("@type", "")):
        return None

    offers = data.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None

    # Detekce AggregateOffer – lowPrice/highPrice = rozsah variant (NE sleva!)
    is_aggregate = "AggregateOffer" in str(offers.get("@type", ""))

    current = None
    if is_aggregate:
        # AggregateOffer: lowPrice/highPrice = různé varianty (75g vs 2750g), NE sleva
        # Hledáme jen explicitní slevu (price vs priceBeforeDiscount)
        val = offers.get("price") or offers.get("lowPrice")
        current = parse_price(val)
    else:
        for key in ["price", "lowPrice"]:
            val = offers.get(key)
            current = parse_price(val)
            if current:
                break
    if not current:
        return None

    original = None

    if is_aggregate:
        # U AggregateOffer: highPrice NENÍ původní cena, je to jen nejvyšší varianta
        # Hledáme JEN explicitní slevu
        for key in ["priceBeforeDiscount"]:
            val = offers.get(key)
            original = parse_price(val)
            if original:
                break
    else:
        for key in ["highPrice", "priceBeforeDiscount"]:
            val = offers.get(key)
            original = parse_price(val)
            if original:
                break

    if not original:
        specs = data.get("priceSpecification", []) or offers.get("priceSpecification", [])
        if isinstance(specs, dict):
            specs = [specs]
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            if any(t in str(spec.get("@type", "")) for t in ["ListPrice", "CrossedOut", "SuggestedRetailPrice"]):
                val = parse_price(spec.get("price"))
                if val:
                    original = val
                    break

    if original and original > current:
        return (original, current)
    return None


def extract_from_microdata(html: str) -> tuple[float, float] | None:
    prices = []
    for pat in [
        r'itemprop=["\']price["\'][^>]*(?:content|value)=["\']([^"\']+)["\']',
        r'(?:content|value)=["\']([^"\']+)["\'][^>]*itemprop=["\']price["\']',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            p = parse_price(m.group(1))
            if p:
                prices.append(p)

    if len(prices) >= 2:
        prices = sorted(set(prices), reverse=True)
        if prices[0] > prices[-1]:
            return (prices[0], prices[-1])
    return None


def extract_from_meta(html: str) -> tuple[float, float] | None:
    meta: dict[str, str] = {}
    for m in re.finditer(
        r'<meta[^>]+(?:property|name)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        meta[m.group(1).lower()] = m.group(2)
    for m in re.finditer(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        meta[m.group(2).lower()] = m.group(1)

    current = None
    for key in ["product:price:amount", "og:price:amount", "price"]:
        if key in meta:
            current = parse_price(meta[key])
            if current:
                break
    if not current:
        return None

    original = None
    for key in ["og:price:regular", "original_price", "compare_at_price", "product:original_price"]:
        if key in meta:
            original = parse_price(meta[key])
            if original:
                break

    if original and original > current:
        return (original, current)
    return None


def extract_from_html_patterns(html: str) -> tuple[float, float] | None:
    # ── Shoptet specifická extrakce ───────────────────────────────────
    # Shoptet: původní cena je v <span class="price-standard">,
    #          aktuální cena je v microdata itemprop="price"
    shoptet_orig_re = re.compile(
        r'class=["\'][^"\']*price-standard[^"\']*["\'][^>]*>'
        r'(?:\s*<[^>]+>)*\s*([0-9][0-9 \u00a0\.,]{1,12})\s*(?:K[čČ]|CZK|€|EUR)?',
        re.IGNORECASE | re.DOTALL,
    )
    shoptet_curr_re = re.compile(
        r'itemprop=["\']price["\'][^>]*content=["\']([0-9][0-9.,]{1,12})["\']'
        r'|class=["\'][^"\']*(?:price-final|main-price|price-main|buy-price|'
        r'product-price__final|price__value)[^"\']*["\'][^>]*>'
        r'(?:\s*<[^>]+>)*\s*([0-9][0-9 \u00a0\.,]{1,12})\s*(?:K[čČ]|CZK|€|EUR)?',
        re.IGNORECASE | re.DOTALL,
    )
    # Bereme PRVNÍ shodu (= hlavní produkt, ne doporučené)
    st_orig = None
    for m in shoptet_orig_re.finditer(html):
        p = parse_price(m.group(1))
        if p:
            st_orig = p
            break

    st_curr = None
    for m in shoptet_curr_re.finditer(html):
        v = m.group(1) or m.group(2)
        p = parse_price(v)
        if p:
            st_curr = p
            break

    if st_orig and st_curr and st_orig > st_curr:
        return (st_orig, st_curr)

    # ── Obecné HTML vzory ─────────────────────────────────────────────
    original_re = re.compile(
        r'class=["\'][^"\']*(?:price.?original|price.?old|price.?before|old.?price|'
        r'was.?price|price.?crossed|puvodni.?cena|cena.?pred|price.?regular|'
        r'before.?price|strikethrough|price.?strike|cena.?bez.?slevy|'
        r'price-before|before-price|price--old|price_old|price--regular|'
        r'WasPrice|OriginalPrice|RrpPrice|price-rrp|normal-price)[^"\']*["\'][^>]*>'
        r'(?:\s*<[^>]+>)*\s*([0-9][0-9 \u00a0\.,]{1,12})\s*(?:K[čČ]|CZK|€|EUR)?',
        re.IGNORECASE | re.DOTALL,
    )
    current_re = re.compile(
        r'class=["\'][^"\']*(?:price.?sale|sale.?price|price.?current|current.?price|'
        r'price.?now|now.?price|price.?discount|aktualni.?cena|prodejni.?cena|'
        r'price.?final|price.?special|special.?price|'
        r'price--sale|price--current|selling-price|offer-price)[^"\']*["\'][^>]*>'
        r'(?:\s*<[^>]+>)*\s*([0-9][0-9 \u00a0\.,]{1,12})\s*(?:K[čČ]|CZK|€|EUR)?',
        re.IGNORECASE | re.DOTALL,
    )

    # Bereme PRVNÍ shodu = hlavní produkt
    orig = None
    for m in original_re.finditer(html):
        p = parse_price(m.group(1))
        if p:
            orig = p
            break

    curr = None
    for m in current_re.finditer(html):
        p = parse_price(m.group(1))
        if p:
            curr = p
            break

    if orig and curr and orig > curr:
        return (orig, curr)

    # ── Fallback: del / strike cena vedle aktuální ceny ──────────────
    # Vzor: <del>..cena..</del>..aktuální cena..
    del_prices = re.findall(
        r'<(?:del|s|strike)[^>]*>\s*(?:<[^>]+>)*\s*([0-9][0-9 \u00a0\.,]{1,10})\s*(?:K[čČ]|CZK|€)?\s*(?:</[^>]+>)*\s*</(?:del|s|strike)>',
        html, re.IGNORECASE | re.DOTALL
    )
    if del_prices:
        after_del = re.search(
            r'</(?:del|s|strike)>.*?([0-9][0-9 \u00a0\.,]{1,10})\s*(?:K[čČ]|CZK|€)',
            html, re.IGNORECASE | re.DOTALL
        )
        if after_del:
            orig = max(p for p in (parse_price(d) for d in del_prices) if p)
            curr = parse_price(after_del.group(1))
            if orig and curr and orig > curr:
                return (orig, curr)

    return None


# ─────────────────────────────────────────────────────────────
#  NÁZEV PRODUKTU
# ─────────────────────────────────────────────────────────────

def extract_product_name(html: str, url: str) -> str:
    for script in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(script.strip())
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and "Product" in str(item.get("@type", "")):
                    name = str(item.get("name", "")).strip()
                    # Přeskoč Heureka/Zbozi review widgety
                    if any(s in name.lower() for s in ["heureka", "zbozi", "hodnocení", "hodnoceni"]):
                        continue
                    if name:
                        return name[:120]
        except Exception:
            pass

    for pat in [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:120]

    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        title = re.split(r'\s*[|–—-]\s*(?=[A-ZÁČĎÉĚÍŇÓŘŠŤŮÚÝŽ])', title)[0].strip()
        if title:
            return title[:120]

    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").replace("_", " ").title()[:120]


# ─────────────────────────────────────────────────────────────
#  PARSOVÁNÍ CENY
# ─────────────────────────────────────────────────────────────

def parse_price(value) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            f = float(value)
            return f if 1 <= f <= 10_000_000 else None

        s = str(value).strip()
        s = re.sub(r'[Kk][Čč]|CZK|EUR|€|\$|£|PLN|,-|\s|\u00a0', '', s)
        if not s:
            return None

        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            parts = s.split(",")
            if len(parts) == 2 and len(parts[1]) <= 2:
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")

        f = float(s)
        return f if 1 <= f <= 10_000_000 else None

    except (ValueError, AttributeError):
        return None
