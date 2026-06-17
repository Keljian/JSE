"""Scrape Australian council and government jobs hosted on NGA.NET."""
import time
import traceback
import re
from urllib.parse import urljoin, urlparse, parse_qs
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from scraping_helpers import scraper_resource_manager, scrape_job_details
from concurrency import paused, cancel_event, OperationCancelledError

def _discover_job_list_url(driver, wait, base_url, log_callback):
    """
    Dynamically discover the correct job list URL for NGA.net sites.
    This handles cases where jobListid or other parameters change.
    """
    log = log_callback or print
    
    # Strategy 1: Try to find "Browse jobs" or similar links on the main page
    navigation_strategies = [
        {
            'name': 'Browse Jobs Links',
            'selectors': [
                'a[href*="jobs.listJobs"]',
                'a[href*="jobs.list"]', 
                'a[href*="jobListid"]',
                'a:contains("Browse jobs")',
                'a:contains("View jobs")',
                'a:contains("All vacancies")',
                'a:contains("Current vacancies")'
            ]
        },
        {
            'name': 'Menu Navigation',
            'selectors': [
                '.nav a[href*="jobs"]',
                '.menu a[href*="jobs"]',
                '.navigation a[href*="jobs"]',
                'nav a[href*="jobs"]'
            ]
        },
        {
            'name': 'Button Elements',
            'selectors': [
                'button[onclick*="jobs"]',
                'input[onclick*="jobs"]',
                '.btn[href*="jobs"]'
            ]
        }
    ]
    
    for strategy in navigation_strategies:
        log(f"Trying URL discovery strategy: {strategy['name']}")
        
        for selector in strategy['selectors']:
            try:
                # Handle :contains() pseudo-selector manually
                if ':contains(' in selector:
                    link_text = selector.split(':contains("')[1].split('")')[0]
                    elements = driver.find_elements(By.PARTIAL_LINK_TEXT, link_text)
                else:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                
                for element in elements:
                    href = element.get_attribute('href')
                    onclick = element.get_attribute('onclick')
                    
                    # Check href attribute
                    if href and ('jobs.listJobs' in href or 'jobs.list' in href or 'jobListid' in href):
                        full_url = urljoin(base_url, href)
                        log(f"Found job list URL via href: {full_url}")
                        return full_url
                    
                    # Check onclick attribute for dynamic URLs
                    if onclick and 'jobs' in onclick:
                        # Extract URL from onclick if it contains one
                        url_match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", onclick)
                        if url_match:
                            discovered_url = url_match.group(1)
                            full_url = urljoin(base_url, discovered_url)
                            log(f"Found job list URL via onclick: {full_url}")
                            return full_url
                            
            except Exception as e:
                log(f"Error with selector '{selector}': {e}")
                continue
    
    # Strategy 2: Try common NGA.net URL patterns
    common_patterns = [
        "index.cfm?event=jobs.listJobs&AudienceTypeCode=EXT",
        "index.cfm?event=jobs.listJobs",
        "index.cfm?event=jobs.list&AudienceTypeCode=EXT", 
        "index.cfm?event=jobs.list",
        "?event=jobs.listJobs&AudienceTypeCode=EXT",
        "?event=jobs.listJobs"
    ]
    
    log("Trying common NGA.net URL patterns...")
    for pattern in common_patterns:
        try:
            test_url = urljoin(base_url.split('?')[0], pattern)
            log(f"Testing pattern: {test_url}")
            
            driver.get(test_url)
            time.sleep(3)
            
            # Check if we're on an error page
            page_title = driver.title.lower()
            current_url = driver.current_url.lower()
            
            if ('error' not in page_title and 'error' not in current_url and 
                'jobs' in page_title and 'browse' in page_title):
                log(f"Successfully found working URL: {test_url}")
                return test_url
                
        except Exception as e:
            log(f"Pattern '{pattern}' failed: {e}")
            continue
    
    return None

