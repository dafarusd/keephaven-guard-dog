# Third-party software in Keephaven Guard Dog

Guard Dog's own code is AGPL-3.0-only (see `LICENSE`). It's built on other people's software and learned from other
people's projects. Each keeps its own license, and nothing in Guard Dog's license changes that.

## Projects Guard Dog learned from

| Project | What Guard Dog takes from it | License |
|---|---|---|
| [Scrapling](https://github.com/D4Vinci/Scrapling) | The fetchers every search runs on. The .deb carries it (version 0.5). | BSD-3-Clause |
| [Scrubbr](https://github.com/jamessk22/Scrubbr) | The structure of the removal letters (a deletion and opt-out request per broker, a reference tag in the subject, a California version citing the statute), grouping sister sites, and reading an age range as a range. The wording is our own. | MIT |
| [incognito](https://github.com/Salicath/incognito) | The Internet Archive check: its 15 profile-address shapes, the archive query and the pacing between requests. | MIT |
| [Eraser](https://github.com/drumandbytes/eraser) | Which brokers to visit, used as leads only. Every entry in `guard_dog/data/brokers.json` was read on the broker's own site; nothing from Eraser is copied in. | MIT |

Their license texts follow, as each project publishes them.

### Scrapling

```
BSD 3-Clause License

Copyright (c) 2024, Karim shoair

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### Scrubbr

```
MIT License

Copyright (c) 2026 jamessk22

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### incognito

```
MIT License

Copyright (c) 2026 Incognito Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Eraser

```
MIT License

Copyright (c) 2026 digisamroc (original author)
Copyright (c) 2026 Drumandbytes OÜ (fork maintainer)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## In the .deb

- **Python 3.12.15** from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (release
  20261013). Python is under the PSF License (Python-2.0), full text at
  `/opt/keephaven-guard-dog/python/lib/python3.12/LICENSE.txt`. It links in OpenSSL (Apache-2.0), SQLite (public
  domain) and 15 other libraries (libffi, expat, zlib, bzip2, xz, mpdecimal, ncurses, libedit, libuuid, Berkeley DB,
  Tcl, Tix, libX11, libXau, libxcb), each under its own license. Their texts are in `packaging/deb/python-licenses/` here and
  `/opt/keephaven-guard-dog/python/licenses/` in the .deb.
- **The Python packages** in `/opt/keephaven-guard-dog/venv`, pinned with hashes in
  `packaging/deb/requirements-lock.txt`. Each one's license files are in its `.dist-info` folder. The copy of this
  file in each .deb (`/usr/share/doc/keephaven-guard-dog/THIRD-PARTY.md`) ends with the list: name, version and
  license, read from that build.

## Downloaded only when you ask

- **Chrome for Testing 156.0.8078.4**, the hidden browser for stealth mode. It isn't in the .deb or this repo.
  Settings downloads it from Playwright's mirror (cdn.playwright.dev) only when you press the button. It's Google's
  software under Google's terms (its `ABOUT` file and chrome://terms); its open-source parts are listed at
  chrome://credits.
