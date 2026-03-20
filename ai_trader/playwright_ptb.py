"""
使用 Playwright 获取 Price to Beat
支持单个和批量获取（单浏览器多标签页，省内存）
"""
import time
import re
from datetime import datetime, timezone


def _extract_ptb_from_page(page, slug, timeout_ms=12000):
    """从单个页面提取 PTB（策略1: 网络拦截 → 策略2: DOM提取）
    Returns: float 或 None
    """
    url = f"https://polymarket.com/event/{slug}"
    ptb_from_network = {"value": None}

    def _handle_response(response):
        if ptb_from_network["value"]:
            return
        try:
            resp_url = response.url
            if ("gamma-api.polymarket.com" in resp_url or
                    "strapi" in resp_url or
                    "polymarket.com/api" in resp_url):
                body = response.text()
                m = re.search(r'"priceToBeat"\s*:\s*"?([\d.]+)"?', body)
                if m:
                    val = float(m.group(1))
                    if 100 < val < 10_000_000:
                        ptb_from_network["value"] = val
        except Exception:
            pass

    page.on("response", _handle_response)

    # 拦截不必要的资源
    def _block_resources(route):
        rt = route.request.resource_type
        req_url = route.request.url
        if rt in ("image", "media", "font"):
            route.abort()
        elif any(x in req_url for x in ["analytics", "segment", "hotjar", "sentry", "gtag"]):
            route.abort()
        else:
            route.continue_()

    page.route("**/*", _block_resources)

    # 导航到页面
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    # 等待网络请求带回 PTB（最多等 8s）
    for _ in range(80):
        if ptb_from_network["value"]:
            break
        page.wait_for_timeout(100)

    if ptb_from_network["value"]:
        return ptb_from_network["value"]

    # === 策略2：等待页面渲染出独立的 PTB 标签 ===
    try:
        page.wait_for_function(
            r"""() => {
                const els = document.querySelectorAll('span, div, p, h1, h2, h3');
                for (const el of els) {
                    if (el.children.length > 0) continue;
                    const t = el.textContent.trim().toLowerCase();
                    if (t === 'price to beat' || t === 'price to beat:') return true;
                }
                return false;
            }""",
            timeout=8000
        )
    except Exception:
        pass

    ptb_text = page.evaluate(r"""() => {
        const allEls = document.querySelectorAll('span, div, p, h1, h2, h3');
        for (const el of allEls) {
            if (el.children.length > 0) continue;
            const txt = el.textContent.trim().toLowerCase();
            if (txt === 'price to beat' || txt === 'price to beat:') {
                let ancestor = el.parentElement;
                for (let depth = 0; depth < 5; depth++) {
                    if (!ancestor) break;
                    const candidates = ancestor.querySelectorAll('span, div, p');
                    for (let c of candidates) {
                        if (c.children.length > 0) continue;
                        const text = c.textContent.trim();
                        if (/^\$[\d,]+(\.\d+)?$/.test(text)) {
                            return text;
                        }
                    }
                    ancestor = ancestor.parentElement;
                }
            }
        }
        return null;
    }""")

    if ptb_text:
        price_str = ptb_text.replace("$", "").replace(",", "").strip()
        return float(price_str)

    return None