def _extract_job_list_params(driver, log_callback):
    """
    Extract jobListid and other parameters from the current page.
    This helps build proper URLs for job detail pages.
    """
    log = log_callback or print
    params = {}
    
    try:
        # Try to find jobListid in current URL
        current_url = driver.current_url
        parsed = urlparse(current_url)
        query_params = parse_qs(parsed.query)
        
        if 'jobListid' in query_params:
            params['jobListid'] = query_params['jobListid'][0]
            log(f"Extracted jobListid: {params['jobListid']}")
        
        if 'AudienceTypeCode' in query_params:
            params['AudienceTypeCode'] = query_params['AudienceTypeCode'][0]
            log(f"Extracted AudienceTypeCode: {params['AudienceTypeCode']}")
        
        # Try to find these params in page source or hidden inputs
        hidden_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="hidden"]')
        for input_elem in hidden_inputs:
            name = input_elem.get_attribute('name')
            value = input_elem.get_attribute('value')
            if name in ['jobListid', 'AudienceTypeCode', 'CurATC', 'CurBID']:
                params[name] = value
                log(f"Found hidden param {name}: {value}")
        
        # Look for JavaScript variables
        page_source = driver.page_source
        js_matches = re.findall(r'jobListid["\']?\s*[:=]\s*["\']?([a-f0-9\-]+)', page_source, re.IGNORECASE)
        if js_matches:
            params['jobListid'] = js_matches[0]
            log(f"Found jobListid in JavaScript: {params['jobListid']}")
            
    except Exception as e:
        log(f"Error extracting parameters: {e}")
    
    return params

@scraper_resource_manager(wait_timeout=30)
def scrape_nga_net(driver, wait, keyword, status_callback, log_callback, location, max_pages, base_url, company_name, profile_id=1):
    """
    Enhanced robust scraper for NGA.net job portals with dynamic URL discovery.
    """
    log = log_callback or print
    log(f"Starting enhanced NGA.net scraper for {company_name}")
    if status_callback:
        status_callback(f"Discovering job portal for {company_name}...", True)

    try:
        # Step 1: Load the base URL
        log(f"Loading base URL: {base_url}")
        driver.get(base_url)
        time.sleep(5)
        
        # Step 2: Check if we're already on a job list page
        current_url = driver.current_url.lower()
        page_title = driver.title.lower()
        
        if 'error' in current_url or 'error' in page_title:
            log("Detected error page. Attempting URL discovery...")
            
            # Go back to the root domain and try discovery
            domain_parts = base_url.split('/cp/')
            if len(domain_parts) > 1:
                root_url = domain_parts[0] + '/cp/'
                log(f"Trying root URL: {root_url}")
                driver.get(root_url)
                time.sleep(3)
            
            # Attempt to discover the correct job list URL
            discovered_url = _discover_job_list_url(driver, wait, base_url, log_callback)
            
            if discovered_url:
                log(f"Using discovered URL: {discovered_url}")
                driver.get(discovered_url)
                time.sleep(5)
            else:
                log("❌ Could not discover valid job list URL")
                return False
        
        # Step 3: Extract parameters for building job detail URLs
        url_params = _extract_job_list_params(driver, log_callback)
        
        # Step 4: Handle iframe switching with enhanced strategies
        iframe_handled = _handle_iframe_switching(driver, wait, log_callback)
        
        # Step 5: Extract jobs with enhanced strategies
        jobs_to_process = _extract_job_listings(driver, wait, base_url, company_name, url_params, log_callback)
        
        if iframe_handled:
            try:
                driver.switch_to.default_content()
                log("Switched back to default content from iframe")
            except Exception as e:
                log(f"Error switching back to default content: {e}")
        
        if not jobs_to_process:
            log(f"❌ No jobs found for {company_name}")
            _log_debug_info(driver, log_callback)
            return False
        
        # Step 6: Process job details
        log(f"✅ Found {len(jobs_to_process)} unique jobs at {company_name}. Processing details...")
        saved_count = scrape_job_details(driver, wait, jobs_to_process, log_callback, profile_id)
        
        log(f"✅ {company_name} scrape complete. Saved {saved_count} new jobs out of {len(jobs_to_process)} found.")
        return saved_count > 0
        
    except Exception as e:
        log(f"❌ Critical error in enhanced NGA.net scraper: {e}")
        log(f"   Full traceback: {traceback.format_exc()}")
        return False

