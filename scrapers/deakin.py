"""Scrape Deakin University job listings and detail pages."""
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import database_manager as db
from scraping_helpers import scraper_resource_manager, _get_pdf_text_from_url
from concurrency import paused, cancel_event, OperationCancelledError

@scraper_resource_manager(wait_timeout=20)
def scrape_deakin_all_jobs(driver, wait, keyword, status_callback=None, log_callback=None, location=None, max_pages=None, profile_id=1, **kwargs):
    """
    Optimized scraper for Deakin University careers page. 'keyword' is unused but kept for decorator compatibility.
    """
    log = log_callback or print
    
    url = "https://careers.deakin.edu.au/en/listing/"
    log(f"Requesting URL: {url}")
    if status_callback: 
        status_callback("Scraping Deakin University...", True)
    
    if cancel_event.is_set(): 
        raise OperationCancelledError("Scraping cancelled by user.")
    
    driver.get(url)
    
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except TimeoutException:
        log("Page failed to load within timeout")
        return False
    
    job_links = []
    selectors_to_try = [
        'a[href*="/en/job/"]', 'a[href*="/job/"]', '.job-title a', 'h3 a', 'tr a'
    ]
    
    for selector in selectors_to_try:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                job_links = [elem for elem in elements if elem.get_attribute("href") and ("/job/" in elem.get_attribute("href"))]
                if job_links:
                    log(f"Found {len(job_links)} potential job links using selector: '{selector}'")
                    break
        except Exception as e:
            log(f"Selector '{selector}' failed: {e}")
            continue
    
    if not job_links:
        log("Could not find any job links on Deakin careers page. The page structure may have changed.")
        return False

    log(f"Found {len(job_links)} total jobs at Deakin University. Processing all jobs...")
    
    job_data = []
    for link in job_links:
        try:
            title = link.text.strip()
            url = link.get_attribute("href")
            if title and url and len(title) >= 5 and title.lower() not in ['more', 'view', 'apply', 'details', 'read more']:
                job_data.append({
                    'title': title,
                    'url': url.split('?')[0]
                })
        except Exception as e:
            log(f"Error extracting job data: {e}")
            continue
    
    seen_urls = set()
    unique_jobs = []
    for job in job_data:
        if job['url'] not in seen_urls:
            seen_urls.add(job['url'])
            unique_jobs.append(job)
    
    log(f"Processing {len(unique_jobs)} unique jobs after deduplication...")
    
    saved_count = 0
    
    for i, job_info in enumerate(unique_jobs):
        if cancel_event.is_set(): 
            raise OperationCancelledError("Scraping cancelled by user.")
        paused.wait()
        
        job_title = job_info['title']
        job_url = job_info['url']
        
        log(f"({i+1}/{len(unique_jobs)}) Processing: {job_title}")

        current_url = driver.current_url
        description, pdf_text_content = "", ""
        
        try:
            driver.get(job_url)
            
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                log(f"   ⚠ Timeout loading page for '{job_title}'")
                continue
            
            description_selectors = [
                '.job-description', 
                '[data-automation="jobAdDetails"]', 
                '.job-details', 
                '.content',
                '.description', 
                'main',
                'article', 
                '.job-content',
                '[role="main"]',
                '.main-content'
            ]
            
            description_found = False
            for desc_selector in description_selectors:
                try:
                    desc_elements = driver.find_elements(By.CSS_SELECTOR, desc_selector)
                    if desc_elements:
                        description = desc_elements[0].text
                        if description and len(description) > 150:
                            description_found = True
                            break
                except Exception:
                    continue
            
            if not description_found:
                try:
                    description = extract_job_content_from_page(driver, job_title, log)
                except Exception as e:
                    log(f"   • Content extraction failed: {e}")
                    description = f"Job description could not be loaded for: {job_title}"
            
            try:
                pdf_links = driver.find_elements(By.CSS_SELECTOR, 'a[href$=".pdf"]')
                if pdf_links:
                    log(f"   • Found {len(pdf_links)} PDF link(s) to process.")
                    for pdf_link in pdf_links:
                        try:
                            pdf_url = pdf_link.get_attribute('href')
                            extracted_text = _get_pdf_text_from_url(pdf_url, driver.current_url, log_callback)
                            if extracted_text:
                                pdf_text_content += extracted_text + "\n\n"
                        except Exception as e:
                            log(f"   • PDF processing failed for a link: {e}")
                            continue
            except Exception as e:
                log(f"   • PDF search failed: {e}")

        except Exception as e:
            log(f"   ✗ Error loading job details for '{job_title}': {e}")
            description = f"Job description could not be loaded for: {job_title}"
        
        try:
            driver.get(current_url)
        except Exception:
            pass
        
        company = "Deakin University"
        job_details = {
            'title': job_title, 
            'company': company, 
            'location': "Victoria",
            'url': job_url, 
            'description': description, 
            'pdf_text': pdf_text_content
        }

        if description and len(description.strip()) >= 30:
            if db.add_job(job_details, 'Deakin', profile_id, log_callback):
                log(f"   ✓ Saved '{job_title}' to database.")
                saved_count += 1
            else:
                log(f"   • Duplicate skipped: {job_title}")
        else:
            log(f"   ✗ Skipped '{job_title}' - description too short ({len(description)} chars).")
    
    log(f"Deakin University scrape complete. Saved {saved_count} new jobs.")
    return saved_count > 0

