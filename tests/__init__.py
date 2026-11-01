# Scans in the tests have no network, so every Internet Archive request fails at once; its pacing between
# requests would only add 22 s to each scan. test_archive checks the pacing with its own numbers.
from guard_dog import archive

archive.PAUSE = archive.BACKOFF = 0
