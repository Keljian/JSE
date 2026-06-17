"""Local runtime defaults for the job application assistant.

Keep this file free of personal details and live API keys. User-specific
credentials, resume paths, and contact details should be entered through the
desktop app settings or provided through an untracked local override.
"""

# Optional personal information for pre-filling forms.
MY_INFO = {
    "first_name": "Candidate",
    "last_name": "",
    "email": "",
    "phone": "",
    "linkedin": "",
    "github": "",
    "portfolio": "",
    "resume_path": "",
    
    # --- Unsloth Studio API Configuration ---
    # Get your API key and base URL from Unsloth Studio
    "unsloth_base_url": "http://localhost:8888/v1",
    "unsloth_api_key": "",
    # "unsloth_model": "unsloth/llama-3-70b-instruct",
    
    # --- Retry Configuration ---
    # Number of retry attempts for failed LLM API calls
    "unsloth_max_retries": 3,
    # Base delay in seconds between retries (doubles each attempt)
    "unsloth_retry_delay": 5,
}
