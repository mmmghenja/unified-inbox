# Third-party software

unified-inbox is MIT-licensed (see [LICENSE](LICENSE)). It depends on the packages below; each keeps its own
license. Nothing here is distributed in this repository: dependencies are installed by `uv sync` and `npm ci`.
The lists come from `uv.lock` and `ui/package-lock.json` (license fields of the installed packages) and are
regenerated when the lock files change.

**Copyleft check.** No GPL or AGPL package is used, at runtime or for development. The weak-copyleft entries
are `certifi` (MPL-2.0, Python) and `dompurify` (dual-licensed `MPL-2.0 OR Apache-2.0`, npm); both are used
unmodified, so their file-level terms are not triggered. `lottie-web`, `markdown-it`, `vue` and the xterm
packages are MIT.

## Python (runtime)

| Package | Version | License |
|---|---|---|
| aiosmtplib | 5.1.3 | MIT |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| emoji | 2.16.0 | BSD License |
| fastapi | 0.141.1 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| httpx2 | 2.13.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| mcp | 2.2.0 | MIT |
| mcp-types | 2.2.0 | MIT |
| opentelemetry-api | 1.44.0 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 13.0.0 | MIT-CMU |
| pyaes | 1.6.1 | MIT |
| pyasn1 | 0.6.4 | BSD-2-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pyjwt | 2.15.0 | MIT |
| pypdf | 6.19.0 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| python-socks | 3.1.1 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| rsa | 4.9.1 | Apache Software License |
| socksio | 1.0.0 | MIT |
| sse-starlette | 3.4.11 | BSD-3-Clause |
| starlette | 1.6.0 | BSD-3-Clause |
| telethon | 1.45.0 | MIT |
| truststore | 0.10.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| uvicorn | 0.53.0 | BSD-3-Clause |
| websockets | 17.1 | BSD-3-Clause |

## Python (development and tests only)

| Package | Version | License |
|---|---|---|
| execnet | 2.1.2 | MIT |
| iniconfig | 2.3.0 | MIT |
| pluggy | 1.6.0 | MIT |
| pygments | 2.21.0 | BSD-2-Clause |
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| pytest-timeout | 2.4.0 | DFSG approved; MIT |
| pytest-xdist | 3.8.0 | MIT |
| ruff | 0.16.8 | MIT |

## npm (ships in the built UI)

| Package | Version | License |
|---|---|---|
| @babel/helper-string-parser | 7.29.7 | MIT |
| @babel/helper-validator-identifier | 7.29.7 | MIT |
| @babel/parser | 7.29.9 | MIT |
| @babel/types | 7.29.8 | MIT |
| @jridgewell/sourcemap-codec | 1.6.0 | MIT |
| @types/trusted-types | 2.0.7 | MIT |
| @vue/compiler-core | 3.5.43 | MIT |
| @vue/compiler-dom | 3.5.43 | MIT |
| @vue/compiler-sfc | 3.5.43 | MIT |
| @vue/compiler-ssr | 3.5.43 | MIT |
| @vue/reactivity | 3.5.43 | MIT |
| @vue/runtime-core | 3.5.43 | MIT |
| @vue/runtime-dom | 3.5.43 | MIT |
| @vue/server-renderer | 3.5.43 | MIT |
| @vue/shared | 3.5.43 | MIT |
| @xterm/addon-fit | 0.11.0 | MIT |
| @xterm/xterm | 6.0.0 | MIT |
| argparse | 3.0.2 | PSF-2.0 |
| csstype | 3.2.3 | MIT |
| dompurify | 3.4.16 | (MPL-2.0 OR Apache-2.0) |
| entities | 7.0.1 | BSD-2-Clause |
| entities | 8.1.0 | BSD-2-Clause |
| estree-walker | 2.0.2 | MIT |
| linkify-it | 6.1.0 | MIT |
| lottie-web | 5.13.0 | MIT |
| magic-string | 0.30.21 | MIT |
| markdown-it | 15.0.2 | MIT |
| mdurl | 2.1.0 | MIT |
| nanoid | 3.3.19 | MIT |
| picocolors | 1.1.1 | ISC |
| postcss | 8.5.28 | MIT |
| punycode.js | 2.3.1 | MIT |
| source-map-js | 1.2.1 | BSD-3-Clause |
| typescript | 5.9.3 | Apache-2.0 |
| uc.micro | 3.0.0 | MIT |
| vue | 3.5.43 | MIT |

## npm (build, lint and test tooling only)

321 packages, none of them shipped in the built UI. Licenses: MIT (269), Apache-2.0 (16), ISC (15), BSD-2-Clause (11), BlueOak-1.0.0 (5), BSD-3-Clause (2), MIT-0 (1), Python-2.0 (1), (MIT OR CC0-1.0) (1).
Two entries deserve a note: `argparse` 2.0.1 is under the Python-2.0 license (permissive, PSF-style) and
the `minimatch`/`glob`/`lru-cache`/`minipass`/`path-scurry` family is BlueOak-1.0.0 (permissive).
Run `npm ls --all` in `ui/` for the full tree.

