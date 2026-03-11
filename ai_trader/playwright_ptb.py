"""
使用 Playwright 独立进程获取 Price to Beat
优化：每次调用独立启动/关闭浏览器，避免内存泄漏和EPIPE错误
"""
import time
import re
from datetime import datetime, timezone


def get_price_to_beat_playwright(slug, timeout_ms=12000):
    """
    用 Playwright 获取 Price to Beat（独立进程模式）
    
    每次调用约 6-8 秒（启动+获取+关闭）
    避免长时间运行导致的内存泄漏和EPIPE错误
    
    Returns: float 或 None
    """
    from playwright.sync_api import sync_playwright
    
    playwright = None
    browser = None
    
    try:
        url = f"https://polymarket.com/event/{slug}"
        t0 = time.time()
        
        # 启动浏览器
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-images",
            ]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = context.new_page()

        # === 策略1：拦截网络请求，直接从 API 响应捕获 PTB ===
        ptb_from_network = {"value": None}

        def _handle_response(response):
            if ptb_from_network["value"]:
                return
            try:
                resp_url = response.url
                # Polymarket 前端会请求 gamma-api 或 strapi 等接口获取 PTB
                if ("gamma-api.polymarket.com" in resp_url or
                        "strapi" in resp_url or
                        "polymarket.com/api" in resp_url):
                    body = response.text()
                    import re as _re
                    m = _re.search(r'"priceToBeat"\s*:\s*"?([\d.]+)"?', body)
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
            elapsed = time.time() - t0
            print(f"✅ PTB={ptb_from_network['value']:.2f} (network intercept, {elapsed:.1f}s)")
            return ptb_from_network["value"]

        # === 策略2：等待页面渲染出独立的 PTB 标签（非描述文字）===
        # 用 wait_for_function 精确等 span/div 里 *恰好* 是 "price to beat" 的叶子节点
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
            pass  # 超时继续提取

        # 提取PTB：找到独立标签后，在其祖先容器内搜索价格
        ptb_text = page.evaluate(r"""() => {
            // 找到精确匹配的标签（排除含子元素的容器）
            const allEls = document.querySelectorAll('span, div, p, h1, h2, h3');
            for (const el of allEls) {
                if (el.children.length > 0) continue;
                const txt = el.textContent.trim().toLowerCase();
                if (txt === 'price to beat' || txt === 'price to beat:') {
                    // 向上找包含价格的祖先容器（最多5层）
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

            // 回退：全页搜索大额美元数字（PTB 量级 $10k-$10M）
            const allLeaves = document.querySelectorAll('span, div, p');
            for (const el of allLeaves) {
                if (el.children.length > 0) continue;
                const text = el.textContent.trim();
                const m = text.match(/^\$([\d,]+(\.\d+)?)$/);
                if (m) {
                    const val = parseFloat(m[1].replace(/,/g, ''));
                    if (val > 10000 && val < 10000000) return text;
                }
            }

            // 最终回退：script 标签 JSON
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const m = s.textContent.match(/"priceToBeat"\s*:\s*"?([\d.]+)"?/);
                if (m) return '$' + m[1];
            }

            return null;
        }""")
        
        elapsed = time.time() - t0
        
        if ptb_text:
            price_str = ptb_text.replace("$", "").replace(",", "").strip()
            price = float(price_str)
            print(f"✅ PTB={price:.2f} (Playwright, {elapsed:.1f}s)")
            return price
        else:
            # 调试：输出页面中所有含 "price" 或 "$" 的文本，帮助定位实际元素
            debug_info = page.evaluate(r"""() => {
                const results = [];
                // 所有包含 "price to beat" 的元素（不限标签）
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const t = node.textContent.trim().toLowerCase();
                    if (t.includes('price to beat') || (t.startsWith('$') && /[\d,]{4,}/.test(t))) {
                        results.push(`[${node.parentElement.tagName}] "${node.textContent.trim()}"`);
                    }
                }
                return results.slice(0, 15).join(' | ') || 'NOTHING_FOUND';
            }""")
            print(f"⚠️ PTB 未找到 (Playwright, {elapsed:.1f}s)")
            print(f"   🔍 调试: {debug_info[:300]}")
            return None
            
    except Exception as e:
        print(f"❌ Playwright 错误: {e}")
        return None
    
    finally:
        # 确保浏览器关闭
        try:
            if browser:
                browser.close()
        except:
            pass
        try:
            if playwright:
                playwright.stop()
        except:
            pass


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        slug = sys.argv[1]
    else:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        base_5m = (now_ts // 300) * 300
        slug = f"btc-updown-5m-{base_5m}"
    
    print(f"测试 slug: {slug}")
    
    t0 = time.time()
    ptb = get_price_to_beat_playwright(slug)
    print(f"PTB={ptb}, 耗时={time.time()-t0:.1f}s")
