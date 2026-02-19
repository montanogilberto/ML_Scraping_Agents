import random, time, requests
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type

class HttpClient:
    def __init__(self, timeout_sec: float, min_delay_sec: float, jitter_sec: float):
        self.timeout_sec=timeout_sec; self.min_delay_sec=min_delay_sec; self.jitter_sec=jitter_sec
    def _sleep(self): time.sleep(self.min_delay_sec + random.random()*self.jitter_sec)

    @retry(retry=retry_if_exception_type((requests.RequestException,)), stop=stop_after_attempt(3),
           wait=wait_exponential_jitter(initial=1,max=20), reraise=True)
    def get_html(self, url: str) -> str:
        self._sleep()
        headers={
            "User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language":"es-MX,es;q=0.9,en;q=0.8",
            "Cache-Control":"no-cache","Pragma":"no-cache",
        }
        r=requests.get(url,headers=headers,timeout=self.timeout_sec,allow_redirects=True)
        r.raise_for_status()
        return r.text

    def get_html_with_fallback(self, url: str, fallback_urls: list = None) -> str:
        """
        Try to fetch HTML with fallback URLs if the primary fails (404).
        Args:
            url: Primary URL to try
            fallback_urls: List of fallback URLs to try if primary fails
        Returns:
            HTML content string
        """
        urls_to_try = [url]
        if fallback_urls:
            urls_to_try.extend(fallback_urls)
        
        last_error = None
        for attempt_url in urls_to_try:
            try:
                self._sleep()
                headers={
                    "User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language":"es-MX,es;q=0.9,en;q=0.8",
                    "Cache-Control":"no-cache","Pragma":"no-cache",
                }
                r=requests.get(attempt_url,headers=headers,timeout=self.timeout_sec,allow_redirects=True)
                if r.status_code == 200:
                    return r.text
                elif r.status_code == 404:
                    # Try next fallback URL
                    last_error = f"404 for {attempt_url}"
                    continue
                else:
                    r.raise_for_status()
            except requests.RequestException as e:
                last_error = str(e)
                continue
        
        # If all URLs fail, raise the last error
        raise requests.HTTPError(f"Failed to fetch from any URL. Last error: {last_error}")
