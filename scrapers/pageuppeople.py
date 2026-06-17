"""Scrape PageUp-powered job boards and extract detail-page content."""
import time
from urllib.parse import urljoin
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from scraping_helpers import scraper_resource_manager, scrape_job_details
from concurrency import paused, cancel_event, OperationCancelledError

@scraper_resource_manager(wait_timeout=20)
def scrape_pageuppeople(driver, wait, keyword, status_callback, log_callback, location, max_pages, base_url, company_name, profile_id=1):
    """Generic scraper for PageUpPeople job portals."""
    log = log_callback or print
    log(f"Requesting PageUpPeople URL: {base_url}")
    if status_callback: status_callback(f"Scraping {company_name}...", True)

    driver.get(base_url)
    time.sleep(5)
    
    jobs_to_process = []
    
    try:
        job_link_patterns = [
            'a[href*="/job/"]',
            'a[href*="/listing/"]',
            'a[href*="job-id"]',
            'a[href*="vacancy"]',
            '.job-title a',
            '.vacancy-title a',
            'h3 a',
            'h4 a'
        ]
        
        log(f"Searching for job links using multiple patterns...")
        found_links = []
        
        for pattern in job_link_patterns:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, pattern)
                if elements:
                    log(f"Found {len(elements)} potential job links using pattern: '{pattern}'")
                    
                    for elem in elements:
                        try:
                            href = elem.get_attribute('href')
                            title = elem.text.strip()
                            
                            if not title or len(title) < 5 or title.lower() in ['more', 'view', 'apply', 'details', 'read more', 'download', 'print']:
                                continue
                            
                            if href and ('/job/' in href or '/listing/' in href or 'job-id' in href or 'vacancy' in href):
                                title = ' '.join(title.replace('\n', ' ').replace('\t', ' ').split())
                                full_url = urljoin(base_url, href)
                                
                                found_links.append({
                                    'title': title, 
                                    'url': full_url, 
                                    'company': company_name, 
                                    'location': 'Victoria'
                                })
                                
                        except Exception as e:
                            log(f"Error processing link element: {e}")
                            continue
                    
                    if found_links:
                        break
                        
            except Exception as e:
                log(f"Pattern '{pattern}' failed: {e}")
                continue
        
        if not found_links:
            log("No direct job links found. Trying table/list structures...")
            
            container_selectors = ["#search-results-table", ".search-results", ".job-list", ".vacancy-list", ".results-table", "table", ".listing-table"]
            
            for container_selector in container_selectors:
                try:
                    container = driver.find_element(By.CSS_SELECTOR, container_selector)
                    log(f"Found container using selector: '{container_selector}'")
                    
                    row_selectors = ["tr.data-row", "tr:not(:first-child)", "tr", "li", ".job-item", ".vacancy-item"]
                    
                    for row_selector in row_selectors:
                        try:
                            rows = container.find_elements(By.CSS_SELECTOR, row_selector)
                            if not rows: continue
                                
                            log(f"Found {len(rows)} rows using selector: '{row_selector}'")
                            
                            for row in rows:
                                try:
                                    link_el = row.find_element(By.TAG_NAME, "a")
                                    title = link_el.text.strip()
                                    url = link_el.get_attribute('href')
                                    
                                    if not title or not url or len(title) < 5: continue
                                    
                                    job_location = "Victoria"
                                    try:
                                        loc_selectors = [".job-location", ".location", "td:nth-child(3)", "td:last-child"]
                                        for loc_selector in loc_selectors:
                                            try:
                                                loc_el = row.find_element(By.CSS_SELECTOR, loc_selector)
                                                loc_text = loc_el.text.strip()
                                                if loc_text and len(loc_text) < 100:
                                                    job_location = loc_text
                                                    break
                                            except NoSuchElementException:
                                                continue
                                    except: pass
                                    
                                    title = ' '.join(title.replace('\n', ' ').replace('\t', ' ').split())
                                    full_url = urljoin(base_url, url)
                                    
                                    found_links.append({
                                        'title': title, 'url': full_url, 'company': company_name, 'location': job_location
                                    })
                                except NoSuchElementException: continue
                                except Exception as e:
                                    log(f"Error processing row: {e}")
                                    continue
                            
                            if found_links: break
                        except NoSuchElementException: continue
                        except Exception as e:
                            log(f"Row selector '{row_selector}' failed: {e}")
                            continue
                    
                    if found_links: break
                except NoSuchElementException: continue
                except Exception as e:
                    log(f"Container selector '{container_selector}' failed: {e}")
                    continue
        
        if not found_links:
            log("No structured job listings found. Scanning entire page for job-like links...")
            
            all_links = driver.find_elements(By.TAG_NAME, "a")
            log(f"Found {len(all_links)} total links on page. Filtering...")
            
            for link in all_links:
                try:
                    href = link.get_attribute('href')
                    text = link.text.strip()
                    
                    if not href or not text or len(text) < 10: continue
                    
                    job_indicators = ['/job/', '/listing/', '/vacancy/', 'job-id', 'position']
                    navigation_words = ['home', 'about', 'contact', 'login', 'search', 'filter', 'more', 'view all', 'apply now', 'download', 'print']
                    
                    if any(indicator in href.lower() for indicator in job_indicators):
                        if not any(nav_word in text.lower() for nav_word in navigation_words):
                            text = ' '.join(text.replace('\n', ' ').replace('\t', ' ').split())
                            full_url = urljoin(base_url, href)
                            
                            found_links.append({'title': text, 'url': full_url, 'company': company_name, 'location': 'Victoria'})
                except: continue
            
            if len(found_links) > 50:
                log(f"Found {len(found_links)} potential jobs. Limiting to first 50.")
                found_links = found_links[:50]
        
        jobs_to_process = found_links

    except Exception as e:
        log(f"Error accessing {company_name} page: {e}")
        return False

    if not jobs_to_process:
        log(f"No jobs found for {company_name}. The page structure may have changed or there are no open positions.")
        log(f"Page title: {driver.title}")
        log(f"Current URL: {driver.current_url}")
        
        page_source = driver.page_source[:1000]
        log(f"Page source preview: {page_source}...")
        return False

    seen_urls = set()
    unique_jobs = []
    for job in jobs_to_process:
        if job['url'] not in seen_urls:
            seen_urls.add(job['url'])
            unique_jobs.append(job)
    
    jobs_to_process = unique_jobs
    log(f"Found {len(jobs_to_process)} unique jobs at {company_name}. Processing details...")
    
    saved_count = scrape_job_details(driver, wait, jobs_to_process, log_callback, profile_id)
    log(f"{company_name} scrape complete. Saved {saved_count} new jobs out of {len(jobs_to_process)} found.")
    return saved_count > 0