async def _extract_ptb_async(page, slug, timeout_ms=12000):
    """异步版 PTB 提取（供 get_ptb_batch 使用）"""
    url = f"https://polymarket.com/event/{slug}"
    ptb_from_network = {"value": None}

    async def _handle_response(response):
        if ptb_from_network["value"]:
            return
        try:
            resp_url = response.url
            if ("gamma-api.polymarket.com" in resp_url or
                    "strapi" in resp_url or
                    "polymarket.com/api" in resp_url):
                body = await response.text()
                m = re.search(r'"priceToBeat"\s*:\s*"?([\d.]+)"?', body)
                if m:
                    val = float(m.group(1))
                    if 100 < val < 10_000_000:
                        ptb_from_network["value"] = val
        except Exception:
            pass

    page.on("response", _handle_response)

    async def _block_resources(route):
        rt = route.request.resource_type
        req_url = route.request.url
        if rt in ("image", "media", "font"):
            await route.abort()
        elif any(x in req_url for x in ["analytics", "segment", "hotjar", "sentry", "gtag"]):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _block_resources)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    for _ in range(80):
        if ptb_from_network["value"]:
            break
        await page.wait_for_timeout(100)

    if ptb_from_network["value"]:
        return ptb_from_network["value"]

    # 策略2: DOM 提取
    try:
        await page.wait_for_function(
            r"""() => {
                const els = document.querySelectorAll('span, div, p, h1, h2, h3');
                for (const el of els) {
                    if (el.children.length > 0) continue;
                    const t = el.textContent.trim().toLowerCase();
                    if (t === 'price to beat' || t === 'price to beat:') return true;
                }
                return false;
            }""",
            timeout=8000
        )
    except Exception:
        pass

    ptb_text = await page.evaluate(r"""() => {
        const allEls = document.querySelectorAll('span, div, p, h1, h2, h3');
        for (const el of allEls) {
            if (el.children.length > 0) continue;
            const txt = el.textContent.trim().toLowerCase();
            if (txt === 'price to beat' || txt === 'price to beat:') {
                let ancestor = el.parentElement;
                for (let depth = 0; depth < 5; depth++) {
                    if (!ancestor) break;
                    const candidates = ancestor.querySelectorAll('span, div, p');
                    for (let c of candidates) {
                        if (c.children.length > 0) continue;
                        const text = c.textContent.trim();
                        if (/^\$[\d,]+(\.\d+)?$/.test(text)) {
                            return text;
                        }
                    }
                    ancestor = ancestor.parentElement;
                }
            }
        }
        return null;
    }""")

    if ptb_text:
        price_str = ptb_text.replace("$", "").replace(",", "").strip()
        return float(price_str)

    return None


def get_price_to_beat_playwright(slug, timeout_ms=12000):
    """单个 PTB 获取（独立启动浏览器，兼容旧调用方式）"""
    from playwright.sync_api import sync_playwright

    playwright = None
    browser = None

    try:
        t0 = time.time()
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-images"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = context.new_page()
        ptb = _extract_ptb_from_page(page, slug, timeout_ms)
        elapsed = time.time() - t0
        if ptb:
            print(f"✅ PTB={ptb:.2f} (Playwright, {elapsed:.1f}s)")
        else:
            print(f"⚠️ PTB 未找到 (Playwright, {elapsed:.1f}s)")
        return ptb

    except Exception as e:
        print(f"❌ Playwright 错误: {e}")
        return None

    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if playwright:
                playwright.stop()
        except Exception:
            pass


def get_ptb_batch(slugs, timeout_ms=12000):
    """批量获取 PTB — 单浏览器 asyncio 并发多页面，省内存
    Args: slugs: ["btc-updown-5m-xxx", "eth-updown-5m-xxx", ...]
    Returns: {"btc-updown-5m-xxx": 70500.0, "eth-updown-5m-xxx": 2150.0, ...}
             获取失败的 slug 不包含在结果中
    """
    import asyncio

    if not slugs:
        return {}

    async def _batch_async():
        from playwright.async_api import async_playwright
        results = {}
        t0 = time.time()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-images"]
            )

            async def _fetch_one(slug_item):
                try:
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                        viewport={"width": 1280, "height": 720}
                    )
                    page = await context.new_page()
                    ptb = await _extract_ptb_async(page, slug_item, timeout_ms)
                    if ptb:
                        results[slug_item] = ptb
                    await context.close()
                except Exception as e:
                    print(f"  ⚠️ PTB获取异常({slug_item[-20:]}): {str(e)[:60]}")

            await asyncio.gather(*[_fetch_one(s) for s in slugs])
            await browser.close()

        elapsed = time.time() - t0
        ok_count = len(results)
        total = len(slugs)
        if ok_count > 0:
            ptb_str = " | ".join(f"{s[-15:]}={results[s]:.2f}" for s in results)
            print(f"✅ 批量PTB {ok_count}/{total} ({elapsed:.1f}s): {ptb_str}")
        else:
            print(f"⚠️ 批量PTB 全部失败 ({elapsed:.1f}s)")
        return results

    return asyncio.run(_batch_async())


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        test_slugs = sys.argv[1:]
    else:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        base_5m = (now_ts // 300) * 300
        test_slugs = [f"btc-updown-5m-{base_5m}", f"eth-updown-5m-{base_5m}"]

    print(f"测试 slugs: {test_slugs}")

    if len(test_slugs) == 1:
        t0 = time.time()
        ptb = get_price_to_beat_playwright(test_slugs[0])
        print(f"PTB={ptb}, 耗时={time.time()-t0:.1f}s")
    else:
        t0 = time.time()
        results = get_ptb_batch(test_slugs)
        print(f"结果: {results}, 耗时={time.time()-t0:.1f}s")
