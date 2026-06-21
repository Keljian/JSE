"""Common Selenium, HTTP, and PDF helpers shared by scraper implementations."""
import functools
import io
import requests
import pdfplumber
import time
from urllib.parse import urljoin

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

import database_manager as db
from concurrency import paused, cancel_event, OperationCancelledError

# --- Helper Functions ---
def _get_pdf_text_from_url(pdf_url, base_url, log_callback):
    """Downloads a PDF from a URL and extracts its text."""
    log_callback = log_callback or print
    if not pdf_url:
        return None
    try:
        # Resolve relative URLs
        full_url = urljoin(base_url, pdf_url)
        log_callback(f"   -> Found PDF: {full_url}. Downloading...")
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(full_url, timeout=20, headers=headers, allow_redirects=True)
        response.raise_for_status()

        if 'application/pdf' not in response.headers.get('Content-Type', '').lower():
            log_callback(f"   -> Warning: URL did not return a PDF content-type. URL: {full_url}")
            return None

        pdf_file = io.BytesIO(response.content)
        full_text = ""
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"
        log_callback(f"   -> Successfully extracted {len(full_text)} chars from PDF.")
        return full_text
    except requests.exceptions.RequestException as e:
        log_callback(f"   -> Error downloading PDF from {full_url}: {e}")
        return None
    except Exception as e:
        log_callback(f"   -> Error parsing PDF file from {full_url}: {e}")
        return None

def scraper_resource_manager(wait_timeout=10):
    """
    A decorator to handle the setup and teardown of WebDriver.
    It now accepts and passes through arbitrary keyword arguments and applies
    centralized browser stealth configurations.
    """
    def decorator(scraper_func):
        @functools.wraps(scraper_func)
        def wrapper(keyword, status_callback=None, log_callback=None, location=None, max_pages=30, **kwargs):
            options = Options()
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            options.add_argument("--headless=new")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            
            driver = None
            log = log_callback or print
            try:
                driver = webdriver.Chrome(options=options)
                
                # Centralized browser stealth configurations
                driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                driver.execute_script("Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]})")
                driver.execute_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']})")
                driver.execute_script("Object.defineProperty(navigator, 'platform', {get: () => 'Win32'})")

                wait = WebDriverWait(driver, wait_timeout)
                # Call the actual scraper function, passing through any extra keyword arguments
                return scraper_func(driver, wait, keyword, status_callback, log_callback, location, max_pages, **kwargs)
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
        return wrapper
    return decorator

def scrape_job_details(driver, wait, job_list, log_callback, profile_id=1):
    """
    Optimized helper to iterate job list, scrape details, and save to DB.
    Relies on explicit waits instead of fixed delays for better performance.
    """
    saved_count = 0
    processed_urls = set()
    log = log_callback or print

    for i, job_info in enumerate(job_list):
        if cancel_event.is_set(): 
            raise OperationCancelledError("Scraping cancelled.")
        paused.wait()
        
        job_url = job_info['url']
        if not job_url or job_url in processed_urls:
            continue
        processed_urls.add(job_url)

        log(f"({i+1}/{len(job_list)}) Processing: {job_info['title']}")
        
        driver.execute_script("window.open('');")
        driver.switch_to.window(driver.window_handles[1])
        
        description, pdf_text_content = "Description not found.", ""
        retry_count = 0
        max_retries = 2

        while retry_count <= max_retries:
            try:
                driver.get(job_url)
                description_found = False
                
                description_selectors = [
                    '.job-details-content', '.job-description', '.job-details', 
                    '.job-content', '.description', '.content', 'article',
                    '#job-description', '#job-details', '#description',
                    '.vacancy-details', '.position-details', '.role-description',
                    '.main-content', '#main-content', '.page-content'
                ]
                
                for desc_selector in description_selectors:
                    try:
                        desc_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, desc_selector)))
                        candidate_desc = desc_element.text.strip()
                        
                        if candidate_desc and len(candidate_desc) > 100:
                            description = candidate_desc
                            description_found = True
                            log(f"   ✓ Found description using: {desc_selector} ({len(description)} chars)")
                            break
                    except TimeoutException: 
                        continue
                    except Exception as e:
                        log(f"   Description selector '{desc_selector}' failed: {e}")
                        continue
                
                if not description_found:
                    try:
                        body_text = driver.find_element(By.TAG_NAME, "body").text
                        lines = body_text.split('\n')
                        filtered_lines = [line.strip() for line in lines if len(line.strip()) > 20]
                        if filtered_lines:
                            description = '\n'.join(filtered_lines)
                            log(f"   ✓ Used filtered body text ({len(description)} chars)")
                        else:
                            description = body_text
                            log(f"   ⚠ Used raw body text ({len(description)} chars)")
                    except Exception as e:
                        log(f"   ⚠ Could not extract body text: {e}")
                
                pdf_link_selectors = 'a[href$=".pdf"], a[href*=".pdf"], a[download*=".pdf"]'
                pdf_links_found = driver.find_elements(By.CSS_SELECTOR, pdf_link_selectors)
                
                if pdf_links_found:
                    unique_pdf_urls = {link.get_attribute('href') for link in pdf_links_found if link.get_attribute('href')}
                    log(f"   📄 Found {len(unique_pdf_urls)} unique PDF links")
                    for pdf_url in unique_pdf_urls:
                        extracted_text = _get_pdf_text_from_url(pdf_url, driver.current_url, log_callback)
                        if extracted_text:
                            pdf_text_content += extracted_text + "\n\n"
                            log(f"   ✓ Extracted PDF content ({len(extracted_text)} chars)")
                
                break
                
            except Exception as e:
                retry_count += 1
                log(f"   ⚠ Attempt {retry_count} failed for '{job_info['title']}': {e}")
                if retry_count <= max_retries:
                    log(f"   🔄 Retrying...")
                else:
                    log(f"   ❌ Max retries reached for '{job_info['title']}'")
                    description = f"Job description could not be loaded after {max_retries + 1} attempts for: {job_info['title']}"
        
        try:
            handles = driver.window_handles
            if len(handles) > 1:
                driver.close()
                driver.switch_to.window(handles[0])
            elif handles:
                driver.switch_to.window(handles[0])
        except Exception as e:
            log(f"   ⚠ Error closing tab: {e}")
            log(f"   Browser session/tab cleanup failed; continuing safely: {e}")
            return saved_count

        job_details = {
            'title': job_info['title'], 
            'company': job_info['company'], 
            'location': job_info.get('location', "Victoria"),
            'url': job_url, 
            'description': description, 
            'pdf_text': pdf_text_content.strip() if pdf_text_content else "",
            # Keep the legacy analysis field and also expose grabbed PDF text
            # where the application workspace expects a position description.
            'position_description_text': pdf_text_content.strip() if pdf_text_content else ""
        }

        if description and len(description.strip()) >= 50:
            if db.add_job(job_details, job_info['company'], profile_id, log_callback):
                log(f"   ✅ Saved '{job_info['title']}' to database.")
                saved_count += 1
            else:
                log(f"   • Duplicate skipped: {job_info['title']}")
        else:
            log(f"   ⚠ Skipped '{job_info['title']}' - insufficient description content")

    return saved_count