def extract_job_content_from_page(driver, job_title, log):
    """
    Intelligently extract job content from Deakin job pages by filtering out navigation and irrelevant content.
    """
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        lines = body_text.split('\n')
        
        skip_patterns = [
            'menu', 'navigation', 'nav', 'footer', 'header', 'sidebar',
            'online courses', 'how online study works', 'key dates', 'ways to study',
            'undergraduate applications', 'postgraduate applications', 'how to apply',
            'fees and scholarships', 'accommodation', 'graduation', 'alumni',
            'research for industry', 'donate', 'news and media', 'contact',
            'facebook', 'twitter', 'instagram', 'linkedin', 'youtube',
            'copyright', 'disclaimer', 'privacy', 'sitemap', 'accessibility',
            'cricos provider', 'deakin university', 'atar calculator',
            'powered by', 'back to search', 'apply now', 'job alert',
            'existing applicant login', 'whatsapp', 'email app'
        ]
        
        job_start_indicators = [
            'job no:', 'work type:', 'location:', 'categories:',
            'who are we?', 'about the role:', 'as a', 'to be successful',
            'here\'s how to apply', 'level a', 'level b', 'level c'
        ]
        
        job_end_indicators = [
            'advertised:', 'applications close:', 'back to search results',
            'powered by pageup', 'connect with deakin', 'we acknowledge the traditional'
        ]
        
        relevant_lines = []
        job_content_started = False
        job_content_ended = False
        
        for line in lines:
            line_lower = line.strip().lower()
            
            if len(line.strip()) < 3:
                continue
                
            if any(indicator in line_lower for indicator in job_end_indicators):
                job_content_ended = True
                break
                
            if not job_content_started:
                if any(indicator in line_lower for indicator in job_start_indicators):
                    job_content_started = True
                    relevant_lines.append(line.strip())
                continue
            
            if job_content_started and not job_content_ended:
                if any(pattern in line_lower for pattern in skip_patterns):
                    continue
                    
                if len(line.strip()) < 10 and line_lower in ['apply', 'menu', 'search', 'home', 'back']:
                    continue
                    
                if len(line.strip()) > 5:
                    relevant_lines.append(line.strip())
        
        if not relevant_lines or len('\n'.join(relevant_lines)) < 200:
            log("   • Trying alternative content extraction...")
            
            alt_selectors = [
                '.job-summary', '.position-details', '.role-description',
                '[class*="job"]', '[id*="job"]', '[class*="position"]',
                '[id*="position"]', '.vacancy', '[class*="role"]'
            ]
            
            for selector in alt_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for element in elements:
                        text = element.text.strip()
                        if len(text) > 200 and 'menu' not in text.lower():
                            return text
                except Exception:
                    continue
            
            full_text = body_text.lower()
            start_markers = ['about the role', 'job description', 'position overview', 'role summary']
            end_markers = ['how to apply', 'applications close', 'advertised:', 'apply now']
            
            for start_marker in start_markers:
                start_idx = full_text.find(start_marker)
                if start_idx != -1:
                    for end_marker in end_markers:
                        end_idx = full_text.find(end_marker, start_idx)
                        if end_idx != -1:
                            extracted = body_text[start_idx:end_idx].strip()
                            if len(extracted) > 200:
                                return extracted
                    
                    extracted = body_text[start_idx:start_idx+2000].strip()
                    if len(extracted) > 200:
                        return extracted
        
        result = '\n'.join(relevant_lines)
        result = result.replace('\n\n\n', '\n\n')
        result = '\n'.join([line for line in result.split('\n') if len(line.strip()) > 0])
        
        return result if len(result) > 100 else f"Filtered job description for: {job_title}"
        
    except Exception as e:
        log(f"   • Content extraction error: {e}")
        return f"Job description could not be extracted for: {job_title}"