def _handle_iframe_switching(driver, wait, log_callback):
    """Enhanced iframe handling with more strategies."""
    log = log_callback or print
    iframe_handled = False
    
    iframe_strategies = [
        (By.ID, "mainFrame"),
        (By.NAME, "mainFrame"),
        (By.CSS_SELECTOR, "iframe[src*='job']"),
        (By.CSS_SELECTOR, "iframe[src*='list']"),
        (By.CSS_SELECTOR, "iframe[name*='main']"),
        (By.CSS_SELECTOR, "iframe[id*='main']"),
        (By.CSS_SELECTOR, "iframe[src*='cfm']"),
        (By.TAG_NAME, "iframe")
    ]
    
    for strategy_name, strategy_selector in iframe_strategies:
        try:
            log(f"Trying iframe strategy: {strategy_name}")
            iframe_elem = wait.until(EC.presence_of_element_located(strategy_selector))
            
            # Check if iframe has loaded content
            iframe_src = iframe_elem.get_attribute('src')
            if iframe_src and ('about:blank' not in iframe_src):
                driver.switch_to.frame(iframe_elem)
                log(f"Successfully switched to iframe using: {strategy_name}")
                iframe_handled = True
                time.sleep(3)
                break
        except TimeoutException:
            continue
        except Exception as e:
            log(f"Iframe strategy {strategy_name} error: {e}")
            continue
    
    return iframe_handled

def _extract_job_listings(driver, wait, base_url, company_name, url_params, log_callback):
    """Enhanced job extraction with better URL building."""
    log = log_callback or print
    jobs_to_process = []
    
    # Enhanced job detection strategies
    job_link_strategies = [
        {
            'name': 'NGA Standard Job Table',
            'container': '#job_list_table, #job-list-table, .job-list-table, table[id*="job"]',
            'links': 'tr.list_item a, tr a[href*="job.cfm"], tr a[href*="displayJob"], tr a[href*="job"], td a'
        },
        {
            'name': 'NGA List Format',
            'container': '.job-list, #jobs-list, .jobs-list, ul[class*="job"], ol[class*="job"]',
            'links': 'li a, .job-item a, a[href*="job"]'
        },
        {
            'name': 'NGA Grid Format',
            'container': '.job-grid, .jobs-grid, .vacancy-grid',
            'links': '.job-card a, .vacancy-card a, a[href*="job"]'
        },
        {
            'name': 'Generic NGA Job Links',
            'container': 'body',
            'links': 'a[href*="job.cfm"], a[href*="displayJob"], a[href*="jobId"], a[href*="vacancy"], a[href*="showJob"]'
        },
        {
            'name': 'Table-based with Enhanced Selectors',
            'container': 'table, .table, [role="table"]',
            'links': 'td a, tr a, .table-cell a'
        }
    ]
    
    for strategy in job_link_strategies:
        try:
            log(f"Trying job detection strategy: {strategy['name']}")
            
            container_elements = driver.find_elements(By.CSS_SELECTOR, strategy['container'])
            if not container_elements:
                log(f"No container found for strategy: {strategy['name']}")
                continue
            
            job_elements = driver.find_elements(By.CSS_SELECTOR, strategy['links'])
            
            if not job_elements:
                log(f"No job links found with strategy: {strategy['name']}")
                continue
            
            log(f"Found {len(job_elements)} potential job links with strategy: {strategy['name']}")
            
            seen_urls = set()
            for el in job_elements:
                try:
                    url = el.get_attribute('href')
                    text = el.text.strip()
                    
                    if not url or not text or url in seen_urls:
                        continue
                    
                    # Enhanced filtering
                    if _is_valid_job_link(url, text, log_callback):
                        full_url = _build_job_url(url, base_url, url_params, log_callback)
                        clean_title = ' '.join(text.replace('\n', ' ').replace('\t', ' ').split())
                        
                        jobs_to_process.append({
                            'title': clean_title,
                            'url': full_url,
                            'company': company_name,
                            'location': 'Victoria'
                        })
                        seen_urls.add(url)
                        log(f"Added job: {clean_title[:50]}...")
                        
                except Exception as e:
                    log(f"Error processing job element: {e}")
                    continue
            
            if jobs_to_process:
                log(f"Successfully found {len(jobs_to_process)} jobs with strategy: {strategy['name']}")
                break
                
        except Exception as e:
            log(f"Strategy '{strategy['name']}' failed: {e}")
            continue
    
    return jobs_to_process

