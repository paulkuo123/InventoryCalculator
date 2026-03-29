"""
Playwright ↔ Selenium 兼容層
讓原有使用 Selenium API 的程式碼能透過 Playwright 執行，無需逐行修改。
"""
import time


# ═══════════════════════════════════════════════════════════════════
# By — 模擬 selenium.webdriver.common.by.By
# ═══════════════════════════════════════════════════════════════════

class By:
    ID = "id"
    CLASS_NAME = "class_name"
    CSS_SELECTOR = "css"
    XPATH = "xpath"
    TAG_NAME = "tag_name"
    NAME = "name"
    LINK_TEXT = "link_text"
    PARTIAL_LINK_TEXT = "partial_link_text"


# ═══════════════════════════════════════════════════════════════════
# NoSuchElementException
# ═══════════════════════════════════════════════════════════════════

class NoSuchElementException(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════
# Keys — 模擬 selenium.webdriver.common.keys.Keys
# ═══════════════════════════════════════════════════════════════════

class Keys:
    RETURN = "Enter"
    ENTER = "Enter"
    ESCAPE = "Escape"
    TAB = "Tab"
    BACKSPACE = "Backspace"
    DELETE = "Delete"
    SPACE = " "
    ARROW_UP = "ArrowUp"
    ARROW_DOWN = "ArrowDown"
    ARROW_LEFT = "ArrowLeft"
    ARROW_RIGHT = "ArrowRight"


# ═══════════════════════════════════════════════════════════════════
# 工具: By + value → Playwright selector
# ═══════════════════════════════════════════════════════════════════

def _by_to_selector(by, value):
    if by == "xpath":
        return f"xpath={value}"
    elif by == "css":
        return value
    elif by == "class_name":
        # 複合 class: "eds-icon.bi-date-input-icon" → ".eds-icon.bi-date-input-icon"
        if '.' in value:
            return '.' + value if not value.startswith('.') else value
        return f".{value}"
    elif by == "id":
        return f"#{value}"
    elif by == "tag_name":
        return value
    elif by == "name":
        return f"[name='{value}']"
    elif by == "link_text":
        return f"a:text-is('{value}')"
    elif by == "partial_link_text":
        return f"a:has-text('{value}')"
    return value


# ═══════════════════════════════════════════════════════════════════
# PlaywrightElement — 包裝 Playwright ElementHandle
# ═══════════════════════════════════════════════════════════════════

class PlaywrightElement:
    def __init__(self, element_handle, page):
        self._el = element_handle
        self._page = page

    def find_element(self, by, value):
        selector = _by_to_selector(by, value)
        child = self._el.query_selector(selector)
        if child is None:
            raise NoSuchElementException(f"找不到元素: {by}={value}")
        return PlaywrightElement(child, self._page)

    def find_elements(self, by, value):
        selector = _by_to_selector(by, value)
        return [PlaywrightElement(c, self._page)
                for c in self._el.query_selector_all(selector)]

    def click(self):
        try:
            self._el.click(timeout=5000)
        except Exception:
            self._el.evaluate("el => el.click()")

    def clear(self):
        self._el.fill("")

    def send_keys(self, text):
        if text in ("Enter", "Escape", "Tab", "Backspace", "Delete"):
            self._page.keyboard.press(text)
        else:
            self._el.type(text, delay=30)

    def get_attribute(self, name):
        if name == "class":
            # Playwright 對 class 回傳 className (跟 Selenium 一樣)
            return self._el.get_attribute("class") or ""
        return self._el.get_attribute(name)

    @property
    def text(self):
        return self._el.inner_text()

    def is_displayed(self):
        return self._el.is_visible()

    def is_visible(self):
        return self._el.is_visible()

    def __eq__(self, other):
        if isinstance(other, PlaywrightElement):
            # 透過 JS 比較兩個 element handle 是否為同一個 DOM 節點
            try:
                return self._page.evaluate(
                    "([a, b]) => a === b",
                    [self._el, other._el])
            except Exception:
                return False
        return False

    def __hash__(self):
        return id(self._el)


# ═══════════════════════════════════════════════════════════════════
# PlaywrightDriver — 包裝 Playwright Page 為 WebDriver-like 介面
# ═══════════════════════════════════════════════════════════════════

class PlaywrightDriver:
    def __init__(self, page):
        self._page = page

    # ── Navigation ──
    def get(self, url):
        self._page.goto(url, wait_until="domcontentloaded")

    @property
    def current_url(self):
        return self._page.url

    # ── Element lookup ──
    def find_element(self, by, value):
        selector = _by_to_selector(by, value)
        el = self._page.query_selector(selector)
        if el is None:
            raise NoSuchElementException(f"找不到元素: {by}={value}")
        return PlaywrightElement(el, self._page)

    def find_elements(self, by, value):
        selector = _by_to_selector(by, value)
        return [PlaywrightElement(e, self._page)
                for e in self._page.query_selector_all(selector)]

    # ── JavaScript ──
    def execute_script(self, script, *args):
        pw_args = []
        for a in args:
            pw_args.append(a._el if isinstance(a, PlaywrightElement) else a)

        if not pw_args:
            return self._page.evaluate(script)

        if len(pw_args) == 1:
            adapted = script.replace("arguments[0]", "____a0____")
            # 判斷是否為 expression（無 { } 包裹且無 return）
            stripped = adapted.strip().rstrip(";").strip()
            if "return " in adapted or "{" in adapted:
                fn = f"(____a0____) => {{ {adapted} }}"
            else:
                fn = f"(____a0____) => {{ return {adapted}; }}"
            return self._page.evaluate(fn, pw_args[0])

        # 多參數
        pnames = [f"____a{i}____" for i in range(len(pw_args))]
        adapted = script
        for i, pn in enumerate(pnames):
            adapted = adapted.replace(f"arguments[{i}]", pn)

        plist = ", ".join(pnames)
        stripped = adapted.strip().rstrip(";").strip()
        if "return " in adapted or "{" in adapted:
            fn = f"([{plist}]) => {{ {adapted} }}"
        else:
            fn = f"([{plist}]) => {{ return {adapted}; }}"
        return self._page.evaluate(fn, pw_args)

    # ── Cookie ──
    def add_cookie(self, cookie_dict):
        self._page.context.add_cookies([cookie_dict])

    # ── Cleanup (no-op, 由 ShopeeCrawler.cleanup 處理) ──
    def quit(self):
        pass


# ═══════════════════════════════════════════════════════════════════
# WebDriverWait — 模擬 selenium.webdriver.support.ui.WebDriverWait
# ═══════════════════════════════════════════════════════════════════

class WebDriverWait:
    def __init__(self, driver, timeout):
        self._driver = driver
        self._timeout = timeout

    def until(self, condition):
        end = time.time() + self._timeout
        while time.time() < end:
            try:
                result = condition(self._driver)
                if result:
                    return result
            except Exception:
                pass
            time.sleep(0.5)
        raise Exception(f"WebDriverWait 超時 ({self._timeout}s)")


# ═══════════════════════════════════════════════════════════════════
# EC — 模擬 selenium expected_conditions
# ═══════════════════════════════════════════════════════════════════

class EC:
    @staticmethod
    def presence_of_element_located(locator):
        by, value = locator
        def _check(driver):
            try:
                return driver.find_element(by, value)
            except Exception:
                return None
        return _check

    @staticmethod
    def visibility_of(element):
        def _check(driver):
            try:
                if element.is_displayed():
                    return element
            except Exception:
                pass
            return None
        return _check

    @staticmethod
    def visibility_of_all_elements_located(locator):
        by, value = locator
        def _check(driver):
            try:
                elements = driver.find_elements(by, value)
                if elements and all(e.is_displayed() for e in elements):
                    return elements
            except Exception:
                pass
            return None
        return _check

    @staticmethod
    def element_to_be_clickable(element_or_locator):
        if isinstance(element_or_locator, tuple):
            by, value = element_or_locator
            def _check(driver):
                try:
                    el = driver.find_element(by, value)
                    if el.is_displayed():
                        return el
                except Exception:
                    pass
                return None
            return _check
        else:
            def _check(driver):
                try:
                    if element_or_locator.is_displayed():
                        return element_or_locator
                except Exception:
                    pass
                return None
            return _check

    @staticmethod
    def url_contains(url_fragment):
        def _check(driver):
            try:
                if url_fragment in driver.current_url:
                    return True
            except Exception:
                pass
            return None
        return _check


# ═══════════════════════════════════════════════════════════════════
# ActionChains — 模擬 selenium ActionChains
# ═══════════════════════════════════════════════════════════════════

class ActionChains:
    def __init__(self, driver):
        self._driver = driver
        self._page = driver._page if isinstance(driver, PlaywrightDriver) else driver
        self._keys = None
        self._element = None

    def send_keys(self, keys):
        self._keys = keys
        return self

    def move_to_element(self, element):
        self._element = element
        return self

    def click(self):
        if self._element:
            self._element.click()
        return self

    def perform(self):
        if self._keys:
            page = self._page if hasattr(self._page, 'keyboard') else self._page._page
            page.keyboard.press(self._keys)
            self._keys = None
