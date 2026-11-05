"""Who may see and handle runs built from consumer feedback: whoever Hub lets read the package.

Feedback belongs to a package, not to one evaluation user, so there is no separate owner list: each check asks
Hub with the caller's own login. Answers are cached per caller and package for a minute.
"""
import threading
import time
from urllib.parse import quote

from ..core import Failure

CACHE_SECONDS = 60


class PackageAccess:
    def __init__(self, packages, clock=time.monotonic):
        self.packages, self.clock = packages, clock
        self.cache, self.lock = {}, threading.Lock()

    def visible(self, subject, token, ontology_id):
        key, now = (subject, ontology_id), self.clock()
        with self.lock:
            found = self.cache.get(key)
        if found and now - found[0] < CACHE_SECONDS:
            return found[1]
        try:
            self.packages.call('GET', f"/api/packages/{quote(ontology_id, safe='')}", token)
            result = True
        except Failure as exc:
            if exc.status not in (403, 404, 410):
                raise
            result = False
        with self.lock:
            self.cache[key] = (now, result)
        return result

    def require(self, subject, token, ontology_id):
        """Not visible reads as not found, so callers cannot tell whether a package has feedback."""
        if not ontology_id or not self.visible(subject, token, ontology_id):
            raise Failure('not_found', '记录不存在或无权访问', 404)