def _is_valid_job_link(url, text, log_callback):
    """Enhanced validation for job links."""
    log = log_callback or print
    
    # Skip navigation and utility links
    skip_terms = ['home', 'back', 'next', 'previous', 'login', 'logout', 'help', 
                  'contact', 'about', 'search', 'filter', 'sort', 'print', 'email']
    if any(term in text.lower() for term in skip_terms):
        return False
    
    # Check text length
    if len(text) < 5 or len(text) > 300:
        return False
    
    # Must contain job-related terms in URL or text
    job_indicators = ['job', 'position', 'vacancy', 'role', 'career', 'opportunity', 'displayJob', 'showJob']
    has_job_indicator = (any(indicator in url.lower() for indicator in job_indicators) or 
                        any(indicator in text.lower() for indicator in job_indicators))
    
    return has_job_indicator

def _build_job_url(relative_url, base_url, url_params, log_callback):
    """Build proper job detail URLs with required parameters."""
    log = log_callback or print
    
    try:
        full_url = urljoin(base_url, relative_url)
        
        # Add required parameters if they're missing
        if url_params and ('jobListid' not in full_url or 'AudienceTypeCode' not in full_url):
            separator = '&' if '?' in full_url else '?'
            
            if 'jobListid' in url_params and 'jobListid' not in full_url:
                full_url += f"{separator}jobListid={url_params['jobListid']}"
                separator = '&'
            
            if 'AudienceTypeCode' in url_params and 'AudienceTypeCode' not in full_url:
                full_url += f"{separator}AudienceTypeCode={url_params['AudienceTypeCode']}"
        
        return full_url
        
    except Exception as e:
        log(f"Error building job URL: {e}")
        return urljoin(base_url, relative_url)

def _log_debug_info(driver, log_callback):
    """Enhanced debug information logging."""
    log = log_callback or print
    
    try:
        log(f"❌ Debug info for failed scrape:")
        log(f"   - Page title: {driver.title}")
        log(f"   - Current URL: {driver.current_url}")
        log(f"   - Page contains 'job': {'job' in driver.page_source.lower()}")
        log(f"   - Page contains 'vacancy': {'vacancy' in driver.page_source.lower()}")
        
        # Count different types of links
        all_links = driver.find_elements(By.TAG_NAME, "a")
        job_related_links = [link for link in all_links 
                           if link.get_attribute('href') and 'job' in link.get_attribute('href').lower()]
        
        log(f"   - Total links on page: {len(all_links)}")
        log(f"   - Job-related links: {len(job_related_links)}")
        
        # Show first few job-related links for debugging
        for i, link in enumerate(job_related_links[:3]):
            href = link.get_attribute('href')
            text = link.text.strip()[:50]
            log(f"   - Job link {i+1}: {text} -> {href}")
        
        # Show page text preview
        page_text = driver.find_element(By.TAG_NAME, "body").text[:500]
        log(f"   - Page text preview: {page_text}...")
        
    except Exception as e:
        log(f"Error in debug logging: {e}")
