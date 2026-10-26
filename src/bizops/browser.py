"""A real browser for agents: sign up for services, check out with an approved card, run web dashboards.

* Headless Chromium via Playwright (optional dependency: `pip install -e '.[browser]'`; the Docker image
  has it). Each business gets its own persistent profile under data/browser/<org>, so logins and
  cookies survive between runs and businesses never share sessions.
* Each agent drives its own tab. Pages are returned as text: the URL, title, readable text and a
  numbered list of the things you can click or type into (refs like e12, or f1e3 inside an iframe,
  which is how card fields on checkout pages appear).
* SSRF guard: every page navigation is checked like fetch_url (no private or local addresses).
* Secrets: tools resolve {{card:...}} and {{login:...}} placeholders right before typing; the values
  are redacted from everything returned, so the model never sees a card number or a password.
* Everything a page says is untrusted data, wrapped like web fetches.
* audit(): an accessibility (axe-core, WCAG 2.x rules) and speed scan of any public page, so no
  scanner API key is ever needed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import PolicyRefusal
from .webclient import check_url, wrap_untrusted

SNAPSHOT_JS = r"""
(prefix) => {
  document.querySelectorAll('[data-bz-ref]').forEach(e => e.removeAttribute('data-bz-ref'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],' +
              '[role=radio],[role=tab],[role=menuitem],[role=option],[role=combobox],[role=switch],[contenteditable=true]';
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const label = el => {
    const bits = [el.getAttribute('aria-label'), el.labels && el.labels[0] && el.labels[0].innerText,
                  el.getAttribute('placeholder'), el.getAttribute('title'), el.getAttribute('alt')];
    if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) bits.unshift(el.innerText);
    bits.push(el.getAttribute('name'), el.getAttribute('autocomplete'));
    for (const b of bits) { const c = clean(b); if (c) return c.slice(0, 80); }
    return '';
  };
  const items = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (items.length >= 250) break;
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    if (!visible(el)) continue;
    const ref = prefix + 'e' + (i++);
    el.setAttribute('data-bz-ref', ref);
    const d = {ref, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || el.getAttribute('role') || '',
               label: label(el)};
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      if (el.type === 'checkbox' || el.type === 'radio') d.checked = el.checked;
      else d.value = el.type === 'password' ? (el.value ? '(filled)' : '') : clean(el.value).slice(0, 60);
    }
    if (el.tagName === 'SELECT') {
      d.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
      d.options = Array.from(el.options).slice(0, 20).map(o => clean(o.text));
    }
    if (el.tagName === 'A') d.href = (el.getAttribute('href') || '').slice(0, 120);
    if (el.disabled) d.disabled = true;
    items.push(d);
  }
  return {title: document.title, text: clean(document.body ? document.body.innerText : '').slice(0, 8000), items};
}
"""


AXE_VERSION = "4.10.2"
AXE_CDN = f"https://cdn.jsdelivr.net/npm/axe-core@{AXE_VERSION}/axe.min.js"

PERF_JS = r"""
async () => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const res = performance.getEntriesByType('resource');
  const lcp = await new Promise(resolve => {
    let last = null;
    try {
      new PerformanceObserver(l => { const e = l.getEntries(); if (e.length) last = e[e.length - 1].startTime; })
        .observe({type: 'largest-contentful-paint', buffered: true});
    } catch (e) {}
    setTimeout(() => resolve(last), 300);
  });
  const bytes = res.reduce((s, r) => s + (r.transferSize || 0), nav.transferSize || 0);
  const imgs = Array.from(document.images);
  return {
    ttfb_ms: Math.round(nav.responseStart || 0), dom_content_loaded_ms: Math.round(nav.domContentLoadedEventEnd || 0),
    load_ms: Math.round(nav.loadEventEnd || 0), lcp_ms: lcp === null ? null : Math.round(lcp),
    requests: res.length + 1, transfer_kb: Math.round(bytes / 1024),
    images: imgs.length, images_without_alt: imgs.filter(i => !i.hasAttribute('alt')).length,
    html_lang: document.documentElement.getAttribute('lang') || null,
    has_viewport_meta: !!document.querySelector('meta[name=viewport]'),
    title: document.title
  };
}
"""


def _fmt_item(d: dict) -> str:
    kind = d["tag"] + (f"[{d['type']}]" if d.get("type") else "")
    s = f"[{d['ref']}] {kind} \"{d.get('label', '')}\""
    if "value" in d:
        s += f" value=\"{d['value']}\""
    if "checked" in d:
        s += " checked" if d["checked"] else " unchecked"
    if d.get("options"):
        s += " options=" + " | ".join(d["options"])
    if d.get("href"):
        s += f" -> {d['href']}"
    if d.get("disabled"):
        s += " (disabled)"
    return s


class Browser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Any = None
        self._contexts: dict[str, Any] = {}           # org_id -> BrowserContext
        self._ctx_used: dict[str, float] = {}         # org_id -> last use
        self._pages: dict[str, Any] = {}              # agent_id -> Page
        self._frames: dict[str, dict[str, Any]] = {}  # agent_id -> ref prefix -> Frame
        self._secrets: dict[str, set[str]] = {}       # agent_id -> values to redact
        self._used: dict[str, float] = {}             # agent_id -> last use
        self._lock = asyncio.Lock()

    @property
    def cfg(self) -> dict[str, Any]:
        return self.settings.policy.get("browser", {})

    @staticmethod
    def installed() -> bool:
        try:
            import playwright.async_api  # noqa: F401
            return True
        except ImportError:
            return False

    async def _context(self, org_id: str) -> Any:
        if org_id in self._contexts:
            self._ctx_used[org_id] = time.monotonic()
            return self._contexts[org_id]
        # Each business's profile is a separate Chromium process (150-300 MB). Keep only the most recently used
        # few open; the profile on disk keeps logins, so a closed one reopens with nothing lost.
        while len(self._contexts) >= int(self.cfg.get("max_open_profiles", 3)):
            oldest = min(self._contexts, key=lambda o: self._ctx_used.get(o, 0.0))
            await self._close_context(oldest)
        if not self.installed():
            raise PolicyRefusal("browser.unavailable", "the browser isn't installed on this server "
                                                       "(pip install -e '.[browser]' and playwright's Chromium)")
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        profile = Path(self.settings.data_dir) / "browser" / org_id
        profile.mkdir(parents=True, exist_ok=True)
        kw: dict[str, Any] = {"headless": True, "viewport": {"width": 1280, "height": 900},
                              "args": ["--disable-dev-shm-usage", "--no-sandbox"], "locale": "en-US"}
        exe = os.environ.get("BIZOPS_CHROMIUM")
        if exe:
            kw["executable_path"] = exe
        ctx = await self._pw.chromium.launch_persistent_context(str(profile), **kw)
        ctx.set_default_timeout(float(self.cfg.get("action_timeout_s", 15)) * 1000)
        blocked = list(self.settings.policy.get("web", {}).get("blocked_domains", []))
        allow_private = bool(self.cfg.get("allow_private_hosts", False))

        async def guard(route: Any) -> None:
            req = route.request
            if req.is_navigation_request() and not allow_private:
                try:
                    await check_url(req.url, blocked)
                except PolicyRefusal:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await ctx.route("**/*", guard)
        self._contexts[org_id] = ctx
        self._ctx_used[org_id] = time.monotonic()
        return ctx

    async def _close_context(self, org_id: str) -> None:
        ctx = self._contexts.pop(org_id, None)
        self._ctx_used.pop(org_id, None)
        for aid, page in list(self._pages.items()):
            try:
                if page.context is ctx:
                    self._pages.pop(aid, None)
                    self._used.pop(aid, None)
                    self._frames.pop(aid, None)
            except Exception:
                self._pages.pop(aid, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

    async def _page(self, agent: dict) -> Any:
        aid = agent["id"]
        page = self._pages.get(aid)
        if page is not None and not page.is_closed():
            self._used[aid] = time.monotonic()
            return page
        ctx = await self._context(agent["org_id"])
        page = await ctx.new_page()
        self._pages[aid] = page
        self._used[aid] = time.monotonic()
        return page

    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await page.wait_for_load_state("networkidle", timeout=4_000)
        except Exception:
            pass

    def _redact(self, agent_id: str, text: str) -> str:
        for s in sorted(self._secrets.get(agent_id, ()), key=len, reverse=True):
            if len(s) >= 3:
                text = text.replace(s, "•••")
        return text

    async def snapshot(self, agent: dict) -> str:
        page = self._pages.get(agent["id"])
        if page is None or page.is_closed():
            return "No page open. Use browser_open first."
        await self._settle(page)
        frames = {"": page.main_frame}
        main = await page.main_frame.evaluate(SNAPSHOT_JS, "")
        lines = [f"URL: {page.url}", f"Title: {main['title']}", "", "--- page text ---", main["text"] or "(empty)",
                 "", "--- elements (use the ref) ---"]
        lines += [_fmt_item(d) for d in main["items"]]
        n = 0
        for fr in page.frames:
            if fr is page.main_frame or fr.is_detached():
                continue
            try:
                sub = await fr.evaluate(SNAPSHOT_JS, f"f{n + 1}")
            except Exception:
                continue
            if not sub["items"]:
                continue
            n += 1
            frames[f"f{n}"] = fr
            lines.append(f"--- inside frame f{n} ({(fr.url or '')[:80]}) ---")
            lines += [_fmt_item(d) for d in sub["items"]]
            if n >= 8:
                break
        self._frames[agent["id"]] = frames
        body = "\n".join(lines)[: int(self.cfg.get("max_chars_returned", 20000))]
        return wrap_untrusted(page.url, self._redact(agent["id"], body))

    def _locate(self, agent: dict, ref: str) -> Any:
        frames = self._frames.get(agent["id"])
        if not frames:
            raise PolicyRefusal("browser.no_snapshot", "take a browser_snapshot first; refs come from it")
        prefix = ref.split("e", 1)[0] if ref.startswith("f") else ""
        frame = frames.get(prefix)
        if frame is None or frame.is_detached():
            raise PolicyRefusal("browser.stale_ref", f"ref {ref} is stale; take a new browser_snapshot")
        return frame.locator(f'[data-bz-ref="{ref}"]').first

    async def open(self, agent: dict, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not self.cfg.get("allow_private_hosts", False):
            await check_url(url, list(self.settings.policy.get("web", {}).get("blocked_domains", [])))
        async with self._lock:
            page = await self._page(agent)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            if "blockedbyclient" in str(e).lower():
                raise PolicyRefusal("web.ssrf", "that address is not a public web page")
            raise
        return await self.snapshot(agent)

    async def click(self, agent: dict, ref: str) -> str:
        await self._locate(agent, ref).click()
        return await self.snapshot(agent)

    async def type(self, agent: dict, ref: str, text: str, *, secrets: list[str] | None = None,
                   submit: bool = False, slowly: bool = False) -> str:
        if secrets:
            self._secrets.setdefault(agent["id"], set()).update(s for s in secrets if s)
        loc = self._locate(agent, ref)
        if slowly:
            await loc.click()
            await loc.fill("")
            await loc.press_sequentially(text, delay=25)
        else:
            await loc.fill(text)
        if submit:
            await loc.press("Enter")
        return await self.snapshot(agent)

    async def select(self, agent: dict, ref: str, option: str) -> str:
        loc = self._locate(agent, ref)
        try:
            await loc.select_option(label=option)
        except Exception:
            await loc.select_option(value=option)
        return await self.snapshot(agent)

    async def press(self, agent: dict, key: str) -> str:
        page = self._pages.get(agent["id"])
        if page is None or page.is_closed():
            return "No page open. Use browser_open first."
        await page.keyboard.press(key)
        return await self.snapshot(agent)

    async def _axe_source(self) -> str:
        if getattr(self, "_axe", None):
            return self._axe
        candidates = [os.environ.get("BIZOPS_AXE_JS") or ""]
        npm = shutil.which("npm")
        if npm:
            try:
                root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=20).stdout.strip()
                candidates.append(os.path.join(root, "axe-core", "axe.min.js"))
            except Exception:
                pass
        cache = Path(self.settings.data_dir) / "cache" / f"axe-{AXE_VERSION}.min.js"
        candidates.append(str(cache))
        for c in candidates:
            if c and os.path.isfile(c):
                self._axe = Path(c).read_text()
                return self._axe
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(AXE_CDN)
            r.raise_for_status()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(r.text)
        self._axe = r.text
        return self._axe

    async def audit(self, agent: dict, url: str, *, mobile: bool = False) -> dict[str, Any]:
        """Accessibility (axe-core) + speed scan of one page, in a fresh tab of the business's browser."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not self.cfg.get("allow_private_hosts", False):
            await check_url(url, list(self.settings.policy.get("web", {}).get("blocked_domains", [])))
        ctx = await self._context(agent["org_id"])
        page = await ctx.new_page()
        try:
            if mobile:
                await page.set_viewport_size({"width": 390, "height": 844})
            resp = await page.goto(url, wait_until="load", timeout=45_000)
            await self._settle(page)
            perf = await page.evaluate(PERF_JS)
            await page.evaluate(await self._axe_source())
            axe = await page.evaluate("""async () => {
              const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa','best-practice']},
                                                resultTypes: ['violations','incomplete']});
              const pick = v => ({id: v.id, impact: v.impact, help: v.help, help_url: v.helpUrl,
                                  wcag: v.tags.filter(t => /^wcag\\d/.test(t)), count: v.nodes.length,
                                  examples: v.nodes.slice(0, 3).map(n => ({target: n.target.join(' '), html: n.html.slice(0, 160)}))});
              return {violations: r.violations.map(pick), needs_review: r.incomplete.map(pick).slice(0, 15),
                      passes: r.passes.length, engine: 'axe-core ' + r.testEngine.version};
            }""")
        finally:
            await page.close()
        order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
        axe["violations"].sort(key=lambda v: (order.get(v["impact"] or "minor", 4), -v["count"]))
        return {"url": url, "status": resp.status if resp else None, "viewport": "mobile 390x844" if mobile else "desktop 1280x900",
                "accessibility": {**axe, "violation_count": len(axe["violations"]),
                                  "by_impact": {k: sum(1 for v in axe["violations"] if v["impact"] == k) for k in order}},
                "performance": perf}

    async def close_idle(self, max_idle_s: float = 1800) -> int:
        """Close tabs nobody used for a while, then whole browsers (a Chromium process per business) with no tab
        left. Logins stay in the profile on disk."""
        n = 0
        for aid, last in list(self._used.items()):
            if time.monotonic() - last > max_idle_s:
                page = self._pages.pop(aid, None)
                self._used.pop(aid, None)
                self._frames.pop(aid, None)
                self._secrets.pop(aid, None)
                if page is not None and not page.is_closed():
                    await page.close()
                    n += 1
        for org_id in list(self._contexts):
            if time.monotonic() - self._ctx_used.get(org_id, 0.0) > max_idle_s and not any(
                    getattr(pg, "context", None) is self._contexts[org_id] for pg in self._pages.values()):
                await self._close_context(org_id)
                n += 1
        return n

    async def aclose(self) -> None:
        for ctx in self._contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        self._pages.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
