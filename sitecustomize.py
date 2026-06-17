"""Add the per-user Python site-packages directory for bundled app runtimes."""
import site
import sys

user_site = site.getusersitepackages()
if user_site and user_site not in sys.path:
    sys.path.append(user_site)
