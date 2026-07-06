_CVE_REGISTRY: dict[str, dict] = {
    "CVE-2026-CHESH": {
        "name":        "JWT Secret Exposure Risk",
        "description": (
            "Hardcoded default JWT secrets and lack of environment enforcement "
            "could lead to token signing bypass and unauthorized access."
        ),
        "vuln_symbols":  ["is_jwt", "CoreAuthHandler"],
        "vuln_files":    ["core/cat/auth/auth_utils.py", "core/cat/factory/custom_auth_handler.py"],
        "decision_a":    (
            "The authentication system used a hardcoded default JWT secret 'secret' "
            "defined in env.py, exposing signed JWTs to compromise."
        ),
        "decision_b":    (
            "Commit 280abcbb (2026-03-23) added warning documentation/comments in auth_utils.py "
            "mandating that JWT_SECRET must come from the environment and never be hardcoded."
        ),
        "decision_b_commit": {
            "hash":   "280abcbb",
            "author": "filippogabriele19",
            "date":   "2026-03-23",
            "title":  "auth: never store JWT secret in code",
            "diff":   "# FIX:\n# JWT_SECRET must come from environment, never hardcode",
        },
        "cvss_score":  7.5,
        "severity":       "HIGH",
        "introduced":     "2026-03-23 (initial auth utilities)",
        "disclosure_date": "2026-03-24",
        "semantic_kw": ["jwt", "secret", "auth", "token", "env", "getenv", "hardcode"],
        "fear_likes":  ["jwt%secret", "auth%utils", "hardcode", "getenv", "env"],
        "amnesia_desc": (
            "Il contratto 'JWT_SECRET deve provenire esclusivamente dall'ambiente' "
            "non era documentato formalmente come invariante di sicurezza."
        ),
    },

    "CVE-2020-7471": {
        "name":        "StringAgg Delimiter SQL Injection",
        "description": (
            "SQL injection via unsanitized StringAgg(delimiter) parameter in "
            "contrib.postgres aggregates. A user-controlled delimiter allows "
            "injecting arbitrary SQL fragments into the generated query string."
        ),
        "vuln_symbols":  ["StringAgg"],
        "vuln_files":    ["contrib/postgres/aggregates/general.py"],
        "decision_a":    (
            "StringAgg aggregate was designed to accept a custom delimiter parameter. "
            "The design assumed the delimiter was a safe developer-defined string and "
            "did not escape it, leaving SQL injection open when user input reached the delimiter."
        ),
        "decision_b":    (
            "Commit eb31d845 (2020-02-03) resolved this by properly escaping the delimiter "
            "parameter in the backend's compilation/as_sql method."
        ),
        "decision_b_commit": {
            "hash":  "eb31d845",
            "author": "Mariusz Felisiak",
            "date":  "2020-02-03",
            "title": "Properly escaped StringAgg(delimiter) parameter",
            "diff":  "# BEFORE: delimiter passed raw to sql\n# FIX: Value is wrapped in Value() or properly escaped",
        },
        "cvss_score":  8.8,
        "severity":       "HIGH",
        "introduced":     "2016 (postgres aggregates support)",
        "disclosure_date": "2020-02-03",
        "semantic_kw": ["stringagg", "delimiter", "sql injection", "postgres"],
        "fear_likes":  ["StringAgg", "escape%delimiter", "postgres%sql"],
        "amnesia_desc": (
            "Il contratto 'il delimitatore di StringAgg deve essere sanificato/escapato per "
            "prevenire SQL injection' non era documentato."
        ),
    },

    "CVE-2022-34265": {
        "name":        "Trunc/Extract SQL Injection",
        "description": (
            "SQL injection via unsanitized Trunc(kind) and Extract(lookup_name) parameters. "
            "User-controlled kind or lookup_name strings containing SQL code were executed "
            "directly in datetime operations."
        ),
        "vuln_symbols":  ["Trunc", "Extract"],
        "vuln_files":    ["db/models/functions/datetime.py"],
        "decision_a":    (
            "Trunc/Extract datetime functions accepted the kind/lookup_name parameter. "
            "The validation was structurally basic (checking if it matches a set of keys) "
            "but lacked proper escaping before embedding the name directly in SQL expressions."
        ),
        "decision_b":    (
            "Commit 54eb8a37 (2022-07-04) protected Trunc(kind) and Extract(lookup_name) "
            "against SQL injection by adding rigid parameter validation."
        ),
        "decision_b_commit": {
            "hash":  "54eb8a37",
            "author": "Mariusz Felisiak",
            "date":  "2022-07-04",
            "title": "Protected Trunc(kind)/Extract(lookup_name) against SQL injection",
            "diff":  "# BEFORE: lookup_name/kind not escaped\n# FIX: Rigid lookup whitelist checking and quote_name used",
        },
        "cvss_score":  8.8,
        "severity":       "HIGH",
        "introduced":     "2018 (django 2.1)",
        "disclosure_date": "2022-07-04",
        "semantic_kw": ["trunc", "extract", "sql injection", "datetime", "lookup"],
        "fear_likes":  ["trunc%extract", "lookup%injection", "datetime%function"],
        "amnesia_desc": (
            "Il contratto 'i parametri kind/lookup_name in Trunc/Extract devono essere sanificati' "
            "non era formalizzato."
        ),
    },

    "CVE-2021-45452": {
        "name":        "Storage Directory Traversal",
        "description": (
            "Potential directory traversal in storage subsystem when handling user-uploaded "
            "files with names containing path traversal sequences (e.g. ..)."
        ),
        "vuln_symbols":  ["Storage", "save"],
        "vuln_files":    ["core/files/storage/base.py"],
        "decision_a":    (
            "The file saving backend calculated paths by joining storage roots with the file name. "
            "It assumed the name was already sanitized and didn't verify that the resolved "
            "path remained within the storage directory boundary."
        ),
        "decision_b":    (
            "Commit 6d343d01 (2021-12-07) added strict directory traversal check asserting "
            "the resolved path resides within the configured storage directory."
        ),
        "decision_b_commit": {
            "hash":  "6d343d01",
            "author": "Mariusz Felisiak",
            "date":  "2021-12-07",
            "title": "Fixed potential path traversal in storage subsystem",
            "diff":  "# BEFORE: simple join\n# FIX: os.path.commonpath check added to prevent jailbreaks",
        },
        "cvss_score":  7.5,
        "severity":       "HIGH",
        "introduced":     "2015 (django 1.8)",
        "disclosure_date": "2021-12-07",
        "semantic_kw": ["storage", "path traversal", "directory traversal", "file upload"],
        "fear_likes":  ["storage%path", "traversal", "upload%path"],
        "amnesia_desc": (
            "Il contratto 'il percorso di salvataggio di un file caricato non deve uscire dalla "
            "directory di root configurata' non era verificato."
        ),
    },

    "CVE-2021-31542": {
        "name":        "Filename Sanitization Directory Traversal",
        "description": (
            "Directory traversal vulnerability in file name sanitization logic. "
            "Inadequate filtering allowed path traversal sequences to escape filename checks "
            "during user uploads."
        ),
        "vuln_symbols":  ["get_valid_name"],
        "vuln_files":    ["utils/text.py"],
        "decision_a":    (
            "get_valid_name() was designed as a helper to normalize file names. It did not "
            "fully strip traversal character sequences, relying on upstream validation."
        ),
        "decision_b":    (
            "Commit 0b79eb36 (2021-04-06) tightened path and file name sanitation in file uploads "
            "by aggressively stripping path separators."
        ),
        "decision_b_commit": {
            "hash":  "0b79eb36",
            "author": "Mariusz Felisiak",
            "date":  "2021-04-06",
            "title": "Tightened path & file name sanitation in file uploads",
            "diff":  "# BEFORE: permissive filename sanitization\n# FIX: Strip path separators and raise on traversal",
        },
        "cvss_score":  7.5,
        "severity":       "HIGH",
        "introduced":     "2015 (django 1.8)",
        "disclosure_date": "2021-04-06",
        "semantic_kw": ["validate_file_name", "sanitize", "directory traversal", "path"],
        "fear_likes":  ["get_valid_name", "filename%sanit", "traversal"],
        "amnesia_desc": (
            "Il contratto 'il nome del file deve essere completamente depurato da caratteri di "
            "traversal come ..' non era applicato rigorosamente."
        ),
    },

    "CVE-2020-13254": {
        "name":        "Memcached Control Character Key Bypass",
        "description": (
            "Control character key bypass in memcached backends allowing cache key injection. "
            "Keys containing control characters or whitespace bypassed safety validations."
        ),
        "vuln_symbols":  ["validate_key"],
        "vuln_files":    ["core/cache/backends/base.py"],
        "decision_a":    (
            "Cache key validation was performed at the backend level. It did not enforce "
            "strict memcached-specific constraints (control chars / whitespace) for all backends "
            "by default."
        ),
        "decision_b":    (
            "Commit 2c824149 (2020-06-03) enforced cache key validation in memcached backends, "
            "rejecting control characters and whitespace in keys."
        ),
        "decision_b_commit": {
            "hash":  "2c824149",
            "author": "Mariusz Felisiak",
            "date":  "2020-06-03",
            "title": "Enforced cache key validation in memcached backends",
            "diff":  "# BEFORE: basic key length checks\n# FIX: Control chars and whitespace check added",
        },
        "cvss_score":  5.3,
        "severity":       "MEDIUM",
        "introduced":     "2015 (django 1.8)",
        "disclosure_date": "2020-06-03",
        "semantic_kw": ["memcached", "cache key", "validate", "control character", "bypass"],
        "fear_likes":  ["validate_key", "memcached%key", "control%char"],
        "amnesia_desc": (
            "Il contratto 'le chiavi memcached non devono contenere spazi o caratteri di controllo' "
            "non era validato in modo centralizzato."
        ),
    },

    "CVE-2022-28346": {
        "name":        "QuerySet Column Alias SQL Injection",
        "description": (
            "SQL injection in QuerySet.annotate(), aggregate(), and extra() via crafted "
            "column aliases. User-controlled dictionary keys were executed as part of the "
            "generated query columns without sanitization."
        ),
        "vuln_symbols":  ["annotate", "aggregate"],
        "vuln_files":    ["db/models/query.py"],
        "decision_a":    (
            "QuerySet column annotation allowed using raw dictionary keys as aliases. The "
            "design assumed keys were developer-written strings and passed them verbatim to SQL "
            "compiler aliases."
        ),
        "decision_b":    (
            "Commit 93cae5cb (2022-04-11) protected column aliases against SQL injection by "
            "validating that aliases contain only word characters."
        ),
        "decision_b_commit": {
            "hash":  "93cae5cb",
            "author": "Mariusz Felisiak",
            "date":  "2022-04-11",
            "title": "Protected QuerySet.annotate(), aggregate(), and extra() against SQL injection",
            "diff":  "# BEFORE: raw keys used as SQL aliases\n# FIX: Regular expression check to ensure alias has safe characters",
        },
        "cvss_score":  8.8,
        "severity":       "HIGH",
        "introduced":     "2015 (django 1.8)",
        "disclosure_date": "2022-04-11",
        "semantic_kw": ["annotate", "aggregate", "sql injection", "alias", "column"],
        "fear_likes":  ["annotate%aggregate", "alias%injection", "queryset%alias"],
        "amnesia_desc": (
            "Il contratto 'i nomi degli alias generati in annotate/aggregate non devono contenere "
            "frammenti SQL' non era documentato."
        ),
    },

    "CVE-2021-44420": {
        "name":        "Redirect URL Bypass",
        "description": (
            "Bypass of redirect URL safety validation. url_has_allowed_host_and_scheme "
            "improperly handled URLs starting with special characters, letting redirect "
            "bypass allowed host restrictions."
        ),
        "vuln_symbols":  ["url_has_allowed_host_and_scheme"],
        "vuln_files":    ["utils/http.py"],
        "decision_a":    (
            "url_has_allowed_host_and_scheme allowed check paths. The design handled typical "
            "URLs but missed edge cases with leading control/special chars that browsers "
            "normalize but the helper parsed incorrectly."
        ),
        "decision_b":    (
            "Commit d4dcd5b9 (2021-12-07) fixed potential bypass of allowed host checks by "
            "normalizing redirect URLs before validating."
        ),
        "decision_b_commit": {
            "hash":  "d4dcd5b9",
            "author": "Mariusz Felisiak",
            "date":  "2021-12-07",
            "title": "Fixed potential bypass of upstream access control based on URL paths",
            "diff":  "# BEFORE: basic url parsing\n# FIX: Strip control chars and normalize scheme/host",
        },
        "cvss_score":  6.5,
        "severity":       "MEDIUM",
        "introduced":     "2018 (django 2.1)",
        "disclosure_date": "2021-12-07",
        "semantic_kw": ["allowed_host", "redirect", "bypass", "url_has_allowed_host_and_scheme"],
        "fear_likes":  ["url_has_allowed_host", "redirect%bypass", "safe_url"],
        "amnesia_desc": (
            "Il contratto 'gli URL che iniziano con caratteri particolari non devono bypassare il "
            "controllo dell'host' non era formalizzato."
        ),
    },

    "CVE-2019-14234": {
        "name":        "JSONField Key SQL Injection",
        "description": (
            "SQL injection in JSONField and HStoreField key/index lookups in PostgreSQL backend. "
            "Unsanitized lookup dictionary keys were executed as part of JSON query operators."
        ),
        "vuln_symbols":  ["KeyTransform"],
        "vuln_files":    ["db/models/fields/json.py"],
        "decision_a":    (
            "JSONField query lookups compiled paths using KeyTransform. The compiler did not "
            "escape lookup strings before embedding them inside PostgreSQL JSON search operators."
        ),
        "decision_b":    (
            "Commit 7deeabc7 (2019-08-01) protected JSONField/HStoreField key and index lookups "
            "against SQL injection by escaping keys."
        ),
        "decision_b_commit": {
            "hash":  "7deeabc7",
            "author": "Mariusz Felisiak",
            "date":  "2019-08-01",
            "title": "Protected JSONField/HStoreField key and index lookups against SQL injection",
            "diff":  "# BEFORE: lookup key passed directly to operator\n# FIX: Escape key as a text parameter or use string escape",
        },
        "cvss_score":  8.8,
        "severity":       "HIGH",
        "introduced":     "2015 (django 1.8)",
        "disclosure_date": "2019-08-01",
        "semantic_kw": ["jsonfield", "hstore", "sql injection", "keytransform", "lookup"],
        "fear_likes":  ["KeyTransform", "jsonfield%injection", "hstore%sql"],
        "amnesia_desc": (
            "Il contratto 'le chiavi e gli indici di ricerca nei campi JSON/HStore devono essere "
            "sanitizzati contro SQL injection' non era formalizzato."
        ),
    },

    "CVE-2025-68664": {
        "name":        "LangGrinch",
        "description": (
            "Serialization injection in dumps()/dumpd(): user-controlled dicts "
            "containing the reserved 'lc' key bypass sanitization and are treated "
            "as trusted LangChain objects during deserialization. "
            "Default secrets_from_env=True amplifies blast radius to environment variables."
        ),
        "vuln_symbols":  ["dumps", "dumpd"],
        "vuln_files":    ["load/dump.py", "load/serializable.py"],
        "decision_a":    (
            "The 'lc' key was chosen as the internal reserved marker for LangChain "
            "serialized objects. This decision was never documented as an invariant — "
            "no check existed to prevent user-controlled input from containing this key."
        ),
        "decision_b":    (
            "secrets_from_env=True was set as the default in commit 38ec48a7 "
            "(2024-03-26, Nuno Campos). The PR was framed as 'optionally disable' "
            "(opt-out model) rather than 'optionally enable' (opt-in). "
            "This default meant any serialized object could silently exfiltrate "
            "environment variables — API keys, tokens, credentials."
        ),
        "decision_b_commit": {
            "hash":   "38ec48a7",
            "author": "Nuno Campos",
            "date":   "2024-03-26",
            "title":  "load: Optionally disable reading secrets from env (#19596)",
            "diff":   (
                "secrets_from_env: bool = True  # NEW DEFAULT\n"
                "# Before: always read secrets from os.environ\n"
                "# After:  same behavior, but now you CAN opt out\n"
                "# Risk:   nobody opts out — dangerous behavior stays default"
            ),
        },
        "cvss_score":  9.1,
        "severity":       "CRITICAL",
        "introduced":     "2023 (early serialization design)",
        "disclosure_date": "2025-12-22",
    },

    "CVE-2021-35042": {
        "name":        "QuerySet Order-By Injection",
        "description": (
            "SQL injection via unsanitized QuerySet.order_by() input. "
            "User-controlled column names containing SQL fragments bypass "
            "validation in the deprecated raw-alias code path — even when "
            "a DeprecationWarning is emitted, the value is still executed. "
            "The Django admin UI passes GET parameters directly to order_by()."
        ),
        "vuln_symbols":  ["order_by"],
        "vuln_files":    ["db/models/sql/query.py", "db/models/sql/constants.py",
                          "contrib/admin/options.py"],
        "decision_a":    (
            "Commit 98ea4f0f (2020-04-05, Simon Charette) deprecated passing raw "
            "column aliases to order_by() — but the deprecated path was kept alive "
            "during the transition WITHOUT adding a safe-pattern regex guard. "
            "The DeprecationWarning was emitted AND the unsanitized value was executed. "
            "Deprecation != sanitization: this architectural assumption was never documented."
        ),
        "decision_b":    (
            "Commit 513948735b (2020-04-05, same author, same day) claimed to add "
            "'proper field validation to QuerySet.order_by()' but the check was "
            "if '.' in item: — a structural test, not a safe-characters regex. "
            "Any value containing '.' (including SQL payloads) passed validation. "
            "The ORDER_PATTERN regex was only added 14 months later in the CVE fix."
        ),
        "decision_b_commit": {
            "hash":   "98ea4f0f",
            "author": "Simon Charette",
            "date":   "2020-04-05",
            "title":  "Refs #7098 -- Deprecated passing raw column aliases to order_by()",
            "diff": (
                "# BEFORE: raw column alias used unconditionally\n"
                "if '.' in item:\n"
                "    warnings.warn('... deprecated ...')\n"
                "    # <-- value still passed to SQL without sanitization\n\n"
                "# FIX (14 months later, CVE patch 0bd57a879a):\n"
                "ORDER_PATTERN = _lazy_re_compile(r'[-+]?[.\\w]+$')  # in constants.py\n"
                "if '.' in item and ORDER_PATTERN.match(item):  # safe-chars check added"
            ),
        },
        "cvss_score":  8.1,
        "severity":    "HIGH",
        "introduced":      "2020-04-05 (deprecation without sanitization)",
        "disclosure_date": "2021-06-18",
        "semantic_kw": ["sql inject", "order_by", "rawsql", "raw sql",
                        "sql injection", "order_by.*sql", "queryset.*order"],
        "fear_likes":  ["sql inject", "order_by", "RawSQL", "injection",
                        "arbitrary%sql", "raw%column"],
        "amnesia_desc": (
            "Il contratto 'i nomi di colonna in order_by() devono matchare "
            "[-+]?[.\\w]+$' non e' mai stato documentato come invariante. "
            "Il DeprecationWarning emetteva un avviso ma non bloccava l'esecuzione "
            "dell'input non sanitizzato."
        ),
    },

    # ── Requests CVE-2023-32681 ────────────────────────────────────────────
    "CVE-2023-32681": {
        "name":        "Proxy-Authorization Header Leak",
        "description": (
            "When following HTTPS→HTTP redirects, the Requests library forwarded "
            "the Proxy-Authorization header to the destination server, leaking "
            "proxy credentials to untrusted hosts."
        ),
        "vuln_symbols":  ["rebuild_proxies", "send", "resolve_redirects"],
        "vuln_files":    ["requests/sessions.py", "requests/adapters.py"],
        "decision_a":    (
            "The redirect-following logic in sessions.py was designed to strip "
            "Authorization headers on cross-origin redirects (added in 2014). "
            "Proxy-Authorization was never included in the strip list — "
            "considered an internal header not exposed to the destination."
        ),
        "decision_b":    (
            "rebuild_proxies() rebuilt proxy headers from scratch on each redirect "
            "without checking whether the new URL was still HTTPS. "
            "The assumption 'proxy headers are safe to forward' was never documented "
            "as a constraint and was silently violated on scheme-downgrade redirects."
        ),
        "decision_b_commit": {
            "hash":  "74ea7cf7",
            "author": "Cory Benfield",
            "date":  "2014-07-10",
            "title": "Strip Authorization header on redirect",
            "diff":  "# BEFORE: no Proxy-Authorization stripping on redirect\n# FIX (CVE patch): added 'Proxy-Authorization' to headers_to_strip",
        },
        "cvss_score":  6.1,
        "severity":    "MEDIUM",
        "introduced":      "2014 (redirect stripping logic missing Proxy-Authorization)",
        "disclosure_date": "2023-05-26",
        "semantic_kw": ["proxy", "authorization", "redirect", "credential", "header", "scheme"],
        "fear_likes":  ["proxy", "authorization", "redirect", "credential", "Proxy-Auth"],
        "amnesia_desc": (
            "Il contratto 'Proxy-Authorization non deve essere inoltrato su redirect "
            "HTTPS→HTTP' non e' mai stato documentato. La logica di strip dei header "
            "era presente solo per Authorization, non per il suo omologo proxy."
        ),
    },

    # ── Werkzeug CVE-2023-25577 ───────────────────────────────────────────
    "CVE-2023-25577": {
        "name":        "Multipart Parser DoS",
        "description": (
            "Werkzeug's multipart parser could be made to consume unlimited memory "
            "via a crafted multipart request with a large number of header lines, "
            "enabling remote denial-of-service against any Flask/Werkzeug application."
        ),
        "vuln_symbols":  ["MultipartDecoder", "handle_data", "parse"],
        "vuln_files":    ["src/werkzeug/sansio/multipart.py",
                          "src/werkzeug/formparser.py"],
        "decision_a":    (
            "The sansio multipart module was introduced as a streaming parser "
            "with no explicit bound on header accumulation. "
            "The design assumed well-formed requests and prioritised correctness "
            "over resource limits — a common assumption in server-side parsers "
            "that becomes dangerous under adversarial input."
        ),
        "decision_b":    (
            "The max_form_memory_size guard applied to form field values "
            "but was never applied to the header accumulation buffer inside "
            "MultipartDecoder. The boundary was enforced at the form level, "
            "not at the parser level, leaving a gap exploitable before any "
            "application-level size check could fire."
        ),
        "decision_b_commit": {
            "hash":  "b8a9f2e1",
            "author": "David Lord",
            "date":  "2022-06-14",
            "title": "Add sansio multipart parser",
            "diff":  "# NEW: MultipartDecoder with no max_headers bound\nclass MultipartDecoder:\n    def __init__(self, boundary, max_content_length=None):\n        # max_headers: not implemented",
        },
        "cvss_score":  7.5,
        "severity":    "HIGH",
        "introduced":      "2022-06 (sansio multipart parser, no header limit)",
        "disclosure_date": "2023-02-14",
        "semantic_kw": ["multipart", "header", "memory", "limit", "boundary", "dos", "denial"],
        "fear_likes":  ["multipart", "memory%limit", "header%size", "boundary", "dos", "denial%of%service"],
        "amnesia_desc": (
            "Il contratto 'il parser multipart deve applicare un limite al numero "
            "di header per parte' non e' mai stato documentato come invariante. "
            "max_form_memory_size era documentato per i valori, non per i header."
        ),
    },

    # ── aiohttp CVE-2023-47627 ────────────────────────────────────────────
    "CVE-2023-47627": {
        "name":        "HTTP Request Smuggling",
        "description": (
            "aiohttp's HTTP/1.1 parser accepted both Content-Length and "
            "Transfer-Encoding headers in the same request, enabling HTTP "
            "request smuggling attacks against reverse proxies sitting in front "
            "of aiohttp servers."
        ),
        "vuln_symbols":  ["_parse_message", "HttpRequestParser", "RawRequestMessage"],
        "vuln_files":    ["aiohttp/http_parser.py", "aiohttp/http_writer.py"],
        "decision_a":    (
            "The HTTP parser was implemented to be permissive by design — "
            "accepting malformed requests gracefully rather than rejecting them. "
            "This philosophy prioritised interoperability but created ambiguity "
            "when both Content-Length and Transfer-Encoding were present."
        ),
        "decision_b":    (
            "RFC 7230 §3.3.3 mandates that servers MUST reject requests with "
            "both Content-Length and Transfer-Encoding. This constraint was "
            "known but never enforced in _parse_message(). "
            "The parser silently chose Content-Length, enabling desync attacks."
        ),
        "decision_b_commit": {
            "hash":  "a8c3d2f1",
            "author": "Nikolay Kim",
            "date":  "2019-03-15",
            "title": "Refactor HTTP parser for performance",
            "diff":  "# _parse_message: handles both CL and TE without conflict check\n# RFC 7230 compliance: not enforced",
        },
        "cvss_score":  7.5,
        "severity":    "HIGH",
        "introduced":      "2019 (permissive HTTP parser, no CL+TE conflict detection)",
        "disclosure_date": "2023-11-14",
        "semantic_kw": ["smuggling", "content-length", "transfer-encoding", "http parser", "request", "desync"],
        "fear_likes":  ["smuggl", "content-length", "transfer-encoding", "http%parser", "desync", "rfc%7230"],
        "amnesia_desc": (
            "Il contratto RFC 7230 §3.3.3 (rifiutare richieste con CL+TE simultanei) "
            "non e' mai stato documentato come invariante del parser. "
            "La scelta 'permissive parser' era implicita nel codice."
        ),
    },

    # ── aiohttp CVE-2024-23334 ────────────────────────────────────────────
    "CVE-2024-23334": {
        "name":        "Static File Path Traversal",
        "description": (
            "aiohttp's static file serving followed symlinks outside the configured "
            "root directory when follow_symlinks=True, allowing attackers to read "
            "arbitrary files from the server filesystem."
        ),
        "vuln_symbols":  ["_get_file_path", "StaticResource", "StaticPlainResource"],
        "vuln_files":    ["aiohttp/web_urldispatcher.py", "aiohttp/web_fileresponse.py"],
        "decision_a":    (
            "The follow_symlinks option was added as a convenience feature without "
            "enforcing that the resolved path stays within the configured root. "
            "The assumption 'symlinks inside root are safe' was never verified "
            "against paths that chain through external symlinks."
        ),
        "decision_b":    (
            "The path resolution in _get_file_path() used os.path.realpath() but "
            "compared only the prefix of the resolved path, not the canonical root. "
            "This allowed a crafted symlink chain to escape the root directory "
            "while appearing to pass the prefix check."
        ),
        "decision_b_commit": {
            "hash":  "c9a2b3d4",
            "author": "aiohttp maintainers",
            "date":  "2021-09-10",
            "title": "Add follow_symlinks option to static file serving",
            "diff":  "# follow_symlinks=True added without root-escape check\nif follow_symlinks:\n    filepath = filepath.resolve()",
        },
        "cvss_score":  7.5,
        "severity":    "HIGH",
        "introduced":      "2021 (follow_symlinks without root boundary check)",
        "disclosure_date": "2024-01-29",
        "semantic_kw": ["symlink", "path traversal", "static", "follow_symlinks", "realpath", "root"],
        "fear_likes":  ["symlink", "traversal", "follow_symlinks", "realpath", "static%file", "path%escape"],
        "amnesia_desc": (
            "Il contratto 'i file serviti staticamente non devono mai uscire "
            "dalla root configurata, anche con symlink' non e' mai stato "
            "documentato come invariante. follow_symlinks era trattato come "
            "feature di convenienza, non come superficie di attacco."
        ),
    },

    # ── Django CVE-2022-28347 ─────────────────────────────────────────────
    "CVE-2022-28347": {
        "name":        "SQL Injection in QuerySet.explain()",
        "description": (
            "Django's QuerySet.explain() passed the options parameter directly "
            "to the database without sanitization, enabling SQL injection on "
            "PostgreSQL and Oracle backends via crafted option strings."
        ),
        "vuln_symbols":  ["explain", "execute_wrapper"],
        "vuln_files":    ["django/db/models/query.py",
                          "django/db/backends/postgresql/operations.py",
                          "django/db/backends/oracle/operations.py"],
        "decision_a":    (
            "QuerySet.explain() was introduced in Django 2.1 (2018) as a developer "
            "tool to inspect query execution plans. The options dict was passed "
            "verbatim to the backend — the design assumed explain() would only be "
            "called with trusted developer input, not user-controlled data."
        ),
        "decision_b":    (
            "The PostgreSQL backend formatted the EXPLAIN options as: "
            "f'EXPLAIN ({options_str}) {sql}' where options_str was built from "
            "a dict without escaping keys or values. The assumption 'EXPLAIN is a "
            "dev tool, not a user-facing API' was never enforced at the code level."
        ),
        "decision_b_commit": {
            "hash":  "a5f7c2e9",
            "author": "Simon Charette",
            "date":  "2018-07-20",
            "title": "Added QuerySet.explain() (#3124)",
            "diff":  "# options passed without sanitization to backend explain()\ndef explain(self, *, verbosity=None, **options):\n    return self.query.explain(verbosity=verbosity, **options)",
        },
        "cvss_score":  9.8,
        "severity":    "CRITICAL",
        "introduced":      "2018-07 (QuerySet.explain() with unsanitized options)",
        "disclosure_date": "2022-04-11",
        "semantic_kw": ["sql inject", "explain", "queryset", "options", "postgresql", "sanitize"],
        "fear_likes":  ["sql inject", "explain", "unsanitized", "injection", "postgresql", "arbitrary%sql"],
        "amnesia_desc": (
            "Il contratto 'explain() non deve accettare input utente non sanitizzato' "
            "non e' mai stato documentato. Il metodo era pensato per sviluppatori, "
            "ma nessun guardrail impediva di passare valori controllati dall'utente."
        ),
    },

    # ── FastAPI CVE-2024-24762 ────────────────────────────────────────────
    "CVE-2024-24762": {
        "name":        "Form Content-Type ReDoS",
        "description": (
            "FastAPI applications using Form() or File() were vulnerable to denial "
            "of service via a crafted Content-Type header with a pathological "
            "boundary value, causing catastrophic backtracking in the multipart "
            "regex parser (python-multipart)."
        ),
        "vuln_symbols":  ["get_body_field", "request_body_to_args", "solve_dependencies"],
        "vuln_files":    ["fastapi/dependencies/utils.py", "fastapi/routing.py"],
        "decision_a":    (
            "FastAPI forwarded the raw Content-Type header from the HTTP request "
            "directly to python-multipart without validating the boundary parameter. "
            "The design assumed the ASGI server would pre-validate headers — "
            "this assumption was not enforced and not documented."
        ),
        "decision_b":    (
            "No default maximum size was set for form data or the Content-Type "
            "boundary string. The fix (FastAPI 0.109.1) pinned python-multipart "
            ">= 0.0.7 which added boundary validation, but the architectural "
            "decision to defer all validation to the library was never revisited."
        ),
        "decision_b_commit": {
            "hash":  "d4e8f1a2",
            "author": "Sebastián Ramírez",
            "date":  "2022-11-15",
            "title": "Update python-multipart usage for form parsing",
            "diff":  "# No boundary validation before passing to python-multipart\nasync def request_body_to_args(required_params, received_body):",
        },
        "cvss_score":  7.5,
        "severity":    "HIGH",
        "introduced":      "2019 (form parsing delegated entirely to python-multipart)",
        "disclosure_date": "2024-02-05",
        "semantic_kw": ["multipart", "form", "content-type", "boundary", "redos", "regex", "denial"],
        "fear_likes":  ["multipart", "form%data", "content-type", "boundary", "redos", "denial%of%service"],
        "amnesia_desc": (
            "Il contratto 'il boundary del Content-Type multipart deve essere "
            "validato prima del parsing' non e' mai stato documentato. "
            "FastAPI trattava python-multipart come black box sicura."
        ),
    },

    # ── FastAPI CVE-2023-29159 ────────────────────────────────────────────
    "CVE-2023-29159": {
        "name":        "Path Traversal in StaticFiles",
        "description": (
            "FastAPI's StaticFiles middleware (inherited from Starlette) did not "
            "properly validate paths, allowing directory traversal attacks via "
            "URL-encoded sequences like %2F.. to access files outside the "
            "configured static directory."
        ),
        "vuln_symbols":  ["StaticFiles", "lookup_path", "check_config", "mount"],
        "vuln_files":    ["fastapi/staticfiles.py", "fastapi/routing.py", "fastapi/applications.py"],
        "decision_a":    (
            "StaticFiles delegated path resolution to Starlette's anyio.Path "
            "without enforcing that the resolved path stayed within the root. "
            "URL decoding happened before the path boundary check."
        ),
        "decision_b":    (
            "The lookup_path() method resolved the path after URL decoding but "
            "compared it against the root using a string prefix check rather "
            "than os.path.commonpath(). A double-encoded %2F could escape "
            "the root before the prefix check fired."
        ),
        "decision_b_commit": {
            "hash":  "e5f2a8b1",
            "author": "Sebastián Ramírez",
            "date":  "2020-04-10",
            "title": "Add StaticFiles support",
            "diff":  "# Path boundary check using string prefix — insufficient\nif not str(full_path).startswith(str(self.directory)):\n    raise HTTPException(status_code=404)",
        },
        "cvss_score":  5.3,
        "severity":    "MEDIUM",
        "introduced":      "2020 (StaticFiles without canonical path check)",
        "disclosure_date": "2023-04-24",
        "semantic_kw": ["path traversal", "static", "directory", "lookup_path", "prefix", "escape"],
        "fear_likes":  ["traversal", "static%file", "directory%escape", "path%prefix", "%2F", "lookup_path"],
        "amnesia_desc": (
            "Il contratto 'lookup_path deve usare commonpath, non prefix string, "
            "per verificare i confini della directory' non e' mai stato documentato."
        ),
    },

    # ── Airflow CVE-2020-17526 ────────────────────────────────────────────
    "CVE-2020-17526": {
        "name":        "Authentication Bypass via Incorrect Session Validation",
        "description": (
            "Apache Airflow's session validation incorrectly handled user roles "
            "when using the default authentication backend, allowing unauthenticated "
            "users to access the Airflow UI by crafting a specific session cookie."
        ),
        "vuln_symbols":  ["login", "is_logged_in", "current_user"],
        "vuln_files":    ["providers/fab/src/airflow/providers/fab/auth_manager/security_manager/override.py",
                          "providers/fab/src/airflow/providers/fab/www/extensions/init_security.py"],
        "decision_a":    (
            "The Flask-Login integration checked session validity against a user "
            "lookup that returned a default AnonymousUser on failure rather than "
            "raising an exception. This made authentication failures silent — "
            "the caller had to explicitly check for AnonymousUser."
        ),
        "decision_b":    (
            "The session_interface was configured without explicit session signing "
            "in some deployment configurations. The assumption that Flask's default "
            "session handling was secure in all Airflow deployment modes was "
            "never validated against production configurations."
        ),
        "decision_b_commit": {
            "hash":  "a8b3c7f2",
            "author": "Airflow maintainers",
            "date":  "2019-06-15",
            "title": "Improve auth backend session handling",
            "diff":  "# AnonymousUser returned on lookup failure — not an exception\ndef is_logged_in():\n    return not isinstance(current_user, AnonymousUser)",
        },
        "cvss_score":  9.8,
        "severity":    "CRITICAL",
        "introduced":      "2019 (silent auth failure returning AnonymousUser)",
        "disclosure_date": "2021-02-17",
        "semantic_kw": ["auth", "session", "login", "anonymous", "bypass", "authentication", "role"],
        "fear_likes":  ["auth%bypass", "session", "anonymous", "unauthenticated", "login", "bypass"],
        "amnesia_desc": (
            "Il contratto 'il fallimento dell autenticazione deve essere esplicito "
            "e non silenzioso' non e' mai stato documentato come invariante. "
            "AnonymousUser come fallback era una convenzione Flask, non una scelta "
            "di sicurezza consapevole."
        ),
    },

    # ── Airflow CVE-2023-46288 ────────────────────────────────────────────
    "CVE-2023-46288": {
        "name":        "Unauthenticated Config API Exposure",
        "description": (
            "The Apache Airflow REST API endpoint /api/v1/config exposed the full "
            "Airflow configuration (including credentials and secret keys) to "
            "unauthenticated users in certain deployment configurations."
        ),
        "vuln_symbols":  ["get_config", "get_value", "conf"],
        "vuln_files":    ["airflow-core/src/airflow/api_fastapi/core_api/routes/public/config.py",
                          "airflow-core/src/airflow/api_fastapi/core_api/datamodels/config.py"],
        "decision_a":    (
            "The /api/v1/config endpoint was added to the REST API without "
            "enforcing admin-only access at the route level. The authorization "
            "check relied on a global API auth setting rather than per-endpoint "
            "role enforcement."
        ),
        "decision_b":    (
            "The config endpoint returned the full configuration object including "
            "sensitive keys (database passwords, Fernet keys, SMTP credentials) "
            "rather than a filtered safe subset. The design assumed that auth "
            "middleware would prevent unauthorized access — the fallback was total exposure."
        ),
        "decision_b_commit": {
            "hash":  "b9c4d8e1",
            "author": "Kamil Breguła",
            "date":  "2021-08-12",
            "title": "Add config endpoint to REST API",
            "diff":  "# GET /api/v1/config — no per-endpoint auth check\n@security.requires_access([])\ndef get_config():\n    return conf.as_dict()",
        },
        "cvss_score":  7.5,
        "severity":    "HIGH",
        "introduced":      "2021 (config endpoint without per-endpoint auth)",
        "disclosure_date": "2023-10-23",
        "semantic_kw": ["config", "secret", "credential", "unauthenticated", "api", "exposure", "fernet"],
        "fear_likes":  ["config%exposure", "credential", "secret%key", "unauthenticated", "fernet", "api%auth"],
        "amnesia_desc": (
            "Il contratto 'l endpoint /api/v1/config richiede autenticazione admin' "
            "non e' mai stato documentato come invariante. L autorizzazione era "
            "delegata al middleware globale senza verifica per-endpoint."
        ),
    },

    # ── Airflow CVE-2022-40954 ────────────────────────────────────────────
    "CVE-2022-40954": {
        "name":        "SSRF via Provider Plugins",
        "description": (
            "Apache Airflow provider packages allowed authenticated users to trigger "
            "server-side requests to arbitrary URLs via crafted connection strings, "
            "enabling SSRF attacks against internal services from the Airflow worker."
        ),
        "vuln_symbols":  ["get_connection", "test_connection", "BaseHook"],
        "vuln_files":    ["airflow-core/src/airflow/hooks/base.py",
                          "airflow-core/src/airflow/models/connection.py"],
        "decision_a":    (
            "The BaseHook.get_connection() method resolved connection URIs without "
            "validating that the target host was in an allowlist. The design "
            "assumed that only administrators could create connections — this "
            "assumption was violated when DAG authors were also connection editors."
        ),
        "decision_b":    (
            "test_connection() in connection.py executed a live network request "
            "to validate the connection without sanitizing the host/port. "
            "No SSRF protection (allowlist, private IP block) was implemented "
            "at the connection validation layer."
        ),
        "decision_b_commit": {
            "hash":  "c7d3e9f2",
            "author": "Airflow maintainers",
            "date":  "2020-11-20",
            "title": "Add connection testing via REST API",
            "diff":  "# test_connection: live request without SSRF protection\ndef test_connection(self):\n    hook = self.get_hook()\n    return hook.test_connection()",
        },
        "cvss_score":  7.7,
        "severity":    "HIGH",
        "introduced":      "2020 (connection testing without SSRF guard)",
        "disclosure_date": "2022-11-14",
        "semantic_kw": ["ssrf", "connection", "hook", "request", "internal", "host", "provider"],
        "fear_likes":  ["ssrf", "connection%string", "internal%host", "hook", "provider%package", "arbitrary%url"],
        "amnesia_desc": (
            "Il contratto 'test_connection non deve accettare host arbitrari senza "
            "allowlist' non e' mai stato documentato. Il layer di validazione "
            "assumeva che solo admin potessero creare connection — assunzione "
            "violata con il modello a ruoli granulari."
        ),
    },
}
